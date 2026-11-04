#!/usr/bin/env python3
"""One-time migration of the repertoire's catalog/songs.csv to the current
SCHEMA.md header (adds my_rating, rated_on). Safe to run again: it does
nothing when the file is already current.

Stop the Hermes gateway first: a karaoke-tools process started before the
upgrade still expects the old header.

Usage: WIKI_ROOT=... venv/bin/python migrate.py
"""

import json
import sys

import catalog
from common import SchemaError


def main() -> int:
    try:
        result = catalog.migrate_songs_csv()
    except SchemaError as e:
        print(json.dumps({"success": False, "error": str(e)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
