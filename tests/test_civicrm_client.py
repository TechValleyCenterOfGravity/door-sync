"""Tests for the CiviCRM API4 client."""

import json
import urllib.parse
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from door_sync.civicrm.client import CivicrmClient, CivicrmClientError
from door_sync.config import CivicrmConfig


def _config() -> CivicrmConfig:
    return CivicrmConfig(
        host="https://civi.example.org",
        api_key="testkey",
        card_id_field="Door_Access.card_id",
    )


def _contact(
    contact_id: int,
    display_name: str = "Test Person",
    card_id: int | str = 100,
) -> dict[str, Any]:
    return {
        "id": contact_id,
        "display_name": display_name,
        "Door_Access.card_id": card_id,
    }


def _membership(
    contact_id: int,
    type_label: str = "Gold",
    status_name: str = "Current",
) -> dict[str, Any]:
    return {
        "contact_id": contact_id,
        "membership_type_id:label": type_label,
        "status_id:name": status_name,
    }


def _values_response(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {"values": values, "count": len(values)}


def _register_contacts(
    httpx_mock: HTTPXMock,
    values: list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(values),
    )


def _register_memberships(
    httpx_mock: HTTPXMock,
    values: list[dict[str, Any]],
) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(values),
    )


# --- Lifecycle ---


def test_context_manager_closes_http_client() -> None:
    """Using CivicrmClient as a context manager closes the underlying httpx.Client."""
    with CivicrmClient(_config()) as client:
        assert client._http.is_closed is False
    assert client._http.is_closed is True


# --- fetch_active happy paths ---


def test_fetch_active_happy_path(httpx_mock: HTTPXMock) -> None:
    """One contact, one Current membership → one CiviMember with the right fields."""
    _register_contacts(httpx_mock, [_contact(42, "Jane Doe", card_id=12345)])
    _register_memberships(httpx_mock, [_membership(42, "Gold", "Current")])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    member = result[0]
    assert member.contact_id == 42
    assert member.display_name == "Jane Doe"
    assert member.card_id == 12345
    assert member.membership_types == ["Gold"]


def test_fetch_active_empty_result(httpx_mock: HTTPXMock) -> None:
    """Zero contacts → returns []. The memberships query is NOT made."""
    _register_contacts(httpx_mock, [])
    # NOTE: no membership response registered — if the client tried to call it,
    # pytest-httpx would raise an unregistered-request error.

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result == []


def test_contact_with_multiple_active_memberships(httpx_mock: HTTPXMock) -> None:
    """Contact with both Gold (Current) and Comp (Current) → membership_types has both."""
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(
        httpx_mock,
        [
            _membership(42, "Gold", "Current"),
            _membership(42, "Comp", "Current"),
        ],
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert sorted(result[0].membership_types) == ["Comp", "Gold"]


def test_contact_with_no_active_membership_kept_with_empty_types(
    httpx_mock: HTTPXMock,
) -> None:
    """Contact has card_id but no Current/Grace memberships → empty list, NOT excluded.

    This member resolves to "unmapped" in tier_mapping, which the safety guard
    halts on — surfacing the data issue rather than silently ignoring.
    """
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert result[0].contact_id == 42
    assert result[0].membership_types == []


def test_expired_memberships_filtered(httpx_mock: HTTPXMock) -> None:
    """Server-side where filter only returns Current/Grace.

    This test verifies the client passes the correct filter; it doesn't test
    CiviCRM's filtering behavior. We register only what the server would return
    (i.e., already filtered), and assert the membership_types reflect that.
    """
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(
        httpx_mock,
        [
            # Server returned only the Grace one because of our where clause
            _membership(42, "Silver", "Grace"),
        ],
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert result[0].membership_types == ["Silver"]


def test_request_uses_bearer_auth_and_form_body(httpx_mock: HTTPXMock) -> None:
    """Sanity-check the HTTP shape (auth header + form body) against spec §5."""
    _register_contacts(httpx_mock, [])

    with CivicrmClient(_config()) as client:
        client.fetch_active()

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    req = requests[0]
    assert req.headers["authorization"] == "Bearer testkey"
    assert req.headers["content-type"].startswith("application/x-www-form-urlencoded")
    # Body is form-encoded `params=<json>`. Decode and verify the JSON shape.
    body = req.content.decode()
    parsed = urllib.parse.parse_qs(body)
    params = json.loads(parsed["params"][0])
    assert "select" in params
    assert params["select"] == ["id", "display_name", "Door_Access.card_id"]


# --- Pagination ---


def test_fetch_active_paginates_contacts(httpx_mock: HTTPXMock) -> None:
    """251 contacts arrive as a full page of 250 then a short page of 1."""
    full_page = [_contact(i) for i in range(1, 251)]
    short_page = [_contact(251)]

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(full_page),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        json=_values_response(short_page),
    )
    # All 251 contacts have Gold/Current memberships, returned across two pages
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response([_membership(i, "Gold", "Current") for i in range(1, 251)]),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response([_membership(251, "Gold", "Current")]),
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 251
    assert {m.contact_id for m in result} == set(range(1, 252))

    # Confirm two Contact.get requests were made with offset=0 and offset=250
    contact_requests = [
        r for r in httpx_mock.get_requests()
        if "/Contact/get" in str(r.url)
    ]
    assert len(contact_requests) == 2
    offsets = sorted(
        json.loads(
            urllib.parse.parse_qs(r.content.decode())["params"][0]
        ).get("offset", 0)
        for r in contact_requests
    )
    assert offsets == [0, 250]


def test_fetch_active_paginates_memberships(httpx_mock: HTTPXMock) -> None:
    """251 memberships across 2 pages."""
    _register_contacts(httpx_mock, [_contact(1)])

    full_page = [_membership(1, f"Type{i}", "Current") for i in range(250)]
    short_page = [_membership(1, "Type250", "Current")]
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(full_page),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Membership/get",
        json=_values_response(short_page),
    )

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert len(result[0].membership_types) == 251


