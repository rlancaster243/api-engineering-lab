from fastapi.testclient import TestClient

from app.main import HealthResponse, app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}
    assert HealthResponse.model_validate(response.json()).status == "ok"


def test_health_rejects_post_with_method_not_allowed() -> None:
    response = client.post("/health")
    assert response.status_code == 405
