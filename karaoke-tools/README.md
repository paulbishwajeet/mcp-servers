# karaoke-tools

A local MCP server for the **KaraokeGayak** Hermes profile (`karaoke-gayak`).
It is deterministic tooling with no LLM calls. It manages the repertoire wiki's
files, measures audio, finds karaoke links on YouTube, fetches lyrics and looks up
soundtrack metadata on Wikipedia. The Hermes agent (Haiku 4.5) does the
identification, tagging and writing, and calls these tools to do it.

```
"suggest a monsoon duet"   -> catalog_search(theme=rain-monsoon, voice_type=duet-mf)
"add Pal Pal Dil Ke Paas"  -> song_lookup -> karaoke_find -> youtube_fetch_audio
                              -> analyze_audio -> cleanup_job -> catalog_upsert
                              -> lyrics_search -> lyrics_save
"lyrics please"            -> lyrics_get (chunks ready for Telegram)
after a show               -> event_apply(events/2026-11-01-diwali.md)
```

## The repertoire wiki

The server works on Neel's karaoke repertoire: mostly Hindi film songs from
Kishore Kumar to Arijit Singh, including female-original songs and duets, sung
over karaoke tracks (YouTube, mostly the Anil Chauhan Karaoke channel). It
lives in its own git repo at `WIKI_ROOT`.

That repo is the **single source of truth**. It is designed to be read by any
agent or tool, not just the Hermes `karaoke-gayak` profile. It holds plain
files only: CSV, text and Markdown, with no database and no audio.

| Path | What |
|---|---|
| `SCHEMA.md` | Every file, column and allowed value. Changes need Neel's approval. |
| `catalog/songs.csv` | One row per song |
| `catalog/review.csv` | Problems waiting for Neel |
| `lyrics/<song_id>.txt` | Hinglish lyrics (plus chords if found) |
| `events/<YYYY-MM-DD>-<slug>.md` | One file per show, from `events/_TEMPLATE.md` |
| `index/anilchauhan.csv` | Cached video list of the Anil Chauhan Karaoke channel |
| `log.md` | Append-only change log |
| `feedback/relations.csv` | 👍/👎 feedback on suggestion pairs (append-only) |
| `ranking.yaml` | Weights for `suggest` (git-tracked, tune freely) |
| `README.md` | Overview, with a "Quick stats" block that `catalog_stats` keeps current |

What Neel uses it for:

- Theme-based set lists for shows and events (solo and duets).
- Mood-based song picks during events ("slow", "peppy", "romantic", "old", "new").
- Lining up the next song by tempo, energy and mood.
- Hinglish shayari or spoken intros before a song, based on its lyrics.
- Karaoke links and lyrics on demand.
- Event history: what was sung where, and what stood out.

The wiki's rules for any agent bootstrapping from it, and how this server
enforces each one:

1. **Read `SCHEMA.md` completely.** This server mirrors it in `vocab.py`, and
   a test fails if the two drift apart.
2. **Load `catalog/songs.csv` for anything about songs.** Filter it with
   `catalog_search` and `catalog_get`; don't guess from model knowledge when
   the catalog has the answer.
3. **Lyrics are in `lyrics/<song_id>.txt`, event history in `events/`.** See
   `lyrics_get` and `event_apply`.
4. **Suggest `status: ready` songs first.** `catalog_search` sorts them
   first. Others can be suggested but must be labelled as not yet practiced.
5. **Write back only in SCHEMA.md's formats, and log each change in
   `log.md`.** Every write tool validates against the schema and appends to
   `log.md` itself.

## Design rules

- **The CSV is the source of truth.** The wiki's `SCHEMA.md` defines every
  file, column and allowed value. `vocab.py` mirrors it, and `tests/test_vocab.py`
  parses `SCHEMA.md` and fails if the two drift apart. Every write is validated
  against the schema. Unknown enum values are rejected and the error lists the
  allowed ones. Each write replaces the whole file atomically (temp file +
  `os.replace`), keeps `songs.csv` sorted by `song_id`, and appends a line to
  `log.md`.
- **Path safety.** Audio paths must resolve inside `SEED_ROOT` or `MEDIA_ROOT`.
  Wiki reads and writes stay inside `WIKI_ROOT`. Any `..` is rejected outright,
  and symlinks are resolved first, so a symlink pointing outside is rejected
  too.
- **Domain allowlists.** YouTube tools accept only `youtube.com`,
  `www.youtube.com`, `m.youtube.com`, `music.youtube.com` and `youtu.be`, with
  exact host match. Lyrics pages are accepted only from `genius.com`,
  `tabs.ultimate-guitar.com`, `hindigeetmala.net`, `geetmanjusha.com`,
  `lyricsindia.net`, `musixmatch.com`, `smule.com` and `LYRICS_EXTRA_DOMAINS`.
  The allowlist is checked again on every redirect hop.
- **Fetched text is data.** Lyrics and page text are returned as-is in fields
  and never interpreted.
- **Fail soft.** Every tool returns `{success, error_code, error, ...}` and
  never raises. In batch tools such as `lyrics_search` and `check_links`, one
  bad item never breaks the batch.
- **Compact outputs.** Search tools return trimmed rows by default to keep
  Haiku's per-turn budget low.
- **No audio leaves the Mac.** `youtube_fetch_audio` writes to `MEDIA_ROOT` for
  analysis only. The agent never sends audio files.

## Files

| File | What |
|---|---|
| `server.py` | MCP tool definitions (thin), docstrings the agent reads |
| `catalog.py` | songs.csv / review.csv read-validate-write, lyrics files, events, stats |
| `vocab.py` | Allowed values and column types, mirrored from SCHEMA.md |
| `audio.py` | `scan_folder`, `analyze_audio` (librosa) |
| `youtube.py` | Channel index, karaoke search, info, audio fetch, cleanup, link checks, playlists, mixes (yt-dlp) |
| `lyrics.py` | Genius, Ultimate Guitar, hindigeetmala, geetmanjusha, lyricsindia, musixmatch, Smule extraction and checks |
| `lookup.py` | Wikipedia soundtrack lookup |
| `suggest.py` | `suggest`, `suggest_more`: candidates, ranking.yaml scoring, numbering |
| `feedback.py` | Relations, ratings, feedback polls and collection, keyboards, `feedback_tap`, session state |
| `tg.py` | Telegram Bot API send side only: `sendMessage`, `sendPoll`, `stopPoll` (to `TELEGRAM_CHAT_ID`) |
| `migrate.py` | One-time songs.csv upgrade (adds `my_rating`, `rated_on`) |
| `common.py` | Env config, result shape, path and URL checks |
| `try_tool.py` | CLI: run any tool outside MCP |

## Tools

Every result also carries `success`, `error_code` and `error`. The examples
below are trimmed.

### Catalog

**`catalog_search(text="", status=[], voice_type=[], tempo_class=[], era=[], genre=[], mood=[], theme=[], situation=[], singer="", min_energy=1, max_energy=5, exclude_ids=[], limit=15, full=false)`**

List filters match when any value overlaps. A filter also accepts a single
string, or values joined with `;` or `,`. `text` is a rapidfuzz match (WRatio ≥ 70)
over title, film and singers. Once `min_energy`/`max_energy` is narrowed,
songs with no energy value are excluded. Rows are sorted `ready`, then
`practicing`, then the rest; within each group by `crowd_score`, then
`singalong`.

