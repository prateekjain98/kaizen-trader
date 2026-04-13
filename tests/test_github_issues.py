"""Tests for the auto GitHub issue creation system."""

import os
import time
from unittest.mock import patch, MagicMock

import pytest

import src.automation.github_issues as gh_issues
from src.automation.github_issues import (
    create_blind_spot_issue,
    create_data_gap_issue,
    create_chronic_underperformer_issue,
    MAX_ISSUES_PER_DAY,
)


@pytest.fixture(autouse=True)
def clean_state():
    """Reset module state before each test."""
    gh_issues.reset_state()
    yield
    gh_issues.reset_state()


def _mock_gh_success(issue_number: int = 42):
    """Return a mock subprocess.run that simulates a successful gh issue create."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = f"https://github.com/prateekjain98/kaizen-trader/issues/{issue_number}\n"
    return mock_result


def _mock_gh_failure():
    """Return a mock subprocess.run that simulates a failed gh issue create."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "gh: authentication required"
    return mock_result


# --- Dedup tests ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(10))
@patch("src.automation.github_issues.log")
def test_dedup_blind_spot(mock_log, mock_run):
    """Same fingerprint_key should not create a second issue."""
    result1 = create_blind_spot_issue("strat|swing|bull|stop_hit|<1h", 3, -2.5, ["momentum_swing"])
    assert result1 == 10

    result2 = create_blind_spot_issue("strat|swing|bull|stop_hit|<1h", 5, -3.0, ["momentum_swing"])
    assert result2 is None
    assert mock_run.call_count == 1


@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(20))
@patch("src.automation.github_issues.log")
def test_dedup_data_gap(mock_log, mock_run):
    """Same suggestion should not create a second issue."""
    result1 = create_data_gap_issue("Add on-chain whale flow data")
    assert result1 == 20

    result2 = create_data_gap_issue("Add on-chain whale flow data")
    assert result2 is None
    assert mock_run.call_count == 1


@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(30))
@patch("src.automation.github_issues.log")
def test_dedup_chronic(mock_log, mock_run):
    """Same strategy_id should not create a second chronic issue."""
    result1 = create_chronic_underperformer_issue("funding_extreme", 15, 20.0, -1.5, 8)
    assert result1 == 30

    result2 = create_chronic_underperformer_issue("funding_extreme", 20, 18.0, -2.0, 10)
    assert result2 is None
    assert mock_run.call_count == 1


# --- Daily cap tests ---

@patch("src.automation.github_issues.subprocess.run")
@patch("src.automation.github_issues.log")
def test_daily_cap_enforced(mock_log, mock_run):
    """No more than MAX_ISSUES_PER_DAY issues should be created per day."""
    mock_run.side_effect = [
        _mock_gh_success(1),
        _mock_gh_success(2),
        _mock_gh_success(3),
        _mock_gh_success(4),  # should never be reached
    ]

    r1 = create_blind_spot_issue("key1", 3, -1.0, ["s1"])
    r2 = create_data_gap_issue("suggestion-A")
    r3 = create_chronic_underperformer_issue("strat_x", 15, 20.0, -1.0, 5)
    assert r1 == 1
    assert r2 == 2
    assert r3 == 3

    # 4th issue should be blocked by daily cap
    r4 = create_blind_spot_issue("key4", 4, -2.0, ["s2"])
    assert r4 is None
    assert mock_run.call_count == 3


@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(99))
@patch("src.automation.github_issues.log")
def test_daily_cap_resets_on_new_day(mock_log, mock_run):
    """Daily cap should reset when the date changes."""
    # Fill the cap
    create_blind_spot_issue("a1", 3, -1.0, ["s1"])
    create_data_gap_issue("b1")
    create_chronic_underperformer_issue("c1", 15, 20.0, -1.0, 5)
    assert mock_run.call_count == 3

    # Simulate date change
    with patch("src.automation.github_issues.time.strftime", return_value="2099-01-01"):
        r = create_blind_spot_issue("new_key", 5, -3.0, ["s3"])
        assert r == 99
        assert mock_run.call_count == 4


