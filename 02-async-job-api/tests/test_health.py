import os

from fastapi.testclient import TestClient

_HEALTH_DUMMY_URL = "postgresql://postgres:postgres@127.0.0.1:1/health_does_not_connect"


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok", "service": "02-async-job-api"}


def test_health_rejects_post(client: TestClient) -> None:
    assert client.post("/health").status_code == 405


def test_health_does_not_target_a_developer_database(client: TestClient) -> None:
    url = os.environ["DATABASE_URL"]
    configured = os.environ.get("TEST_DATABASE_URL", "").strip()
    if configured:
        assert url == configured
    else:
        assert url == _HEALTH_DUMMY_URL
