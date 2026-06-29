"""Tests for the UniFi Access client."""

import hashlib
import json as _json
import logging
import ssl
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from door_sync.config import UnifiConfig, load
from door_sync.models import Diff, ResolvedMember, UnifiUser
from door_sync.unifi.client import (
    UnifiClient,
    UnifiClientError,
    _compute_nfc_id,
    _parse_nfc_id,
    _parse_sync_alias,
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


# --- Sync alias parsing ---


def test_parse_sync_alias_recovers_card_id() -> None:
    """door-sync imports cards as sync-<padded card_id>; recover the card_id."""
    assert _parse_sync_alias("sync-01234") == 1234
    assert _parse_sync_alias("sync-00007") == 7


def test_parse_sync_alias_non_sync_returns_none() -> None:
    """Cards not provisioned by door-sync (no 'sync-' alias) are unrecognized."""
    assert _parse_sync_alias("") is None
    assert _parse_sync_alias("Front Desk Card") is None
    assert _parse_sync_alias("sync-") is None
    assert _parse_sync_alias("sync-abc") is None


def test_parse_sync_alias_rejects_non_digit_suffix() -> None:
    """Only plain ASCII digits are a valid card number — int() would otherwise
    accept signs, whitespace, and unicode digits."""
    assert _parse_sync_alias("sync--5") is None  # signed
    assert _parse_sync_alias("sync-+5") is None
    assert _parse_sync_alias("sync- 12") is None  # whitespace-padded
    assert _parse_sync_alias("sync-1_234") is None  # underscore digit grouping
    # Non-ASCII digits (str.isdigit() is True for these) must be rejected by the
    # isascii() guard: a superscript and a regular non-ASCII decimal digit.
    assert _parse_sync_alias("sync-²") is None  # U+00B2 superscript two
    assert _parse_sync_alias("sync-১") is None  # U+09E7 Bengali digit one


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
        host="https://192.0.2.1:12445",
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
        ssl=MagicMock(
            SSLContext=MagicMock(return_value=mock_ctx),
            # Use the real ssl constants so the stub matches production values
            # (e.g. PROTOCOL_TLS_CLIENT is 2, not 0) rather than magic numbers.
            CERT_NONE=ssl.CERT_NONE,
            PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
            TLSVersion=ssl.TLSVersion,
        ),
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


def test_init_verifies_tls_fingerprint_match(make_client: Callable[..., UnifiClient]) -> None:
    """Matching fingerprint at init constructs the client successfully."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    client = make_client(config=config, cert=real_cert)
    assert client._http is not None


def test_init_accepts_colon_separated_fingerprint(make_client: Callable[..., UnifiClient]) -> None:
    """The fingerprint can be passed as AA:BB:CC:... (common format)."""
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    colon_form = ":".join(real_fp[i : i + 2] for i in range(0, len(real_fp), 2))
    config = _unifi_config(fingerprint=colon_form)
    make_client(config=config, cert=real_cert)


def test_context_manager_closes_http_client() -> None:
    real_cert = b"fake-cert-bytes"
    real_fp = hashlib.sha256(real_cert).hexdigest()
    config = _unifi_config(fingerprint=real_fp)
    with _patched_tls(real_cert):
        with UnifiClient(config) as client:
            assert not client._http.is_closed
    assert client._http.is_closed


# --- Response envelope + retries ---


@pytest.fixture
def make_client() -> Iterator[Callable[..., UnifiClient]]:
    """Build UnifiClients with TLS stubbed; close them all at teardown.

    Returns a factory. Every client it creates is closed after the test,
    even when an assertion raises mid-test, so the underlying httpx.Client
    never leaks.

    Args (all optional):
        config: a bespoke UnifiConfig. When omitted, a default config is
            built and pinned to ``cert``.
        dry_run: construct the client in dry-run mode.
        cert: DER certificate the stubbed TLS layer presents during
            fingerprint verification. The client validates it against
            ``config.tls_fingerprint`` — so when you pass both ``config=``
            and ``cert=``, the config's fingerprint must already match
            ``cert`` or construction raises a TLS mismatch. (Passing
            ``cert=`` alone is enough; the auto-built config is pinned to
            it.)
    """
    created: list[UnifiClient] = []

    def _factory(
        config: UnifiConfig | None = None,
        *,
        dry_run: bool = False,
        cert: bytes = b"fake-cert",
        managed_policy_ids: set[str] | None = None,
    ) -> UnifiClient:
        if config is None:
            fp = hashlib.sha256(cert).hexdigest()
            config = _unifi_config(fingerprint=fp)
        with _patched_tls(cert):
            client = UnifiClient(config, dry_run=dry_run, managed_policy_ids=managed_policy_ids)
        created.append(client)
        return client

    yield _factory
    for client in created:
        client.close()


def test_non_success_envelope_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """code != SUCCESS raises UnifiClientError with the code + msg."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "CODE_AUTH_FAILED", "msg": "Authentication failed.", "data": None},
    )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "CODE_AUTH_FAILED" in str(exc_info.value)
    assert "Authentication failed." in str(exc_info.value)


def test_http_500_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
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
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 500" in str(exc_info.value)


def test_http_402_raises_immediately_no_retry(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """402 'Request Failed' is non-standard 4xx; no retries."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=402,
        text="request failed",
    )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 402" in str(exc_info.value)
    # Only one request should have been made.
    assert len(httpx_mock.get_requests()) == 1


def test_http_429_honors_retry_after_seconds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """429 with Retry-After: 5 waits >= 5 seconds, then 200 succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s))
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        status_code=429,
        headers={"Retry-After": "5"},
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={
            "code": "SUCCESS",
            "data": [],
            "msg": "success",
            "pagination": {"page_num": 1, "page_size": 100, "total": 0},
        },
    )
    client = make_client()
    client.fetch_users()
    assert any(s >= 5 for s in sleeps)


