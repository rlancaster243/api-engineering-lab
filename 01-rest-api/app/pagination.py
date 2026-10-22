"""Signed keyset cursor (spec section 4.4, ADR 0007).

The cursor marks the last row of a page in ``created_at DESC, id DESC`` order. It is
HMAC-signed and bound to the requesting user and the sort order, so that an edited,
cross-user or cross-sort cursor is rejected with the same error as a malformed one and
gives no oracle about which check failed.

Wire form: ``base64url(payload) "." base64url(HMAC-SHA256(key, base64url(payload)))``,
unpadded, where the payload is compact JSON with sorted keys.
"""

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

CURSOR_KEY_ENV = "CURSOR_SIGNING_KEY"
MIN_KEY_BYTES = 32
MAX_CURSOR_LENGTH = 512
SORT_TAG = "created_at_desc,id_desc"
CURSOR_VERSION = 1

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MICROSECOND = timedelta(microseconds=1)
_PAYLOAD_KEYS = frozenset({"v", "u", "s", "t", "i"})


class InvalidCursor(Exception):
    """The cursor is malformed, tampered with, or not valid for this user and ordering."""


class ConfigError(Exception):
    """Required configuration is missing or unsafe. The application must not start."""


@dataclass(frozen=True)
class CursorPosition:
    """Ordering key of the last row served: ``(created_at, id)``."""

    created_at: datetime
    item_id: uuid.UUID


def load_cursor_key(environ: Mapping[str, str]) -> bytes:
    """Read the signing key from trusted settings; fail closed when absent or weak."""
    raw = environ.get(CURSOR_KEY_ENV, "")
    key = raw.encode("utf-8")
    if not key:
        raise ConfigError(f"{CURSOR_KEY_ENV} is required")
    if len(key) < MIN_KEY_BYTES:
        raise ConfigError(f"{CURSOR_KEY_ENV} must be at least {MIN_KEY_BYTES} bytes")
    return key


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    try:
        data = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise InvalidCursor from exc
    # Reject non-canonical encodings so one position has one valid spelling.
    if _b64encode(data) != text:
        raise InvalidCursor
    return data


def _to_micros(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    # Integer microseconds are exact; PostgreSQL timestamptz has microsecond precision.
    # Coarser encodings (milliseconds, float seconds) can skip or repeat rows whose
    # timestamps differ by less than the encoding's resolution.
    return (value.astimezone(UTC) - _EPOCH) // _ONE_MICROSECOND


class CursorCodec:
    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ConfigError(f"signing key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key

    def _sign(self, payload_b64: str) -> bytes:
        return hmac.new(self._key, payload_b64.encode("ascii"), hashlib.sha256).digest()

    def encode(self, position: CursorPosition, user_id: uuid.UUID) -> str:
        payload = {
            "v": CURSOR_VERSION,
            "u": str(user_id),
            "s": SORT_TAG,
            "t": _to_micros(position.created_at),
            "i": str(position.item_id),
        }
        payload_b64 = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        return f"{payload_b64}.{_b64encode(self._sign(payload_b64))}"

    def decode(self, token: str, user_id: uuid.UUID) -> CursorPosition:
        """Return the position, or raise ``InvalidCursor`` for any defect."""
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_LENGTH:
            raise InvalidCursor
        payload_b64, sep, signature_b64 = token.partition(".")
        if not sep or not payload_b64 or not signature_b64 or "." in signature_b64:
            raise InvalidCursor
        signature = _b64decode(signature_b64)
        # Verify the signature before parsing anything it covers.
        if not hmac.compare_digest(signature, self._sign(payload_b64)):
            raise InvalidCursor
        payload = self._parse_payload(_b64decode(payload_b64))
        if payload["u"] != str(user_id) or payload["s"] != SORT_TAG:
            raise InvalidCursor
        return self._position(payload)

    @staticmethod
    def _parse_payload(raw: bytes) -> dict[str, object]:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidCursor from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != _PAYLOAD_KEYS
            or payload["v"] != CURSOR_VERSION
            or isinstance(payload["v"], bool)
        ):
            raise InvalidCursor
        return payload

    @staticmethod
    def _position(payload: dict[str, object]) -> CursorPosition:
        micros = payload["t"]
        if isinstance(micros, bool) or not isinstance(micros, int) or micros < 0:
            raise InvalidCursor
        raw_id = payload["i"]
        if not isinstance(raw_id, str):
            raise InvalidCursor
        try:
            item_id = uuid.UUID(raw_id)
            created_at = _EPOCH + timedelta(microseconds=micros)
        except (ValueError, OverflowError) as exc:
            raise InvalidCursor from exc
        if str(item_id) != raw_id:
            raise InvalidCursor
        return CursorPosition(created_at=created_at, item_id=item_id)
