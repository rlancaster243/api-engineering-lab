"""Access-token verification (spec section 5, F-012 to F-015, ADR 0005).

No network and no identity provider: keys are generated per session and tokens are
minted by the ``mint_token`` fixture. Hand-built tokens cover what a library refuses to
encode (``alg: none``, HMAC keyed with a public key).
"""

import base64
import dataclasses
import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.request
from typing import Annotated, Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import current_principal
from app.config import OidcSettings
from app.errors import install_error_handling
from app.tokens import (
    InsufficientScope,
    InvalidToken,
    JwksKeySource,
    KeysUnavailable,
    Principal,
    TokenVerifier,
)
from tests.conftest import (
    EC_KID,
    OIDC_AUDIENCE,
    OIDC_ISSUER,
    RSA_KID,
    MintToken,
    StaticKeySource,
)


@pytest.fixture
def verifier(oidc_settings: OidcSettings, static_keys: StaticKeySource) -> TokenVerifier:
    return TokenVerifier(oidc_settings, static_keys)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _handmade(header: dict[str, Any], claims: dict[str, Any], signature: bytes = b"") -> str:
    """Assemble a token without a JWT library, so nothing can refuse to build it."""
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(claims).encode())
    return f"{head}.{body}.{_b64(signature)}"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": OIDC_ISSUER,
        "aud": OIDC_AUDIENCE,
        "sub": "user-123",
        "exp": now + 300,
    } | overrides


# --- Valid tokens -------------------------------------------------------------------------


def test_valid_rs256_token_gives_the_principal(verifier: TokenVerifier, mint_token: MintToken):
    principal = verifier.verify(mint_token())
    assert principal == Principal(issuer=OIDC_ISSUER, subject="user-123", scopes=frozenset())


def test_valid_es256_token_gives_the_principal(verifier: TokenVerifier, mint_token: MintToken):
    principal = verifier.verify(mint_token(alg="ES256"))
    assert principal.subject == "user-123"
    assert principal.issuer == OIDC_ISSUER


def test_scopes_come_from_scope_and_scp_claims(verifier: TokenVerifier, mint_token: MintToken):
    token = mint_token({"scope": "items:read items:write", "scp": ["extra", "items:read"]})
    assert verifier.verify(token).scopes == {"items:read", "items:write", "extra"}


def test_scp_may_be_a_single_string(verifier: TokenVerifier, mint_token: MintToken):
    assert verifier.verify(mint_token({"scp": "items:read"})).scopes == {"items:read"}


def test_audience_list_containing_ours_passes(verifier: TokenVerifier, mint_token: MintToken):
    token = mint_token({"aud": ["other-api", OIDC_AUDIENCE]})
    assert verifier.verify(token).subject == "user-123"


@pytest.mark.parametrize("typ", ["JWT", "jwt", "at+jwt", "AT+JWT", "application/at+jwt", None])
def test_accepted_typ_values(verifier: TokenVerifier, mint_token: MintToken, typ: str | None):
    assert verifier.verify(mint_token(header={"typ": typ})).subject == "user-123"


@pytest.mark.parametrize("typ", ["id+jwt", "JOSE", "none"])
def test_other_typ_values_are_rejected(verifier: TokenVerifier, mint_token: MintToken, typ: Any):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(header={"typ": typ}))


# --- F-012: missing / expired -------------------------------------------------------------


def test_f012_expired_token_beyond_leeway_is_rejected(
    verifier: TokenVerifier, mint_token: MintToken
):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"exp": int(time.time()) - 120}))


def test_f012_token_expired_within_leeway_is_accepted(
    verifier: TokenVerifier, mint_token: MintToken
):
    assert verifier.verify(mint_token({"exp": int(time.time()) - 10})).subject == "user-123"


def test_missing_exp_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"exp": None}))


def test_future_nbf_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"nbf": int(time.time()) + 600}))


# --- F-013: forged signature --------------------------------------------------------------


def test_f013_signature_from_a_different_key_is_rejected(
    verifier: TokenVerifier, mint_token: MintToken
):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(key=attacker, kid=RSA_KID))


