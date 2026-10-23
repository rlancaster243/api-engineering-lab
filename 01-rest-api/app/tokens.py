"""OIDC access-token verification (spec section 5, ADR 0005).

The verifier answers one question: is this bearer string a currently valid access token
from the configured issuer, for this API? It does not map the token to a user; that is
PR-06. Every check fails closed, and no exception message or log line contains the raw
token, because a token is a credential.
"""

import json
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from app.config import OidcSettings

# RFC 9068 names the access-token type "at+jwt"; plain "JWT" is what many providers send.
# Anything else (for example an ID token marked "id+jwt") is not an access token.
_ACCEPTED_TYP = frozenset({"jwt", "at+jwt", "application/at+jwt"})
_FETCH_TIMEOUT_SECONDS = 5


class InvalidToken(Exception):
    """The token is malformed, forged, expired, or not meant for this API (401)."""


class KeysUnavailable(Exception):
    """Signing keys could not be obtained, so the token cannot be judged (503)."""


class InsufficientScope(Exception):
    """The token is valid but lacks the scope this API requires (403)."""


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    scopes: frozenset[str]


class KeySource(Protocol):
    def signing_key(self, kid: str) -> object:
        """Return a key usable by ``jwt.decode``.

        Raises ``InvalidToken`` for an unknown ``kid`` and ``KeysUnavailable`` when keys
        cannot be obtained.
        """
        ...


_MAX_JWKS_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could move the fetch from https to http and defeat the https-only rule.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _fetch_jwks(url: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(_MAX_JWKS_BYTES + 1)
    if len(body) > _MAX_JWKS_BYTES:
        raise ValueError("JWKS response too large")
    return json.loads(body)


class JwksKeySource:
    """JWKS keys with a hard TTL and rate-limited refetch on an unknown ``kid``.

    The cache is written here rather than delegated to a library so the two security
    rules are visible: keys are never served past ``cache_seconds`` (a revoked key must
    stop working), and an attacker sending random ``kid`` values cannot make this service
    hammer the provider (at most one refetch per ``min_refetch_seconds``).
    """

    def __init__(
        self,
        jwks_url: str,
        cache_seconds: int,
        *,
        fetch: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_refetch_seconds: int = 60,
    ) -> None:
        self._fetch = fetch if fetch is not None else (lambda: _fetch_jwks(jwks_url))
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._min_refetch_seconds = min_refetch_seconds
        self._keys: dict[str, jwt.PyJWK] | None = None
        self._fetched_at = 0.0
        self._last_attempt: float | None = None
        # Sync routes run in a thread pool; serialize refreshes so a burst makes one fetch.
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        # Record the attempt first so a failing provider is not retried on every request.
        self._last_attempt = self._clock()
        try:
            key_set = jwt.PyJWKSet.from_dict(self._fetch())
        except Exception:
            raise KeysUnavailable("could not obtain signing keys") from None
        self._keys = {key.key_id: key for key in key_set.keys if key.key_id}
        self._fetched_at = self._last_attempt

    def signing_key(self, kid: str) -> object:
        with self._lock:
            now = self._clock()
            if self._keys is None or now - self._fetched_at >= self._cache_seconds:
                # Drop the stale set first: if the refetch fails, nothing old is served.
                self._keys = None
                self._refresh()
            assert self._keys is not None
            if kid not in self._keys:
                last = self._last_attempt
                if last is not None and now - last < self._min_refetch_seconds:
                    raise InvalidToken("unknown signing key")
                self._refresh()
                assert self._keys is not None
                if kid not in self._keys:
                    raise InvalidToken("unknown signing key")
            return self._keys[kid].key


class TokenVerifier:
    def __init__(self, settings: OidcSettings, keys: KeySource) -> None:
        self.settings = settings
        self.keys = keys

    def verify(self, raw: str) -> Principal:
        settings = self.settings

        try:
            header = jwt.get_unverified_header(raw)
        except jwt.PyJWTError:
            raise InvalidToken("malformed token") from None

        # Before any key lookup: this is what blocks "none" and HS256-signed-with-the-
        # public-key confusion. The decoder is then given only this one algorithm.
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in settings.algorithms:
            raise InvalidToken("algorithm not allowed")

        typ = header.get("typ")
        if typ is not None and (not isinstance(typ, str) or typ.lower() not in _ACCEPTED_TYP):
            raise InvalidToken("unsupported token type")

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("missing key id")

        # The key comes only from our configured source. jku, x5u, jwk and x5c headers
        # are attacker-controlled and are never read.
        key = self.keys.signing_key(kid)

        try:
            claims = jwt.decode(
                raw,
                key,  # type: ignore[arg-type]
                algorithms=[alg],
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            raise InvalidToken("token rejected") from None

        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidToken("invalid subject")

        scopes = _scopes(claims)
        if settings.required_scope is not None and settings.required_scope not in scopes:
            raise InsufficientScope("missing required scope")

        return Principal(issuer=claims["iss"], subject=subject, scopes=scopes)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    """Union of ``scope`` (space-separated string) and ``scp`` (list or single string)."""
    found: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        found.update(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        found.add(scp)
    elif isinstance(scp, list):
        found.update(item for item in scp if isinstance(item, str))
    return frozenset(found)
