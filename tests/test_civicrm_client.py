"""Tests for the CiviCRM API4 client."""

import pytest

from door_sync.civicrm.client import CivicrmClient
from door_sync.config import CivicrmConfig


def _config() -> CivicrmConfig:
    return CivicrmConfig(
        host="https://civi.example.org",
        api_key="testkey",
        card_id_field="Door_Access.card_id",
    )


def test_context_manager_closes_http_client() -> None:
    """Using CivicrmClient as a context manager closes the underlying httpx.Client."""
    with CivicrmClient(_config()) as client:
        assert client._http.is_closed is False
    assert client._http.is_closed is True


def test_fetch_active_raises_not_implemented_for_now() -> None:
    """Sanity: skeleton-only client raises NotImplementedError. Replaced in Task 3."""
    with CivicrmClient(_config()) as client:
        with pytest.raises(NotImplementedError):
            client.fetch_active()