```json
{"total": 3, "returned": 3, "rows": [
  {"song_id": "pal-pal-dil-ke-paas-1973", "title": "Pal Pal Dil Ke Paas", "film": "Blackmail",
   "year": "1973", "singers": "Kishore Kumar", "voice_type": "solo-male", "tempo_class": "slow",
   "bpm": "61.5", "energy": "3", "mood": "romantic;longing",
   "karaoke_url": "https://www.youtube.com/watch?v=pw6r-izZArA", "status": "ready",
   "my_key_change": "-2"}]}
```

An unknown filter value returns
`{"success": false, "error_code": "invalid_filter", "error": "mood: dreamy not allowed. Allowed: romantic, happy, ..."}`.

**`catalog_get(ref)`**: an exact `song_id` returns `{"found": true, "row": {...all 37 columns}}`.
Anything else returns the top 5 fuzzy matches:
`{"found": false, "matches": [{"song_id": "pal-pal-dil-ke-paas-1973", "title": "Pal Pal Dil Ke Paas", "film": "Blackmail", "year": "1973", "score": 90.0}]}`.

**`catalog_upsert(row)`**: inserts or merges.

- **Ids.** Without a `song_id`, the id is the title slug plus year, with
  `-2`, `-3` on collision. Ids never change once set.
- **Merging.** With an existing `song_id`, only the fields you pass change.
- **Field formats.** Multi-value fields take a list or a `;`-joined string.
  `era` is filled from `year`.
- **Defaults for new songs.** `status` is `practicing` for
  `added_from=local-mp3` and `wishlist` otherwise. `added_on` is today.
- **Placeholders.** `N/A`, `none` and `?` are stored as empty (empty means
  unknown), and the result includes a warning.
- **Duplicates.** Adding a song whose title and film match an existing row
  returns a "Possible duplicate of: ..." warning.

```json
{"action": "add", "song_id": "pal-pal-dil-ke-paas-1973", "changed": ["song_id", "title", "..."],
 "row": {"...": "..."}, "warnings": []}
{"success": false, "error_code": "validation_failed",
 "errors": ["voice_type: 'male-solo' is not allowed. Allowed: solo-male, solo-female, duet-mf, duet-mm, duet-ff, group."]}
```

**`catalog_stats()`** returns counts and refreshes the README's "Quick stats" block. The
README is rewritten, and a log line added, only when the numbers changed.

```json
{"songs": 4, "by_status": {"practicing": 1, "ready": 2, "wishlist": 1}, "by_voice_type": {"...": 1},
 "by_era": {"1970s": 1}, "by_tempo_class": {"slow": 1}, "open_reviews": 1, "events_logged": 1,
 "missing_lyrics": 4, "missing_lyrics_ids": ["..."], "missing_links": 4, "missing_links_ids": ["..."],
 "readme_updated": true}
```

**`review_add(song_id, source_ref, problem, details)`** returns
`{"row_number": 3, "row": {...}, "song_flagged": true}` and sets
`needs_review=true` on the song.

**`review_list(open_only=true)`** returns `{"count": 1, "items": [{"row_number": 3, "logged_at": "2026-10-27 23:10", ...}]}`.

**`review_resolve(row_number)`** marks the row `resolved=true`; rows are never
deleted. It clears the song's `needs_review` when no other reviews are open for
that song.

**`lyrics_save(song_id, text, source, url="", chords="")`** writes
`lyrics/<song_id>.txt` in SCHEMA.md's header format and sets `lyrics_file`,
`lyrics_source` and `lyrics_url` on the row. A `# ` header pasted back into the
text is dropped, and runs of blank lines are collapsed to one.

```
# Pal Pal Dil Ke Paas
# Blackmail (1973) | Kishore Kumar | source: genius | https://genius.com/...

[Chorus]
Pal-pal dil ke paas tum rehti ho
...
```

**`lyrics_get(song_id)`** returns
`{"title": "Pal Pal Dil Ke Paas", "meta": "Blackmail (1973) | ...", "text": "...", "chords": "", "chunks": ["Pal Pal Dil Ke Paas\n\n[Chorus]\n..."]}`.
`chunks` are split at blank-line verse boundaries, each under 3,500 characters.
A single verse that is too long is split by lines, and a single line that is
too long by characters.

**`event_apply(event_file)`** parses the `## Set list` table of
`events/<file>.md` (header columns `song_id` and `Outcome`). For the songs in
that set it recomputes `times_performed`, `crowd_score` and `last_performed`
from **all** event files, with standout = 3, good = 2, weak = 1, averaged.
Re-running it changes nothing. The template placeholder
`standout / good / weak` counts as a performance with no score.

```json
{"event_file": "events/2026-11-01-diwali.md", "event_date": "2026-11-01", "set_list_rows": 4,
 "updated": [{"song_id": "pal-pal-dil-ke-paas-1973", "title": "Pal Pal Dil Ke Paas",
              "changes": {"times_performed": {"from": "", "to": "1"}, "crowd_score": {"from": "", "to": "3"},
                          "last_performed": {"from": "", "to": "2026-11-01"}}}],
 "unmatched": [{"song_id": "not-in-catalog-1999", "title": "Mystery"}], "warnings": []}
```

### Local audio

**`scan_folder(subpath="", offset=0, limit=50)`** lists `.mp3` files under
`SEED_ROOT` recursively. Hidden files are skipped, and symlinks pointing
outside `SEED_ROOT` are ignored. The fingerprint is
`sha1(size + first 1 MB)`. To find duplicates, only files that share a size
with another file are fingerprinted, so large folders stay fast.

```json
{"total": 212, "offset": 0, "limit": 50, "next_offset": 50,
 "files": [{"path": "Kishore/pal pal.mp3", "file_name": "pal pal.mp3", "size_bytes": 7732844,
            "fingerprint": "3b1f…", "duration_sec": 322.1, "id3_title": "Pal Pal Dil Ke Paas",
            "id3_artist": "Kishore Kumar", "id3_album": "Blackmail", "duplicate": false}],
 "duplicates": [["Kishore/pal pal.mp3", "copy.mp3"]]}
```

**`analyze_audio(path)`** takes a path relative to `SEED_ROOT`, or an absolute
path inside `SEED_ROOT` or `MEDIA_ROOT`. It decodes mono at 22,050 Hz, at most
the first 240 s. Real result for the Anil Chauhan karaoke track of *Pal Pal
Dil Ke Paas*:

```json
{"duration_sec": 322, "decoder": "soundfile", "bpm": 123.1, "bpm_candidates": [123.1, 61.5, 246.2],
 "musical_key": "F# major", "key_confidence": 0.91,
 "key_alternatives": [{"key": "C# major", "r": 0.595}, {"key": "F# minor", "r": 0.589}, {"key": "A# minor", "r": 0.585}],
 "energy": 3, "percussive_ratio": 0.366, "onset_strength": 1.51, "rms_db": -13.1, "onset_rate": 3.5,
 "intro_sec": 13.8, "analyzed_sec": 240.0}
```

The output fields are computed as follows:

- **`bpm`.** From `librosa.beat.beat_track`, refined from the mean
  inter-beat interval of the tracked beats. At hop 512, beat_track alone can
  only return 117.5 or 123.0 near 120. `bpm_candidates` lists the value, its
  half and its double; the agent picks the felt tempo. The ballad above feels
  like 61.5.
