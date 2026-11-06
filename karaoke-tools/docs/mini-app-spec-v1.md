# KaraokeGayak Mini App: Specification v1

Based on Neel's spec of 2026-11-06 (Suggest Similar, then Event Planner), refined. Items marked **[change]** or **[added]** are my suggestions; everything else is Neel's original intent. Open questions are in §9.

## 1. Overview

A Telegram Mini App inside the KaraokeGayak bot. It's backed by the Hermes agent and the `karaoke-tools` code running on Neel's MacBook. It helps a karaoke singer build and use a catalog of songs/tracks, discover similar songs, and plan and review events.

The app and the chat agent share the same data (`karaoke-gayak-wiki` repo: songs.csv, lyrics, events, feedback) and the same ranking code. The chat stays for natural requests (adding songs, shayari, questions). The app is for anything with lists and taps.

## 2. Scope

| Feature | Version | Notes |
|---|---|---|
| Suggest Similar | **v1** | §4 |
| Song edit / save to catalog | **v1** | part of Suggest Similar (§4.6) |
| Home screen with search **[added]** | **v1** | §3.2 |
| Event Planner: list, create, edit | **v1** | §5 |
| Event Feedback | **v1** | §5.6, part of the Event Planner |

## 3. Opening the app

### 3.1 From a chat request
- Neel types e.g. "suggest similar songs of Pal Pal Dil Ke Paas", "give me songs like Tum Hi Ho", "let's plan an event", "feedback of last event". The agent opens the matching page: Suggest Similar, Events (new event form), or the Feedback view of the most recent past event.
- **[change] The agent can't open the app by itself.** Telegram only opens a Mini App when the user taps a button. So the agent replies with one short line and a button, e.g. *"Similar to Pal Pal Dil Ke Paas"* with **[🎵 Open]**. One tap opens the app directly on that page.
  - Technically this is a `web_app` button (or a `t.me/<bot>/<app>?startapp=<code>` link) sent by a new karaoke-tools tool, `open_app(view, seed)`. The start code can only be a short ID (64 safe characters max), so the server stores the request and passes its ID.
- **[change] No slash commands.** Hermes reserves `/...` for itself (that's what broke `/stats`), so `/event planner` won't reach the agent. Use plain words ("plan an event"), or the menu button below.

### 3.2 From the bot's menu button **[added]**
- Set the bot's menu button (bottom-left of the chat) to **🎵 KaraokeGayak**. It opens the app's **Home**: a search box, "recent seeds", and (in v2) Events.
- This means the app works without typing a command at all, which is handy at events.

## 4. Suggest Similar

### 4.1 Getting the results
Input: a seed song (title, or a YouTube link). The seed doesn't have to be in the catalog.

1. **Catalog section:** the 10 best catalog matches, using the existing `suggest` ranking (`ranking.yaml`): YouTube Mix overlap, your like-links (§4.4), rating, status, crowd score, and closeness in era / tempo / energy / mood / genre to the seed.
   **[change]** Title isn't a similarity signal (two songs with similar names aren't similar), so it's used only to *find* the seed, not to rank.
2. **YouTube Mix section** (decided: **seed with the original song, show karaoke tracks**): the seed's YouTube Mix is fetched from the *original* song video (better variety), then each result is turned into its **karaoke track**: taken from the catalog if the song is there, otherwise looked up as "<title> karaoke" (Anil Chauhan channel index first, which is instant, then a YouTube search). Songs with no karaoke track found stay in the list marked "no karaoke track". The 10 best songs (YouTube + YouTube Music) that are **not** in the catalog, ranked by mix position and your like-links.
3. Exclude the seed itself, duplicates, and jukebox/compilation videos.
4. **[added] Speed:** karaoke lookups run in parallel and are cached per song, so only new songs cost a search. The catalog section shows immediately. The YouTube Mix section fills in a few seconds later with a loading indicator. Mix results are cached per seed for 24 hours, so Next, Back and re-opening are instant.

### 4.2 The list
Two sections, **From your catalog** and **YouTube Mix (not in catalog)**, each 10 rows. Each row shows:

