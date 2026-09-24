# google-workspace

A Google Sheets MCP (Model Context Protocol) server. It lets an agent
append expense rows to a specific Google Sheet, authenticating as a
**service account** rather than an OAuth user login -- so it can run
unattended (no browser sign-in flow, no token refresh babysitting).

Built for a Hermes Agent nonprofit-treasurer profile, but it's a
general-purpose tool: any Hermes profile (or any MCP client) that needs
to write to one fixed spreadsheet can use it.

Receipts themselves are **not** stored by this server -- they live in
the `treasurer-wiki` git repo. The `Receipt Ref` column is a reference
to that file's path in the repo (e.g. `receipts/2026/office-depot.jpg`),
not a Drive link.

## What it exposes

One MCP tool:

```
append_expense_row(
    date: str,
    submitted_by: str,
    category: str,
    amount: float,
    payee: str,
    payment_method: str,
    approved_by: str,
    notes: str,
    receipt_ref: str,
) -> dict
```

Appends one row to the **first tab** of the spreadsheet at
`TREASURER_SPREADSHEET_ID`, in exactly this column order: `Date`,
`Submitted By`, `Category`, `Amount`, `Payee`, `Payment Method`,
`Approved By`, `Notes`, `Receipt Ref`. Uses the Sheets API
`spreadsheets.values.append` with `valueInputOption="USER_ENTERED"`, so
dates and amounts land as real dates/numbers in Sheets, not plain text.

Returns:

```json
{"success": true, "row_number": 42, "error": null}
```

On any failure (missing env vars, bad credentials, spreadsheet not
shared with the service account, invalid input, etc.) you get back
`success: false` and a human-readable `error` string -- this server
never raises an unhandled exception through the MCP layer.

## Required environment variables

Read and validated at server startup; the server exits immediately with
a clear message if any are missing.

| Variable | Meaning |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Absolute path to the service account key JSON file. |
| `TREASURER_SPREADSHEET_ID` | The target spreadsheet's ID (the long ID in its URL). |

The key file's contents are never logged or returned by any tool -- only
its path is read from the environment.

## GCP / service account prerequisites

(Brief -- assumes you already know how to create a GCP project and
service account.)

1. Enable the **Google Sheets API** on the GCP project the service
   account belongs to.
2. Create (or reuse) a service account, and generate a JSON key for it.
   Keep that key file outside version control (see below).
3. Share the target **spreadsheet** with the service account's email
   address (looks like `name@project-id.iam.gserviceaccount.com`) as
   **Editor**.

Without step 3, every call will fail with a clear "permission denied --
share as Editor" error rather than a cryptic 403.

## Install

### Python virtual environment

This server uses its own dedicated venv, independent of Hermes Agent's
own environment, so it stays fully portable.

```bash
cd google-workspace
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(Optional, for running the test suite: `pip install -r
tests/requirements-test.txt`.)

## Standalone smoke test (no Hermes involved)

Before wiring this into any agent, confirm it works on its own.

### A. Set the environment variables

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="/absolute/path/to/secrets/treasurer-service-account.json"
export TREASURER_SPREADSHEET_ID="1AbC...xyz"          # from the sheet's URL
```

### B. Quick CLI check of the core function

With the venv active, from inside `google-workspace/`:

```bash
python3 -c "
from server import append_expense_row_impl
result = append_expense_row_impl(
    '2026-09-22', 'Alex', 'Supplies', 42.50, 'Office Depot',
    'Debit Card', 'Jamie', 'Printer paper', 'receipts/2026/office-depot.jpg',
)
print(result)
"
```

You should see `{'success': True, 'row_number': <n>, 'error': None}` and
a new row in the spreadsheet.

### C. Run it as an actual MCP stdio server

```bash
pip install "mcp[cli]"   # adds the `mcp` CLI/inspector, if not already present
mcp dev server.py
```

This opens the MCP Inspector in your browser, where you can call
`append_expense_row` directly and see the raw JSON response -- the same
shape any Hermes profile will get. (Make sure the environment variables
are exported in the same shell before running `mcp dev`.)

### D. Run the test suite

```bash
pip install -r tests/requirements-test.txt
pytest
```

All Google API calls are mocked in `tests/test_server.py` -- the suite
never hits the real Sheets API and never needs a real service account
key.

### E. Check the log

After a call or two:

```bash
cat logs/google_workspace_server.log
```

You should see one line per call with the outcome and non-sensitive
identifiers (row numbers) -- never the service account key contents, and
never spreadsheet field values.

## Wiring into a Hermes Agent profile

Once you're satisfied it works standalone, add it to the profile's
`~/.hermes/profiles/<profile>/config.yaml` under `mcp_servers:`:

```yaml
mcp_servers:
  google_workspace:
    command: "/absolute/path/to/hermes-mcp-servers/google-workspace/venv/bin/python"
    args: ["/absolute/path/to/hermes-mcp-servers/google-workspace/server.py"]
    env:
      GOOGLE_SERVICE_ACCOUNT_JSON: "/absolute/path/to/secrets/treasurer-service-account.json"
      TREASURER_SPREADSHEET_ID: "1AbC...xyz"
```

Use the venv's own `python` binary (not your system Python) so the
pinned dependencies in `requirements.txt` are the ones actually used.

## Security notes

- **Never commit the service account key file.** Keep it outside this
  repo, or in a local `secrets/` folder -- either way, make sure your
  `.gitignore` covers it (this project's `.gitignore` already excludes
  `secrets/` and `*.json`, but double-check before your first commit if
  you keep the key anywhere under this repo).
- The service account only has access to what you explicitly share with
  it (one spreadsheet) -- it is not a domain-wide credential.
