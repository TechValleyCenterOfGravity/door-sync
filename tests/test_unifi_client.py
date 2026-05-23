"""Tests for the UniFi Access client."""

import hashlib
import json as _json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

from door_sync.config import UnifiConfig
from door_sync.models import Diff, ResolvedMember, UnifiUser
from door_sync.unifi.client import (
    UnifiClient,
    UnifiClientError,
    _compute_nfc_id,
    _parse_nfc_id,
    _redact,
    _split_name,
)

# --- Card-ID encoding helpers ---


def test_compute_nfc_id_known_values() -> None:
    """Verified encoding: (FC << 16) | CN as uppercase hex, no padding."""
    assert _compute_nfc_id(42, 1234) == "2A04D2"
    assert _compute_nfc_id(42, 1235) == "2A04D3"


def test_compute_nfc_id_zero_card_number() -> None:
    """CN=0 still produces FC-prefixed hex, not just '0'."""
    assert _compute_nfc_id(42, 0) == "2A0000"


def test_parse_nfc_id_matching_facility_code() -> None:
    """Inverse of _compute_nfc_id for the same FC."""
    assert _parse_nfc_id("2A04D2", 42) == 1234
    assert _parse_nfc_id("2A04D3", 42) == 1235


def test_parse_nfc_id_mismatched_facility_code_returns_none() -> None:
    """Foreign-FC cards are out of our namespace; signal via None."""
    assert _parse_nfc_id("2A04D2", 99) is None


def test_parse_nfc_id_garbage_returns_none() -> None:
    """Unparseable strings return None instead of raising."""
    assert _parse_nfc_id("not-hex", 42) is None
    assert _parse_nfc_id("", 42) is None


def test_parse_nfc_id_lowercase_hex_still_parses() -> None:
    """Defensive: don't trust UniFi to always uppercase the response."""
    assert _parse_nfc_id("2a04d3", 42) == 1235


# --- Name splitting ---


def test_split_name_two_words() -> None:
    assert _split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_three_words_splits_on_last_space() -> None:
    """A middle name or compound first name belongs with first_name."""
    assert _split_name("Mary Anne Doe") == ("Mary Anne", "Doe")


def test_split_name_single_word_pads_last_name() -> None:
    """UniFi requires both fields on create; em-dash flags it for review."""
    assert _split_name("Madonna") == ("Madonna", "—")


def test_split_name_empty_string_raises() -> None:
    """Empty display_name should never reach us; defensive."""
    with pytest.raises(ValueError):
        _split_name("")


# --- Card-ID redaction ---


def test_redact_none() -> None:
    assert _redact(None) == "none"


def test_redact_short_card_id_zero_pads() -> None:
    """Card 7 redacts to ****0007, not ****7."""
    assert _redact(7) == "****0007"


def test_redact_full_width_card_id() -> None:
    assert _redact(1234) == "****1234"


def test_redact_strips_high_digits() -> None:
    """Only the last 4 digits ever appear in logs."""
    assert _redact(98765) == "****8765"


# --- Construction / TLS ---


def _unifi_config(fingerprint: str = "AA" * 32) -> UnifiConfig:
    return UnifiConfig(
        host="192.0.2.1",
        api_key="testkey",
        tls_fingerprint=fingerprint,
        facility_code=42,
    )


def _patched_tls(cert_der: bytes) -> Any:
    """Context-manager that stubs socket+ssl to return cert_der as peer cert."""
    mock_ssock = MagicMock()
    mock_ssock.getpeercert.return_value = cert_der
    mock_ssock.__enter__.return_value = mock_ssock
    mock_ssock.__exit__.return_value = None

    mock_ctx = MagicMock()
    mock_ctx.wrap_socket.return_value = mock_ssock

    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    mock_sock.__exit__.return_value = None

    return patch.multiple(
        "door_sync.unifi.client",
        socket=MagicMock(create_connection=MagicMock(return_value=mock_sock)),
        ssl=MagicMock(SSLContext=MagicMock(return_value=mock_ctx), CERT_NONE=0, PROTOCOL_TLS_CLIENT=0),
    )


def test_init_raises_on_tls_fingerprint_mismatch() -> None:
    """Wrong fingerprint at init must raise before httpx.Client is built."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    wrong_fp = "BB" * 32
    assert real_fp != wrong_fp
    config = _unifi_config(fingerprint=wrong_fp)
    with _patched_tls(real_cert):
        with pytest.raises(UnifiClientError) as exc_info:
            UnifiClient(config)
        assert "TLS fingerprint mismatch" in str(exc_info.value)


def test_init_verifies_tls_fingerprint_match() -> None:
    """Matching fingerprint at init constructs the client successfully."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    with _patched_tls(real_cert):
        client = UnifiClient(config)
    assert client._http is not None
    client.close()


