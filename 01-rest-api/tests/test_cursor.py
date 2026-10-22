import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.pagination import (
    CURSOR_KEY_ENV,
    MAX_CURSOR_LENGTH,
    MIN_KEY_BYTES,
    SORT_TAG,
    ConfigError,
    CursorCodec,
    CursorPosition,
    InvalidCursor,
    load_cursor_key,
)

KEY = b"k" * MIN_KEY_BYTES
USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
ITEM = uuid.UUID("c83cae64-4195-475b-9a05-96be0e52834a")
CREATED = datetime(2026, 10, 16, 14, 0, 0, 123456, tzinfo=UTC)

codec = CursorCodec(KEY)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _forge(payload: dict, *, key: bytes = KEY) -> str:
    """A correctly signed token whose payload has a deliberate defect.

    Proves each binding check works on its own rather than being caught by the signature.
    """
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload_b64}.{_b64(CursorCodec(key)._sign(payload_b64))}"


def _good_payload() -> dict:
    return {
        "v": 1,
        "u": str(USER),
        "s": SORT_TAG,
        "t": int((CREATED - datetime(1970, 1, 1, tzinfo=UTC)) / timedelta(microseconds=1)),
        "i": str(ITEM),
    }


def test_forge_helper_with_a_good_payload_decodes() -> None:
    # Guards the helper itself: every negative test below changes exactly one field.
    assert codec.decode(_forge(_good_payload()), USER) == CursorPosition(CREATED, ITEM)


# --- round trip -------------------------------------------------------------------------


def test_round_trip_preserves_microseconds_and_id() -> None:
    position = codec.decode(codec.encode(CursorPosition(CREATED, ITEM), USER), USER)
    assert position == CursorPosition(CREATED, ITEM)
    assert position.created_at.microsecond == 123456


def test_timestamps_one_microsecond_apart_stay_distinct() -> None:
    later = CREATED + timedelta(microseconds=1)
    a = codec.encode(CursorPosition(CREATED, ITEM), USER)
    b = codec.encode(CursorPosition(later, ITEM), USER)
    assert a != b
    assert codec.decode(b, USER).created_at == later


def test_other_utc_offsets_round_trip_to_the_same_instant() -> None:
    local = CREATED.astimezone(timezone(timedelta(hours=-7)))
    token = codec.encode(CursorPosition(local, ITEM), USER)
    assert codec.decode(token, USER).created_at == CREATED


def test_encoding_a_naive_timestamp_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        codec.encode(CursorPosition(datetime(2026, 10, 16), ITEM), USER)


def test_token_is_urlsafe_and_within_the_length_limit() -> None:
    token = codec.encode(CursorPosition(CREATED, ITEM), USER)
    assert len(token) <= MAX_CURSOR_LENGTH
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


# --- integrity and binding --------------------------------------------------------------


def test_cursor_is_bound_to_the_user() -> None:
    token = codec.encode(CursorPosition(CREATED, ITEM), USER)
    with pytest.raises(InvalidCursor):
        codec.decode(token, OTHER_USER)


def test_cursor_signed_with_another_key_is_rejected() -> None:
    with pytest.raises(InvalidCursor):
        codec.decode(_forge(_good_payload(), key=b"z" * MIN_KEY_BYTES), USER)


def test_editing_the_payload_without_resigning_is_rejected() -> None:
    token = codec.encode(CursorPosition(CREATED, ITEM), USER)
    signature = token.split(".")[1]
    edited = dict(_good_payload(), t=_good_payload()["t"] + 1)
    edited_b64 = _b64(json.dumps(edited, separators=(",", ":"), sort_keys=True).encode())
    with pytest.raises(InvalidCursor):
        codec.decode(f"{edited_b64}.{signature}", USER)