- **`musical_key`.** Mean `chroma_cqt` correlated with the Krumhansl-Kessler
  major and minor profiles for all 24 keys. `key_confidence` combines the
  winning correlation with its margin over the runner-up.
- **`intro_sec`.** Seconds until 80% of the next 2 s is above half the
  track's median RMS.

**Energy calibration.** Karaoke masters are uniformly loud (mean RMS −8 to
−16 dBFS) and have dense onsets, so loudness and onset rate barely separate
ballads from dance numbers. I hand-labelled 14 Hindi karaoke tracks, from
*Sajni* (soft) to *Aaj Ki Raat* (dance). Two features ranked them best:

| Feature | Maps 0 → 1 over |
|---|---|
| percussive share of the signal (HPSS, middle 60 s) | 0.20 → 0.55 |
| mean onset strength | 1.2 → 1.95 |

`energy = 1 + count(mean of the two ≥ 0.2, 0.4, 0.6, 0.8)`, and silence is
always 1. On the calibration set this gave:

| Energy | Tracks |
|---|---|
| 1 | Sajni |
| 2 | Ye Raatein, Tum Se Hi |
| 3 | Kuchh Na Kaho, Ajeeb Daastaan, Phoolon Ke Rang Se |
| 4 | Aaj Ki Raat, Bole Chudiyan, You Are My Sonia, Mana Ho Tum |
| 5 | Pehla Nasha medley |

Treat it as a measurement for the agent to sanity-check against what it
knows about the song. The raw values are returned so the constants at the
top of `audio.py` can be re-tuned.

MP3 decoding uses soundfile. Its wheel bundles libsndfile 1.2.2, which reads
MP3 with no ffmpeg and no PATH lookup. If soundfile fails, the server falls
back to ffmpeg via the explicit `FFMPEG_PATH`.

### YouTube (yt-dlp, no login, no API key)

**`channel_index_refresh(channel_url=PREFERRED_CHANNEL_URL)`** does a flat
listing of the channel's `/videos` tab, without downloading anything, into
`index/anilchauhan.csv`. Live run: **2,006 videos in 31 s**. An empty listing
never overwrites an existing index.

```json
{"channel": "ANIL CHAUHAN KARAOKE", "channel_url": "https://www.youtube.com/@AnilChauhankaraoke/videos",
 "rows": 2006, "previous_rows": 0, "index_file": "index/anilchauhan.csv"}
```

**`karaoke_find(title, film="", singers="", max_results=5)`** works in three steps:

1. **Normalise titles.** Lowercase and NFKC (the channel writes
   "𝕀𝕞𝕡𝕣𝕠𝕧𝕖𝕕" in maths letters). Then strip bracketed parts, Devanagari,
   punctuation and noise words: *karaoke, full, with (scrolling) lyrics, eng,
   hindi, hq, hd, original, clean, track, improved/renewed/recreated, video,
   official*.
2. **Score against the index.** Score = 85% `token_set_ratio` (the query's
   words all present in the title gives 100) + 15% plain `ratio` (penalises
   titles that merely contain the query). These versions lose points unless
   the query asks for them:
   - −25: cover, unplugged, female (version), lofi, remix, reprise, slowed,
     reverb, instrumental, mashup, medley.
   - −5: abridged or short versions.
3. **Search if needed.** A strong match is **≥ 85**: "Pal Pal Dil Ke Paas"
   against "Pal Pal Dil Ke Paas Tum Rehti Ho" scores 96, and different songs
   that share words score below 80. Without a strong match, the tool runs
   `ytsearch10:"<title> <film> karaoke"` (flat) and ranks the results:
   1. The preferred channel first, if its title similarity is ≥ 70 with no
      penalty.
   2. Then title similarity.
   3. Then whether the title contains "karaoke".

```json
{"used_search": false, "candidates": [
  {"url": "https://www.youtube.com/watch?v=pw6r-izZArA",
   "title": "Pal Pal Dil Ke Paas Tum Rehti Ho - Full Karaoke With Scrolling Lyrics Eng & हिंदी",
   "channel": "ANIL CHAUHAN KARAOKE", "duration_sec": 323, "score": 96.2, "source": "index"},
  {"url": "https://www.youtube.com/watch?v=chKG5o_7xCw",
   "title": "Pal Pal Dil Ke Paas Tum Rehti Ho - (𝔸𝕓𝕣𝕚𝕕𝕘𝕖𝕕 𝕍𝕖𝕣𝕤𝕚𝕠𝕟) Karaoke …",
   "channel": "ANIL CHAUHAN KARAOKE", "duration_sec": 257, "score": 91.2, "source": "index"}]}
```

**`youtube_info(url)`** returns metadata only:
`{"url", "video_id", "title", "channel", "channel_url", "duration_sec", "upload_date", "availability": "public", "description": "<first 1,500 chars>"}`.

**`youtube_fetch_audio(url)`** downloads best audio, converted to MP3, as
`MEDIA_ROOT/<job_id>/audio.mp3`. `job_id` has the same format as
media-download (`YYYYMMDD-HHMMSS-<sha1(url)[:8]>`). The job folder is deleted
on any failure, and videos over `MAX_DURATION_SEC` fail with `too_long`
before anything downloads. Live run: 7.7 MB in 18 s.

```json
{"job_id": "20261027-230134-69f5d31d",
 "audio_path": "/…/media-tmp/20261027-230134-69f5d31d/audio.mp3",
 "url": "https://www.youtube.com/watch?v=pw6r-izZArA", "title": "Pal Pal Dil Ke Paas Tum Rehti Ho - …",
 "channel": "ANIL CHAUHAN KARAOKE", "duration_sec": 322, "upload_date": "20170719", "availability": "public"}
```

**`cleanup_job(job_id)`** has the same id-format and path checks as media-download.
It returns `{"success": true}`, or `{"success": true, "note": "already gone"}`.

**`playlist_list(url, offset=0, limit=50)`** lists a playlist's videos (any
YouTube URL with `list=`). It's a flat listing: ids and titles only, nothing
downloaded. The 146-video playlist below lists in about 3 s. Page with
`offset`/`limit` (max 200).

```json
{"playlist_url": "https://www.youtube.com/playlist?list=PLMJjuIqd6cqlhxmr1jFnHaOj1hlkEbNeo",
 "playlist_title": "karaoke", "total": 146, "offset": 0, "limit": 50, "next_offset": 50,
 "videos": [{"video_id": "…", "url": "https://www.youtube.com/watch?v=…",
             "title": "Kuch Na Kaho Karaoke With Scrolling Lyrics Eng & हिंदी",
             "channel": "…", "duration_sec": 379, "unavailable": false}]}
```

**`playlist_triage(url, offset=0, limit=50)`** sorts a page of a playlist
against the catalog, so the agent gets a short work list instead of raw
titles. For each video it parses the song name from the title. It takes the
first segment split on `|`, `||`, ` - `, `:`, `__`, `(` or `[`, then strips
karaoke noise, version words (unplugged, remix, piano, reprise, key/scale) and
URLs. It records the version separately, then scores the song name against
every catalog title:

- 100 for an exact match.
- 95 when a catalog title of 2+ words starts the song name ("Bole Chudiyan"
  in "Bole Chudiyan Bole Kangna"). A title of 3 words or fewer followed by
  3+ more words scores 80 instead ("Tum Hi Ho" vs "Tum Hi Ho Bandhu Sakha…").
