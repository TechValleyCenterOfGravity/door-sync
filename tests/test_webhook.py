import hashlib
import hmac
import logging
import queue
import time

from door_sync import webhook
from door_sync.config import WebhookConfig
from door_sync.models import ReconcileRequest

SECRET = "test-secret-abcdefghij"  # >= 16 chars


def _wcfg(**overrides: object) -> WebhookConfig:
    base: dict[str, object] = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8787,
        "hmac_secret": SECRET,
        "max_body_bytes": 1024,
        "max_skew_seconds": 300,
        "debounce_seconds": 0.0,
    }
    base.update(overrides)
    return WebhookConfig(**base)  # type: ignore[arg-type]


def _digest(body: bytes, *, secret: str = SECRET, ts: int) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _sign(body: bytes, *, secret: str = SECRET, ts: int | None = None) -> dict[str, str]:
    if ts is None:
        ts = int(time.time())
    return {
        "X-Door-Sync-Timestamp": str(ts),
        "X-Door-Sync-Signature": f"sha256={_digest(body, secret=secret, ts=ts)}",
        "Content-Type": "application/json",
    }


def _client(wcfg: WebhookConfig | None = None) -> tuple[object, "queue.Queue[object]"]:
    q: queue.Queue[object] = queue.Queue()
    app = webhook.create_app(wcfg or _wcfg(), q)
    app.testing = True
    return app.test_client(), q


# --- _verify_signature unit tests ---


def test_verify_signature_valid() -> None:
    body = b'{"contact_id": 5}'
    ts = int(time.time())
    assert webhook._verify_signature(
        SECRET,
        body,
        timestamp=str(ts),
        signature=f"sha256={_digest(body, ts=ts)}",
        max_skew_seconds=300,
        now=ts,
    )


def test_verify_signature_bad_digest() -> None:
    body = b'{"contact_id": 5}'
    ts = int(time.time())
    assert not webhook._verify_signature(
        SECRET, body, timestamp=str(ts), signature="sha256=deadbeef", max_skew_seconds=300, now=ts
    )


def test_verify_signature_wrong_secret() -> None:
    body = b'{"contact_id": 5}'
    ts = int(time.time())
    assert not webhook._verify_signature(
        SECRET,
        body,
        timestamp=str(ts),
        signature=f"sha256={_digest(body, secret='other-secret', ts=ts)}",
        max_skew_seconds=300,
        now=ts,
    )


def test_verify_signature_tampered_body() -> None:
    ts = int(time.time())
    sig = f"sha256={_digest(b'original', ts=ts)}"
    assert not webhook._verify_signature(
        SECRET, b"tampered", timestamp=str(ts), signature=sig, max_skew_seconds=300, now=ts
    )


def test_verify_signature_missing_headers() -> None:
    body = b"{}"
    ts = int(time.time())
    assert not webhook._verify_signature(
        SECRET, body, timestamp=None, signature=None, max_skew_seconds=300, now=ts
    )
    assert not webhook._verify_signature(
        SECRET,
        body,
        timestamp=str(ts),
        signature=None,
        max_skew_seconds=300,
        now=ts,
    )


def test_verify_signature_replay_rejected() -> None:
    body = b"{}"
    ts = int(time.time()) - 10_000
    assert not webhook._verify_signature(
        SECRET,
        body,
        timestamp=str(ts),
        signature=f"sha256={_digest(body, ts=ts)}",
        max_skew_seconds=300,
        now=time.time(),
    )


def test_verify_signature_accepts_bare_and_prefixed() -> None:
    body = b"{}"
    ts = int(time.time())
    digest = _digest(body, ts=ts)
    assert webhook._verify_signature(
        SECRET, body, timestamp=str(ts), signature=digest, max_skew_seconds=300, now=ts
    )
    assert webhook._verify_signature(
        SECRET, body, timestamp=str(ts), signature=f"sha256={digest}", max_skew_seconds=300, now=ts
    )


def test_verify_signature_non_integer_timestamp() -> None:
    body = b"{}"
    assert not webhook._verify_signature(
        SECRET,
        body,
        timestamp="not-a-number",
        signature="sha256=abc",
        max_skew_seconds=300,
        now=time.time(),
    )


# --- endpoint tests ---


def test_membership_changed_valid_enqueues_and_202() -> None:
    client, q = _client()
    body = b'{"contact_id": 7}'
    resp = client.post("/civicrm/membership-changed", data=body, headers=_sign(body))  # type: ignore[attr-defined]
    assert resp.status_code == 202
    item = q.get_nowait()
    assert isinstance(item, ReconcileRequest)
    assert item.reason == "membership-changed"
    assert item.contact_id == 7
    assert q.empty()


def test_membership_changed_bad_signature_401_and_no_enqueue() -> None:
    client, q = _client()
    body = b'{"contact_id": 7}'
    headers = _sign(body)
    headers["X-Door-Sync-Signature"] = "sha256=bad"
    resp = client.post("/civicrm/membership-changed", data=body, headers=headers)  # type: ignore[attr-defined]
    assert resp.status_code == 401
    assert q.empty()