| Element | Behaviour |
|---|---|
| **Title** (link) | Opens the YouTube player (§4.3). **[added]** A small second line under the title: film · year · singer, plus a status badge (ready / practicing / wishlist) for catalog songs. |
| **Energy** | 1–5 shown as a small bar or chip. **[added]** Plus a tempo label (slow / medium / fast). |
| **Like** ♥ | Toggle (§4.4). |
| **Favourite / rating** | Shows the current rating, e.g. ⭐8. Tap opens a 1–10 wheel picker (§4.5). |
| **Similar** ↪ | Opens Suggest Similar with this song as the new seed (§4.7). |
| **Edit** ✎ | Opens the edit sheet (§4.6). |
| **[added] Why** ⓘ | Tap shows the ranking reasons, e.g. "mix #2 · both sources · ⭐9 · liked after Pal Pal". Optional, but helps you trust and tune the ranking. |

### 4.3 Player
- Tapping the title opens a **YouTube player overlay** above the list, with a fullscreen option. Closing it returns to the list at the same scroll position.
- **Karaoke first:** the title opens the **karaoke track** (per the decision in §4.1). **[added]** An **Original** tab is also there, for hearing how the song goes.
- **[added] Fallback:** some uploaders block embedding (your third test link, `TnDmK0ujJ-0`, did). For those the player shows **Open in YouTube**, which leaves Telegram.
- Playback stops if Telegram goes to the background (a Telegram/iOS limitation).

### 4.4 Like (your liking network)
- Tapping ♥ links the seed → the liked song in the database. Unliked songs are not linked at all, as in your spec.
- **[added] Tapping again un-likes** and removes the link. The data is stored append-only (`like` / `unlike` rows in `feedback/relations.csv`), so history isn't lost.
- **[added] The link belongs to the current seed.** On a drilled-down page (§4.7), a like links that page's seed, not the original one.
- **[change] Direction:** stored as seed → song, but used both ways when finding similar songs ("A is like B" also means "B is like A"). It stays directed for "what to sing *after*" in the event planner later.
- **[added] The existing 👍/👎 poll data** is kept as history. 👍 rows count as likes; 👎 rows are kept as a **small penalty** (decided), e.g. −1 each in `ranking.yaml`, fading over time like likes.
- **[added] Liking a YouTube Mix song** that isn't in the catalog is allowed. The link is stored against its YouTube ID and moves to the song automatically if it's added to the catalog later.

### 4.5 Rating 1–10
- Tap the rating to open a wheel picker 1–10, with **Clear**.
- Meaning (yours): **10 = favourite and most comfortable; 5 and below = needs practice.**
- **Decided: the rating drives status.** Rating 6–10 sets `status: ready`; rating 1–5 sets `status: practicing`. Unrated songs keep their current status (wishlist stays wishlist until rated). Setting status by hand in the Edit sheet is still possible, but the next rating change overrides it.
- **[added] Rating a YouTube Mix song** means adding it to the catalog first. The app asks *"Add to catalog?"*, saves the rating, and runs the normal add pipeline (tags, lyrics, karaoke link) in the background. It takes about a minute, and a badge shows "adding…" until it's done.

### 4.6 Edit sheet
- Opens as a bottom sheet over the list.
- Fields: title, film, year, singers, music director, voice type, genre / mood / theme / situation (chips limited to SCHEMA.md values), tempo class, energy, status, rating, key change, notes, **karaoke YouTube link** (paste a link, or tap **Find** to search Anil Chauhan first), and alternate links.
- **[added]** For a YouTube Mix song, the sheet title is **"Add to catalog"**: the fields come prefilled from the auto-lookup, and Save creates the catalog row.
- **[added]** When the karaoke link changes, offer **Re-analyse** (tempo/key/energy from the new track).
- Saves go through `catalog_upsert`, so validation and the change log apply. **[added]** The app shows field errors inline rather than failing silently.
- **[change] Autosave everything else.** Like and rating save on tap, with a short "Saved ✓ / Undo" toast. Only the edit sheet has an explicit Save, because typing should be confirmed.