- Otherwise the mean of token-set and token-sort ratio, so spelling variants
  match ("Diwana Hua Badal" ≈ "Deewana Hua Badal": 91) but a short title like
  "Pal" doesn't match every video containing the word.

| Group | Meaning |
|---|---|
| `existing` | A catalog row of the same version scores ≥ 85. Includes `song_id` and `link`: `same`, `alt` (in `karaoke_alt_urls`), `different` or `none`. |
| `new_version` | The song is in the catalog but this video is an unplugged/remix/piano/reprise version without its own row |
| `maybe` | Best score 75–85: the agent should confirm with `catalog_get` |
| `new` | Not in the catalog |
| `medley` | Title says medley/mashup/jukebox/non-stop. Titles with 3+ `\|\|` separators stay in their group with a "may be a medley" note. |
| `unavailable` | Private, deleted or untitled entries |

Live result for the 146-video playlist: 35 existing, 10 new versions, 8 maybe,
83 new, 10 medleys.

```json
{"total": 146, "counts": {"existing": 35, "new_version": 10, "maybe": 8, "new": 83, "medley": 10, "unavailable": 0},
 "existing": [{"video_id": "…", "url": "…", "title": "Soch Na Sake Karaoke", "duration_sec": 283,
               "song_text": "soch na sake", "version": "", "song_id": "soch-na-sake-2016",
               "catalog_title": "Soch Na Sake", "score": 100.0, "link": "different"}],
 "new": [{"video_id": "…", "url": "…", "title": "SAGAR KINARE - KARAOKE || Unplugged | Lower Key | …",
          "duration_sec": 250, "song_text": "sagar kinare", "version": "unplugged"}], "…": "…"}
```

Typical use from Telegram ("add the songs in this playlist"):
1. `playlist_triage` page by page.
2. For `existing` with `link: different`, `catalog_upsert` the new link, as
   `karaoke_url` or into `karaoke_alt_urls`.
3. Run the usual add steps for `new` and `new_version`.
4. Ask about `maybe` and `medley`.

**`youtube_mix(song="", url="", seed="karaoke", source="youtube", limit=25)`**
shows what YouTube considers similar songs. The related-videos API is gone,
but every video has an auto-generated Mix playlist (`list=RD<id>`), and
YouTube Music has a song radio (`list=RDAMVM<id>` on `music.youtube.com`).
yt-dlp flat-lists both without a login. Mixes are endless, so the listing is
capped at `playlistend = limit + 5`; it takes about 1 s per source. Nothing is
downloaded and nothing is written.

1. **Seed.**
   - With `url`, the video in that allowlisted YouTube URL.
   - Otherwise, with `seed="karaoke"` (the default), a
     `ytsearch5:"<song> karaoke"` search. It takes the karaoke video closest
     to the song name: closeness first, then the Anil Chauhan channel, then
     the fewest extra words. Other songs, and unplugged/remix/female/cover
     versions not asked for, are skipped. A karaoke seed gives a mix of
     karaoke tracks, so every entry has a singable link. Tested on Pal Pal
     Dil Ke Paas, Kesariya and Tum Hi Ho: 20 of 20 entries had karaoke links,
     and 9–13 of 20 were already in the catalog, against 6–9 with an
     original seed. It also picked the right song where an original seed
     didn't (Pal Pal landed on the 2019 title track).
   - With `seed="original"`, a `ytsearch5:"<song> song"` search for the
     official upload. Karaoke, cover, instrumental, unplugged, reprise,
     slowed and lofi results are skipped, and label channels win (Saregama,
     T-Series, Sony Music India, Zee Music, Tips, YRF, Shemaroo, Universal
     Music India, Eros Now, Venus).

   The chosen seed is returned so you can see what it picked; pass `url` to be
   exact.
2. **Mix.** `source` is `youtube`, `youtube-music`, or `both` (merged by
   video id; each item records its `sources`). A mix that can't be fetched
   returns `error_code: mix_unavailable`. With `both`, one failing source
   becomes a note and the other is still returned.
3. **Song and film from each video title** (rules, no LLM). Emoji and
   brackets become separators, then the title is split on `|`, ` - `, `: `,
   `~`, `__` and `//`. Noise is stripped: Full Video/Song/Audio, Official
   Video, Lyrical, Lyrics, HD/4K, Karaoke, Title Song, Bonus Track, version
   words, Devanagari repeats, `Song -`/`Full Video:` prefixes. Segments that
   are lists of people are dropped: commas, `&`, `ft.`, or a catalog
   singer/composer/lyricist name, also at the end of a segment.

   The first remaining segment is the song. A film comes from:
   - `From "X"`;
   - film-first `Film - Song` (Shemaroo) or `Film: Song` (T-Series);
   - a segment matching a catalog film;
   - the second segment when a list of people follows it ("Song | Film |
     Actor, Actor").

   Otherwise the film is left empty rather than guessed. The raw title is
   kept on each video.
4. **Clean.** Other uploads of the seed's song are dropped. Duplicates (the
   original and its karaoke, or two channels) merge into one entry with all
   their videos. `kind` is one of:
   - **original**
   - **karaoke**
   - **cover:** unplugged, reprise, remix, slowed, lofi, instrumental, piano
     and so on.
   - **jukebox:** jukebox, medley, mashup, non-stop, "best of", or 15+
     minutes long. Kept but flagged, and never merged.
   - **other:** reaction, interview, trailer, status.

   An entry takes the best kind among its videos. YouTube's order is kept as
   `rank`.
