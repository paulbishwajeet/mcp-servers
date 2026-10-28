#!/usr/bin/env python3
"""Call one karaoke-tools tool outside MCP and print its JSON result.

Usage:
  venv/bin/python try_tool.py <tool_name> '<json args>'
  venv/bin/python try_tool.py --list

Examples:
  venv/bin/python try_tool.py catalog_search '{"mood": ["romantic"], "limit": 5}'
  venv/bin/python try_tool.py karaoke_find '{"title": "Pal Pal Dil Ke Paas", "film": "Blackmail"}'

Env vars (WIKI_ROOT, SEED_ROOT, MEDIA_ROOT, FFMPEG_PATH, ...) are read the
same way as the server. Point WIKI_ROOT at a scratch copy of the wiki when
experimenting: write tools change files for real.
"""

import json
import sys

import server


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        print("\n".join(sorted(server.TOOLS)))
        return 0
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in server.TOOLS:
        print(__doc__, file=sys.stderr)
        print("Tools: " + ", ".join(sorted(server.TOOLS)), file=sys.stderr)
        return 2
    try:
        args = json.loads(sys.argv[2]) if len(sys.argv) == 3 else {}
    except json.JSONDecodeError as e:
        print(f"Bad JSON args: {e}", file=sys.stderr)
        return 2
    if not isinstance(args, dict):
        print("JSON args must be an object.", file=sys.stderr)
        return 2
    server.media_root().mkdir(parents=True, exist_ok=True)
    try:
        result = server.TOOLS[sys.argv[1]](**args)
    except TypeError as e:  # wrong argument names
        print(f"Bad arguments: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
