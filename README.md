# hermes-mcp-servers

A collection of custom, local-first MCP (Model Context Protocol) servers
built for use with [Hermes Agent](https://github.com/) profiles
(`~/.hermes/profiles/<profile>/config.yaml` -> `mcp_servers:`), and
generally reusable with any MCP-compatible client.

Each subfolder is a standalone server with its own venv, dependencies,
and README -- add the ones you need to any agent profile's config.

## Servers

- **[local-ocr](local-ocr/)** -- fully offline OCR for receipt/invoice
  images and PDFs via Tesseract. No network calls, no cloud vision APIs;
  only plain extracted text ever leaves the server.

More servers will be added here over time.