def test_malformed_json_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """200 with non-JSON body raises UnifiClientError."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        text="<html>not json</html>",
    )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "malformed JSON" in str(exc_info.value)


# --- fetch_users ---


def _user_row(
    contact_id: int = 42,
    user_id: str = "uuid-42",
    first_name: str = "Jane",
    last_name: str = "Doe",
    status: str = "ACTIVE",
    policy_id: str = "pol-1",
    nfc_token: str | None = None,
    display_id: str = "100001",
    user_email: str | None = None,
) -> dict[str, Any]:
    """Build a /users row matching the real API shape.

    A card is included only when ``nfc_token`` is given; the card object carries
    ``id``/``token``/``type`` (the real fields — there is no ``nfc_id`` here).
    Card numbers are resolved from the token via the card list (see
    ``_card_tokens_page``), so a carded row needs a matching /tokens mock.
    """
    nfc_cards = (
        [{"id": display_id, "token": nfc_token, "type": "id_card"}] if nfc_token is not None else []
    )
    row: dict[str, Any] = {
        "id": user_id,
        "first_name": first_name,
        "last_name": last_name,
        "employee_number": str(contact_id),
        "status": status,
        "nfc_cards": nfc_cards,
        "access_policy_ids": [policy_id],
    }
    if user_email is not None:
        row["user_email"] = user_email
    return row


def _card_tokens_page(cards: list[tuple[int, str]], total: int | None = None) -> dict[str, Any]:
    """Build a /credentials/nfc_cards/tokens page from (card_id, token) pairs.

    Each card is rendered with the door-sync alias ``sync-<card_id>`` and a
    ``display_id`` — matching the real card-list shape (no ``nfc_id``)."""
    rows = [
        {
            "display_id": f"10{card_id:04d}",
            "alias": f"sync-{card_id:05d}",
            "token": token,
            "card_type": "id_card",
            "status": "assigned",
        }
        for card_id, token in cards
    ]
    return _cards_page(rows, total=total)


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


def test_fetch_users_happy_path(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """One page, returns list[UnifiUser] with parsed fields."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, nfc_token="tok-42")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-42")]),
    )
    client = make_client()
    users = client.fetch_users()
    assert len(users) == 1
    u = users[0]
    assert u.contact_id == 42
    assert u.display_name == "Jane Doe"
    assert u.card_id == 1234  # resolved via token "tok-42" -> alias "sync-01234"
    assert u.active is True
    assert u.policy == "pol-1"