def test_init_accepts_colon_separated_fingerprint() -> None:
    """The fingerprint can be passed as AA:BB:CC:... (common format)."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    colon_form = ":".join(real_fp[i : i + 2] for i in range(0, len(real_fp), 2))
    config = _unifi_config(fingerprint=colon_form)
    with _patched_tls(real_cert):
        client = UnifiClient(config)
    client.close()


def test_context_manager_closes_http_client() -> None:
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    with _patched_tls(real_cert):
        with UnifiClient(config) as client:
            assert client._http.is_closed is False
    assert client._http.is_closed is True


# --- Response envelope + retries ---


def _make_client() -> UnifiClient:
    """Build a UnifiClient with TLS verification stubbed out."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        return UnifiClient(config)


def test_non_success_envelope_raises(httpx_mock: HTTPXMock) -> None:
    """code != SUCCESS raises UnifiClientError with the code + msg."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "CODE_AUTH_FAILED", "msg": "Authentication failed.", "data": None},
    )
    client = _make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "CODE_AUTH_FAILED" in str(exc_info.value)
    assert "Authentication failed." in str(exc_info.value)
    client.close()


def test_http_500_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive 500s exhaust retries and raise."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
            status_code=500,
            text="server error",
        )
    client = _make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 500" in str(exc_info.value)
    client.close()


def test_http_402_raises_immediately_no_retry(httpx_mock: HTTPXMock) -> None:
    """402 'Request Failed' is non-standard 4xx; no retries."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=402,
        text="request failed",
    )
    client = _make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 402" in str(exc_info.value)
    # Only one request should have been made.
    assert len(httpx_mock.get_requests()) == 1
    client.close()


def test_http_429_honors_retry_after_seconds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 with Retry-After: 5 waits >= 5 seconds, then 200 succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s)
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=429,
        headers={"Retry-After": "5"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "SUCCESS", "data": [], "msg": "success", "pagination": {"page_num": 1, "page_size": 100, "total": 0}},
    )
    client = _make_client()
    client.fetch_users()
    assert any(s >= 5 for s in sleeps)
    client.close()


def test_malformed_json_raises(httpx_mock: HTTPXMock) -> None:
    """200 with non-JSON body raises UnifiClientError."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        text="<html>not json</html>",
    )
    client = _make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "malformed JSON" in str(exc_info.value)
    client.close()


# --- fetch_users ---


def _user_row(
    contact_id: int = 42,
    user_id: str = "uuid-42",
    first_name: str = "Jane",
    last_name: str = "Doe",
    status: str = "ACTIVE",
    nfc_id: str = "2A04D2",
    policy_id: str = "pol-1",
    nfc_token: str = "tok-42",
) -> dict[str, Any]:
    return {
        "id": user_id,
        "first_name": first_name,
        "last_name": last_name,
        "employee_number": str(contact_id),
        "status": status,
        "nfc_cards": [{"id": "100001", "nfc_id": nfc_id, "token": nfc_token}],
        "access_policy_ids": [policy_id],
    }


def _users_page(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "msg": "success",
        "data": rows,
        "pagination": {
            "page_num": 1,
            "page_size": 100 if total is None else min(100, total),
            "total": len(rows) if total is None else total,
        },
    }


def test_fetch_users_happy_path(httpx_mock: HTTPXMock) -> None:
    """One page, returns list[UnifiUser] with parsed fields."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42)]),
    )
    client = _make_client()
    users = client.fetch_users()
    assert len(users) == 1
    u = users[0]
    assert u.contact_id == 42
    assert u.display_name == "Jane Doe"
    assert u.card_id == 1234  # 2A04D2 decoded with FC=42 -> CN=1234
    assert u.active is True
    assert u.policy == "pol-1"
    client.close()


def test_fetch_users_paginates(httpx_mock: HTTPXMock) -> None:
    """101 users across 2 pages; follows until short page."""
    page1 = [_user_row(contact_id=i, user_id=f"uuid-{i}") for i in range(1, 101)]
    page2 = [_user_row(contact_id=101, user_id="uuid-101")]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(page1, total=101),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=2&page_size=100&expand[]=access_policy",
        json=_users_page(page2, total=101),
    )
    client = _make_client()
    users = client.fetch_users()
    assert len(users) == 101
    assert {u.contact_id for u in users} == set(range(1, 102))
    client.close()


def test_fetch_users_skips_admin_without_employee_number(
    httpx_mock: HTTPXMock,
) -> None:
    rows = [
        _user_row(contact_id=42),
        {**_user_row(contact_id=0), "employee_number": ""},  # admin
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(rows),
    )
    client = _make_client()
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}
    client.close()