### 4.7 Similar drill-down and navigation
- **Similar** opens a new Suggest Similar page for that song.
- **[change]** Use Telegram's **native Back button** (top-left of the Mini App header) instead of a custom link. It's the standard place, and it works with the phone's back gesture. The app keeps a stack, so Back returns to the previous seed with its results and scroll position intact.
- **[added]** A breadcrumb under the header (e.g. *Pal Pal Dil Ke Paas › Yeh Shaam Mastani*) shows where you are.

### 4.8 Next page
- **Next** shows the next 10 catalog + 10 YouTube Mix songs for the same seed. **[added] Previous** is there too.
- **[added] When a section runs out**, show "No more catalog matches" or "End of YouTube Mix". The mix is fetched 40 deep, so expect up to ~4 pages there. Catalog depth depends on how many songs fit.

### 4.9 Closing
- Telegram's native **Close** in the header always works. **[added]** A **Done** button at the bottom also closes the app (`WebApp.close()`).
- **[added]** On close, the chat gets one summary line from the bot, e.g. *"Saved 4 likes, 2 ratings, 1 song added (similar to Pal Pal Dil Ke Paas)"*, so the chat history records what happened.

## 5. Event Planner

### 5.1 Events page
- Opened from the Home screen, or when Neel says "let's plan an event" / "show my events" in chat.
- Two lists: **Upcoming** and **Past**. Each event shows name, date/time, theme, number of songs, total time, and a status badge.
- **[added] Status lifecycle:** `draft` (being built, autosaved) → `planned` (locked in with Create) → `performed` (feedback given). Drafts mean closing the app halfway never loses work.
- Actions: **+ New event**, tap an event to **open / edit**, **Feedback** (past events).

### 5.2 New event
Form fields:
- **Name** (e.g. "Diwali 2026")
- **Date and time**
- **Theme**: chips from the catalog's situation/theme values (Diwali, wedding, rain, romantic evening…), plus free text
- **Instructions**: free text, e.g. "45 minutes, mostly 70s–80s, 2 duets, start peppy, end with a singalong, nothing too high"
- **[added] Duration or number of songs** (optional, but makes the suggestions fit the time)

**[added] How instructions are understood:** the app server asks Claude (Haiku, with your Anthropic key, a fraction of a cent) to turn the instructions into filters: target duration, eras, voice types, energy arc, must-include and avoid. It shows them as editable chips ("45 min · 1970s–80s · 2 duets · peppy start · singalong end") before suggesting, so you can see what it understood and fix it.

### 5.3 Building the playlist
The screen has two parts: **Playlist** (top, starts empty) and **Suggestions** (below).

- **Suggestions:** 10 from the catalog, ranked by theme/situation match, the parsed instructions, rating, crowd score, and "not sung for this kind of event recently". **[added]** A **More** button gives the next 10.
- **YouTube ideas** (decided: optional section, collapsed by default): once the playlist has songs, a section seeded from the YouTube Mixes of the playlist songs shows up to 10 karaoke tracks *outside the catalog*, with the same row controls. Songs suggested by several playlist songs rank higher. Added songs are marked *outside catalog*.
- Each suggestion row:

| Element | Behaviour |
|---|---|
| **Title** (link) | Player overlay as in §4.3 (Original / Karaoke tabs, fallback "Open in YouTube") |
| **Rating** | Same 1–10 wheel as §4.5 (also updates status) |
| **Add** ✚ | Moves the song into the Playlist. **[added]** A new suggestion slides in to keep 10 visible. |
| **Dislike** ✕ | **[change]** On a *suggestion* it means "not for this event": the song is hidden from this event's suggestions and replaced. On a *playlist* song it removes the song from the playlist (back into suggestions). It is **not** a global dislike and doesn't touch ratings or likes. |
| **Similar** ↪ | Opens Suggest Similar seeded with this song. Back returns to the event page exactly as it was. **[added]** When opened from an event, every row in Suggest Similar gets an extra **Add to event** button, so YouTube Mix songs can go into the playlist too (marked *outside catalog*). |
| **Edit** ✎ | Same edit sheet as §4.6 |

- **Playlist** rows have the same controls plus:
  - **[added] Drag to reorder.** The order is the running order on the night.
  - **[added] Auto-order** button: arranges by the energy arc from the instructions (strong opener, hardest songs early, no two slow or two very fast in a row, singalong closer).
  - **[added] Live summary bar:** total time vs target, number of duets, and a mini energy curve.
  - **[added] Warnings:** song already in the playlist; song sung at a similar event for the same crowd recently; outside-catalog or `practicing` song (unrehearsed).