def test_fetch_users_paginates(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
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
    client = make_client()
    users = client.fetch_users()
    assert len(users) == 101
    assert {u.contact_id for u in users} == set(range(1, 102))


def test_fetch_users_skips_admin_without_employee_number(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
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
    client = make_client()
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}


def test_fetch_users_skips_non_int_employee_number(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
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
    client = make_client()
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}


def test_fetch_users_skips_non_positive_employee_number(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """employee_number "0" or negative is not a CiviCRM contact_id (auto-increment
    starts at 1). Skipping prevents such a user from being silently deactivated
    on the next cycle: with no matching ResolvedMember, the reconciler's diff
    would put them in to_deactivate.
    """
    rows = [
        _user_row(contact_id=42),
        {**_user_row(contact_id=42), "id": "uuid-zero", "employee_number": "0"},
        {**_user_row(contact_id=42), "id": "uuid-neg", "employee_number": "-5"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(rows),
    )
    client = make_client()
    users = client.fetch_users()
    assert {u.contact_id for u in users} == {42}
    # And the caches must not have been populated with the bad contact_ids.
    assert 0 not in client._unifi_user_id_by_contact
    assert -5 not in client._unifi_user_id_by_contact


def test_fetch_users_logs_warning_on_multiple_cards(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    row = _user_row(contact_id=42)
    row["nfc_cards"] = [
        {"id": "100001", "token": "tok-1", "type": "id_card"},
        {"id": "100002", "token": "tok-2", "type": "id_card"},
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1"), (1235, "tok-2")]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = make_client()
        users = client.fetch_users()
    assert users[0].card_id == 1234  # uses the first card (token "tok-1")
    assert any("2 cards" in rec.message for rec in caplog.records)


def test_fetch_users_filters_unmanaged_policy(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """A globally auto-applied policy in access_policy_ids is ignored: u.policy is
    the configured (managed) tier policy, regardless of array order."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-global", "pol-1"]  # global sorts first
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = make_client(managed_policy_ids={"pol-1"})
    users = client.fetch_users()
    assert users[0].policy == "pol-1"


def test_fetch_users_no_warning_when_one_managed_policy_plus_global(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    """global + one managed policy is unambiguous; no 'access policies' warning."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-global", "pol-1"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = make_client(managed_policy_ids={"pol-1"})
        client.fetch_users()
    assert not any("access policies" in rec.message for rec in caplog.records)


def test_fetch_users_warns_on_multiple_managed_policies(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    """Two *managed* policies on one user is genuinely ambiguous -> warn, use first."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-1", "pol-2"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = make_client(managed_policy_ids={"pol-1", "pol-2"})
        users = client.fetch_users()
    # "use the first" — array order is ["pol-1", "pol-2"], so pol-1 wins.
    assert users[0].policy == "pol-1"
    assert any("2 access policies" in rec.message for rec in caplog.records)


def test_fetch_users_unmanaged_only_yields_policy_none(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """A user holding only the global policy (no tier policy) reads as policy=None."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-global"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = make_client(managed_policy_ids={"pol-1"})
    users = client.fetch_users()
    assert users[0].policy is None


def test_fetch_users_explicit_empty_managed_set_owns_no_policy(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """An explicit empty managed set means door-sync owns no policies, so every
    policy is external and the user's policy reads as None. This is distinct from
    omitting the argument (None), which keeps the legacy take-first behavior."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-1"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = make_client(managed_policy_ids=set())
    users = client.fetch_users()
    assert users[0].policy is None


def test_fetch_users_omitted_managed_set_uses_legacy_first_policy(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Omitting managed_policy_ids (None) keeps the legacy behavior: the first
    access policy is taken as the user's policy."""
    row = _user_row(contact_id=42)
    row["access_policy_ids"] = ["pol-1", "pol-2"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    client = make_client()  # managed_policy_ids omitted -> None
    users = client.fetch_users()
    assert users[0].policy == "pol-1"


def test_fetch_users_resolves_card_id_from_token_via_alias(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """The /users endpoint returns a card's token (not its Wiegand nfc_id), so
    card_id is recovered by joining that token to the card list, whose alias
    encodes the number (sync-<card_id>). Mirrors the real /users card shape
    ({id, token, type}) with synthetic values."""
    token = "tok-card-1234-synthetic"  # opaque; only the token->alias join matters
    row = {
        "id": "uuid-synthetic-0001",
        "first_name": "Jane",
        "last_name": "Doe",
        "employee_number": "42",
        "status": "ACTIVE",
        "nfc_cards": [{"id": "100025", "token": token, "type": "id_card"}],
        "access_policy_ids": [],
    }
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page(
            [
                {
                    "display_id": "100025",
                    "alias": "sync-01234",
                    "token": token,
                    "card_type": "id_card",
                    "status": "assigned",
                }
            ]
        ),
    )
    client = make_client()
    users = client.fetch_users()
    assert users[0].contact_id == 42
    assert users[0].card_id == 1234  # recovered via token -> alias "sync-01234"


def test_fetch_users_unrecognized_card_token_yields_card_id_none(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """A card whose token isn't in the door-sync-managed card list (no sync-
    alias) resolves to card_id=None rather than a wrong number."""
    row = {
        "id": "uuid-1",
        "first_name": "Jane",
        "last_name": "Doe",
        "employee_number": "1",
        "status": "ACTIVE",
        "nfc_cards": [{"id": "100099", "token": "tok-unknown", "type": "id_card"}],
        "access_policy_ids": [],
    }
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([{"display_id": "1", "alias": "Front Desk", "token": "tok-other"}]),
    )
    client = make_client()
    users = client.fetch_users()
    assert users[0].card_id is None


def test_fetch_users_parses_user_email(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_email="jane@example.com")]),
    )
    client = make_client()
    users = client.fetch_users()
    assert users[0].email == "jane@example.com"


def test_fetch_users_missing_user_email_is_none(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42)]),  # no user_email key
    )
    client = make_client()
    users = client.fetch_users()
    assert users[0].email is None


# --- apply preconditions & dry-run ---


def _diff(
    to_add: tuple[ResolvedMember, ...] = (),
    to_update_credential: tuple[tuple[ResolvedMember, UnifiUser], ...] = (),
    to_update_policy: tuple[tuple[ResolvedMember, UnifiUser], ...] = (),
    to_deactivate: tuple[UnifiUser, ...] = (),
    unmapped: tuple[ResolvedMember, ...] = (),
) -> Diff:
    return Diff(
        to_add=to_add,
        to_update_credential=to_update_credential,
        to_update_policy=to_update_policy,
        to_deactivate=to_deactivate,
        unmapped=unmapped,
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


def test_apply_requires_prior_fetch_users(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Calling apply() before fetch_users() must raise."""
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.apply(_diff(to_deactivate=(_unifi_user(99),)))
    assert "fetch_users" in str(exc_info.value)


def test_apply_dry_run_makes_no_writes(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    """Non-empty diff in dry-run logs intentions but issues zero httpx writes."""
    client = make_client(dry_run=True)

    # Seed the precondition: a fetch_users that returns empty.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    # In dry-run the token-map read path is exercised when the diff has cards
    # (spec §8). Register the response so httpx_mock doesn't error.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )

    diff = _diff(
        to_add=(_resolved(1),),
        to_deactivate=(_unifi_user(2),),
    )
    with caplog.at_level(logging.INFO, logger="door_sync.unifi.client"):
        client.apply(diff)

    # fetch_users GET + token-map GET; NO writes (PUT/POST/DELETE).
    all_requests = httpx_mock.get_requests()
    write_requests = [r for r in all_requests if r.method in ("PUT", "POST", "DELETE")]
    assert write_requests == []
    # Two log lines: would-add and would-deactivate.
    messages = [r.message for r in caplog.records]
    assert any("would-add" in m for m in messages)
    assert any("would-deactivate" in m for m in messages)
    # Card IDs are redacted: "1234" must never appear in a message unless
    # the redacted "****1234" form also appears in that same message.
    assert any("****1234" in m for m in messages)
    assert not any("1234" in m and "****1234" not in m for m in messages)


# --- NFC token map ---


def _cards_page(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
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


def test_token_map_keys_by_parsed_card_id(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Build dict[card_id → token] from the sync- alias; cards not provisioned
    by door-sync (no sync- alias) are skipped. Reverse map is built too."""
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page(
            [
                {"alias": "sync-01234", "token": "tok-1234", "display_id": "100001"},
                {"alias": "sync-01235", "token": "tok-1235", "display_id": "100002"},
                {"alias": "Front Desk", "token": "tok-manual", "display_id": "100003"},
                {"alias": "", "token": "tok-noalias", "display_id": "100004"},
            ]
        ),
    )
    token_map = client._ensure_nfc_token_map()
    assert token_map == {1234: "tok-1234", 1235: "tok-1235"}
    assert client._card_id_by_token == {"tok-1234": 1234, "tok-1235": 1235}


def test_token_map_cached_across_calls(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Second call doesn't re-fetch."""
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    first = client._ensure_nfc_token_map()
    second = client._ensure_nfc_token_map()
    assert first is second
    assert len(httpx_mock.get_requests()) == 1


# --- Card import ---


def test_import_cards_uses_2col_csv_format(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Multipart body contains <nfc_id>,sync-<padded> lines, no header."""
    client = make_client()

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


def test_import_cards_empty_token_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """A row with empty token in the response signals a failed import."""
    client = make_client()

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


def test_import_cards_empty_list_is_noop(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    client = make_client()
    client._import_cards([])
    assert len(httpx_mock.get_requests()) == 0


def test_import_cards_fc_mismatch_in_response_does_not_leak_card_number(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """If the import response contains an nfc_id whose FC doesn't match our
    config, the raised error mentions only the FC bytes — not the raw nfc_id
    (which encodes the card number, architecture §11)."""
    client = make_client()
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
            "data": [{"alias": "x", "nfc_id": "5904D2", "token": "tok"}],
        },
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._import_cards([1234])
    message = str(exc_info.value)
    # FC bytes are operational, not credential material — present.
    # facility_code 42 (from the test config) must be named as the expected FC.
    assert "got FC 89" in message
    assert "expected 42" in message
    # The raw nfc_id and the card-number portion must NOT appear.
    assert "5904D2" not in message
    assert "1234" not in message
    assert "04D2" not in message


def test_import_cards_unparseable_nfc_id_does_not_leak_string(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """If the import response contains a non-hex nfc_id, the error says so
    structurally — the raw string is not included."""
    client = make_client()
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
            "data": [{"alias": "x", "nfc_id": "garbage-not-hex", "token": "tok"}],
        },
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._import_cards([1234])
    message = str(exc_info.value)
    assert "not valid hex" in message
    # Raw string must not appear.
    assert "garbage-not-hex" not in message


# --- apply: live writes ---


def test_apply_deactivate_sets_status(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_deactivate → PUT /users/:id with status=DEACTIVATED."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    client = make_client()

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
    client.apply(_diff(to_deactivate=(fetched[0],)))

    write_req = httpx_mock.get_requests()[-1]
    body = _json.loads(write_req.content)
    assert body == {"status": "DEACTIVATED"}


def test_apply_update_credential_swaps_card(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_update_credential with changed card_id: DELETE old, PUT new."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)

    client = make_client()

    # Token map fetch (old card 1234 known; the new card 1235 is not yet in the
    # map). Registered before fetch_users() because fetch resolves card numbers
    # from tokens via the card list.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    # Fetch returns user 42 with old card_id=1234 (token=tok-1234).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()

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
    diff = _diff(to_update_credential=((resolved, fetched[0]),))
    client.apply(diff)

    # Verify the DELETE body referenced the OLD token.
    delete_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "DELETE" and r.url.path.endswith("/nfc_cards/delete")
    )
    assert _json.loads(delete_req.content) == {"token": "tok-1234"}
    # And the PUT body referenced the NEW token.
    bind_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path.endswith("/nfc_cards")
    )
    assert _json.loads(bind_req.content) == {"token": "tok-1235", "force_add": False}


def test_apply_update_credential_name_only(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """display_name changes but card_id doesn't: only PUT name, no card calls."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map fetch (matching card, no import needed). Registered before
    # fetch_users() because fetch resolves card numbers from tokens.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    first_name="Old",
                    last_name="Name",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()

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
    diff = _diff(to_update_credential=((resolved, fetched[0]),))
    client.apply(diff)

    put_req = httpx_mock.get_requests()[-1]
    body = _json.loads(put_req.content)
    assert body == {"first_name": "New", "last_name": "Name"}
    # The token-map fetch IS to /credentials/nfc_cards/tokens — that counts.
    # But there should be NO calls to /users/uuid-42/nfc_cards or /import.
    user_nfc_calls = [
        r
        for r in httpx_mock.get_requests()
        if "/users/uuid-42/nfc_cards" in str(r.url) or "/nfc_cards/import" in str(r.url)
    ]
    assert user_nfc_calls == []


def test_apply_update_policy_replaces(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42", policy_id="pol-old")]),
    )
    fetched = client.fetch_users()

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, target_policy="pol-new")
    diff = _diff(to_update_policy=((resolved, fetched[0]),))
    client.apply(diff)

    put_req = httpx_mock.get_requests()[-1]
    assert _json.loads(put_req.content) == {"access_policy_ids": ["pol-new"]}


def test_apply_update_policy_sends_only_tier_policy(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """A policy update sends ONLY the tier policy, never the auto-applied global
    one: re-sending the global ID would convert it into a manual per-user
    assignment. The global policy auto-applies on its own."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client(managed_policy_ids={"pol-old", "pol-new"})

    row = _user_row(contact_id=42, user_id="uuid-42")
    row["access_policy_ids"] = ["pol-global", "pol-old"]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    fetched = client.fetch_users()
    assert fetched[0].policy == "pol-old"  # managed policy, not the global one

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, target_policy="pol-new")
    client.apply(_diff(to_update_policy=((resolved, fetched[0]),)))

    body = _json.loads(httpx_mock.get_requests()[-1].content)
    assert body == {"access_policy_ids": ["pol-new"]}  # global NOT included


def test_apply_create_new_user_path(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_add for unknown contact_id: POST /users, then bind card + assign policy."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Empty initial fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    # Token-map fetch.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    # Import new card.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01234", "nfc_id": "2A04D2", "token": "tok-1234"}],
        },
    )
    # POST /users → returns the new user_id.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": {"id": "uuid-new", "first_name": "Jane", "last_name": "Doe"},
        },
    )
    # PUT bind card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
    )
    client.apply(_diff(to_add=(resolved,)))

    post_user = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {"first_name": "Jane", "last_name": "Doe", "employee_number": "42"}


def test_apply_reactivate_inactive_user_path(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_add for cached-inactive contact: prep, bind, assign, then activate."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map fetch (card already known). Registered before fetch_users()
    # because fetch resolves card numbers from tokens.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    # Fetch returns user 42 inactive with the same card.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    status="DEACTIVATED",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    client.fetch_users()

    # PUT profile (still deactivated), then PUT activate.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT bind card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1234)
    client.apply(_diff(to_add=(resolved,)))

    # No DELETE calls.
    delete_calls = [r for r in httpx_mock.get_requests() if r.method == "DELETE"]
    assert delete_calls == []

    # First PUT is profile-only (no status), final PUT is activation-only.
    user_puts = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    assert len(user_puts) == 2
    assert "status" not in _json.loads(user_puts[0].content)
    assert _json.loads(user_puts[1].content) == {"status": "ACTIVE"}


def test_apply_reactivate_clears_stale_email_when_none(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """Reactivating a member whose CiviCRM email was removed clears the stale
    UniFi email in the same cycle (profile PUT sends user_email="")."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map fetch, registered before fetch_users() (fetch resolves card
    # numbers from tokens).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    # Fetch returns user 42 inactive, same card, but carrying a stale email.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    status="DEACTIVATED",
                    nfc_token="tok-1234",
                    user_email="stale@example.com",
                )
            ]
        ),
    )
    client.fetch_users()

    # Profile PUT and activate PUT both target /users/uuid-42.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1234)  # email defaults to None
    client.apply(_diff(to_add=(resolved,)))

    user_puts = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    profile_put_body = _json.loads(
        next(r for r in user_puts if "employee_number" in _json.loads(r.content)).content
    )
    assert profile_put_body["user_email"] == ""


def test_apply_reactivate_swaps_card_when_changed(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_add for cached-inactive with different card_id: prep, delete old, bind, assign, activate."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map: old card known, new one not. Registered before fetch_users()
    # (fetch resolves card numbers from tokens).
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    # Fetch: inactive user with OLD card_id=1234.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    status="DEACTIVATED",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    client.fetch_users()

    # Import for new card 1235.
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={
            "code": "SUCCESS",
            "msg": "success",
            "data": [{"alias": "sync-01235", "nfc_id": "2A04D3", "token": "tok-1235"}],
        },
    )
    # PUT profile (still deactivated), then PUT activate.
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
    # PUT bind new.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = _resolved(contact_id=42, card_id=1235)
    client.apply(_diff(to_add=(resolved,)))

    # Confirm sequence: PUT user profile → DELETE old → PUT new card → PUT policy → PUT activate.
    methods_paths = [
        (r.method, r.url.path)
        for r in httpx_mock.get_requests()
        if r.url.path.startswith("/api/v1/developer/users/uuid-42")
    ]
    assert methods_paths == [
        ("PUT", "/api/v1/developer/users/uuid-42"),
        ("DELETE", "/api/v1/developer/users/uuid-42/nfc_cards/delete"),
        ("PUT", "/api/v1/developer/users/uuid-42/nfc_cards"),
        ("PUT", "/api/v1/developer/users/uuid-42/access_policies"),
        ("PUT", "/api/v1/developer/users/uuid-42"),
    ]

    # First PUT is profile-only (no status), final PUT is activation-only.
    user_puts = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    assert len(user_puts) == 2
    assert "status" not in _json.loads(user_puts[0].content)
    assert _json.loads(user_puts[1].content) == {"status": "ACTIVE"}


def test_apply_executes_deactivate_update_credential_update_policy_add_order(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """Single diff with one entry in each bucket; assert HTTPX call sequence."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # 3 users in fetch: 100 (deactivate), 101 (update_credential), 102 (update_policy).
    # Users 101 and 102 have names matching what _resolved() produces ("Member N")
    # so no name-PUT fires — only the card swap for 101 and policy update for 102.
    # Token-map fetch, registered before fetch_users() (fetch resolves card
    # numbers from tokens). Includes card 1238 so no import is needed for the
    # 101 swap; 100/101/102 hold cards 1234/1235/1236.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "t100"), (1235, "t101"), (1236, "t102"), (1238, "t1238")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(contact_id=100, user_id="u100", nfc_token="t100"),
                _user_row(
                    contact_id=101,
                    user_id="u101",
                    first_name="Member",
                    last_name="101",
                    nfc_token="t101",
                ),
                _user_row(
                    contact_id=102,
                    user_id="u102",
                    first_name="Member",
                    last_name="102",
                    nfc_token="t102",
                    policy_id="old",
                ),
            ]
        ),
    )
    fetched = client.fetch_users()
    by_id = {u.contact_id: u for u in fetched}

    # Pre-set generic SUCCESS responses for the writes.
    for url, method in [
        ("https://192.0.2.1:12445/api/v1/developer/users/u100", "PUT"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u101/nfc_cards/delete", "DELETE"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u101/nfc_cards", "PUT"),
        ("https://192.0.2.1:12445/api/v1/developer/users/u102/access_policies", "PUT"),
    ]:
        httpx_mock.add_response(
            method=method,
            url=url,
            json={"code": "SUCCESS", "msg": "success", "data": None},
        )

    diff = _diff(
        to_deactivate=(by_id[100],),
        to_update_credential=((_resolved(101, card_id=1238), by_id[101]),),
        to_update_policy=((_resolved(102, target_policy="new"), by_id[102]),),
    )
    client.apply(diff)

    write_path_methods = [
        (r.method, r.url.path)
        for r in httpx_mock.get_requests()
        if r.method in ("PUT", "POST", "DELETE")
        and "/credentials/nfc_cards/import" not in r.url.path
    ]
    # Expected order: deactivate(100), update_credential(101 DELETE then PUT card),
    # update_policy(102).
    assert write_path_methods == [
        ("PUT", "/api/v1/developer/users/u100"),
        ("DELETE", "/api/v1/developer/users/u101/nfc_cards/delete"),
        ("PUT", "/api/v1/developer/users/u101/nfc_cards"),
        ("PUT", "/api/v1/developer/users/u102/access_policies"),
    ]


def test_fetch_users_warning_does_not_leak_card_id(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    """The multi-card warning must not log full card_ids (architecture §11)."""
    row = _user_row(contact_id=42)
    # Two cards; both resolve to card_ids under the sync- alias scheme.
    row["nfc_cards"] = [
        {"id": "100001", "token": "tok-1", "type": "id_card"},  # CN=1234
        {"id": "100002", "token": "tok-2", "type": "id_card"},  # CN=1235
    ]
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([row]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1"), (1235, "tok-2")]),
    )
    with caplog.at_level(logging.WARNING, logger="door_sync.unifi.client"):
        client = make_client()
        client.fetch_users()

    # The warning message must not contain the raw card numbers.
    for rec in caplog.records:
        assert "1234" not in rec.message
        assert "1235" not in rec.message


def test_apply_dry_run_still_fetches_token_map_when_diff_has_cards(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Dry-run with card-bearing diff must still issue the token-map GET
    so a dry-run report reflects which cards would need import (spec §8)."""
    client = make_client(dry_run=True)

    # Empty fetch_users.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    # Token-map fetch — must be issued in dry-run when the diff has cards.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )

    client.apply(_diff(to_add=(_resolved(99, card_id=9999),)))

    # The token-map endpoint MUST have been called.
    token_calls = [
        r for r in httpx_mock.get_requests() if "/credentials/nfc_cards/tokens" in str(r.url)
    ]
    assert len(token_calls) == 1
    # No import POST (writes are suppressed in dry-run).
    import_calls = [
        r for r in httpx_mock.get_requests() if "/credentials/nfc_cards/import" in str(r.url)
    ]
    assert import_calls == []


def test_apply_dry_run_no_token_map_when_diff_has_no_cards(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """Dry-run with cardless diff (deactivate only) skips the token-map fetch."""
    client = make_client(dry_run=True)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42")]),
    )
    fetched = client.fetch_users()

    client.apply(_diff(to_deactivate=(fetched[0],)))

    # Only the fetch_users GET, no token-map fetch.
    token_calls = [
        r for r in httpx_mock.get_requests() if "/credentials/nfc_cards/tokens" in str(r.url)
    ]
    assert token_calls == []


def test_apply_inter_call_delay_invoked(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """time.sleep(0.075) is called once per write."""
    sleeps: list[float] = []
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s))
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42, user_id="uuid-42")]),
    )
    fetched = client.fetch_users()

    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    client.apply(_diff(to_deactivate=(fetched[0],)))
    # One write → one sleep of 0.075.
    assert sleeps == [0.075]