5. **Catalog marks** (read-only). Each `song_title` is scored against
   `songs.csv` with the same matcher as `playlist_triage`, plus the film as
   evidence: same film +10, a different film −5 (parsed films can be wrong, so
   they can't veto a strong title match). A one-word catalog title
   only matches exactly ("Safar" doesn't match "Lo Safar"). A best score
   ≥ 85 sets `in_catalog` (the song_id) and `status`; `match_score` is always
   given.

Live result with `seed="original"`, seeded with the 1973 Shemaroo upload of
*Pal Pal Dil Ke Paas* (`source="both"`, 20 entries, 12 in the catalog):

```json
{"seed": {"video_id": "AMuRRXCuy-4", "title": "Blackmail - Pal Pal Dil Ke Paas Tum Rehti Ho - Kishore Kumar",
          "channel": "Shemaroo", "url": "https://www.youtube.com/watch?v=AMuRRXCuy-4",
          "song_title": "Pal Pal Dil Ke Paas Tum Rehti Ho"},
 "source": "both", "fetched": 37, "returned": 20, "in_catalog": 12,
 "entries": [
  {"rank": 1, "song_title": "O Mere Dil Ke Chain", "film": "", "kind": "original",
   "sources": ["youtube", "youtube-music"], "in_catalog": "o-mere-dil-ke-chain-1972",
   "status": "practicing", "match_score": 100.0,
   "videos": [{"url": "https://www.youtube.com/watch?v=_w14bUcxl1c",
               "title": "O Mere Dil Ke Chain | Full Lyrics Video | Kishore Kumar | Rajesh Khanna",
               "channel": "ChordCanvas", "duration_sec": 274, "kind": "original"}]},
  {"rank": 7, "song_title": "Dekha Ek Khwab", "film": "Silsila", "kind": "original",
   "in_catalog": "dekha-ek-khwab-to-yeh-silsile-hue-1981", "match_score": 90.8, "…": "…"},
  {"rank": 10, "song_title": "Tu Tu Hai Wahi", "film": "Yeh Vaada Raha", "kind": "original",
   "in_catalog": "", "match_score": 61.4, "…": "…"}],
 "notes": []}
```

Mixes are regenerated on every call, so two calls rarely return the same
list.

**`check_links(limit=50, offset=0)`** checks metadata only and never modifies
the catalog. Network failures and rate limits go in `errors`, not `dead`,
because they aren't evidence that the link is dead.

```json
{"checked": 50, "total_with_links": 180, "next_offset": 50,
 "dead": [{"song_id": "…", "title": "…", "url": "…", "status": "private", "error": "Private video…"}],
 "errors": []}
```

`error_code` values for YouTube: `domain_not_allowed`, `invalid_url`,
`private`, `not_found`, `unavailable`, `login_required`, `rate_limited`,
`too_long`, `ffmpeg_not_found`, `download_failed`.

### Lyrics and metadata

**`lyrics_search(title, film="", singers="")`** tries the sources in order
and stops at the first candidate that **passes**. A candidate passes when it
looks like lyrics, has a `devanagari_ratio` of 0.2 or less (Hinglish), and at
least half of the title's words appear somewhere in the text. That last check
rejects mislabelled pages, such as an Ultimate Guitar page titled "Bole
Chudiyan" that holds a different song. If nothing passes, `best` is the most
lyric-like text found, for example Devanagari lyrics the agent can
transliterate and save with `source=transliterated`.

| # | Source | How |
|---|---|---|
| 1 | Genius | API `/search` with `GENIUS_ACCESS_TOKEN`, else the public `genius.com/api/search/multi`. Hits are ranked by title, then artist, with a bonus for "Romanized" and a penalty for lofi/remix/cover; up to 3 pages tried. Lyrics come from the `data-lyrics-container` divs. |
| 2 | Ultimate Guitar | Search page `js-store` JSON, preferring Chords and skipping Pro. The tab is split into lyrics without chords and a chord sheet. |
| 3 | hindigeetmala.net | `search.php?value=`. Lyrics come from `div.song [itemprop=text]`, Hinglish, one `<br>` per line. |
| 4 | geetmanjusha.com | `search?term=`. The page holds the lyrics twice; the Roman `<pre>` is used, not the Devanagari one. |
| 5 | lyricsindia.net | `songs/search?txt=`, ranked by title with a bonus when the result's film matches. `pre#ASCII` is in ITRANS ("jiivan miiThii", "aa.Nkho.n"), converted to Hinglish ("jeevan meethi", "aankhon"). Singer markers like `ki:` are kept. |
| 6 | musixmatch.com | Search needs a login, so the tool tries `/lyrics/<first singer>/<title>` directly. Logged-out pages render lyrics with JavaScript, so in practice this returns `needs_js` or `no_match`. |
| 7 | Smule | Search page, then the song page. Reports `needs_js` when lyrics aren't in the server HTML. |

For sources 3–5, search results are ranked by title similarity. Labels that
contain every title word get +5 and those missing words get −5, so "Pal Pal
Dil Ke Paas, Tum Rahti Ho" beats "Pal Pal Dil Ke"; "version" results are
penalised. Up to 3 pages per source are tried.

Each candidate has a `schema_source`: the SCHEMA.md `lyrics_source` value to
save it with. Every source here has its own value (`genius`, `ultimate-guitar`,
`hindigeetmala`, `geetmanjusha`, `lyricsindia`, `musixmatch`, `smule`). Each attempt has a `status`: `ok`, `blocked` (bot
challenge), `needs_js`, `no_match`, `not_found`, `network_error` or `error`.

Live result:

```json
{"found": true,
 "best": {"source": "genius", "schema_source": "genius",
          "url": "https://genius.com/Genius-romanizations-kishore-kumar-pal-pal-dil-ke-paas-romanized-lyrics",
          "text": "[Chorus]\nPal-pal dil ke paas tum rehti ho\n…", "chords": "",
          "checks": {"line_count": 32, "devanagari_ratio": 0.0, "title_words_in_first_lines": 1.0,
                     "title_words_found": 1.0, "looks_like_lyrics": true}, "passes": true},
 "attempts": [{"source": "genius", "status": "ok", "passes": true, "is_best": true, "…": "…"}]}
```

**Source status, tested live from this Mac:**

| Source | Result |
|---|---|
| Genius | **Works.** Public search and song pages, no token needed. |
| Ultimate Guitar | **Works**, intermittently. Cloudflare-blocked on 2026-10-27, fine on 2026-10-28/29. |
| hindigeetmala.net | **Works.** Found Pal Pal Dil Ke Paas, Bole Chudiyan and Bhanware Ki Gunjan. |
| geetmanjusha.com | **Works.** Found Pal Pal Dil Ke Paas and Bhanware Ki Gunjan. |
| lyricsindia.net | **Works.** Found Pal Pal Dil Ke Paas, Bhanware Ki Gunjan and Aaj Rapat Jaayen (in no other source). |
| musixmatch.com | Reachable, but lyrics only render with JavaScript for logged-out visitors. |
| Smule | **Blocked.** Cloudflare challenge. Song pages also render lyrics client-side. |

The tool doesn't try to get around bot challenges or logins.

**`lyrics_from_url(url)`** runs the same extraction for one allowlisted page
(any of the seven sites above).
Other `LYRICS_EXTRA_DOMAINS` hosts use a generic `[class*=lyric]` extractor.
Returns `{"source", "url", "text", "chords", "checks", "passes", "needs_js": false}`,
or `error_code: needs_js` / `blocked`.

**`song_lookup(title, film="", year="")`** queries Wikipedia (en, no key). It
finds the film article, then returns the infobox and the text of the Music or
Soundtrack section. Tables are rendered as `cell | cell` rows; references and
edit links are removed. Live result:

```json
{"found": true, "page_title": "Blackmail (1973 film)",
 "page_url": "https://en.wikipedia.org/wiki/Blackmail_(1973_film)", "release_year": "1973",
 "infobox": {"directed_by": "Vijay Anand", "music_by": "Kalyanji-Anandji",
             "release_date": "30 November 1973", "language": "Hindi"},
 "section_title": "Music",
 "soundtrack_text": "… All lyrics were by Rajendra Krishan.\n\nSong | Singer\n\"Pal Pal Dil Ke Paas\" | Kishore Kumar\n\"Main Doob Jata Hoon\" | Kishore Kumar\n\"Mile Mile Do Badan Khile Khile Do Chaman\" | Kishore Kumar, Lata Mangeshkar\n…",
 "song_mentioned": true}
```

`{"found": false}` means no film article matched.

### Suggestions and feedback

"Songs like X", "what after X", "next peppy old" and plan suggestions are ranked
by a deterministic tool, `suggest`, not by the agent: Haiku is unreliable at
arithmetic over 40 candidates. The weights live in the wiki's **`ranking.yaml`**
(git-tracked, re-read on every call). Neel's taps on Telegram buttons feed back
into the ranking:

- **👍 / 👎 = relation feedback:** "does song Y fit after reference X?" It's
  about the pair, never about Y alone. Next time X is the reference, Y moves
  up or down. For a request with no reference song (`next peppy old`), the
  normalised request is the reference (`query:old peppy`).
- **⭐ 1–10 = song rating:** `my_rating` on the song, used in all ranking.

**`suggest(mode, reference="", url="", references=[], query="", limit_catalog=10, limit_outside=10, exclude=[], send_buttons=true)`**

| `mode` | What | Lists |
|---|---|---|
| `like` | songs like `reference` | `limit_catalog` catalog + `limit_outside` outside |
| `after` | what to sing after `reference` (quick) | 5 catalog + 3 outside |
| `next` | `query` words like `peppy old`: catalog only, no YouTube | 3 catalog |
| `plan` | several `references` (a set being planned) | as `like` |

1. **Resolve the reference.** A `song_id` or a title (fuzzy, ≥ 90), or a
   YouTube `url`. A catalog reference with a `karaoke_url` seeds the mix from
   that video.
2. **Collect candidates.** `youtube_mix(source="both", limit=40)` per
   reference (not for `next`): its `in_catalog` matches become catalog
   candidates, the rest outside candidates (jukebox and "other" skipped).
   Catalog songs with the same mood and era as a reference are added as fill.
   For `next`, every catalog song that fits all the query words is a
   candidate:
   - era words: old, classic, retro, new, latest, 70s…
   - tempo: slow, medium, fast
   - voice: duet, female…
   - SCHEMA mood/genre/theme/situation values
3. **Score** with `ranking.yaml` (below).
4. **Drop:** `exclude` (song_ids or titles: tonight's list, the set), the
   references themselves, jukeboxes, retired songs, and in plans songs rated ≤
   `exclude_rating_at_or_below_in_plans`. `max_per_singer` caps catalog songs
   per singer.
5. **Number continuously** across groups and pages: catalog first, then
   outside, 1..N. `suggest_more` continues the sequence. For outside items on
   the page, the karaoke link comes from the mix when it has a karaoke video,
   otherwise from `karaoke_find`, otherwise `no karaoke track found`.
6. **Save** the ranked list and the number→song map to
   `STATE_ROOT/last_suggestions.json` (last 5 requests), then **send the
   feedback polls**. If YouTube fails, catalog results still come back with
   `warning: "YouTube Mix unavailable"`.

```json
{"request_id": "20261104-213012-1a2b3c", "mode": "like", "reference_key": "pal-pal-dil-ke-paas-1973",
 "reference_title": "Pal Pal Dil Ke Paas",
 "items": [
  {"n": 1, "group": "catalog", "song_key": "o-mere-dil-ke-chain-1972", "title": "O Mere Dil Ke Chain",
   "film": "Mere Jeevan Saathi", "year": "1972", "singers": "Kishore Kumar", "tempo_class": "slow",
   "energy": "2", "status": "ready", "my_rating": "9", "my_key_change": "-2",
   "karaoke_url": "https://www.youtube.com/watch?v=…", "score": 13,
   "reasons": "mix #6 · ready · crowd 2.6 · ⭐9"},
  {"n": 11, "group": "outside", "song_key": "yt:…", "title": "Chalte Chalte", "karaoke_url": "https://…",
   "video_url": "https://…", "score": 5, "reasons": "mix #3 · both · outside"}],
 "remaining": {"catalog": 12, "outside": 21}, "buttons_sent": true, "warnings": []}
```

**`ranking.yaml`** (defaults; anything missing falls back to these):

| Key | Default | Points |
|---|---|---|
| `mix_position` | `{top5: 3, top15: 2, top40: 1}` | by best rank in the reference's YouTube Mix |
| `both_sources` | `2` | in both the YouTube Mix and the YouTube Music radio |
| `multiple_mixes_per_extra` | `2` | plans: per extra reference mix the song appears in |
| `status` | `{ready: 3, practicing: 2, wishlist: 1, outside: 0}` | catalog status |
| `crowd_score` | `{gte_2_5: 2, gte_2_0: 1}` | event crowd score |
| `same_era` | `1` | same era as a reference (or a query era word) |
| `tempo_or_energy_within_1` | `1` | same tempo_class, or energy within 1 |
| `same_mood` | `1` | a mood in common |
| `relation` | `{per_up: 2, per_down: -2, cap: 6, half_life_days: 180}` | Σ per_up/per_down × 0.5^(age_days / half_life_days) over `relations.csv` rows for (reference, song), capped at ±cap |
| `rating` | `{"9-10": 3, "7-8": 2, "5-6": 0, "3-4": -1, "1-2": -3}` | by `my_rating` |
| `max_per_singer` | `3` | catalog songs per singer per list (0 = no cap) |
| `exclude_rating_at_or_below_in_plans` | `2` | plans drop songs rated this low |

`reasons` names the parts that applied, for example `mix #2 · both · ready ·
⭐9 · 👍×2 after this`.

**`suggest_more()`** returns the next page of the last list (numbers continue,
e.g. 21–40). It makes no new YouTube calls and also sends polls (or buttons)
for the new page.

**Feedback polls** (`FEEDBACK_MODE=poll`, the default). After each `suggest`
or `suggest_more` page, the bot sends two Telegram **multi-select polls** that
list the page's songs. Neel ticks every song that applies and taps **Vote**
once per poll, instead of one tap and one bot reply per song.

- 👍 poll: `👍 Which fit after <reference>?` (`next`: `👍 Which fit "<request
  words>"?`; plans: `👍 Which fit this plan?`).
- 👎 poll: the same songs, `👎 Which don't fit after <reference>?`.
- Options are `#n Title (Singer)`, clipped to Telegram's 100 characters with
  `…`. Polls are anonymous with `allows_multiple_answers`. Regular polls let
  you change your vote until it's collected.
- Telegram allows **12 options per poll** (Bot API 10.3, checked 2026-11-05;
  historically 10): `tg.MAX_POLL_OPTIONS`. A page that fits gets one pair. A
  bigger page gets a pair per group (catalog / outside), split into balanced
  parts (13 → 7 + 6) when a group exceeds 12; the questions then end in
  `(catalog)` / `(outside)` / `(catalog #1–#9)`. A set of fewer than 2 songs
  gets no poll: the bot names those numbers and suggests typing `fb 13👍`.
- The first poll carries `ReplyKeyboardRemove`, so the old buttons disappear.
- Each poll's `message_id`, type (up/down) and option-index → song map are saved
  in the request's state with `status: open`.

**How votes are read.** The gateway owns the bot's incoming updates, so the
server never calls `getUpdates` or sets a webhook, and never sees vote events.
Instead it **closes** each poll with `stopPoll(chat_id, message_id)`, which
returns the final Poll with a `voter_count` per option. Neel is the only voter,
so a count of 1 means he ticked that song.

**`feedback_collect(request_id="")`** closes the open polls for one list
(default: all) and appends to `feedback/relations.csv`:
- `up` for each song ticked in a 👍 poll, `down` for each in a 👎 poll, with
  the list's reference key/title, type and request text;
- **conflict**: a song ticked in both polls of the same list records nothing
  and is reported;
- **no feedback**: nothing ticked records nothing;
- **idempotent**: closed polls are skipped. A poll that can't be closed
  (deleted, too old) is marked `lost` and reported, never raised.

It returns `{collected, conflicts, lost, overridden, reply}`, where `reply` is
one line like `Saved: 👍 3 songs, 👎 1 song (after Pal Pal Dil Ke Paas)`.

**Automatic collection,** so Neel rarely has to say anything:
1. Every `suggest` call first collects the open polls of earlier lists. The
   votes count in the new ranking, and the summary comes back as
   `previous_feedback`.
2. Before **any** tool runs, polls open for more than 24 hours are collected.
   When there are none, this is a local state check with no network.
3. Saying `done` collects the latest list.

**Typed feedback still works** (`feedback_tap`). `3 👍`, `3 👎` and `fb 1👍 3👎`
are saved immediately and **override** that song's poll ticks when the poll is
collected. `3 ⭐9`, `rate 3 9` and `rate <song> 9` set ratings. In poll mode
there is no rating UI: a bare `3 ⭐` replies `Type 3 ⭐<1-10> to rate <title>`.

**`FEEDBACK_MODE=off`** sends no feedback UI at all (typed feedback still
works). **`FEEDBACK_MODE=keyboard`** keeps the older per-song reply keyboard,
which is described next. Inline buttons aren't an option either way: Hermes's
`callback_query` handler acts only on its own prefixes and silently drops any
other `callback_data`, so taps would never reach the agent
(`BUTTON_MODE=inline` is refused).

*Keyboard mode:* reply-keyboard rows `["3 👍", "3 👎", "3 ⭐"]` plus
`["more", "done"]`, and a rating pad after `3 ⭐` (`["3 ⭐1" … "3 ⭐5"]`,
`["3 ⭐6" … "3 ⭐10"]`, `["back"]`). A page stays in one keyboard (a chat shows
one reply keyboard at a time) unless it passes ~100 buttons. `send_buttons`
re-sends it. Tap texts:

| Text | Action |
|---|---|
| `3 👍` / `3 👎` | Appends to `relations.csv` for (that list's reference, song #3). Reply: `👍 noted: Yeh Shaam Mastani after Pal Pal Dil Ke Paas`. Tapping again in the same list replaces the first tap. In a plan, a row per reference song. |
| `3 ⭐` | keyboard mode: the rating pad; poll mode: `Type 3 ⭐<1-10> to rate …` |
| `3 ⭐9`, `rate 3 9` | `my_rating=9`, `rated_on=today` via `catalog_upsert`. For an outside song nothing is written: `needs_add: true` with its title/url, and the rating is kept pending in state. |
| `rate <song> 9` | `set_rating` by title |
| `back` | keyboard mode: the main keyboard again |
| `done` | poll mode: `feedback_collect()` for the latest list; keyboard mode: removes the keyboard |
| `more` | `suggest_more()` |
| `fb 1👍 3👎 5⭐8` | each item in order; one combined reply |

Numbers refer to the **most recent** list. Emoji variants parse (skin tones,
variation selectors, `★`). Unknown numbers reply `#12 isn't in the last list`.
Text that isn't feedback returns `handled: false`.

**`set_rating(ref, rating)`** sets a song's rating by song_id or title.

**`relations_rekey(old_key, new_key)`** rewrites a key in `relations.csv`,
e.g. `yt:<id>` → `song_id`. **`catalog_upsert` does this automatically**: when
a newly added song's `karaoke_url` or alt URLs contain a video that was
suggested from outside, its relations move to the new `song_id` and any
pending ⭐ rating is applied.

**`relations_for(ref)`** lists a song's strongest relations both ways:
`goes_after_it` and `it_goes_after`.

**Data.** `feedback/relations.csv` (append-only, columns in SCHEMA.md),
`my_rating`/`rated_on` on `songs.csv`, and `ranking.yaml`, all in the wiki.
Session state lives in `STATE_ROOT`, outside the repo.

**Upgrading an existing wiki:** stop the gateway, then run
`WIKI_ROOT=… venv/bin/python migrate.py` once. It adds the empty `my_rating`
and `rated_on` columns and changes nothing else; running it again does
nothing. Until then, catalog tools return `schema_mismatch` with this hint.

## Environment variables

See `.env.example`. Hermes sets these in the `env:` block of `config.yaml`;
the server does not read a `.env` file.

| Var | Default | Purpose |
|---|---|---|
| `WIKI_ROOT` | `~/.hermes/profiles/karaoke-gayak/karaoke-gayak-wiki` | Repertoire repo |
| `SEED_ROOT` | unset | Local MP3 folder (read-only; scan and analyze only) |
| `MEDIA_ROOT` | `~/.hermes/profiles/karaoke-gayak/media-tmp` | Temp downloads (created on startup) |
| `PREFERRED_CHANNEL_URL` | `https://www.youtube.com/@AnilChauhankaraoke` | Indexed, ranked first |
| `MAX_DURATION_SEC` | `900` | Skip longer videos |
| `FFMPEG_PATH` | `ffmpeg` | Full path recommended: `/usr/local/bin/ffmpeg` |
| `YTDLP_JS_RUNTIME` | unset (deno, then node, on PATH) | `node:/full/path/to/node` for YouTube's JS challenges |
| `GENIUS_ACCESS_TOKEN` | unset | Optional free Genius API client token |
| `LYRICS_EXTRA_DOMAINS` | empty | Extra allowed lyrics domains, comma-separated |
| `LYRICS_DELAY_SEC` | `2` | Delay between lyrics requests |
| `TELEGRAM_BOT_TOKEN` | unset | The KaraokeGayak bot token (same bot as the gateway); send side only (sendMessage, sendPoll, stopPoll). Pass `"${TELEGRAM_BOT_TOKEN}"` in config.yaml. |
| `TELEGRAM_CHAT_ID` | unset | The only allowed destination: Neel's Telegram user id (DM chat id). `"${TELEGRAM_HOME_CHANNEL}"` in this profile. |
| `FEEDBACK_MODE` | `poll` | `poll`: 👍/👎 multi-select polls collected with stopPoll; `keyboard`: the older per-song reply keyboard; `off`: no feedback UI is sent (typed `fb 1👍` / `rate 3 9` still work) |
| `BUTTON_MODE` | `reply` | `reply` keyboard; `inline` is refused (Hermes drops unknown inline taps) |
| `STATE_ROOT` | `~/.hermes/profiles/karaoke-gayak/state` | Session state (last suggestion lists, pending ratings) |

## Install (Intel Mac)

```bash
brew install ffmpeg python@3.12      # ffmpeg -> /usr/local/bin/ffmpeg
cd karaoke-tools
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install -r requirements-dev.txt   # pytest
```

YouTube now needs a JavaScript runtime to solve its signature and "n"
challenges. Without one, yt-dlp warns "Signature solving failed" and downloads
may fail. `yt-dlp[default]` installs `yt-dlp-ejs`, the solver scripts, locally,
so nothing is fetched at runtime. Point `YTDLP_JS_RUNTIME` at a node (≥ 20) or
deno binary. On this Mac that's `node:/Users/bishpaul/.local/bin/node`.

**Warm-up.** The first `analyze_audio` after install takes about 45 s while
numba compiles librosa's kernels and caches them in the venv. Later runs, in
any process, take about 3–9 s. Run one analysis by hand after installing (see
B below) so the agent never pays this.

### Pinned for Intel Mac (macOS x86_64)

Checked on 2026-10-27. A fresh `pip install -r requirements-dev.txt`
resolves to 63 packages, all wheels, with nothing built from source.

| Package | Pin | Why |
|---|---|---|
| `numba` | `==0.62.1` | 0.63+ (latest is 0.68.1) has no macOS x86_64 wheels; pip would build LLVM from source |
| `llvmlite` | `==0.45.1` | Same: 0.46+ (latest 0.50.0) has no Intel wheels |
| `numpy` | `<2.4` (gets 2.3.5) | Required by numba 0.62.1; numpy itself still ships Intel wheels |
| `cryptography` | `<49` (gets 48.0.1) | Pulled in by mcp; 49.0.0+ has no Intel wheel (pip would need Rust) |

Unpinned: `mcp` (2.3.0), `yt-dlp[default]` (2026.10.26 + yt-dlp-ejs 0.8.0),
`librosa` (1.0.0), `soundfile` (0.14.0, bundles libsndfile 1.2.2), `mutagen`,
`requests`, `beautifulsoup4`, `rapidfuzz`, `pyyaml` (6.0.3, Intel wheel). On
Apple Silicon you can drop the pins.

## Testing

### A. Unit tests (offline)

```bash
venv/bin/python -m pytest
```

312 tests, no network: requests and yt-dlp are patched to fail loudly. Each
test runs against a fresh temp copy of `tests/fixtures/wiki/`, which is a copy
of the real `SCHEMA.md` and the empty skeleton. The tests cover:

- **Catalog:** upsert and merge, id collisions, enum and range rejection,
  atomic write, sort order, CSV quoting, era fill, `log.md`, review flow,
  event idempotency.
- **Schema:** the vocab ↔ SCHEMA.md drift check.
- **Audio:** a synthesized 120 BPM click track, a C-major chord progression
  and silence.
- **Path safety:** traversal and symlink escapes for audio, wiki and cleanup.
- **Allowlists:** domain checks for YouTube and lyrics, including redirect
  hops.
- **Extraction:** lyrics from saved Genius, Ultimate Guitar and Smule pages,
  and the Wikipedia section and infobox parsing.
- **Karaoke matching:** title normalisation and index matching.
- **Telegram:** chunking.
- **MCP:** a protocol round trip over stdio with launchd's minimal PATH.

**When SCHEMA.md changes:** copy it to `tests/fixtures/wiki/SCHEMA.md` and run
the tests. `test_vocab.py` names every value that drifted, and `vocab.py` needs
updating until it passes.

### B. One tool at a time (network)

Use a scratch copy of the wiki, because write tools change files for real:

```bash
cp -R ~/.hermes/profiles/karaoke-gayak/karaoke-gayak-wiki /tmp/kg-wiki-test
export WIKI_ROOT=/tmp/kg-wiki-test MEDIA_ROOT=/tmp/kg-media FFMPEG_PATH=/usr/local/bin/ffmpeg \
       YTDLP_JS_RUNTIME=node:/Users/bishpaul/.local/bin/node
venv/bin/python try_tool.py --list
venv/bin/python try_tool.py karaoke_find '{"title": "Pal Pal Dil Ke Paas", "film": "Blackmail"}'
venv/bin/python try_tool.py youtube_fetch_audio '{"url": "https://www.youtube.com/watch?v=pw6r-izZArA"}'
venv/bin/python try_tool.py analyze_audio '{"path": "/tmp/kg-media/<job_id>/audio.mp3"}'
venv/bin/python try_tool.py cleanup_job '{"job_id": "<job_id>"}'
```

### C. MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
  -e WIKI_ROOT=/tmp/kg-wiki-test -e MEDIA_ROOT=/tmp/kg-media \
  -e FFMPEG_PATH=/usr/local/bin/ffmpeg -e YTDLP_JS_RUNTIME=node:/Users/bishpaul/.local/bin/node \
  venv/bin/python server.py
```

Add `--cli --method tools/list` to list the 32 tools in the terminal instead of
the browser.

## Wiring into Hermes

In `~/.hermes/profiles/karaoke-gayak/config.yaml`:

```yaml
mcp_servers:
  karaoke_tools:
    command: "/Users/bishpaul/workspace/github/mcp-servers/karaoke-tools/venv/bin/python"
    args: ["/Users/bishpaul/workspace/github/mcp-servers/karaoke-tools/server.py"]
    env:
      WIKI_ROOT: "/Users/bishpaul/.hermes/profiles/karaoke-gayak/karaoke-gayak-wiki"
      SEED_ROOT: "/Users/bishpaul/Music/Karaoke"          # your MP3 folder
      MEDIA_ROOT: "/Users/bishpaul/.hermes/profiles/karaoke-gayak/media-tmp"
      PREFERRED_CHANNEL_URL: "https://www.youtube.com/@AnilChauhankaraoke"
      MAX_DURATION_SEC: "900"
      FFMPEG_PATH: "/usr/local/bin/ffmpeg"
      YTDLP_JS_RUNTIME: "node:/Users/bishpaul/.local/bin/node"
      LYRICS_DELAY_SEC: "2"
      # GENIUS_ACCESS_TOKEN: "..."
      # Placeholders, not values: Hermes fills them in from the profile's .env
      # when it starts the server, so the token never appears in config.yaml.
      TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN}"
      TELEGRAM_CHAT_ID: "${TELEGRAM_HOME_CHANNEL}"
      FEEDBACK_MODE: "poll"
      BUTTON_MODE: "reply"
      STATE_ROOT: "/Users/bishpaul/.hermes/profiles/karaoke-gayak/state"
```

Use the venv's own `python` and absolute paths everywhere. Hermes runs under
launchd and doesn't share your shell's PATH or working directory, which is why
ffmpeg and node are given as full paths.

## Troubleshooting

- **`schema_mismatch`.** A wiki CSV's header doesn't match SCHEMA.md, or
  the file is missing. Check `WIKI_ROOT`. Writes refuse to run until the
  header matches.
- **"Signature solving failed" / "n challenge solving failed" in the
  stderr log, or downloads failing.** yt-dlp can't find a JS runtime. Set
  `YTDLP_JS_RUNTIME=node:/full/path/to/node` and make sure `yt-dlp-ejs` is
  installed (`venv/bin/pip install -U "yt-dlp[default]"`).
- **YouTube tools that used to work now fail** (`download_failed`,
  `rate_limited`). YouTube changed something. Upgrade yt-dlp and restart the
  gateway:

  ```bash
  venv/bin/pip install -U "yt-dlp[default]"
  launchctl stop <hermes-gateway-label>
  launchctl start <hermes-gateway-label>
  ```

  Use `stop`/`start`, not `launchctl kickstart`, which doesn't actually restart
  the gateway on this machine.
- **`rate_limited` with "confirm you're not a bot".** YouTube is throttling
  this IP. Wait, and avoid running `check_links` over the whole catalog in one
  go.
- **`ffmpeg_not_found` from `youtube_fetch_audio`.** Set `FFMPEG_PATH` to
  `/usr/local/bin/ffmpeg`. Under launchd, a bare `ffmpeg` isn't on PATH.
- **`lyrics_search` returns `blocked` for Ultimate Guitar or Smule.** Smule is
  always blocked (Cloudflare); Ultimate Guitar sometimes. The Indian lyrics
  sites usually cover old Hindi songs. Otherwise paste lyrics yourself and
  save them with `source=neel`.
- **The first `analyze_audio` takes about a minute.** numba is compiling on
  the first run; see Warm-up. If it happens every time, the venv's
  `site-packages` isn't writable for the cache.
- **`pip install` tries to build numba, llvmlite or cryptography.** A pin
  was removed or loosened; see the pins table above.
- **The server connects and then dies, or Hermes reports protocol errors.**
  Something printed to stdout, which is the MCP channel. All logging goes to
  stderr, and yt-dlp runs with a stderr-only logger and stdout redirected;
  keep it that way when editing.