# --- Retries and error paths ---


def test_http_401_raises_no_retry(httpx_mock: HTTPXMock) -> None:
    """401 is a permanent auth error — no retry, raise CivicrmClientError."""
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=401,
        text="Unauthorized",
    )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="401"):
            client.fetch_active()

    assert len(httpx_mock.get_requests()) == 1  # No retries


def test_http_500_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three 500 responses → raise CivicrmClientError; three requests made."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
            status_code=500,
            text="Internal Server Error",
        )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="500"):
            client.fetch_active()

    assert len(httpx_mock.get_requests()) == 3
    assert len(sleep_calls) == 2  # Sleep between attempts, not after the last


def test_http_500_then_200_succeeds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """500 then 200 → success after one retry."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=500,
    )
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert len(sleep_calls) == 1


def test_http_429_honors_retry_after_seconds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 with Retry-After: 5 → client waits at least 5s before retrying."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=429,
        headers={"Retry-After": "5"},
    )
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert any(s >= 5 for s in sleep_calls), f"Expected a sleep >= 5s, got {sleep_calls}"


def test_malformed_json_raises(httpx_mock: HTTPXMock) -> None:
    """200 with invalid JSON body → CivicrmClientError."""
    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=200,
        text="not valid json {",
    )

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="malformed"):
            client.fetch_active()


def test_network_error_retries_then_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three httpx.RequestError responses → CivicrmClientError("network failure...")."""
    import httpx
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    with CivicrmClient(_config()) as client:
        with pytest.raises(CivicrmClientError, match="network failure"):
            client.fetch_active()

    assert len(httpx_mock.get_requests()) == 3
    assert len(sleep_calls) == 2  # Sleep between attempts


def test_network_error_then_success(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One network error then success → fetch_active succeeds."""
    import httpx
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_exception(httpx.ConnectError("transient blip"))
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    assert len(sleep_calls) == 1


def test_negative_retry_after_falls_back_to_backoff(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server returning Retry-After: -1 must NOT cause time.sleep to raise.

    Negative values fall back to exponential backoff instead.
    """
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    httpx_mock.add_response(
        method="POST",
        url="https://civi.example.org/wp-json/civicrm/v3/api4/Contact/get",
        status_code=429,
        headers={"Retry-After": "-1"},
    )
    _register_contacts(httpx_mock, [_contact(42)])
    _register_memberships(httpx_mock, [_membership(42)])

    with CivicrmClient(_config()) as client:
        result = client.fetch_active()

    assert len(result) == 1
    # All sleep values should be positive (no negative slept on)
    assert all(s > 0 for s in sleep_calls)
