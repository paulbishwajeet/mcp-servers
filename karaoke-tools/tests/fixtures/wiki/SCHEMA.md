# SCHEMA: KaraokeGayak repertoire

This file defines the data format. Any agent or tool that reads or writes this repo must follow it. Changes to this file need Neel's explicit approval.

## Files

| Path | What |
|---|---|
| `catalog/songs.csv` | One row per song. The source of truth. |
| `catalog/review.csv` | Problems waiting for Neel: unsure matches, missing lyrics, failed steps. |
| `lyrics/<song_id>.txt` | Hinglish lyrics for one song (plus chords if found). |
| `events/<YYYY-MM-DD>-<slug>.md` | One file per show, from `events/_TEMPLATE.md`. |
| `index/anilchauhan.csv` | Cached video list of the Anil Chauhan Karaoke channel: `video_id,title,url,duration_sec,indexed_on`. |
| `log.md` | Append-only change log. |

## CSV format rules

- UTF-8, comma-separated, every text field double-quoted, header row exactly as below.
- Multi-value fields use `;` with no spaces around it: `romantic;longing`.
- Empty means unknown. Never write `N/A`, `none`, or `?`.
- Rows are sorted by `song_id`. Writes replace the whole file atomically (write temp file, then rename).
- `song_id` is permanent: lowercase ASCII slug of the title, plus `-<year>` when the year is known, plus `-2`, `-3` on collision. Example: `pal-pal-dil-ke-paas-1973`. Never change an existing id; it links lyrics and events.

## `songs.csv` columns

| Column | Type | Meaning / allowed values |
|---|---|---|
| song_id | slug | see above |
| title | text | Song title in Hinglish, common spelling ("Pal Pal Dil Ke Paas") |
| film | text | Film or album name; `non-film` for private albums/singles |
| year | int | Film release year |
| singers | multi | Original singers, full names ("Kishore Kumar;Lata Mangeshkar") |
| music_director | multi | ("Kalyanji-Anandji" counts as one value) |
| lyricist | multi | |
| language | enum | `hindi`, `urdu`, `punjabi`, `bengali`, `english`, `mixed` |
| voice_type | enum | `solo-male`, `solo-female`, `duet-mf`, `duet-mm`, `duet-ff`, `group` |
| era | enum | `pre-1960`, `1960s`, `1970s`, `1980s`, `1990s`, `2000s`, `2010s`, `2020s` (from year) |
| bpm | number | Measured from the karaoke track, one decimal |
| tempo_class | enum | `slow` (< ~85 BPM felt), `medium`, `fast` (> ~120 BPM felt). The *felt* tempo, checked against knowledge of the song, because measured BPM can be half or double. |
| musical_key | text | Key of the karaoke track as measured, e.g. `D major`, `A minor` |
| energy | int 1-5 | 1 = very soft, 5 = full-on dance/party |
| duration_sec | int | Of the karaoke track |
| genre | multi | `romantic-ballad`, `sad`, `peppy`, `dance-party`, `disco`, `qawwali`, `sufi`, `ghazal`, `classical-based`, `folk`, `bhangra-punjabi`, `bhajan-devotional`, `patriotic`, `cabaret`, `item`, `rock`, `lullaby`, `comedy` |
| mood | multi | `romantic`, `happy`, `playful`, `sad`, `longing`, `nostalgic`, `energetic`, `calm`, `devotional`, `philosophical`, `celebratory`, `inspirational` |
| emotion | multi | Finer-grained feeling from the lyrics, free text but short: `first-love`, `heartbreak`, `separation`, `yearning`, `friendship-bond`, `teasing`, `gratitude`, `hope`, `pride`, `loneliness`, `devotion`, `joy-of-life` |
| theme | multi | `love`, `separation`, `friendship`, `rain-monsoon`, `nature`, `travel-journey`, `night-moon`, `youth-college`, `family-mother`, `wedding`, `festival`, `patriotism`, `life-philosophy`, `dance`, `beauty-praise`, `farewell` |
| situation | multi | `wedding`, `sangeet`, `anniversary`, `birthday`, `farewell`, `reunion`, `holi`, `diwali`, `navratri`, `independence-day`, `valentines`, `new-year`, `house-party`, `opener`, `closer`, `late-evening`, `rainy-day`, `road-trip`, `antakshari` |
| singalong | int 1-5 | How readily a mixed Indian crowd sings along |
| karaoke_url | url | Best YouTube karaoke link (Anil Chauhan preferred) |
| karaoke_alt_urls | multi | Up to 2 backup links |
| karaoke_channel | text | Channel of `karaoke_url` |
| lyrics_file | path | `lyrics/<song_id>.txt`, empty if missing |
| lyrics_source | enum | `genius`, `smule`, `ultimate-guitar`, `other-web`, `neel`, `transliterated` |
| lyrics_url | url | Page the lyrics came from |
| added_from | enum | `local-mp3`, `youtube-link`, `song-name` |
| added_on | date | YYYY-MM-DD |
| status | enum | `ready`, `practicing`, `wishlist`, `retired`. New songs from the local folder start as `practicing` until Neel confirms; everything else starts as `wishlist`. |
| my_key_change | int | Semitones Neel shifts from the karaoke track, e.g. `-2`. Empty = unknown, `0` = as is. |
| notes | text | Neel's performance notes |
| times_performed | int | From events |
| crowd_score | number | Average outcome across events: standout = 3, good = 2, weak = 1 |
| last_performed | date | From events |
| confidence | enum | `high`, `medium`, `low`: confidence in the song identification and metadata |
| needs_review | bool | `true` if anything in the row is uncertain; a matching row exists in review.csv |

## `review.csv` columns

`logged_at, song_id, source_ref, problem, details, resolved`
- `problem` is one of `unsure-match`, `no-lyrics`, `lyrics-mismatch`, `no-karaoke-link`, `dead-link`, `analysis-failed`, `metadata-conflict`.
- `source_ref` is the mp3 path, YouTube URL or song name that started it.
- `resolved` is `false` until fixed, then `true` (rows are not deleted).

## Lyrics files

```
# Pal Pal Dil Ke Paas
# Blackmail (1973) | Kishore Kumar | source: genius | https://...

<lyrics in Hinglish, verses separated by a blank line>

## Chords (optional, from ultimate-guitar)
...
```

## Event files

Copy `events/_TEMPLATE.md`. After an event, update `times_performed`, `crowd_score` and `last_performed` for every song in the set.

## Rules for any agent

1. Read the file before writing; never rely on memory of its contents.
2. Never delete a song row. Use `status: retired`.
3. Only use values from the allowed lists. If a new value is really needed, propose it to Neel; don't invent it.
4. Never store audio in this repo.
5. Log every add, update and event in `log.md`.
