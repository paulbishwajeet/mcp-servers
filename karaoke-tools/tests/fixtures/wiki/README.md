# KaraokeGayak repertoire

Neel's karaoke repertoire: mostly Hindi film songs from Kishore Kumar to Arijit Singh, including female-original songs and duets, sung over karaoke tracks (YouTube, mostly the Anil Chauhan Karaoke channel).

This repo is the **single source of truth** and is designed to be read by any agent or tool, not just the Hermes `karaoke-gayak` profile. Plain files only: CSV, text and Markdown. No database, no audio.

## If you are an agent bootstrapping from this repo

1. Read `SCHEMA.md` completely. It defines every file, column and allowed value.
2. Load `catalog/songs.csv` for anything about songs. Filter it; don't guess from your own knowledge when the catalog has the answer.
3. Lyrics are in `lyrics/<song_id>.txt`. Event history is in `events/`.
4. Suggest `status: ready` songs first. Others can be suggested but must be labelled as not yet practiced.
5. Write back only in the formats `SCHEMA.md` defines, and log each change in `log.md`.

## What Neel uses it for

- Theme-based set lists for shows and events (solo and duets).
- Mood-based song picks during events ("slow", "peppy", "romantic", "old", "new").
- Lining up the next song by tempo, energy and mood.
- Hinglish shayari or spoken intros before a song, based on its lyrics.
- Karaoke links and lyrics on demand.
- Event history: what was sung where, and what stood out.

## Quick stats

Kept up to date by the agent after bulk changes.

- Songs: 0 (ready: 0, practicing: 0, wishlist: 0)
- Events logged: 0
- Last updated: -
