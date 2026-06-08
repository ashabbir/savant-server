import pytest
from flask import Flask, jsonify, g

import hardening


def setup_function():
    hardening._rate_limit_data.clear()


def test_retry_with_backoff_success_first_try():
    calls = {"n": 0}

    @hardening.retry_with_backoff(max_retries=3, initial_delay=0.001)
    def fn():
        calls["n"] += 1
        return "ok"

    assert fn() == "ok"
    assert calls["n"] == 1


def test_retry_with_backoff_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []
    monkeypatch.setattr(hardening.time, "sleep", lambda s: sleeps.append(s))

    @hardening.retry_with_backoff(max_retries=3, initial_delay=0.5, max_delay=1.0)
    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("fail")
        return "ok"

    assert fn() == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


def test_retry_with_backoff_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(hardening.time, "sleep", lambda _s: None)

    @hardening.retry_with_backoff(max_retries=2, initial_delay=0.1)
    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        fn()


def test_validation_helpers():
    ok, err = hardening.validate_required_fields({"name": "x"}, ["name"])
    assert ok is True and err is None

    ok, err = hardening.validate_required_fields({}, ["name"])
    assert ok is False and "Missing required field" in err

    ok, err = hardening.validate_required_fields({"name": "   "}, ["name"])
    assert ok is False and "cannot be empty" in err

    ok, err = hardening.validate_string_length("abc", "name", max_length=3)
    assert ok is True and err is None

    ok, err = hardening.validate_string_length("abcd", "name", max_length=3)
    assert ok is False and "exceeds maximum length" in err

    ok, err = hardening.validate_enum("a", "mode", ["a", "b"])
    assert ok is True and err is None

    ok, err = hardening.validate_enum("x", "mode", ["a", "b"])
    assert ok is False and "must be one of" in err


def test_sanitize_text_and_safe_limit():
    assert hardening.sanitize_text("<b>hi</b>") == "hi"
    assert hardening.sanitize_text("javascript:alert(1)") == "alert(1)"
    assert hardening.sanitize_text(42) == "42"

    assert hardening.safe_limit(None) == 100
    assert hardening.safe_limit(0, default=5, maximum=10) == 1
    assert hardening.safe_limit(20, default=5, maximum=10) == 10
    assert hardening.safe_limit(7, default=5, maximum=10) == 7


def test_check_rate_limit_allows_then_blocks():
    # deterministic clock
    t = {"now": 1000.0}
    hardening.time.time = lambda: t["now"]

    ok, err = hardening.check_rate_limit("1.2.3.4", max_requests=2, window_seconds=10)
    assert ok is True and err is None
    ok, err = hardening.check_rate_limit("1.2.3.4", max_requests=2, window_seconds=10)
    assert ok is True and err is None

    ok, err = hardening.check_rate_limit("1.2.3.4", max_requests=2, window_seconds=10)
    assert ok is False and "Rate limit exceeded" in err

    # move beyond window; request allowed again
    t["now"] = 1011.0
    ok, err = hardening.check_rate_limit("1.2.3.4", max_requests=2, window_seconds=10)
    assert ok is True and err is None


def test_rate_limit_decorator_blocks_after_threshold():
    app = Flask(__name__)

    @app.route("/limited")
    @hardening.rate_limit(max_requests=1, window_seconds=60)
    def limited():
        return jsonify({"ok": True})

    c = app.test_client()
    first = c.get("/limited")
    assert first.status_code == 200
    second = c.get("/limited")
    assert second.status_code == 429


def test_validate_request_decorator_success_and_failures(monkeypatch):
    app = Flask(__name__)

    @app.route("/v", methods=["POST"])
    @hardening.validate_request(
        required_fields=["name"],
        max_length={"name": 5},
        enum_fields={"kind": ["a", "b"]},
    )
    def validated():
        return jsonify(g.validated_data)

    c = app.test_client()

    # missing required
    r = c.post("/v", json={"kind": "a"})
    assert r.status_code == 400

    # max length violation
    r = c.post("/v", json={"name": "toolong", "kind": "a"})
    assert r.status_code == 400

    # enum violation
    r = c.post("/v", json={"name": "ok", "kind": "z"})
    assert r.status_code == 400

    # sanitize + success
    r = c.post("/v", json={"name": "ok", "kind": "a"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["name"] == "ok"
    assert data["kind"] == "a"

    # force get_json exception path
    class _BadReq:
        remote_addr = None

        @staticmethod
        def get_json(*_args, **_kwargs):
            raise RuntimeError("bad-json")

    monkeypatch.setattr(hardening, "request", _BadReq())

    @hardening.validate_request(required_fields=["x"])
    def dummy():
        return "ok"

    with app.app_context():
        body, status = dummy()
        assert status == 400
        assert body.get_json()["error"] == "Invalid JSON in request body"