def test_unifi_client_constructs_from_loaded_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """Loading the example config and instantiating UnifiClient must
    produce a client whose base_url and TLS-verify hostname are sensible.

    This catches the regression where config.host was validated as a full URL
    but the client was treating it as a bare hostname.
    """
    repo_root = Path(__file__).parent.parent

    env_path = tmp_path / "env"
    env_path.write_text("CIVICRM_API_KEY=test\nUNIFI_API_KEY=test\n")
    config = load(config_path=repo_root / "config.example.toml", env_path=env_path)

    # Stub TLS verification — we only care about whether the URL gets parsed
    # without producing absurd shapes.
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    # Patch the fingerprint in the loaded config (frozen dataclass — make a copy).
    config_unifi = replace(config.unifi, tls_fingerprint=fp)

    client = make_client(config=config_unifi)
    # base_url must NOT have https:// doubled.
    assert str(client._http.base_url).count("https://") == 1
    # And it must be a valid URL.
    assert "://" in str(client._http.base_url)


def test_unifi_client_host_without_port_defaults_to_12445(
    make_client: Callable[..., UnifiClient],
) -> None:
    """If config.host omits the port, both TLS verification and base_url
    must default to UniFi Access's fixed port 12445 — same target for both.

    Regression guard: previously base_url=config.host let httpx default to
    443 while _verify_tls_fingerprint pinned on 12445, so the two could
    even hit different servers.
    """
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = UnifiConfig(
        host="https://192.0.2.1",  # no port
        api_key="testkey",
        tls_fingerprint=fp,
        facility_code=42,
    )
    client = make_client(config=config)
    # base_url has the default port baked in.
    assert str(client._http.base_url) == "https://192.0.2.1:12445"
    # And the internal hostname/port used by TLS verification matches.
    assert client._hostname == "192.0.2.1"
    assert client._port == 12445