def test_membership_changed_missing_signature_401() -> None:
    client, q = _client()
    resp = client.post(  # type: ignore[attr-defined]
        "/civicrm/membership-changed", data=b"{}", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401
    assert q.empty()


def test_membership_changed_replay_rejected_401() -> None:
    client, q = _client()
    body = b"{}"
    old = int(time.time()) - 10_000
    resp = client.post(  # type: ignore[attr-defined]
        "/civicrm/membership-changed", data=body, headers=_sign(body, ts=old)
    )
    assert resp.status_code == 401
    assert q.empty()


def test_membership_changed_oversize_body_413() -> None:
    client, q = _client(_wcfg(max_body_bytes=10))
    body = b'{"contact_id": 1234567890}'  # > 10 bytes
    resp = client.post("/civicrm/membership-changed", data=body, headers=_sign(body))  # type: ignore[attr-defined]
    assert resp.status_code == 413
    assert q.empty()


def test_contact_id_extraction_tolerant() -> None:
    client, q = _client()
    body = b'{"no_contact_here": true}'
    resp = client.post("/civicrm/membership-changed", data=body, headers=_sign(body))  # type: ignore[attr-defined]
    assert resp.status_code == 202
    item = q.get_nowait()
    assert isinstance(item, ReconcileRequest)
    assert item.contact_id is None


def test_healthz_ok() -> None:
    client, _ = _client()
    resp = client.get("/healthz")  # type: ignore[attr-defined]
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "ok"


def test_no_pii_in_logs(caplog) -> None:  # type: ignore[no-untyped-def]
    client, _ = _client()
    body = b'{"contact_id": 7, "display_name": "Jane Secret", "email": "jane@example.com"}'
    with caplog.at_level(logging.INFO, logger="door_sync.webhook"):
        resp = client.post("/civicrm/membership-changed", data=body, headers=_sign(body))  # type: ignore[attr-defined]
    assert resp.status_code == 202
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Jane Secret" not in text
    assert "jane@example.com" not in text
    assert "7" in text  # contact_id IS logged


# --- regression tests for review findings ---


def test_verify_signature_non_ascii_header_fails_closed() -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str, and Werkzeug
    latin-1 decodes header bytes -- so a malformed signature used to surface as
    an unauthenticated 500 with a traceback instead of a 401."""
    body = b"{}"
    ts = int(time.time())
    for bad in ("café", "sha256=café", "ÿ" * 64):
        assert (
            webhook._verify_signature(
                SECRET, body, timestamp=str(ts), signature=bad, max_skew_seconds=300, now=ts
            )
            is False
        ), bad


def test_membership_changed_non_ascii_signature_401() -> None:
    client, q = _client()
    body = b"{}"
    headers = _sign(body)
    headers["X-Door-Sync-Signature"] = "sha256=café"
    resp = client.post("/civicrm/membership-changed", data=body, headers=headers)  # type: ignore[attr-defined]
    assert resp.status_code == 401
    assert q.empty()


def test_non_decimal_contact_id_still_enqueues() -> None:
    """str.isdigit() is True for superscripts and circled digits, which int()
    rejects. That ValueError escaped the helper, 500ing a correctly signed
    request and silently dropping the reconcile trigger."""
    for weird in ("²", "②", "12²"):
        client, q = _client()
        body = f'{{"contact_id": "{weird}"}}'.encode()
        resp = client.post("/civicrm/membership-changed", data=body, headers=_sign(body))  # type: ignore[attr-defined]
        assert resp.status_code == 202, weird
        item = q.get_nowait()
        assert isinstance(item, ReconcileRequest)
        assert item.contact_id is None


def test_extract_contact_id_survives_deep_nesting() -> None:
    """json.loads raises RecursionError, not ValueError, on deeply nested input;
    the docstring promises None on any parse issue."""
    assert webhook._extract_contact_id(b"[" * 20_000 + b"]" * 20_000) is None


def test_body_cap_is_passed_to_waitress(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Flask's MAX_CONTENT_LENGTH only applies after waitress has read the whole
    body; waitress defaults to 1 GB, so the cap must reach it too."""
    captured: dict[str, object] = {}

    class _FakeServer:
        def run(self) -> None:
            pass

        def close(self) -> None:
            pass

    def _fake_create_server(app: object, **kwargs: object) -> _FakeServer:
        captured.update(kwargs)
        return _FakeServer()

    monkeypatch.setattr(webhook, "create_server", _fake_create_server)
    wcfg = _wcfg(max_body_bytes=4096)
    server = webhook.start(wcfg, work_queue=queue.Queue())
    server.stop(timeout=0.5)
    assert captured["max_request_body_size"] == 4096


def test_stop_warns_when_the_serving_thread_will_not_die(caplog) -> None:  # type: ignore[no-untyped-def]
    """join() result was discarded, so a thread that refused to stop was
    invisible to the operator."""
    import threading

    never_stops = threading.Event()
    thread = threading.Thread(target=never_stops.wait, daemon=True)
    thread.start()

    class _Server:
        def close(self) -> None:
            pass

    server = webhook.WebhookServer(_Server(), thread, threading.Event())
    try:
        with caplog.at_level(logging.WARNING, logger="door_sync.webhook"):
            server.stop(timeout=0.05)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "did not stop within" in text
    finally:
        never_stops.set()
        thread.join(timeout=2)
