import pytest

from app.config import ConfigError, load_settings

BASE_ENVIRON = {"DATABASE_URL": "postgresql://postgres:postgres@localhost:5433/async_job_api_lab"}


def test_missing_database_url_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({})


def test_load_settings_accepts_plain_postgresql_url() -> None:
    settings = load_settings(BASE_ENVIRON)
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_worker_settings_default_when_unset() -> None:
    settings = load_settings(BASE_ENVIRON)
    assert settings.lease_seconds == 60
    assert settings.worker_poll_seconds == 1.0


def test_worker_settings_accept_overrides() -> None:
    settings = load_settings({**BASE_ENVIRON, "LEASE_SECONDS": "30", "WORKER_POLL_SECONDS": "0.25"})
    assert settings.lease_seconds == 30
    assert settings.worker_poll_seconds == 0.25


@pytest.mark.parametrize("value", ["0", "-0.5", "-1", "abc", "1.0x"])
def test_invalid_worker_poll_seconds_raises_config_error(value: str) -> None:
    with pytest.raises(ConfigError, match="WORKER_POLL_SECONDS"):
        load_settings({**BASE_ENVIRON, "WORKER_POLL_SECONDS": value})


@pytest.mark.parametrize("value", ["inf", "Infinity", "-inf", "-Infinity", "1e400", "nan", "NaN"])
def test_non_finite_worker_poll_seconds_raises_config_error(value: str) -> None:
    """``float()`` accepts inf and nan; ``stop.wait(inf)`` then raises OverflowError.

    The diagnostic must name the non-finite problem: a range message would send an
    operator looking for the wrong mistake, and it is the isfinite guard, not the
    upper bound, that rejects these.
    """
    with pytest.raises(ConfigError, match="WORKER_POLL_SECONDS") as raised:
        load_settings({**BASE_ENVIRON, "WORKER_POLL_SECONDS": value})
    assert "finite" in str(raised.value)


@pytest.mark.parametrize("value", ["3601", "100000", "1e10"])
def test_worker_poll_seconds_above_the_upper_bound_raises_config_error(value: str) -> None:
    """A poll longer than an hour is a misconfiguration, not a poll interval."""
    with pytest.raises(ConfigError, match="3600"):
        load_settings({**BASE_ENVIRON, "WORKER_POLL_SECONDS": value})


def test_worker_poll_seconds_accepts_the_upper_bound() -> None:
    assert (
        load_settings({**BASE_ENVIRON, "WORKER_POLL_SECONDS": "3600"}).worker_poll_seconds == 3600
    )


@pytest.mark.parametrize("value", ["0", "-5", "3600", "4000", "abc", "1.5"])
def test_invalid_lease_seconds_raises_config_error(value: str) -> None:
    """LEASE_SECONDS must be an integer in (0, 3600) — the bound is exclusive."""
    with pytest.raises(ConfigError, match="LEASE_SECONDS"):
        load_settings({**BASE_ENVIRON, "LEASE_SECONDS": value})


def test_lease_seconds_accepts_boundary_adjacent_values() -> None:
    assert load_settings({**BASE_ENVIRON, "LEASE_SECONDS": "1"}).lease_seconds == 1
    assert load_settings({**BASE_ENVIRON, "LEASE_SECONDS": "3599"}).lease_seconds == 3599


def test_empty_worker_values_fall_back_to_defaults() -> None:
    settings = load_settings({**BASE_ENVIRON, "LEASE_SECONDS": "", "WORKER_POLL_SECONDS": ""})
    assert settings.lease_seconds == 60
    assert settings.worker_poll_seconds == 1.0