def test_tampered_payload_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    head, _, sig = mint_token().split(".")
    forged = _b64(json.dumps(_claims(sub="someone-else")).encode())
    with pytest.raises(InvalidToken):
        verifier.verify(f"{head}.{forged}.{sig}")


@pytest.mark.parametrize("raw", ["", "not-a-token", "a.b.c", "a.b"])
def test_malformed_tokens_are_rejected(verifier: TokenVerifier, raw: str):
    with pytest.raises(InvalidToken):
        verifier.verify(raw)


# --- F-014: issuer and audience -----------------------------------------------------------


def test_f014_wrong_issuer_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"iss": "https://evil.test"}))


def test_f014_wrong_audience_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"aud": "some-other-api"}))


@pytest.mark.parametrize("claim", ["iss", "aud"])
def test_missing_issuer_or_audience_is_rejected(
    verifier: TokenVerifier, mint_token: MintToken, claim: str
):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({claim: None}))


# --- F-015: key id ------------------------------------------------------------------------


def test_f015_unknown_kid_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(kid="no-such-key"))


def test_f015_missing_kid_is_rejected(verifier: TokenVerifier, mint_token: MintToken):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(header={"kid": None}))


@pytest.mark.parametrize("kid", ["", 7])
def test_empty_or_non_string_kid_is_rejected(verifier: TokenVerifier, kid: Any):
    # Hand-built: PyJWT refuses to encode a non-string kid.
    token = _handmade({"alg": "RS256", "typ": "JWT", "kid": kid}, _claims(), b"sig")
    with pytest.raises(InvalidToken):
        verifier.verify(token)


# --- Algorithm attacks --------------------------------------------------------------------


class RecordingKeys(StaticKeySource):
    def __init__(self, keys: dict[str, Any]) -> None:
        super().__init__(keys)
        self.lookups: list[str] = []

    def signing_key(self, kid: str) -> object:
        self.lookups.append(kid)
        return super().signing_key(kid)


def test_alg_none_is_rejected_before_any_key_lookup(
    oidc_settings: OidcSettings, rsa_key: rsa.RSAPrivateKey
):
    keys = RecordingKeys({RSA_KID: rsa_key.public_key()})
    token = _handmade({"alg": "none", "typ": "JWT", "kid": RSA_KID}, _claims())
    with pytest.raises(InvalidToken):
        TokenVerifier(oidc_settings, keys).verify(token)
    assert keys.lookups == []


def test_algorithm_confusion_hs256_with_public_key_is_rejected(
    oidc_settings: OidcSettings, rsa_key: rsa.RSAPrivateKey
):
    # The classic attack: sign with HMAC, using the public key's PEM as the secret. A
    # verifier that trusts the header's alg would accept it. Built by hand because
    # PyJWT refuses to encode it.
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": RSA_KID}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(_claims()).encode())}"
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    token = f"{signing_input}.{_b64(signature)}"

    keys = RecordingKeys({RSA_KID: rsa_key.public_key()})
    with pytest.raises(InvalidToken):
        TokenVerifier(oidc_settings, keys).verify(token)
    assert keys.lookups == []


def test_algorithm_outside_the_allowlist_is_rejected(
    oidc_settings: OidcSettings,
    static_keys: StaticKeySource,
    mint_token: MintToken,
    rsa_key: rsa.RSAPrivateKey,
):
    only_rs256 = dataclasses.replace(oidc_settings, algorithms=("RS256",))
    # A correctly signed PS256 token with the right key is still refused.
    token = mint_token(alg="PS256", key=rsa_key)
    with pytest.raises(InvalidToken):
        TokenVerifier(only_rs256, static_keys).verify(token)


def test_jku_header_is_ignored(
    verifier: TokenVerifier, mint_token: MintToken, rsa_key: rsa.RSAPrivateKey
):
    jku = {"jku": "https://attacker.test/jwks.json"}
    # A genuine token carrying a jku header verifies against the configured keys...
    assert verifier.verify(mint_token(header=jku)).subject == "user-123"
    # ...and a token signed by the attacker's key is not rescued by pointing jku at it.
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(header=jku, key=attacker))


