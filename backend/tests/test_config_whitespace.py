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
    monkeypatch.setattr(config, "load_dotenv", Mock())


def test_padded_explicit_url_is_trimmed_and_takes_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///environment.db")

    assert config.resolve_database_url(" \t sqlite:///my database.db\r\n") == (
        "sqlite:///my database.db"
    )


@pytest.mark.parametrize("environment_url", [None, "sqlite:///environment.db"])
def test_whitespace_only_explicit_url_uses_postgres_settings(
    monkeypatch, environment_url
):
    for name, value in POSTGRES_ENV.items():
        monkeypatch.setenv(name, value)
    if environment_url is not None:
        monkeypatch.setenv("DATABASE_URL", environment_url)

    url = make_url(config.resolve_database_url(" \t\r\n"))

    assert url.drivername == "postgresql+psycopg"
    assert url.username == POSTGRES_ENV["POSTGRES_USER"]
    assert url.password == POSTGRES_ENV["POSTGRES_PASSWORD"]
    assert url.host == POSTGRES_ENV["POSTGRES_HOST"]
    assert url.port == 5433
    assert url.database == POSTGRES_ENV["POSTGRES_DB"]
