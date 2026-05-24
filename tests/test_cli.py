"""Tests for door_sync.cli — pretty-printers for show-diff and validate-config."""

import io

from door_sync import cli
from door_sync.config import ConfigIssue
from door_sync.models import Diff, ResolvedMember, UnifiUser


def test_print_diff_renders_five_sections() -> None:
    diff = Diff(
        to_add=[ResolvedMember(1, "Alice", 0x1234, "p1", "tier")],
        to_update_credential=[
            (
                ResolvedMember(2, "Bob", 0x5678, "p1", "tier"),
                UnifiUser(2, "Bob", 0x1111, True, "p1"),
            ),
        ],
        to_update_policy=[
            (
                ResolvedMember(3, "Carol", 0x9999, "p2", "tier"),
                UnifiUser(3, "Carol", 0x9999, True, "p1"),
            ),
        ],
        to_deactivate=[UnifiUser(4, "Dave", 0xABCD, True, "p1")],
        unmapped=[ResolvedMember(5, "Eve", 0xFFFF, None, "unmapped")],
    )
    out = io.StringIO()

    cli.print_diff(diff, file=out)

    text = out.getvalue()
    assert "=== ADD (1) ===" in text
    assert "Alice" in text
    assert "=== UPDATE CREDENTIAL (1) ===" in text
    assert "Bob" in text
    assert "=== UPDATE POLICY (1) ===" in text
    assert "Carol" in text
    assert "=== DEACTIVATE (1) ===" in text
    assert "Dave" in text
    assert "=== UNMAPPED (1) ===" in text
    assert "Eve" in text
    # Eve has target_policy=None — no [policy=...] tag should appear on her line
    eve_line = next(line for line in text.splitlines() if "Eve" in line)
    assert "[policy=" not in eve_line


def test_print_diff_empty_sections_still_print_header() -> None:
    diff = Diff([], [], [], [], [])
    out = io.StringIO()

    cli.print_diff(diff, file=out)

    text = out.getvalue()
    assert "=== ADD (0) ===" in text
    assert "=== UNMAPPED (0) ===" in text


def test_print_config_issues_one_line_per_issue() -> None:
    issues = [
        ConfigIssue(path="civicrm.host", message="must start with https://"),
        ConfigIssue(path="UNIFI_API_KEY", message="required env var is missing or empty"),
    ]
    out = io.StringIO()

    cli.print_config_issues(issues, file=out)

    text = out.getvalue()
    assert "civicrm.host: must start with https://" in text
    assert "UNIFI_API_KEY: required env var is missing or empty" in text