def test_embedded_jwk_header_is_ignored(verifier: TokenVerifier, mint_token: MintToken):
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    embedded = jwt.algorithms.RSAAlgorithm.to_jwk(attacker.public_key(), as_dict=True)
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(header={"jwk": embedded}, key=attacker))


# --- Subject ------------------------------------------------------------------------------


@pytest.mark.parametrize("sub", [None, "", "   ", 123, ["a"]])
def test_missing_blank_or_non_string_sub_is_rejected(
    verifier: TokenVerifier, mint_token: MintToken, sub: Any
):
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token({"sub": sub}))


# --- Scope --------------------------------------------------------------------------------


def test_required_scope_present(
    oidc_settings: OidcSettings, static_keys: StaticKeySource, mint_token: MintToken
):
    scoped = dataclasses.replace(oidc_settings, required_scope="items:write")
    token = mint_token({"scope": "items:read items:write"})
    assert "items:write" in TokenVerifier(scoped, static_keys).verify(token).scopes


def test_required_scope_absent_is_insufficient(
    oidc_settings: OidcSettings, static_keys: StaticKeySource, mint_token: MintToken
):
    scoped = dataclasses.replace(oidc_settings, required_scope="items:write")
    with pytest.raises(InsufficientScope):
        TokenVerifier(scoped, static_keys).verify(mint_token({"scope": "items:read"}))


def test_required_scope_may_come_from_scp(
    oidc_settings: OidcSettings, static_keys: StaticKeySource, mint_token: MintToken
):
    scoped = dataclasses.replace(oidc_settings, required_scope="items:write")
    token = mint_token({"scp": ["items:write"]})
    assert TokenVerifier(scoped, static_keys).verify(token).scopes == {"items:write"}


def test_invalid_token_wins_over_scope_check(
    oidc_settings: OidcSettings, static_keys: StaticKeySource, mint_token: MintToken
):
    # A forged token must read as 401, never 403, so scope is not an oracle for forgers.
    scoped = dataclasses.replace(oidc_settings, required_scope="items:write")
    with pytest.raises(InvalidToken):
        TokenVerifier(scoped, static_keys).verify(mint_token({"iss": "https://evil.test"}))


def test_token_text_never_appears_in_exception_messages(
    verifier: TokenVerifier, mint_token: MintToken
):
    token = mint_token({"aud": "wrong"})
    with pytest.raises(InvalidToken) as caught:
        verifier.verify(token)
    assert token not in str(caught.value)
    assert caught.value.__cause__ is None


# --- JwksKeySource ------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeJwks:
    """A fetch callable that counts calls and serves a mutable key set (or fails)."""

    def __init__(self, keys: dict[str, Any]) -> None:
        self.keys = keys
        self.calls = 0
        self.fail = False

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise OSError("provider down")
        entries = []
        for kid, public_key in self.keys.items():
            algorithm = (
                jwt.algorithms.ECAlgorithm
                if isinstance(public_key, ec.EllipticCurvePublicKey)
                else jwt.algorithms.RSAAlgorithm
            )
            entry = algorithm.to_jwk(public_key, as_dict=True)
            entries.append(entry | {"kid": kid, "use": "sig"})
        return {"keys": entries}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jwks(rsa_key: rsa.RSAPrivateKey) -> FakeJwks:
    return FakeJwks({RSA_KID: rsa_key.public_key()})


@pytest.fixture
def source(jwks: FakeJwks, clock: FakeClock) -> JwksKeySource:
    return JwksKeySource("https://issuer.test/jwks", 300, fetch=jwks, clock=clock)


def test_jwks_keys_are_cached_within_ttl(source: JwksKeySource, jwks: FakeJwks, clock: FakeClock):
    source.signing_key(RSA_KID)
    clock.now += 299
    source.signing_key(RSA_KID)
    assert jwks.calls == 1


def test_jwks_key_verifies_a_real_token(
    source: JwksKeySource, oidc_settings: OidcSettings, mint_token: MintToken
):
    assert TokenVerifier(oidc_settings, source).verify(mint_token()).subject == "user-123"


