"""APP_ENV is required, recognised, and decided exactly once.

A deployment that sets DATABASE_PATH but forgets APP_ENV used to fall back to
``local``, which seeded two tenants whose API keys are published in the README.
These tests pin the closed door: no default, no unknown values, and one source of
truth for the seeding decision.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import DEV_API_KEYS, local_env, seed_local_tenants
from app.config import LOCAL_ENVS, Settings, load_settings
from app.db import connect, initialize
from app.main import app, prepare_database

DOCUMENTED_KEY = "local-dev-key-alpha"
OTHER_DOCUMENTED_KEY = "local-dev-key-beta"


def _env(monkeypatch: pytest.MonkeyPatch, database_path: Path, app_env: str | None) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)


def _tenant_names(database_path: Path) -> list[str]:
    connection = connect(str(database_path))
    try:
        return [row["name"] for row in connection.execute("SELECT name FROM tenants")]
    finally:
        connection.close()


def test_omitting_app_env_raises_instead_of_defaulting_to_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DATABASE_PATH is set, i.e. someone is configuring this for real."""
    _env(monkeypatch, tmp_path / "registry.sqlite3", None)

    with pytest.raises(RuntimeError) as raised:
        load_settings()

    assert "APP_ENV is required" in str(raised.value)


def test_forgotten_app_env_never_exposes_the_published_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The actual defect: a real DATABASE_PATH plus a forgotten APP_ENV."""
    _env(monkeypatch, tmp_path / "registry.sqlite3", None)

    started = None
    try:
        with TestClient(app) as client:
            started = client.get("/v1/assets", headers={"X-API-Key": DOCUMENTED_KEY}).status_code
    except RuntimeError:
        started = None

    assert started is None, (
        f"startup did not fail closed: the README key authenticated with {started}"
    )


def test_unknown_app_env_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A typo must not silently change whether the documented keys exist."""
    _env(monkeypatch, tmp_path / "registry.sqlite3", "prod")

    with pytest.raises(RuntimeError) as raised:
        load_settings()

    assert "APP_ENV='prod'" in str(raised.value)
    assert "local" in str(raised.value)


def test_production_startup_seeds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The published key must not authenticate a deployment that said production."""
    database_path = tmp_path / "registry.sqlite3"
    _env(monkeypatch, database_path, "production")

    with TestClient(app) as client:
        response = client.get("/v1/assets", headers={"X-API-Key": DOCUMENTED_KEY})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert _tenant_names(database_path) == []


def test_local_env_reads_settings_not_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-read of os.environ at call time is the split brain this pins shut."""
    _env(monkeypatch, tmp_path / "registry.sqlite3", "production")
    settings = load_settings()

    monkeypatch.setenv("APP_ENV", "local")
    assert local_env(settings) is False

    local = Settings(database_path=settings.database_path, app_env="local")
    monkeypatch.delenv("APP_ENV", raising=False)
    assert local_env(local) is True


@pytest.mark.parametrize("app_env", [*LOCAL_ENVS, "production"])
def test_local_env_agrees_with_the_seeding_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: str
) -> None:
    """One decision: the predicate and the seeding call must never disagree."""
    database_path = tmp_path / "registry.sqlite3"
    _env(monkeypatch, database_path, app_env)
    settings = load_settings()

    prepare_database(settings)

    seeded = _tenant_names(database_path)
    if local_env(settings):
        assert sorted(seeded) == sorted(DEV_API_KEYS.values())
    else:
        assert seeded == []
    assert (app_env in LOCAL_ENVS) is local_env(settings)


def test_startup_seeding_follows_settings_not_a_second_environment_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Split brain: settings say production, os.environ says local.

    Startup must seed from the configuration it loaded. A caller that re-reads
    os.environ would seed here, and the published key would work in production.
    """
    database_path = tmp_path / "registry.sqlite3"
    _env(monkeypatch, database_path, "production")
    settings = load_settings()

    monkeypatch.setenv("APP_ENV", "local")
    prepare_database(settings)

    assert local_env(settings) is False
    assert _tenant_names(database_path) == []
    connection = connect(str(database_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
    finally:
        connection.close()


def test_local_app_env_seeds_the_documented_quick_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The README flow: APP_ENV=local, then curl with the published key."""
    _env(monkeypatch, tmp_path / "registry.sqlite3", "local")

    with TestClient(app) as client:
        created = client.post(
            "/v1/assets",
            headers={"X-API-Key": DOCUMENTED_KEY},
            json={
                "name": "Q3 report",
                "source_uri": "https://files.example.test/q3.pdf",
                "content_type": "application/pdf",
                "size_bytes": 4096,
            },
        )
        assert created.status_code == 201, created.text

        asset_id = created.json()["id"]
        fetched = client.get(f"/v1/assets/{asset_id}", headers={"X-API-Key": DOCUMENTED_KEY})
        assert fetched.status_code == 200

        other_tenant = client.get("/v1/assets", headers={"X-API-Key": OTHER_DOCUMENTED_KEY})
        assert other_tenant.status_code == 200
        assert other_tenant.json()["assets"] == []


def test_seeding_local_tenants_is_idempotent(tmp_path: Path) -> None:
    """Startup may run more than once against the same file."""
    database_path = tmp_path / "registry.sqlite3"
    connection = connect(str(database_path))
    try:
        initialize(connection)
        seed_local_tenants(connection)
        seed_local_tenants(connection)
        assert connection.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == len(
            DEV_API_KEYS
        )
    finally:
        connection.close()


def test_settings_requires_an_environment() -> None:
    """A default here would be the second place that decides local mode."""
    with pytest.raises(TypeError):
        Settings(database_path="var/assets.sqlite3")  # type: ignore[call-arg]