- **Create** locks in the playlist: status → `planned`, the event file is written, and the chat gets a one-line confirmation. **[added]** If the playlist contains outside-catalog songs, Create offers to add them to the catalog first (so feedback can be tracked on them later).

### 5.4 Editing an existing event
- Open an event → **Edit** returns to the screen in §5.3 with the saved playlist: add songs (Suggestions, Similar, or **[added]** a search box over the catalog), remove, reorder, and change name/date/theme/instructions.
- **[added]** Saving changes to a `planned` event keeps it `planned`; editing a `performed` event only allows feedback changes, so history stays honest.

### 5.5 Song feedback after the event
- Opened from the Events page (**Feedback** on a past event) or by "feedback of last event" in chat.
- For each song in the playlist, in running order:
  - **Comment**: free text on how it went
  - **Outcome**: Standout / Good / Weak (one tap; this is what updates `crowd_score`). Decided: outcome + comment is enough, no per-event rating.
  - **[added] Skipped** toggle for songs that didn't get sung
  - Rating wheel, in case the event changed how you feel about the song
- **[added]** Event-level fields: **Audience requests** (added to the wishlist automatically) and **Overall notes**.
- **Finish feedback**: status → `performed`, runs `event_apply` (updates `times_performed`, `crowd_score`, `last_performed`), and the chat gets a one-line summary. Feedback can be edited later, and re-running is safe.
- **[added] Learning from the running order:** consecutive songs that both came out Standout or Good are stored as "fits after" links, so future suggestions and Auto-order learn from real performances.

## 6. Data

| Data | Where | Change |
|---|---|---|
| Songs, ratings, status, edits | `catalog/songs.csv` | uses `my_rating` / `rated_on` (already added) |
| Likes | `feedback/relations.csv` | new verdicts `like` / `unlike`; keys = song_id or `yt:<video_id>` |
| Ranking weights | `ranking.yaml` | add a like-link weight; review the 👎 weight (§9 Q4) |
| Mix cache, page stacks, start codes | `STATE_ROOT` (outside the repo) | new |
| Events | `events/*.md` | same format, plus front-matter fields `time`, `theme`, `instructions`, `parsed_filters`, `status` (draft/planned/performed), and per-song `comment`, `skipped` |
| Event drafts | `events/*.md` with `status: draft` | autosaved while building |

Everything in the repo keeps being backed up by the nightly sync.

## 7. Non-functional

- **Hosting:** an app server (Python/FastAPI) on the Mac, a launchd service, and Tailscale Funnel for the HTTPS address ($0).
- **Login:** every request carries Telegram's signed session data. The server verifies the signature with the bot token and accepts only Neel's Telegram user id. Opening the URL in a normal browser is refused.
- **Safety:** the server can only do app actions (read the catalog, suggest, like, rate, edit via `catalog_upsert`, add songs). No shell, no file browsing. A file lock stops the agent and the app writing songs.csv at the same time.
- **Look:** uses Telegram's light/dark theme colours. Big tap targets, since it'll be used at events.
- **Availability:** the Mac must be awake. If the server is down, the chat still works.

## 8. Out of scope for v1

Lyrics view, shayari, the Live view on the night, tonight mode, and the song-graph visualisation.

## 9. Decisions (2026-11-06)

1. Event Feedback is part of the v1 Event Planner.
2. Rating drives status: 6–10 = ready, 1–5 = practicing.
3. Suggest Similar seeds the YouTube Mix with the **original** song, but shows **karaoke tracks**: from the catalog if present, otherwise found via "<title> karaoke" (Anil Chauhan first).
4. Old 👎 poll data is kept as a small penalty.
5. The chat polls are **removed** once the app works (SOUL.md and karaoke-tools are updated in the same phase).
6. Event suggestions: 10 from the catalog, plus an optional **YouTube ideas** section.
7. Event feedback: Standout / Good / Weak plus a comment per song.

No open questions left. Next step: build prompts by phase.