def test_jwks_refetches_after_ttl(source: JwksKeySource, jwks: FakeJwks, clock: FakeClock):
    source.signing_key(RSA_KID)
    clock.now += 301
    source.signing_key(RSA_KID)
    assert jwks.calls == 2


def test_jwks_fetch_failure_after_ttl_never_serves_stale_keys(
    source: JwksKeySource, jwks: FakeJwks, clock: FakeClock
):
    source.signing_key(RSA_KID)
    jwks.fail = True
    clock.now += 301
    with pytest.raises(KeysUnavailable):
        source.signing_key(RSA_KID)
    # Still unavailable on the next call, not served from the expired cache.
    with pytest.raises(KeysUnavailable):
        source.signing_key(RSA_KID)


def test_jwks_recovers_when_the_provider_returns(
    source: JwksKeySource, jwks: FakeJwks, clock: FakeClock
):
    source.signing_key(RSA_KID)
    jwks.fail = True
    clock.now += 301
    with pytest.raises(KeysUnavailable):
        source.signing_key(RSA_KID)
    jwks.fail = False
    assert source.signing_key(RSA_KID) is not None


def test_jwks_fetch_exception_at_first_use_is_keys_unavailable(
    source: JwksKeySource, jwks: FakeJwks
):
    jwks.fail = True
    with pytest.raises(KeysUnavailable):
        source.signing_key(RSA_KID)


def test_jwks_unparseable_response_is_keys_unavailable(clock: FakeClock):
    source = JwksKeySource("https://issuer.test/jwks", 300, fetch=lambda: {"nope": 1}, clock=clock)
    with pytest.raises(KeysUnavailable):
        source.signing_key(RSA_KID)


def test_jwks_unknown_kid_refetches_once_and_finds_a_rotated_key(
    source: JwksKeySource,
    jwks: FakeJwks,
    clock: FakeClock,
    rsa_key: rsa.RSAPrivateKey,
    ec_key: ec.EllipticCurvePrivateKey,
):
    source.signing_key(RSA_KID)
    jwks.keys[EC_KID] = ec_key.public_key()  # the provider rotated a key in
    clock.now += 61
    assert source.signing_key(EC_KID) is not None
    assert jwks.calls == 2


def test_jwks_repeated_unknown_kids_do_not_refetch_within_the_window(
    source: JwksKeySource, jwks: FakeJwks, clock: FakeClock
):
    source.signing_key(RSA_KID)
    clock.now += 61
    for _ in range(3):
        with pytest.raises(InvalidToken):
            source.signing_key("nope")
        clock.now += 5
    assert jwks.calls == 2  # the first fetch plus a single refetch


def test_jwks_failed_attempts_are_also_rate_limited(
    source: JwksKeySource, jwks: FakeJwks, clock: FakeClock
):
    source.signing_key(RSA_KID)
    jwks.fail = True
    clock.now += 61
    with pytest.raises(KeysUnavailable):
        source.signing_key("nope")
    with pytest.raises(InvalidToken):
        source.signing_key("nope")
    assert jwks.calls == 2


def test_jwks_returns_the_key_for_the_requested_kid(
    clock: FakeClock,
    oidc_settings: OidcSettings,
    mint_token: MintToken,
    rsa_key: rsa.RSAPrivateKey,
):
    key_b = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = FakeJwks({"key-a": rsa_key.public_key(), "key-b": key_b.public_key()})
    verifier = TokenVerifier(
        oidc_settings, JwksKeySource("https://issuer.test/jwks", 300, fetch=jwks, clock=clock)
    )
    assert verifier.verify(mint_token(key=key_b, kid="key-b")).subject == "user-123"
    with pytest.raises(InvalidToken):
        verifier.verify(mint_token(key=rsa_key, kid="key-b"))


def test_jwks_fetch_refuses_redirects():
    from app.tokens import _NoRedirect

    handler = _NoRedirect()
    request = urllib.request.Request("https://issuer.test/jwks")
    assert (
        handler.redirect_request(request, None, 302, "Found", {}, "http://evil.test/jwks") is None
    )


