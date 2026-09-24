#!/usr/bin/env python3
"""
google-workspace: a Google Sheets MCP server, authenticating as a service
account so it can run unattended.

Exposes one MCP tool:
  - append_expense_row: appends one expense row to the first tab of a
    fixed spreadsheet (TREASURER_SPREADSHEET_ID).

Runs as a stdio MCP server: `python server.py`.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional, TypedDict

from mcp.server.fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Logging: local file only, inside this project folder. We log tool names,
# outcomes, and non-sensitive identifiers (row numbers) -- never the
# service account key contents, never full credential data, and never
# spreadsheet field values.
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "google_workspace_server.log"

logger = logging.getLogger("google_workspace")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.FileHandler(LOG_FILE)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REQUIRED_ENV_VARS = (
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "TREASURER_SPREADSHEET_ID",
)


class ConfigError(Exception):
    """Raised when required environment variables are missing or invalid."""


class AppendExpenseResult(TypedDict):
    success: bool
    row_number: Optional[int]
    error: Optional[str]


def load_config() -> dict:
    """
    Read and validate the required environment variables. Never logs or
    returns the contents of the service account key file -- only its
    path.
    """
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set them before starting this server -- see README.md."
        )

    key_path = Path(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]).expanduser()
    if not key_path.is_file():
        raise ConfigError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON does not point to an existing file: {key_path}"
        )

    return {
        "service_account_json": str(key_path),
        "spreadsheet_id": os.environ["TREASURER_SPREADSHEET_ID"],
    }


# ---------------------------------------------------------------------------
# Google API client -- built lazily and cached, so importing this module
# (e.g. for tests) never touches the filesystem or network on its own.
# ---------------------------------------------------------------------------

_sheets_service = None


def _build_credentials(key_path: str) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)


def _get_sheets_service(config: dict):
    global _sheets_service
    if _sheets_service is None:
        creds = _build_credentials(config["service_account_json"])
        _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def _http_error_status(exc: HttpError) -> Optional[int]:
    try:
        return int(exc.resp.status)
    except Exception:  # noqa: BLE001 - best-effort only
        return None


def _extract_row_number(append_response: dict) -> Optional[int]:
    """
    Parse a row number like 5 out of a values.append response's
    updates.updatedRange, e.g. "Sheet1!A5:I5".
    """
    updated_range = append_response.get("updates", {}).get("updatedRange", "")
    cell_part = updated_range.split("!")[-1]
    match = re.match(r"[A-Z]+(\d+)", cell_part)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Core implementation, kept separate from the MCP tool wrapper below so
# it's easy to unit-test with a mocked Google API client.
# ---------------------------------------------------------------------------

def append_expense_row_impl(
    date: str,
    submitted_by: str,
    category: str,
    amount: float,
    payee: str,
    payment_method: str,
    approved_by: str,
    notes: str,
    receipt_ref: str,
) -> AppendExpenseResult:
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("append_expense_row rejected: config error")
        return {"success": False, "row_number": None, "error": str(exc)}

    if not str(date).strip() or not str(payee).strip():
        logger.info("append_expense_row rejected: missing required field(s)")
        return {"success": False, "row_number": None, "error": "date and payee are required and cannot be empty"}

    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        logger.info("append_expense_row rejected: invalid amount")
        return {"success": False, "row_number": None, "error": f"amount must be a number, got: {amount!r}"}

    try:
        service = _get_sheets_service(config)
    except Exception as exc:  # noqa: BLE001 - auth/client-build failures
        logger.error("append_expense_row failed: could not build Sheets client: %s", exc)
        return {"success": False, "row_number": None, "error": f"Could not authenticate with Google Sheets: {exc}"}

    row = [date, submitted_by, category, amount_value, payee, payment_method, approved_by, notes, receipt_ref]

    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=config["spreadsheet_id"],
            range="A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except HttpError as exc:
        status = _http_error_status(exc)
        logger.error("append_expense_row failed: Sheets API HttpError status=%s", status)
        if status == 404:
            return {"success": False, "row_number": None, "error": "Spreadsheet not found. Check TREASURER_SPREADSHEET_ID."}
        if status == 403:
            return {"success": False, "row_number": None, "error": "Permission denied. Share the spreadsheet with the service account's email as Editor."}
        if status == 400 and "Office file" in str(exc):
            return {
                "success": False, "row_number": None,
                "error": (
                    "TREASURER_SPREADSHEET_ID points to an uploaded Excel/Office file, not a "
                    "native Google Sheet. In Drive, open it and use File > Save as Google Sheets, "
                    "then point TREASURER_SPREADSHEET_ID at the new file's ID (and re-share it "
                    "with the service account as Editor)."
                ),
            }
        return {"success": False, "row_number": None, "error": f"Google Sheets API error: {exc}"}
    except Exception as exc:  # noqa: BLE001 - last-resort guard; must never raise through MCP
        logger.error("append_expense_row failed: unexpected error: %s", exc)
        return {"success": False, "row_number": None, "error": f"Unexpected error appending row: {exc}"}

    row_number = _extract_row_number(response)
    logger.info("append_expense_row ok: row_number=%s", row_number)
    return {"success": True, "row_number": row_number, "error": None}


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("google-workspace")


@mcp.tool()
def append_expense_row(
    date: str,
    submitted_by: str,
    category: str,
    amount: float,
    payee: str,
    payment_method: str,
    approved_by: str,
    notes: str,
    receipt_ref: str,
) -> AppendExpenseResult:
    """
    Append one expense row to the first tab of the treasurer spreadsheet
    (TREASURER_SPREADSHEET_ID), in column order: Date, Submitted By,
    Category, Amount, Payee, Payment Method, Approved By, Notes,
    Receipt Ref.

    Uses valueInputOption="USER_ENTERED" so dates and amounts render as
    real dates/numbers in Sheets, not plain text.

    Args:
        date: Expense date, e.g. "2026-09-22".
        submitted_by: Name of the person submitting the expense.
        category: Expense category, e.g. "Supplies".
        amount: Expense amount as a number, e.g. 42.50.
        payee: Who was paid.
        payment_method: How it was paid, e.g. "Debit Card".
        approved_by: Name of the approver.
        notes: Free-text notes.
        receipt_ref: A reference to the receipt (e.g. its file path in
            the treasurer-wiki git repo).

    Returns:
        A dict with:
          success (bool): whether the row was appended.
          row_number (int | None): the 1-indexed sheet row the data
              landed on, if determinable.
          error (str | None): human-readable error message, or null on
              success.
    """
    return append_expense_row_impl(
        date, submitted_by, category, amount, payee, payment_method, approved_by, notes, receipt_ref,
    )


if __name__ == "__main__":
    try:
        load_config()
    except ConfigError as exc:
        print(f"google-workspace server: {exc}", file=sys.stderr)
        sys.exit(1)
    mcp.run()
