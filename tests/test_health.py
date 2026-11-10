"""Tests for health endpoints and error handling. No real database needed.

TestClient is used without a `with` block, so the app lifespan (which connects to
the database) does not run. We attach a fake pool to app.state ourselves.
"""

from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient

from codelens.main import app


class FakeConnection:
    def __init__(self, vector_installed: bool, fail: bool):
        self.vector_installed = vector_installed
        self.fail = fail

    def execute(self, sql: str):
        if self.fail:
            raise psycopg.OperationalError("connection refused")
        return self

    def fetchone(self):
        return (1,) if self.vector_installed else None


class FakePool:
    def __init__(self, vector_installed: bool = True, fail: bool = False):
        self.vector_installed = vector_installed
        self.fail = fail

    @contextmanager
    def connection(self):
        yield FakeConnection(self.vector_installed, self.fail)


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False lets us see the 500 response the real client would get.
    return TestClient(app, raise_server_exceptions=False)


def test_health_does_not_need_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok(client):
    app.state.pool = FakePool()
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_database_is_down(client):
    app.state.pool = FakePool(fail=True)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_ready_returns_503_when_pgvector_missing(client):
    app.state.pool = FakePool(vector_installed=False)
    response = client.get("/ready")
    assert response.status_code == 503
    assert "pgvector" in response.json()["error"]["message"]


def test_validation_errors_use_the_standard_error_shape(client):
    @app.get("/_needs_int")
    def needs_int(n: int) -> dict:
        return {"n": n}

    response = client.get("/_needs_int", params={"n": "secret-value"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_input"
    assert "n" in body["error"]["message"]
    assert "secret-value" not in response.text


def test_unexpected_error_hides_internal_details(client):
    class ExplodingPool:
        def connection(self):
            raise RuntimeError("secret internal detail")

    app.state.pool = ExplodingPool()
    response = client.get("/ready")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