def test_jwks_fetch_redirect_surfaces_as_http_error(monkeypatch: pytest.MonkeyPatch):
    from app import tokens

    class Redirecting(urllib.request.BaseHandler):
        handler_order = 100

        def https_open(self, req: urllib.request.Request):
            return self.parent.error(
                "http", req, io.BytesIO(b""), 302, "Found", {"Location": "http://evil.test/"}
            )

    real = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: real(*h, Redirecting))
    with pytest.raises(urllib.error.HTTPError):
        tokens._fetch_jwks("https://issuer.test/jwks")


def test_jwks_oversized_response_is_keys_unavailable(
    monkeypatch: pytest.MonkeyPatch, jwks: FakeJwks
):
    from app import tokens

    # A valid key set, so only its size can make the fetch fail. Trailing whitespace is
    # the padding: a body cut off at the cap would otherwise be invalid JSON anyway, and
    # the test could not tell the size check from a parse error.
    valid = jwks()
    body: dict[str, bytes] = {}

    class FakeOpener:
        def open(self, url: str, timeout: float):
            return io.BytesIO(body["data"])

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: FakeOpener())
    source = JwksKeySource("https://issuer.test/jwks", 300)

    body["data"] = json.dumps(valid).encode()
    assert source.signing_key(RSA_KID) is not None  # control: same keys, under the cap

    body["data"] = json.dumps(valid).encode() + b" " * tokens._MAX_JWKS_BYTES
    assert len(body["data"]) > tokens._MAX_JWKS_BYTES
    with pytest.raises(KeysUnavailable):
        JwksKeySource("https://issuer.test/jwks", 300).signing_key(RSA_KID)


# --- HTTP mapping -------------------------------------------------------------------------


class _FailingKeys:
    def signing_key(self, kid: str) -> object:
        raise KeysUnavailable("down")


def _build_app(settings: OidcSettings, keys: Any) -> TestClient:
    app = FastAPI()
    install_error_handling(app)
    app.state.token_verifier = TokenVerifier(settings, keys)

    @app.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, str]:
        return {"subject": principal.subject}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def http(oidc_settings: OidcSettings, static_keys: StaticKeySource) -> TestClient:
    return _build_app(oidc_settings, static_keys)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_http_valid_token_reaches_the_route(http: TestClient, mint_token: MintToken):
    response = http.get("/whoami", headers=_bearer(mint_token()))
    assert response.status_code == 200
    assert response.json() == {"subject": "user-123"}


def test_f012_http_missing_token_is_unauthenticated(http: TestClient):
    response = http.get("/whoami")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header", ["Basic abc", "Bearer", "Bearer ", "Token abc", "Bearer a b", "abc"]
)
def test_http_malformed_authorization_is_unauthenticated(http: TestClient, header: str):
    response = http.get("/whoami", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_http_bearer_scheme_is_case_insensitive(http: TestClient, mint_token: MintToken):
    response = http.get("/whoami", headers={"Authorization": f"bEaReR {mint_token()}"})
    assert response.status_code == 200


def test_http_no_verifier_is_unauthenticated(oidc_settings: OidcSettings, mint_token: MintToken):
    client = _build_app(oidc_settings, StaticKeySource({}))
    client.app.state.token_verifier = None  # type: ignore[attr-defined]
    response = client.get("/whoami", headers=_bearer(mint_token()))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_http_bad_token_is_invalid_token_and_never_echoed(http: TestClient, mint_token: MintToken):
    token = mint_token({"aud": "wrong"})
    response = http.get("/whoami", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert token not in response.text


def test_http_keys_unavailable_is_503(oidc_settings: OidcSettings, mint_token: MintToken):
    client = _build_app(oidc_settings, _FailingKeys())
    response = client.get("/whoami", headers=_bearer(mint_token()))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_http_missing_scope_is_403(
    oidc_settings: OidcSettings, static_keys: StaticKeySource, mint_token: MintToken
):
    scoped = dataclasses.replace(oidc_settings, required_scope="items:write")
    client = _build_app(scoped, static_keys)
    token = mint_token({"scope": "items:read"})
    response = client.get("/whoami", headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert token not in response.text
