from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from app.core import config


POSTGRES_ENV = {
    "POSTGRES_USER": "taskly",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_HOST": "db.example.test",
    "DB_PORT": "5433",
    "POSTGRES_DB": "taskly_test",
}


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for name in ("DATABASE_URL", *POSTGRES_ENV):
        monkeypatch.delenv(name, raising=False)
    dotenv = Mock()
    monkeypatch.setattr(config, "load_dotenv", dotenv)
    return dotenv


@pytest.mark.parametrize(
    ("explicit", "environment", "expected"),
    [
        ("sqlite:///explicit.db", "sqlite:///environment.db", "sqlite:///explicit.db"),
        ("sqlite:///explicit.db", None, "sqlite:///explicit.db"),
        (None, "sqlite:///environment.db", "sqlite:///environment.db"),
        ("", "sqlite:///environment.db", "sqlite:///environment.db"),
    ],
)
def test_direct_url_precedence(monkeypatch, explicit, environment, expected):
    if environment is not None:
        monkeypatch.setenv("DATABASE_URL", environment)

    assert config.resolve_database_url(explicit) == expected


@pytest.mark.parametrize("environment_url", [None, ""])
def test_builds_postgres_url_with_preserved_credentials(monkeypatch, environment_url):
    values = {
        **POSTGRES_ENV,
        "POSTGRES_USER": "user@team",
        "POSTGRES_PASSWORD": "p@ss:/?#% word",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    if environment_url is not None:
        monkeypatch.setenv("DATABASE_URL", environment_url)

    url = make_url(config.resolve_database_url())

    assert url.drivername == "postgresql+psycopg"
    assert url.username == values["POSTGRES_USER"]
    assert url.password == values["POSTGRES_PASSWORD"]
    assert url.host == values["POSTGRES_HOST"]
    assert url.port == 5433
    assert url.database == values["POSTGRES_DB"]


@pytest.mark.parametrize("existing_url", [None, "sqlite:///environment.db"])
def test_loads_repository_dotenv_without_overriding_environment(
    monkeypatch, isolated_config, existing_url
):
    if existing_url is not None:
        monkeypatch.setenv("DATABASE_URL", existing_url)

    def load_dotenv(path, *, override):
        import os

        if override or "DATABASE_URL" not in os.environ:
            monkeypatch.setenv("DATABASE_URL", "sqlite:///dotenv.db")

    isolated_config.side_effect = load_dotenv

    assert config.resolve_database_url() == (existing_url or "sqlite:///dotenv.db")
    isolated_config.assert_called_once_with(
        Path(__file__).resolve().parents[2] / ".env", override=False
    )


@pytest.mark.parametrize("missing", list(POSTGRES_ENV))
def test_missing_postgres_setting_raises_key_error(monkeypatch, missing):
    for name, value in POSTGRES_ENV.items():
        if name != missing:
            monkeypatch.setenv(name, value)

    with pytest.raises(KeyError) as error:
        config.resolve_database_url()

    assert error.value.args == (missing,)


@pytest.mark.parametrize("port", ["", "not-a-port", "5432.5"])
def test_noninteger_postgres_port_is_rejected(monkeypatch, port):
    for name, value in POSTGRES_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DB_PORT", port)

    with pytest.raises(ValueError):
        config.resolve_database_url()
