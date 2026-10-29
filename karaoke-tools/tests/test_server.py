"""MCP protocol over stdio: the server starts with launchd's minimal PATH,
lists every tool, and round-trips a call. Also: no tool raises."""

import asyncio
import json
import sys

import pytest

import server
from conftest import ROOT

EXPECTED_TOOLS = sorted([
    "catalog_search", "catalog_get", "catalog_upsert", "catalog_stats",
    "review_add", "review_list", "review_resolve", "lyrics_save", "lyrics_get", "event_apply",
    "scan_folder", "analyze_audio",
    "channel_index_refresh", "karaoke_find", "youtube_info", "youtube_fetch_audio",
    "cleanup_job", "check_links", "playlist_list", "playlist_triage",
    "lyrics_search", "lyrics_from_url", "song_lookup",
])


def _payload(result) -> dict:
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if data:
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def test_registry_matches_expected():
    assert sorted(server.TOOLS) == EXPECTED_TOOLS


def test_mcp_protocol_lists_tools_and_round_trips(wiki, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    media = tmp_path / "fresh-media"  # created on startup
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        env={"WIKI_ROOT": str(wiki), "MEDIA_ROOT": str(media), "PATH": "/usr/bin:/bin"},
        cwd=str(ROOT),
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                up = await session.call_tool("catalog_upsert", {"row": {
                    "title": "Pal Pal Dil Ke Paas", "year": 1973, "mood": ["romantic"]}})
                search = await session.call_tool("catalog_search", {"mood": "romantic"})
                bad = await session.call_tool("youtube_info", {"url": "https://evil.com/x"})
                return sorted(t.name for t in tools.tools), _payload(up), _payload(search), _payload(bad)

    names, up, search, bad = asyncio.run(run())
    assert names == EXPECTED_TOOLS
    assert up["success"] and up["song_id"] == "pal-pal-dil-ke-paas-1973"
    assert search["rows"][0]["song_id"] == "pal-pal-dil-ke-paas-1973"
    assert bad["error_code"] == "domain_not_allowed"
    assert media.is_dir()


def test_tools_never_raise(wiki, monkeypatch):
    import catalog

    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalog, "load_songs", explode)
    r = server.TOOLS["catalog_search"]()
    assert r["success"] is False and r["error_code"] == "internal_error"


@pytest.mark.parametrize("name,args", [
    ("catalog_get", {"ref": ""}), ("review_resolve", {"row_number": "abc"}),
    ("lyrics_get", {"song_id": "nope"}), ("event_apply", {"event_file": "x.txt"}),
    ("karaoke_find", {"title": ""}), ("lyrics_search", {"title": ""}),
    ("song_lookup", {"title": ""}), ("analyze_audio", {"path": "../x.mp3"}),
    ("catalog_upsert", {"row": {}}), ("check_links", {"limit": "x"}),
])
def test_bad_input_fails_soft(wiki, name, args):
    r = server.TOOLS[name](**args)
    assert r["success"] is False and r["error_code"] and r["error"]