def test_fetch_users_skips_non_int_employee_number(
    httpx_mock: HTTPXMock,
) -> None:
    rows = [
        _user_row(contact_id=42),
        {**_user_row(contact_id=0), "employee_number": "bob"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(rows),
    )
    client = _make_client()
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}
    client.close()


def test_fetch_users_logs_warning_on_multiple_cards(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    row = _user_row(contact_id=42)
    row["nfc_cards"] = [
        {"id": "100001", "nfc_id": "2A04D2", "token": "tok-1"},
        {"id": "100002", "nfc_id": "2A04D3", "token": "tok-2"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = _make_client()
        users = client.fetch_users()
    assert users[0].card_id == 1234  # uses the first card
    assert any("2 cards" in rec.message for rec in caplog.records)
    client.close()


def test_fetch_users_foreign_fc_card_yields_card_id_none(
    httpx_mock: HTTPXMock,
) -> None:
    """A card with a non-configured facility code -> card_id=None on the user."""
    row = _user_row(contact_id=42, nfc_id="990000")  # FC=99, not 42
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = _make_client()
    users = client.fetch_users()
    assert users[0].card_id is None
    client.close()


# --- apply preconditions & dry-run ---


def _diff(
    to_add: list[ResolvedMember] | None = None,
    to_update_credential: list[tuple[ResolvedMember, UnifiUser]] | None = None,
    to_update_policy: list[tuple[ResolvedMember, UnifiUser]] | None = None,
    to_deactivate: list[UnifiUser] | None = None,
    unmapped: list[ResolvedMember] | None = None,
) -> Diff:
    return Diff(
        to_add=to_add or [],
        to_update_credential=to_update_credential or [],
        to_update_policy=to_update_policy or [],
        to_deactivate=to_deactivate or [],
        unmapped=unmapped or [],
    )


def _resolved(
    contact_id: int,
    card_id: int | None = 1234,
    target_policy: str = "pol-1",
) -> ResolvedMember:
    return ResolvedMember(
        contact_id=contact_id,
        display_name=f"Member {contact_id}",
        card_id=card_id,
        target_policy=target_policy,
        resolution="tier",
    )


def _unifi_user(
    contact_id: int,
    card_id: int | None = 1234,
    active: bool = True,
    policy: str | None = "pol-1",
) -> UnifiUser:
    return UnifiUser(
        contact_id=contact_id,
        display_name=f"Member {contact_id}",
        card_id=card_id,
        active=active,
        policy=policy,
    )


def test_apply_requires_prior_fetch_users(httpx_mock: HTTPXMock) -> None:
    """Calling apply() before fetch_users() must raise."""
    client = _make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.apply(_diff(to_deactivate=[_unifi_user(99)]))
    assert "fetch_users" in str(exc_info.value)
    client.close()


def test_apply_dry_run_makes_no_writes(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-empty diff in dry-run logs intentions but issues zero httpx writes."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config, dry_run=True)

    # Seed the precondition: a fetch_users that returns empty.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    diff = _diff(
        to_add=[_resolved(1)],
        to_deactivate=[_unifi_user(2)],
    )
    with caplog.at_level(logging.INFO, logger="door_sync.unifi.client"):
        client.apply(diff)

    # Only the one fetch_users GET should have been issued — no writes.
    assert len(httpx_mock.get_requests()) == 1
    # Two log lines: would-add and would-deactivate.
    messages = [r.message for r in caplog.records]
    assert any("would-add" in m for m in messages)
    assert any("would-deactivate" in m for m in messages)
    # Card IDs are redacted.
    assert any("****1234" in m for m in messages)
    assert not any("1234 " in m and "****" not in m for m in messages)
    client.close()


# --- NFC token map ---


def _cards_page(
    rows: list[dict[str, Any]], total: int | None = None
) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "msg": "success",
        "data": rows,
        "pagination": {
            "page_num": 1,
            "page_size": 100,
            "total": len(rows) if total is None else total,
        },
    }


def test_token_map_keys_by_parsed_card_id(httpx_mock: HTTPXMock) -> None:
    """Build dict[card_id → token]; foreign-FC and unparseable rows are skipped."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([
            {"nfc_id": "2A04D2", "token": "tok-1234", "display_id": "100001"},
            {"nfc_id": "2A04D3", "token": "tok-1235", "display_id": "100002"},
            {"nfc_id": "990000", "token": "tok-foreign", "display_id": "100003"},
            {"nfc_id": "not-hex", "token": "tok-bad", "display_id": "100004"},
        ]),
    )
    token_map = client._ensure_nfc_token_map()
    assert token_map == {1234: "tok-1234", 1235: "tok-1235"}
    client.close()


def test_token_map_cached_across_calls(httpx_mock: HTTPXMock) -> None:
    """Second call doesn't re-fetch."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    first = client._ensure_nfc_token_map()
    second = client._ensure_nfc_token_map()
    assert first is second
    assert len(httpx_mock.get_requests()) == 1
    client.close()


# --- Card import ---


def test_import_cards_uses_2col_csv_format(httpx_mock: HTTPXMock) -> None:
    """Multipart body contains <nfc_id>,sync-<padded> lines, no header."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # First, an empty token-map fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    # Then the import.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [
                {"alias": "sync-01234", "nfc_id": "2A04D2", "token": "tok-1234"},
                {"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"},
            ],
        },
    )
    client._import_cards([1234, 1235])

    # Inspect the second request — the multipart body must contain our CSV.
    import_req = httpx_mock.get_requests()[1]
    body = import_req.content.decode("utf-8", errors="replace")
    assert "2A04D2,sync-01234" in body
    assert "2A04D3,sync-01235" in body
    # No header row.
    assert "nfc_id,alias" not in body
    # Token map updated.
    assert client._nfc_token_map == {1234: "tok-1234", 1235: "tok-1235"}
    client.close()


def test_import_cards_empty_token_raises(httpx_mock: HTTPXMock) -> None:
    """A row with empty token in the response signals a failed import."""
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01234", "nfc_id": "2A04D2", "token": ""}],
        },
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._import_cards([1234])
    assert "card_id=****1234" in str(exc_info.value)
    client.close()


def test_import_cards_empty_list_is_noop(httpx_mock: HTTPXMock) -> None:
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)
    client._import_cards([])
    assert len(httpx_mock.get_requests()) == 0
    client.close()


# --- apply: live writes ---


def test_apply_deactivate_sets_status(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_deactivate → PUT /users/:id with status=DEACTIVATED."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Prime the cache via fetch_users.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42")]),
    )
    fetched = client.fetch_users()

    # The deactivate write.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    client.apply(_diff(to_deactivate=[fetched[0]]))

    write_req = httpx_mock.get_requests()[-1]
    body = _json.loads(write_req.content)
    assert body == {"status": "DEACTIVATED"}
    client.close()


def test_apply_update_credential_swaps_card(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """to_update_credential with changed card_id: DELETE old, PUT new."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    # Fetch returns user 42 with old card_id=1234 (nfc_id=2A04D2, token=tok-1234).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42", nfc_id="2A04D2", nfc_token="tok-1234",
        )]),
    )
    fetched = client.fetch_users()

    # Token map fetch (the new card is not yet in the map).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([
            {"nfc_id": "2A04D2", "token": "tok-1234"},
        ]),
    )
    # Import for the new card 1235.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"}],
        },
    )
    # PUT name update (_resolved gives "Member 42"; fetched user is "Jane Doe").
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # DELETE old card.
    httpx_mock.add_response(
        method="DELETE",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards/delete",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT new card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1235)
    diff = _diff(to_update_credential=[(resolved, fetched[0])])
    client.apply(diff)

    # Verify the DELETE body referenced the OLD token.
    delete_req = next(
        r for r in httpx_mock.get_requests()
        if r.method == "DELETE" and r.url.path.endswith("/nfc_cards/delete")
    )
    assert _json.loads(delete_req.content) == {"token": "tok-1234"}
    # And the PUT body referenced the NEW token.
    bind_req = next(
        r for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path.endswith("/nfc_cards")
    )
    assert _json.loads(bind_req.content) == {"token": "tok-1235", "force_add": False}
    client.close()


def test_apply_update_credential_name_only(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """display_name changes but card_id doesn't: only PUT name, no card calls."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = _unifi_config(fingerprint=fp)
    with _patched_tls(cert):
        client = UnifiClient(config)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(
            contact_id=42, user_id="uuid-42",
            first_name="Old", last_name="Name", nfc_id="2A04D2",
        )]),
    )
    fetched = client.fetch_users()

    # Token-map fetch (matching card, no import needed).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"nfc_id": "2A04D2", "token": "tok-1234"}]),
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="New Name",
        card_id=1234,  # same as fetched
        target_policy="pol-1",
        resolution="tier",
    )
    diff = _diff(to_update_credential=[(resolved, fetched[0])])
    client.apply(diff)

    put_req = httpx_mock.get_requests()[-1]
    body = _json.loads(put_req.content)
    assert body == {"first_name": "New", "last_name": "Name"}
    # The token-map fetch IS to /credentials/nfc_cards/tokens — that counts.
    # But there should be NO calls to /users/uuid-42/nfc_cards or /import.
    user_nfc_calls = [
        r for r in httpx_mock.get_requests()
        if "/users/uuid-42/nfc_cards" in str(r.url)
        or "/nfc_cards/import" in str(r.url)
    ]
    assert user_nfc_calls == []
    client.close()
