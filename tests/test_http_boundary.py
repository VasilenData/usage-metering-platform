import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture(autouse=True)
def clear_limiter():
    app.limits.clear()
    yield
    app.limits.clear()


def assert_protected(response, status):
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["server-timing"].startswith("app;dur=")


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_requests_keep_security_headers(chunked):
    content = (b"x" * 4096 for _ in range(3)) if chunked else b"x" * 8193
    assert_protected(TestClient(app.app).post("/api/sandbox", content=content), 413)


def test_rate_limit_keeps_security_headers_and_retry_after():
    for _ in range(240):
        assert app.limits.admit("anonymous", "testclient")
    response = TestClient(app.app).get("/api/overview")
    assert_protected(response, 429)
    assert response.headers["retry-after"] == "60"


@pytest.mark.parametrize("length", ["invalid", "-1"])
def test_invalid_length_keeps_security_headers(length):
    assert_protected(TestClient(app.app).post("/api/sandbox", headers={"Content-Length": length}), 400)


@pytest.mark.parametrize(
    "body",
    [
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b'{"x": -Infinity}',
        b'{"x": 1e400}',
        b'{"x": "\\u0000"}',
        b'{"\\u0000": "value"}',
        b'{"x": "\\ud800"}',
        b'{"x": 1, "x": 2}',
        b"[" * 65 + b"0" + b"]" * 65,
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'{"x": "\xff"}',
        b'{"x":',
    ],
)
def test_unsupported_json_rejected_before_authentication_or_database(body, monkeypatch):
    class UntouchedStore:
        def authenticate(self, _):
            pytest.fail("Malformed JSON must fail before token lookup or database access")

    monkeypatch.setattr(app, "store", UntouchedStore())
    response = TestClient(app.app).post(
        "/api/events",
        content=body,
        headers={"Authorization": "Bearer boundary-test", "Content-Type": "application/json"},
    )
    assert_protected(response, 422)
    assert response.json() == {"detail": "Invalid or unsupported JSON body"}


def test_supported_unicode_nested_json_reaches_authentication(monkeypatch):
    class InvalidTokenStore:
        def authenticate(self, _):
            return None

    monkeypatch.setattr(app, "store", InvalidTokenStore())
    response = TestClient(app.app).post(
        "/api/events",
        json={"properties": {"label": "Lisboa 🌍", "nested": [{"rate": 0.25, "ok": True}]}},
        headers={"Authorization": "Bearer boundary-test"},
    )
    assert_protected(response, 401)
