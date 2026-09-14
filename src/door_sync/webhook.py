"""Embedded webhook receiver (architecture §13 / design guide Appendix C).

A sync Flask app served by waitress in a second thread of the daemon. The HTTP
thread does the minimum: verify the HMAC signature over the raw request body,
validate the payload, and enqueue a work item. It returns 202 at once. The
scheduler's single drainer thread is the only writer to UniFi/state/audit, so
the HTTP path never touches those resources — no locks, no concurrent writes.

Layering: this module is top-of-graph. Phase 1 imports config + models only (it
merely enqueues; it does not call orchestrator). Nothing imports it except the
run wiring in __main__. No asyncio — sync Flask + waitress (architecture §3, §13).

Logging: contact_id only. Never log member names, emails, or card IDs (§11).
"""

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from typing import Any

from flask import Flask, Response, request
from waitress.server import create_server  # type: ignore[import-untyped]

from door_sync.config import WebhookConfig
from door_sync.models import ReconcileRequest

_logger = logging.getLogger("door_sync.webhook")

_TS_HEADER = "X-Door-Sync-Timestamp"
_SIG_HEADER = "X-Door-Sync-Signature"
_SIG_PREFIX = "sha256="


def _verify_signature(
    secret: str,
    raw_body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    max_skew_seconds: int,
    now: float,
) -> bool:
    """Return True iff `signature` is a valid HMAC-SHA256 over ``f"{ts}." + body``
    under `secret`, and `timestamp` is within `max_skew_seconds` of `now`.

    Constant-time compare; fail-closed on any missing or malformed input. The
    timestamp binding gives replay protection. `signature` may be bare hex or
    ``sha256=<hex>``.
    """
    if not secret or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > max_skew_seconds:
        return False
    provided = signature[len(_SIG_PREFIX) :] if signature.startswith(_SIG_PREFIX) else signature
    signed = f"{ts}.".encode("ascii") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    # compare_digest raises TypeError on non-ASCII str, and Werkzeug latin-1
    # decodes header bytes -- so compare as bytes and fail closed on anything
    # that is not ASCII. A malformed header must be a 401, never a 500.
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected.encode("ascii"), provided_bytes)


def _extract_contact_id(raw_body: bytes) -> int | None:
    """Best-effort parse of the CiviCRM contact_id, for logging only.

    Returns None on any parse issue — the reconcile is whole-population
    regardless, so a missing id never blocks the trigger.
    """
    try:
        payload = json.loads(raw_body)
        if isinstance(payload, dict):
            cid = payload.get("contact_id")
            if isinstance(cid, int) and not isinstance(cid, bool):
                return cid
            # isdecimal(), not isdigit(): isdigit() is True for superscripts and
            # other forms int() rejects, which would raise straight past this
            # helper and drop the trigger.
            if isinstance(cid, str) and cid.isdecimal():
                return int(cid)
    except Exception:  # noqa: BLE001 - best-effort parse; see docstring
        return None
    return None


def create_app(webhook_config: WebhookConfig, work_queue: "queue.Queue[object]") -> Flask:
    """Build the Flask app. Routes close over the webhook config slice and the
    shared work queue that the scheduler thread drains.
    """
    app = Flask("door_sync.webhook")
    wcfg = webhook_config
    app.config["MAX_CONTENT_LENGTH"] = wcfg.max_body_bytes  # defense in depth

    @app.get("/healthz")
    def healthz() -> Response:
        """Liveness probe for cloudflared / local monitoring. No auth."""
        return Response("ok", status=200, mimetype="text/plain")

    @app.post("/civicrm/membership-changed")
    def membership_changed() -> Response:
        """Verify HMAC, enqueue a full-reconcile trigger, return 202.

        A bad or missing signature aborts with 401 BEFORE anything is enqueued.
        """
        raw = request.get_data(cache=False, as_text=False)
        if not _verify_signature(
            wcfg.hmac_secret,
            raw,
            timestamp=request.headers.get(_TS_HEADER),
            signature=request.headers.get(_SIG_HEADER),
            max_skew_seconds=wcfg.max_skew_seconds,
            now=time.time(),
        ):
            _logger.warning("webhook rejected: invalid or missing signature")
            return Response(status=401)
        contact_id = _extract_contact_id(raw)  # logging only
        work_queue.put(ReconcileRequest(reason="membership-changed", contact_id=contact_id))
        _logger.info("membership webhook accepted; contact_id=%s; reconcile queued", contact_id)
        return Response(status=202)

    return app


class WebhookServer:
    """Handle for the waitress-served Flask app running in a daemon thread."""

    def __init__(self, server: Any, thread: threading.Thread, stopping: threading.Event) -> None:
        self._server = server
        self._thread = thread
        self._stopping = stopping

    def stop(self, *, timeout: float = 10.0) -> None:
        """Gracefully stop the server and join its thread (clean daemon exit)."""
        self._stopping.set()
        self._server.close()
        self._thread.join(timeout=timeout)


def start(webhook_config: WebhookConfig, *, work_queue: "queue.Queue[object]") -> WebhookServer:
    """Create a waitress server bound to the configured loopback host/port and
    run it in a daemon thread. Returns a handle whose ``.stop()`` shuts it down.
    """
    wcfg = webhook_config
    app = create_app(wcfg, work_queue)
    # Cap the body at the waitress layer too. Flask's MAX_CONTENT_LENGTH only
    # applies after waitress has already read the whole request, and waitress
    # defaults to 1 GB (spooling past 512 KB to a tempfile).
    server = create_server(
        app,
        host=wcfg.host,
        port=wcfg.port,
        max_request_body_size=wcfg.max_body_bytes,
    )
    stopping = threading.Event()

    def _serve() -> None:
        try:
            server.run()
        except OSError:
            # stop() closes the listening socket out from under the asyncore
            # select() loop; the resulting EBADF is an expected teardown
            # artifact. Re-raise anything that is NOT a shutdown so genuine
            # serving failures still surface.
            if not stopping.is_set():
                raise

    thread = threading.Thread(target=_serve, name="door-sync-webhook", daemon=True)
    thread.start()
    _logger.info("webhook listening on %s:%d", wcfg.host, wcfg.port)
    return WebhookServer(server, thread, stopping)
