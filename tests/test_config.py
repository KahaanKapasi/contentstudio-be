import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("postgres://user:pw@host:5432/db", "postgresql+psycopg://user:pw@host:5432/db"),
        ("postgresql://user:pw@host/db?sslmode=require", "postgresql+psycopg://user:pw@host/db?sslmode=require"),
        ("postgresql+psycopg://user:pw@host/db", "postgresql+psycopg://user:pw@host/db"),
        ("sqlite:///./local.db", "sqlite:///./local.db"),
        ("sqlite:////abs/path/x.db", "sqlite:////abs/path/x.db"),
    ],
)
def test_database_url_is_normalized_for_psycopg3(raw, expected):
    assert Settings(database_url=raw).database_url == expected


def test_database_url_only_rewrites_scheme_prefix():
    url = "postgres://user:postgresql://@host/db"
    assert Settings(database_url=url).database_url == "postgresql+psycopg://user:postgresql://@host/db"