def test_unifi_client_host_with_custom_port_preserves_it(
    make_client: Callable[..., UnifiClient],
) -> None:
    """An explicit non-standard port in config.host is preserved in both
    base_url and the TLS-verification target.
    """
    cert = b"fake-cert"
    fp = hashlib.sha256(cert).hexdigest()
    config = UnifiConfig(
        host="https://192.0.2.1:8443",
        api_key="testkey",
        tls_fingerprint=fp,
        facility_code=42,
    )
    client = make_client(config=config)
    assert str(client._http.base_url) == "https://192.0.2.1:8443"
    assert client._port == 8443


def test_apply_create_includes_user_email_when_set(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """A new user with an email POSTs user_email in the create body."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={"code": "SUCCESS", "msg": "success", "data": {"id": "uuid-new"}},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email="jane@example.com",
    )
    client.apply(_diff(to_add=(resolved,)))

    post_user = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {
        "first_name": "Jane",
        "last_name": "Doe",
        "employee_number": "42",
        "user_email": "jane@example.com",
    }


def test_apply_create_omits_user_email_when_none(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """A new user without an email POSTs no user_email key (unchanged body)."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={"code": "SUCCESS", "msg": "success", "data": {"id": "uuid-new"}},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-new/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email=None,
    )
    client.apply(_diff(to_add=(resolved,)))

    post_user = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "POST" and r.url.path == "/api/v1/developer/users"
    )
    body = _json.loads(post_user.content)
    assert body == {"first_name": "Jane", "last_name": "Doe", "employee_number": "42"}