# --- Blind spot issue creation ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(55))
@patch("src.automation.github_issues.log")
def test_blind_spot_issue_creation(mock_log, mock_run):
    """Blind spot issue should be created with correct title and labels."""
    result = create_blind_spot_issue("momentum|swing|bull|stop_hit|<1h", 5, -3.14, ["momentum_swing", "vwap_revert"])
    assert result == 55

    args = mock_run.call_args[0][0]
    assert "gh" in args[0]
    assert "issue" in args
    assert "create" in args
    assert "--label" in args
    idx = args.index("--title") + 1
    assert "Blind Spot" in args[idx]


# --- Data gap issue creation ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(66))
@patch("src.automation.github_issues.log")
def test_data_gap_issue_creation(mock_log, mock_run):
    """Data gap issue should include the suggestion in title and body."""
    result = create_data_gap_issue("Integrate Glassnode on-chain metrics", "Losses in bear markets")
    assert result == 66

    args = mock_run.call_args[0][0]
    idx_title = args.index("--title") + 1
    assert "Data Gap" in args[idx_title]
    idx_body = args.index("--body") + 1
    assert "Glassnode" in args[idx_body]
    assert "Losses in bear markets" in args[idx_body]


# --- Chronic underperformer issue creation ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(77))
@patch("src.automation.github_issues.log")
def test_chronic_underperformer_issue_creation(mock_log, mock_run):
    """Chronic underperformer issue should contain strategy details."""
    result = create_chronic_underperformer_issue("funding_extreme", 21, 18.5, -2.3, 12)
    assert result == 77

    args = mock_run.call_args[0][0]
    idx_title = args.index("--title") + 1
    assert "funding_extreme" in args[idx_title]
    idx_body = args.index("--body") + 1
    assert "21 Days" in args[idx_body]
    assert "18.5%" in args[idx_body]


# --- GITHUB_REPO not set ---

@patch("src.automation.github_issues._get_repo", return_value="")
@patch("src.automation.github_issues.log")
def test_skips_when_repo_not_set(mock_log, mock_repo):
    """Should gracefully skip issue creation when GITHUB_REPO is not set."""
    result = create_blind_spot_issue("key1", 3, -1.0, ["s1"])
    assert result is None
    mock_log.assert_any_call("warn", "GITHUB_REPO not set -- skipping issue creation")


# --- gh CLI failure ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_failure())
@patch("src.automation.github_issues.log")
def test_handles_gh_cli_failure(mock_log, mock_run):
    """Should handle gh CLI errors gracefully and return None."""
    result = create_blind_spot_issue("key_fail", 3, -1.0, ["s1"])
    assert result is None
    # Should have logged the error
    error_calls = [c for c in mock_log.call_args_list if c[0][0] == "error"]
    assert len(error_calls) >= 1
    assert "gh issue create failed" in error_calls[0][0][1]


# --- subprocess exception ---

@patch("src.automation.github_issues.subprocess.run", side_effect=OSError("command not found"))
@patch("src.automation.github_issues.log")
def test_handles_subprocess_exception(mock_log, mock_run):
    """Should handle subprocess exceptions gracefully."""
    result = create_data_gap_issue("some suggestion")
    assert result is None
    error_calls = [c for c in mock_log.call_args_list if c[0][0] == "error"]
    assert len(error_calls) >= 1
    assert "Failed to create GitHub issue" in error_calls[0][0][1]


# --- Different trigger keys are not duplicates ---

@patch("src.automation.github_issues.subprocess.run", return_value=_mock_gh_success(88))
@patch("src.automation.github_issues.log")
def test_different_keys_are_not_duplicates(mock_log, mock_run):
    """Different trigger keys should create separate issues."""
    r1 = create_blind_spot_issue("key_a", 3, -1.0, ["s1"])
    r2 = create_blind_spot_issue("key_b", 4, -2.0, ["s2"])
    assert r1 == 88
    assert r2 == 88
    assert mock_run.call_count == 2


# --- Issue number parsing ---

@patch("src.automation.github_issues.subprocess.run")
@patch("src.automation.github_issues.log")
def test_issue_number_parsed_from_url(mock_log, mock_run):
    """Issue number should be correctly parsed from the gh CLI output URL."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "https://github.com/owner/repo/issues/12345\n"
    mock_run.return_value = mock_result

    result = create_blind_spot_issue("parse_test", 3, -1.0, ["s1"])
    assert result == 12345
