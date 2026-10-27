import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.errors import ERROR_SPECS, ErrorCode
from app.main import app as real_app

real_client = TestClient(real_app)


def _assert_envelope(response, code: ErrorCode) -> dict:
    body = response.json()
    assert set(body) == {"error"}, "FastAPI's default {'detail': ...} must never leak through"
    error = body["error"]
    assert error["code"] == code.value
    assert response.status_code == ERROR_SPECS[code].status
    assert isinstance(error["message"], str) and error["message"]
    uuid.UUID(error["request_id"])
    assert response.headers["X-Request-ID"] == error["request_id"]
    return error


# --- framework errors on the real application -------------------------------------------


def test_unknown_route_uses_the_envelope() -> None:
    _assert_envelope(real_client.get("/no-such-route"), ErrorCode.ROUTE_NOT_FOUND)


def test_wrong_method_uses_the_envelope_and_keeps_allow_header() -> None:
    response = real_client.post("/health")
    _assert_envelope(response, ErrorCode.METHOD_NOT_ALLOWED)
    assert "GET" in response.headers["Allow"]


def test_health_response_is_unchanged_and_carries_a_request_id() -> None:
    response = real_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    uuid.UUID(response.headers["X-Request-ID"])


# --- validation errors ------------------------------------------------------------------


def test_validation_error_names_the_field_and_never_echoes_input(client: TestClient) -> None:
    response = client.post("/v1/items", json={"content_id": "a", "user_id": "SECRET-123"})
    error = _assert_envelope(response, ErrorCode.VALIDATION_ERROR)
    assert {"field": "body.user_id", "rule": "extra_forbidden"} in error["details"]
    assert "SECRET-123" not in response.text


def test_value_rule_failures_do_not_echo_the_submitted_value(client: TestClient) -> None:
    secret = "SECRET-" + "x" * 300
    response = client.post("/v1/items", json={"content_id": secret})
    _assert_envelope(response, ErrorCode.VALIDATION_ERROR)
    assert "SECRET-" not in response.text


def test_malformed_json_body_uses_the_envelope(client: TestClient) -> None:
    response = client.post(
        "/v1/items", content="{not json", headers={"Content-Type": "application/json"}
    )
    _assert_envelope(response, ErrorCode.VALIDATION_ERROR)


def test_malformed_item_id_is_a_422_validation_error(client: TestClient) -> None:
    error = _assert_envelope(client.get("/v1/items/not-a-uuid"), ErrorCode.VALIDATION_ERROR)
    assert error["details"][0]["field"] == "path.item_id"


def test_empty_patch_is_a_422_validation_error(client: TestClient) -> None:
    response = client.patch(f"/v1/items/{uuid.uuid4()}", json={})
    _assert_envelope(response, ErrorCode.VALIDATION_ERROR)


def test_patch_over_http_distinguishes_omitted_from_null(client: TestClient) -> None:
    item = f"/v1/items/{uuid.uuid4()}"
    assert client.patch(item, json={"title": "t"}).json() == {"title": "t"}
    assert client.patch(item, json={"notes": None}).json() == {"notes": None}


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", ""])
def test_invalid_limit_is_rejected_not_clamped(client: TestClient, limit: str) -> None:
    error = _assert_envelope(
        client.get("/v1/items", params={"limit": limit}), ErrorCode.VALIDATION_ERROR
    )
    assert error["details"][0]["field"] == "query.limit"


def test_limit_defaults_to_25_and_accepts_the_bounds(client: TestClient) -> None:
    assert client.get("/v1/items").headers["X-Applied-Limit"] == "25"
    for limit in ("1", "100"):
        response = client.get("/v1/items", params={"limit": limit})
        assert response.status_code == 200
        assert response.headers["X-Applied-Limit"] == limit
    assert client.get("/v1/items").json() == {"items": [], "next_cursor": None}


def test_create_returns_201_with_the_documented_resource_shape(client: TestClient) -> None:
    response = client.post("/v1/items", json={"content_id": " article-042 ", "title": "T"})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id", "content_id", "title", "url", "notes", "created_at", "updated_at"
    }  # fmt: skip
    assert body["content_id"] == "article-042"
    assert body["created_at"] == "2026-10-16T14:00:00Z"


# --- every contract code ----------------------------------------------------------------


@pytest.mark.parametrize("code", list(ErrorCode))
def test_each_error_code_renders_its_documented_status(client: TestClient, code: ErrorCode) -> None:
    response = client.get(f"/_raise/{code.value}")
    _assert_envelope(response, code)
    if ERROR_SPECS[code].status == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    else:
        assert "WWW-Authenticate" not in response.headers


def test_request_ids_are_server_generated_and_unique(client: TestClient) -> None:
    first = client.get("/no-such-route", headers={"X-Request-ID": "attacker-chosen"})
    second = client.get("/no-such-route")
    assert first.json()["error"]["request_id"] != "attacker-chosen"
    assert first.json()["error"]["request_id"] != second.json()["error"]["request_id"]


# --- unexpected failures ----------------------------------------------------------------


def test_unexpected_exception_is_sanitized_and_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = client.get("/_boom")
    error = _assert_envelope(response, ErrorCode.INTERNAL_ERROR)
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text
    assert any(error["request_id"] in record.getMessage() for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)


def test_unmapped_http_exception_never_leaks_its_detail(client: TestClient) -> None:
    response = client.get("/_http/418")
    _assert_envelope(response, ErrorCode.INTERNAL_ERROR)
    assert "secret internal detail" not in response.text


# --- database failures over HTTP (real app) ---------------------------------------------


def _patch_create_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def raise_it(self, body):  # noqa: ANN001, ANN202
        raise exc

    monkeypatch.setattr("app.service.ItemService.create", raise_it)


def test_non_duplicate_integrity_error_is_a_sanitized_500_not_503(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_create_to_raise(monkeypatch, IntegrityError("INSERT", {}, Exception("fk_violation")))
    with caplog.at_level(logging.ERROR, logger="app.errors"):
        response = api.post("/v1/items", json={"content_id": "x"})
    error = _assert_envelope(response, ErrorCode.INTERNAL_ERROR)
    assert response.status_code == 500
    assert "fk_violation" not in response.text
    assert "INSERT" not in response.text
    assert any(error["request_id"] in record.getMessage() for record in caplog.records)


def test_generic_non_unavailable_dbapi_error_is_a_sanitized_500(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_create_to_raise(
        monkeypatch, ProgrammingError("SELECT bad", {}, Exception("undefined_table"))
    )
    response = api.post("/v1/items", json={"content_id": "x"})
    _assert_envelope(response, ErrorCode.INTERNAL_ERROR)
    assert "undefined_table" not in response.text
    assert "SELECT bad" not in response.text
