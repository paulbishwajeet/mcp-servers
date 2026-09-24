"""
Tests for google-workspace's core logic (server.append_expense_row_impl,
server.load_config).

Run with: pytest (from inside google-workspace/, with the venv active).

All Google API calls are mocked -- this suite never hits the real Sheets
API and never needs a real service account key.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


REQUIRED_ENV_VARS = (
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "TREASURER_SPREADSHEET_ID",
)


@pytest.fixture(autouse=True)
def _reset_cached_services(monkeypatch):
    # Each test controls its own env vars and service mocks; make sure no
    # state leaks between tests via the module-level service cache.
    monkeypatch.setattr(server, "_sheets_service", None)
    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake_key_file(tmp_path):
    key_file = tmp_path / "service-account.json"
    key_file.write_text('{"type": "service_account"}')
    return key_file


@pytest.fixture
def configured_env(monkeypatch, fake_key_file):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(fake_key_file))
    monkeypatch.setenv("TREASURER_SPREADSHEET_ID", "sheet-123")


def _http_error(status: int, message: str = "error body") -> HttpError:
    resp = MagicMock()
    resp.status = status
    body = json.dumps({"error": {"message": message}}).encode()
    return HttpError(resp, body)


# ---------------------------------------------------------------------------
# load_config / missing env vars
# ---------------------------------------------------------------------------

def test_load_config_fails_clearly_when_all_env_vars_missing():
    with pytest.raises(server.ConfigError) as exc_info:
        server.load_config()

    message = str(exc_info.value)
    for name in REQUIRED_ENV_VARS:
        assert name in message


def test_load_config_fails_clearly_when_one_env_var_missing(monkeypatch, fake_key_file):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(fake_key_file))
    # TREASURER_SPREADSHEET_ID intentionally left unset

    with pytest.raises(server.ConfigError) as exc_info:
        server.load_config()

    assert "TREASURER_SPREADSHEET_ID" in str(exc_info.value)


def test_load_config_fails_clearly_when_key_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("TREASURER_SPREADSHEET_ID", "sheet-123")

    with pytest.raises(server.ConfigError) as exc_info:
        server.load_config()

    assert "does not point to an existing file" in str(exc_info.value)


def test_append_expense_row_returns_structured_error_when_env_missing():
    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Notes", "receipt-ref",
    )

    assert result["success"] is False
    assert result["row_number"] is None
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in result["error"]


# ---------------------------------------------------------------------------
# append_expense_row_impl -- mocked Sheets API
# ---------------------------------------------------------------------------

def test_append_expense_row_success(monkeypatch, configured_env):
    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "Sheet1!A5:I5"},
    }
    monkeypatch.setattr(server, "_get_sheets_service", lambda config: fake_service)

    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Bought paper", "receipts/2026/office-depot.jpg",
    )

    assert result == {"success": True, "row_number": 5, "error": None}

    append_call = fake_service.spreadsheets.return_value.values.return_value.append
    _, kwargs = append_call.call_args
    assert kwargs["spreadsheetId"] == "sheet-123"
    assert kwargs["valueInputOption"] == "USER_ENTERED"
    assert kwargs["body"]["values"] == [[
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Bought paper", "receipts/2026/office-depot.jpg",
    ]]


def test_append_expense_row_rejects_missing_payee(configured_env):
    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert "payee" in result["error"]


def test_append_expense_row_rejects_invalid_amount(configured_env):
    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", "not-a-number", "Office Depot",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert "amount" in result["error"]


def test_append_expense_row_handles_spreadsheet_not_found(monkeypatch, configured_env):
    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = _http_error(404)
    monkeypatch.setattr(server, "_get_sheets_service", lambda config: fake_service)

    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert result["row_number"] is None
    assert "not found" in result["error"].lower()


def test_append_expense_row_handles_office_file_not_supported(monkeypatch, configured_env):
    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = _http_error(
        400,
        "This operation is not supported for this document. The document must not be an Office file.",
    )
    monkeypatch.setattr(server, "_get_sheets_service", lambda config: fake_service)

    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert result["row_number"] is None
    assert "Office file" in result["error"]
    assert "Save as Google Sheets" in result["error"]


def test_append_expense_row_handles_permission_denied(monkeypatch, configured_env):
    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = _http_error(403)
    monkeypatch.setattr(server, "_get_sheets_service", lambda config: fake_service)

    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert "permission" in result["error"].lower()


def test_append_expense_row_never_raises_on_unexpected_error(monkeypatch, configured_env):
    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.append.return_value.execute.side_effect = RuntimeError("boom")
    monkeypatch.setattr(server, "_get_sheets_service", lambda config: fake_service)

    result = server.append_expense_row_impl(
        "2026-09-22", "Alex", "Supplies", 42.50, "Office Depot",
        "Debit Card", "Jamie", "Notes", "ref",
    )

    assert result["success"] is False
    assert result["error"] is not None
