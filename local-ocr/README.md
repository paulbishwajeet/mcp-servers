# local-ocr

A fully local, offline OCR MCP (Model Context Protocol) server. It reads
text out of receipt/invoice photos and PDFs on-device using
[Tesseract](https://github.com/tesseract-ocr/tesseract), and hands back
only the extracted plain text to whatever agent asked for it.

## Why local-only

Receipts and invoices carry financial and personal data. This server:

- Never calls a cloud vision or LLM API to read an image.
- Never makes any network request of any kind, anywhere in its code path
  (no telemetry, no "check for updates," nothing that phones home).
- Never returns image bytes to the caller -- only plain extracted text.
- Logs only the file name and basic stats (page count, character count)
  to a local log file for debugging -- never the extracted text itself,
  since that text may contain sensitive financial/personal data.

This makes it safe to point at real receipts from a grocery list manager,
a rental property tracker, a nonprofit treasurer's ledger, or any other
agent profile that needs to read paper documents.

It is a general-purpose tool, not tied to any one use case -- any Hermes
Agent profile can add it to `mcp_servers:` and get the same capability.

## What it exposes

One MCP tool:

```
ocr_extract_text(file_path: str, language: str = "eng") -> dict
```

- `file_path`: absolute path to a `.jpg`, `.jpeg`, `.png`, `.webp`,
  `.bmp`, `.tiff`, or `.pdf` file.
- `language`: a Tesseract language code (e.g. `"eng"`, `"eng+fra"`).
  Must match a language pack you have installed.

Returns a dict:

```json
{
  "success": true,
  "text": "WHOLE FOODS MARKET\n123 Main St\n...",
  "page_count": 1,
  "char_count": 812,
  "error": null
}
```

On any failure (file missing, unsupported extension, corrupt file,
Tesseract/Poppler not installed, etc.) you get back `success: false`,
`text: ""`, and a human-readable `error` string -- the server never
raises an unhandled exception through the MCP layer.

## Install

### 1. Homebrew dependencies

```bash
brew install tesseract
brew install poppler   # needed for PDF -> image conversion (pdf2image)
```

Verify:

```bash
tesseract --version
pdftoppm -v
```

### 2. Python virtual environment

This server uses its own dedicated venv, independent of Hermes Agent's
own environment, so it stays fully portable.

```bash
cd local-ocr
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(Optional, for running the test suite: `pip install -r
tests/requirements-test.txt`.)

## Standalone smoke test (no Hermes involved)

Before wiring this into any agent, confirm it works on its own.

### A. Quick CLI check of the core OCR function

With the venv active, from inside `local-ocr/`:

```bash
python3 -c "
from server import run_ocr
result = run_ocr('/absolute/path/to/a/receipt.jpg')
print(result)
"
```

You should see a dict with `success: true` and a non-trivial `char_count`.
If `char_count` is at or near zero, the scan is likely too blurry/dark
for Tesseract, not a bug in the server.

Try a PDF the same way:

```bash
python3 -c "
from server import run_ocr
result = run_ocr('/absolute/path/to/an/invoice.pdf')
print(result)
"
```

### B. Run it as an actual MCP stdio server

`mcp` ships a dev inspector you can use to talk to the server over stdio
like a real MCP client would:

```bash
pip install "mcp[cli]"   # adds the `mcp` CLI/inspector, if not already present
mcp dev server.py
```

This opens the MCP Inspector in your browser, where you can call
`ocr_extract_text` directly with a `file_path` and `language` and see the
raw JSON response -- the same shape any Hermes profile will get.

### C. Run the test suite

```bash
pip install -r tests/requirements-test.txt
pytest
```

The missing-file and unsupported-extension tests always run. The real
image/PDF extraction tests auto-skip if you haven't dropped a sample file
into `tests/fixtures/` yet, and also auto-skip if `tesseract` isn't on
your `PATH`. Drop a real (or scrubbed) sample receipt image and/or PDF
into `tests/fixtures/` to exercise them -- that folder is gitignored so
sample receipts never get committed.

### D. Check the log

After a call or two:

```bash
cat logs/ocr_server.log
```

You should see one line per call with the file name, page count, and
character count -- never the extracted text.

## Wiring into a Hermes Agent profile

Once you're satisfied it works standalone, add it to any profile's
`~/.hermes/profiles/<profile>/config.yaml` under `mcp_servers:`:

```yaml
mcp_servers:
  local_ocr:
    command: "/absolute/path/to/hermes-mcp-servers/local-ocr/venv/bin/python"
    args: ["/absolute/path/to/hermes-mcp-servers/local-ocr/server.py"]
```

Use the venv's own `python` binary (not your system Python) so the
pinned dependencies in `requirements.txt` are the ones actually used.

This same block works for any Hermes profile -- grocery list manager,
rental property manager, nonprofit treasurer, or anything else you add
later. There's nothing profile-specific about this server; it's a
general local-OCR capability any agent can opt into.

## Extending this server

`server.py` keeps the OCR implementation (`run_ocr`) separate from the
`@mcp.tool()`-decorated wrapper (`ocr_extract_text`) specifically so a
second tool -- e.g. a receipt-field parser that calls `run_ocr()` and
then regexes out vendor/total/date -- can be added as another
`@mcp.tool()` function in this same file without restructuring what's
already here.
