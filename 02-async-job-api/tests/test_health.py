from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok", "service": "02-async-job-api"}


def test_health_rejects_post(client: TestClient) -> None:
    assert client.post("/health").status_code == 405