def test_apply_update_credential_email_only(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """email changes but name and card don't: one PUT carrying only user_email."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    user_email="old@example.com",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",  # same name as _user_row default
        card_id=1234,  # same card
        target_policy="pol-1",
        resolution="tier",
        email="new@example.com",
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    put_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    )
    body = _json.loads(put_req.content)
    assert body == {"user_email": "new@example.com"}
    user_nfc_calls = [
        r for r in httpx_mock.get_requests() if "/users/uuid-42/nfc_cards" in str(r.url)
    ]
    assert user_nfc_calls == []


def test_apply_update_credential_email_cleared_to_none(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """email removed in CiviCRM (resolved.email=None) while UniFi has an email:
    one PUT with user_email="" to clear it; no nfc_cards calls."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    user_email="old@example.com",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",  # same name as _user_row default
        card_id=1234,  # same card
        target_policy="pol-1",
        resolution="tier",
        email=None,  # email removed in CiviCRM
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    put_req = next(
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    )
    body = _json.loads(put_req.content)
    assert body == {"user_email": ""}
    user_nfc_calls = [
        r for r in httpx_mock.get_requests() if "/users/uuid-42/nfc_cards" in str(r.url)
    ]
    assert user_nfc_calls == []


def test_apply_update_credential_name_and_email_single_put(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """name AND email change together: a single PUT carries all changed fields."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    first_name="Old",
                    last_name="Name",
                    user_email="old@example.com",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="New Name",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email="new@example.com",
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    put_reqs = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    assert len(put_reqs) == 1  # one combined PUT, not two
    body = _json.loads(put_reqs[0].content)
    assert body == {"first_name": "New", "last_name": "Name", "user_email": "new@example.com"}


def test_apply_dry_run_logs_would_import_for_unknown_cards(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture, make_client: Callable[..., UnifiClient]
) -> None:
    """Dry-run report includes a would-import line for cards not in the token map."""
    client = make_client(dry_run=True)

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()

    # Token map: 9998 is known, 9999 is unknown.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(9998, "tok-9998")]),
    )

    diff = _diff(to_add=(_resolved(1, card_id=9998), _resolved(2, card_id=9999)))
    with caplog.at_level(logging.INFO, logger="door_sync.unifi.client"):
        client.apply(diff)

    messages = [r.message for r in caplog.records]
    # Only card 9999 should produce a would-import line (9998 is already known).
    assert any("would-import" in m and "****9999" in m for m in messages)
    assert not any("would-import" in m and "****9998" in m for m in messages)


def test_apply_reactivate_includes_user_email_when_set(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """to_add for cached-inactive contact with email: profile PUT includes user_email."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map fetch (card already known). Registered before fetch_users()
    # because fetch resolves card numbers from tokens.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    # Fetch returns user 42 inactive with the same card.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    status="DEACTIVATED",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    client.fetch_users()

    # PUT profile (still deactivated), then PUT activate.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT bind card.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT assign policy.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/access_policies",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )
    # PUT activate.
    httpx_mock.add_response(
        method="PUT",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Member 42",
        card_id=1234,
        target_policy="pol-1",
        resolution="tier",
        email="react@example.com",
    )
    client.apply(_diff(to_add=(resolved,)))

    # The profile PUT is the one whose body contains "employee_number".
    # The activate PUT contains only {"status": "ACTIVE"}.
    user_puts = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path == "/api/v1/developer/users/uuid-42"
    ]
    assert len(user_puts) == 2
    profile_put_body = _json.loads(
        next(r for r in user_puts if "employee_number" in _json.loads(r.content)).content
    )
    assert profile_put_body["user_email"] == "react@example.com"
    assert profile_put_body["employee_number"] == "42"
    # The activate PUT carries only status; select it by body, not by index,
    # so the assertion doesn't depend on request ordering.
    activate_put_body = _json.loads(
        next(r for r in user_puts if "employee_number" not in _json.loads(r.content)).content
    )
    assert activate_put_body == {"status": "ACTIVE"}


# --- network-error retry & exhaustion ---


def test_network_error_then_success(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """One transient httpx.RequestError then a 200 → fetch_users succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s))
    httpx_mock.add_exception(httpx.ConnectError("transient blip"))
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([_user_row(contact_id=42)]),
    )
    client = make_client()
    users = client.fetch_users()
    assert len(users) == 1
    assert len(sleeps) == 1


def test_network_error_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """Three consecutive httpx.RequestErrors exhaust retries and raise."""
    sleeps: list[float] = []
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda s: sleeps.append(s))
    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "network failure" in str(exc_info.value)
    assert len(httpx_mock.get_requests()) == 3
    assert len(sleeps) == 2  # Sleep between attempts, not after the last.


def test_http_429_exhausts_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """Three consecutive 429s exhaust retries and raise (HTTP 429 path)."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    for _ in range(3):
        httpx_mock.add_response(
            method="GET",
            url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
            status_code=429,
        )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "HTTP 429" in str(exc_info.value)
    assert len(httpx_mock.get_requests()) == 3


# --- malformed-payload envelope guards ---


def test_unwrap_non_object_envelope_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """A 200 whose JSON body is a list (not an object) raises with envelope-shape detail."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=["not", "an", "object"],
    )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "unexpected envelope shape" in str(exc_info.value)


def test_fetch_users_non_list_data_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """SUCCESS envelope whose `data` is not a list raises from fetch_users."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json={"code": "SUCCESS", "msg": "ok", "data": {"not": "a list"}},
    )
    client = make_client()
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()
    assert "expected list of users" in str(exc_info.value)


def test_token_map_non_list_data_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """SUCCESS envelope whose `data` is not a list raises from _ensure_nfc_token_map."""
    client = make_client()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json={"code": "SUCCESS", "msg": "ok", "data": {"not": "a list"}},
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._ensure_nfc_token_map()
    assert "expected list of cards" in str(exc_info.value)


def test_import_cards_non_list_data_raises(
    httpx_mock: HTTPXMock, make_client: Callable[..., UnifiClient]
) -> None:
    """SUCCESS import envelope whose `data` is not a list raises from _import_cards."""
    client = make_client()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/import",
        json={"code": "SUCCESS", "msg": "ok", "data": {"not": "a list"}},
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client._import_cards([1234])
    assert "expected list from /nfc_cards/import" in str(exc_info.value)


# --- create-user with no id ---


def test_apply_create_user_missing_id_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """POST /users returning a SUCCESS envelope with no `id` raises UnifiClientError."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page([], total=0),
    )
    client.fetch_users()
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_cards_page([]),
    )
    # POST /users → SUCCESS but the data object has no "id".
    httpx_mock.add_response(
        method="POST",
        url="https://192.0.2.1:12445/api/v1/developer/users",
        json={"code": "SUCCESS", "msg": "success", "data": {"first_name": "Jane"}},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",
        card_id=None,  # no card → no import POST, isolates the create path
        target_policy="pol-1",
        resolution="tier",
    )
    with pytest.raises(UnifiClientError) as exc_info:
        client.apply(_diff(to_add=(resolved,)))
    assert "no id" in str(exc_info.value)
    assert "contact=42" in str(exc_info.value)


# --- update_credential that removes a card (card_id None) ---


def test_apply_update_credential_removes_card(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, make_client: Callable[..., UnifiClient]
) -> None:
    """resolved.card_id is None while the UniFi user has a card: delete the old
    card and issue NO new-card bind/import (the 403->365 skip branch)."""
    monkeypatch.setattr("door_sync.unifi.client.time.sleep", lambda _: None)
    client = make_client()

    # Token-map fetch (preimport runs because to_update_credential is non-empty;
    # the resolved member has no card so no import is needed). Registered before
    # fetch_users() because fetch resolves card numbers from tokens.
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/credentials/nfc_cards/tokens?page_num=1&page_size=100",
        json=_card_tokens_page([(1234, "tok-1234")]),
    )
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        json=_users_page(
            [
                _user_row(
                    contact_id=42,
                    user_id="uuid-42",
                    nfc_token="tok-1234",
                )
            ]
        ),
    )
    fetched = client.fetch_users()

    # DELETE old card.
    httpx_mock.add_response(
        method="DELETE",
        url="https://192.0.2.1:12445/api/v1/developer/users/uuid-42/nfc_cards/delete",
        json={"code": "SUCCESS", "msg": "success", "data": None},
    )

    resolved = ResolvedMember(
        contact_id=42,
        display_name="Jane Doe",  # matches fetched user → no name PUT
        card_id=None,  # card removed in CiviCRM
        target_policy="pol-1",
        resolution="tier",
    )
    client.apply(_diff(to_update_credential=((resolved, fetched[0]),)))

    delete_req = next(r for r in httpx_mock.get_requests() if r.method == "DELETE")
    assert _json.loads(delete_req.content) == {"token": "tok-1234"}
    # No bind (PUT .../nfc_cards) and no import.
    bind_calls = [
        r
        for r in httpx_mock.get_requests()
        if r.method == "PUT" and r.url.path.endswith("/nfc_cards")
    ]
    assert bind_calls == []
    import_calls = [r for r in httpx_mock.get_requests() if "/nfc_cards/import" in str(r.url)]
    assert import_calls == []