def test_flipping_a_signature_character_is_rejected() -> None:
    payload, signature = codec.encode(CursorPosition(CREATED, ITEM), USER).split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(InvalidCursor):
        codec.decode(f"{payload}.{flipped}", USER)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("s", "created_at_asc,id_asc"),  # another sort order
        ("v", 2),  # unknown version
        ("v", True),  # bool is not an int here
        ("u", str(OTHER_USER)),  # another user, validly signed
        ("t", -1),
        ("t", True),
        ("t", 1.5),
        ("t", "1"),
        ("t", 10**30),  # beyond the datetime range
        ("i", "not-a-uuid"),
        ("i", str(ITEM).upper()),  # non-canonical spelling
        ("i", 7),
    ],
)
def test_signed_but_defective_payload_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(InvalidCursor):
        codec.decode(_forge(dict(_good_payload(), **{field: value})), USER)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {k: v for k, v in p.items() if k != "i"},  # missing key
        lambda p: dict(p, extra="x"),  # unknown key
    ],
)
def test_signed_payload_with_wrong_keys_is_rejected(mutate) -> None:
    with pytest.raises(InvalidCursor):
        codec.decode(_forge(mutate(_good_payload())), USER)


def test_signed_non_object_and_non_json_payloads_are_rejected() -> None:
    for raw in (b"[1,2]", b"42", b"null", b"\xff\xfe", b"not json"):
        payload_b64 = _b64(raw)
        token = f"{payload_b64}.{_b64(codec._sign(payload_b64))}"
        with pytest.raises(InvalidCursor):
            codec.decode(token, USER)


@pytest.mark.parametrize(
    "token",
    [
        "",
        " ",
        "x",
        ".",
        "a.",
        ".b",
        "a.b",
        "a.b.c",
        "....",
        "é.é",
        "a b.c d",
        "e30=.e30=",  # padding is not part of the wire form
        "x" * (MAX_CURSOR_LENGTH + 1),
        "\x00.\x00",
    ],
)
def test_garbage_is_rejected(token: str) -> None:
    with pytest.raises(InvalidCursor):
        codec.decode(token, USER)


def test_non_canonical_signature_spelling_is_rejected() -> None:
    # A 32-byte digest is 43 base64 characters; the last carries 2 unused bits, so a
    # different last character can decode to the *same* bytes and would still verify.
    # The canonical-encoding check keeps one position to one valid token.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    payload, signature = codec.encode(CursorPosition(CREATED, ITEM), USER).split(".")
    index = alphabet.index(signature[-1])
    twin = alphabet[(index & ~3) | ((index + 1) & 3)]
    assert twin != signature[-1]
    assert base64.urlsafe_b64decode(signature + "=") == base64.urlsafe_b64decode(
        signature[:-1] + twin + "="
    )
    with pytest.raises(InvalidCursor):
        codec.decode(f"{payload}.{signature[:-1]}{twin}", USER)


def test_validly_signed_but_oversized_token_is_rejected_by_the_length_cap() -> None:
    padded = json.dumps(_good_payload(), separators=(",", ":"), sort_keys=True) + " " * 600
    payload_b64 = _b64(padded.encode())
    token = f"{payload_b64}.{_b64(codec._sign(payload_b64))}"
    assert len(token) > MAX_CURSOR_LENGTH
    with pytest.raises(InvalidCursor):
        codec.decode(token, USER)


def test_non_string_cursor_is_rejected() -> None:
    with pytest.raises(InvalidCursor):
        codec.decode(None, USER)  # type: ignore[arg-type]


# --- key handling -----------------------------------------------------------------------


def test_load_cursor_key_requires_a_present_strong_key() -> None:
    with pytest.raises(ConfigError, match=CURSOR_KEY_ENV):
        load_cursor_key({})
    with pytest.raises(ConfigError, match=CURSOR_KEY_ENV):
        load_cursor_key({CURSOR_KEY_ENV: ""})
    with pytest.raises(ConfigError, match="at least"):
        load_cursor_key({CURSOR_KEY_ENV: "k" * (MIN_KEY_BYTES - 1)})
    assert load_cursor_key({CURSOR_KEY_ENV: "k" * MIN_KEY_BYTES}) == KEY


def test_codec_refuses_a_short_key() -> None:
    with pytest.raises(ConfigError):
        CursorCodec(b"short")
