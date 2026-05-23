"""Tests for the UniFi Access client."""

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

from door_sync.config import UnifiConfig
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


def _make_client(httpx_mock: HTTPXMock) -> UnifiClient:
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
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()  # type: ignore[attr-defined]
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
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()  # type: ignore[attr-defined]
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
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()  # type: ignore[attr-defined]
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
    client = _make_client(httpx_mock)
    client.fetch_users()  # type: ignore[attr-defined]
    assert any(s >= 5 for s in sleeps)
    client.close()


def test_malformed_json_raises(httpx_mock: HTTPXMock) -> None:
    """200 with non-JSON body raises UnifiClientError."""
    httpx_mock.add_response(
        method="GET",
        url="https://192.0.2.1:12445/api/v1/developer/users?page_num=1&page_size=100&expand[]=access_policy",
        text="<html>not json</html>",
    )
    client = _make_client(httpx_mock)
    with pytest.raises(UnifiClientError) as exc_info:
        client.fetch_users()  # type: ignore[attr-defined]
    assert "malformed JSON" in str(exc_info.value)
    client.close()
