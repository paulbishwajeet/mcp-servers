/* KaraokeGayak Mini App. Plain JS, no build step. Every API call carries
   Telegram's signed initData; the server refuses anything else (401). */
"use strict";

const WA = window.Telegram && window.Telegram.WebApp;
const SESSION = makeId();
const views = [];            // the navigation stack
let vocabCache = null;
const adding = {};           // song_key -> job_id while an add job runs

// --- tiny helpers ---------------------------------------------------------------------------

function makeId() {
  const b = new Uint8Array(12);
  crypto.getRandomValues(b);
  return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false)
    e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}

const $ = (id) => document.getElementById(id);
// replaceChildren, minus null/false kids (replaceChildren would print them as "null").
const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const singers = (s) => (s || "").split(";").map((x) => x.trim()).filter(Boolean).join(", ");

class ApiError extends Error {
  constructor(status, body) { super((body && (body.error || body.detail)) || `HTTP ${status}`); this.status = status; this.body = body || {}; }
}

async function api(path, opts = {}) {
  const headers = { "X-Telegram-Init-Data": (WA && WA.initData) || "", "X-App-Session": SESSION };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method: opts.method || "GET", headers, keepalive: !!opts.keepalive,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401) { showRefused(); throw new ApiError(401, body); }
  if (!res.ok || (body && body.success === false)) throw new ApiError(res.status, body);
  return body;
}

const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v !== null && v !== undefined)
  .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");

function haptic(kind) {
  try { WA && WA.HapticFeedback && WA.HapticFeedback.impactOccurred(kind || "light"); } catch (_) { /* old client */ }
}

function confirmBox(text) {
  return new Promise((resolve) => {
    if (WA && WA.showConfirm && WA.isVersionAtLeast && WA.isVersionAtLeast("6.2")) WA.showConfirm(text, (ok) => resolve(!!ok));
    else resolve(window.confirm(text));
  });
}

// --- toast -------------------------------------------------------------------------------

let toastTimer = null;
function toast(text, undo) {
  $("toast-text").textContent = text;
  const u = $("toast-undo");
  u.hidden = !undo;
  u.onclick = undo ? () => { $("toast").hidden = true; undo(); } : null;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, undo ? 5000 : 2500);
}
const fail = (e) => toast(e && e.status === 401 ? "Not allowed" : `⚠️ ${e && e.message || e}`);

function showRefused() {
  fill($("views"), el("div", { class: "notice" },
    el("b", { text: "Open KaraokeGayak from Telegram." }),
    el("p", { class: "hint", text: "This page only works inside the KaraokeGayak bot." })));
  $("crumbs").hidden = true;
}

// --- navigation stack ----------------------------------------------------------------------

function topView() { return views[views.length - 1]; }

function push(view) {
  const cur = topView();
  if (cur) { cur.scrollY = window.scrollY; cur.el.hidden = true; }
  view.el = el("section", { class: "view" });
  views.push(view);
  $("views").append(view.el);
  window.scrollTo(0, 0);
  updateChrome();
  view.render();
}

function pop() {
  if (views.length < 2) return;
  const v = views.pop();
  v.closed = true;
  v.el.remove();
  const cur = topView();
  cur.el.hidden = false;
  updateChrome();
  requestAnimationFrame(() => window.scrollTo(0, cur.scrollY || 0));
  if (cur.onShow) cur.onShow();  // e.g. an event page picks up songs added on a Similar page
}

function updateChrome() {
  const titles = views.filter((v) => v.kind === "similar").map((v) => v.title);
  $("crumbs").hidden = titles.length < 2;
  $("crumbs").textContent = titles.slice(-3).join(" › ");
  if (WA && WA.BackButton) {
    if (views.length > 1 || overlayOpen()) WA.BackButton.show(); else WA.BackButton.hide();
  }
}

function playerFull() { return !$("player").hidden && !$("player").classList.contains("mini"); }
function overlayOpen() { return playerFull() || !$("rating").hidden || !$("edit").hidden; }

function onBack() {
  if (playerFull()) return closePlayer();
  if (!$("rating").hidden) return closeSheet("rating");
  if (!$("edit").hidden) return closeSheet("edit");
  pop();
}

function closeSheet(id) { $(id).hidden = true; updateChrome(); }

// --- Done / close ------------------------------------------------------------------------------

async function closeSession(keepalive) {
  try { return await api("/api/session/close", { method: "POST", body: {}, keepalive }); }
  catch (_) { return null; }
}

async function done() {
  await closeSession(false);
  if (WA && WA.close) WA.close(); else window.close();
}

function doneButton() {
  return el("button", { class: "btn secondary wide", type: "button", onclick: done }, "Done");
}

// --- Home -----------------------------------------------------------------------------------

function homeView() {
  const v = { kind: "home", title: "Home" };
  v.render = async () => {
    const input = el("input", { type: "search", placeholder: "Song title or YouTube link",
      enterkeyhint: "search", autocomplete: "off" });
    const results = el("div", { class: "list results", hidden: true });
    const recent = el("div", { class: "list recent" }, el("div", { class: "empty loading" }));
    const go = () => {
      const q = input.value.trim();
      if (!q) return;
      input.blur();
      if (/^https?:\/\/(www\.|m\.|music\.)?(youtube\.com|youtu\.be)\//i.test(q)) push(similarView({ url: q, title: "YouTube link" }));
      else push(similarView({ seed: q, title: q }));
    };
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 2) { results.hidden = true; return; }
        try {
          const r = await api(`/api/search?${qs({ q })}`);
          fill(results, ...(r.rows.length ? r.rows.map((row) => el("button", { type: "button",
            class: "item", onclick: () => push(similarView({ seed: row.song_id, title: row.title })) },
            el("div", { class: "title", text: row.title }),
            el("div", { class: "sub", text: [row.film, row.year, singers(row.singers)].filter(Boolean).join(" · ") })))
            : [el("div", { class: "empty", text: "No catalog match. Press search to use it as a seed anyway." })]));
          results.hidden = false;
        } catch (e) { if (e.status !== 401) fail(e); }
      }, 300);
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    fill(v.el,
      el("h1", { text: "🎵 KaraokeGayak" }),
      el("div", { class: "home-actions" },
        el("button", { class: "btn", type: "button", onclick: () => push(eventsView()) }, "📅 Events"),
        el("button", { class: "btn secondary", type: "button", onclick: () => push(eventFormView(null)) }, "＋ New event")),
      el("form", { class: "search", onsubmit: (e) => { e.preventDefault(); go(); } },
        input, el("button", { class: "btn", type: "submit" }, "Similar")),
      results,
      el("div", { class: "section-title", text: "Recent seeds" }),
      recent,
      doneButton());
    try {
      const r = await api("/api/home");
      fill(recent, ...(r.recent.length ? r.recent.map((s) => el("button", { type: "button",
        onclick: () => push(similarView({ seed: s.seed, url: s.url, title: s.title || s.seed })) },
        el("div", { text: s.title || s.seed || s.url })))
        : [el("div", { class: "empty", text: "No seeds yet. Search for a song above." })]));
    } catch (e) { if (e.status !== 401) fill(recent, el("div", { class: "empty", text: e.message })); }
  };
  return v;
}

// --- Suggest Similar -----------------------------------------------------------------------------

function similarView({ seed = "", url = "", title = "", event = null, karaoke = "" }) {
  // event: {id, name} when opened from an event page: rows get "＋ Event".
  // karaoke: the exact track the row was playing, for the 🎯 pinned row.
  const v = { kind: "similar", seed, url, karaoke, title: title || seed || url, page: 0, data: null, token: 0, event };
  v.render = () => loadPage(v, 0);
  return v;
}

async function loadPage(v, page) {
  const token = ++v.token;
  v.page = page;
  fill(v.el, el("h1", { text: v.title }), el("div", { class: "empty loading" }));
  let r;
  try {
    r = await api(`/api/similar?${qs({ seed: v.seed, url: v.url, karaoke: v.karaoke, page })}`);
  } catch (e) {
    if (e.status === 401) return;
    fill(v.el, el("h1", { text: v.title }), el("div", { class: "notice", text: e.message }),
      el("div", { class: "pager" }, el("button", { class: "btn", type: "button", onclick: () => loadPage(v, page) }, "Retry")));
    return;
  }
  if (token !== v.token || v.closed) return;
  v.session = r.session;
  v.data = r;
  v.title = r.seed.title || v.title;
  updateChrome();
  renderSimilar(v);
  window.scrollTo(0, 0);
  if (!r.outside) pollMix(v, page, token);
}

async function pollMix(v, page, token) {
  for (let i = 0; i < 6; i++) {
    let r;
    try { r = await api(`/api/similar/mix?${qs({ session: v.session, page })}`); }
    catch (e) { if (e.status === 401) return; await sleep(2000); continue; }
    if (token !== v.token || v.closed) return;
    if (r.mix === "loading") continue;
    const y = window.scrollY;
    if (!r.pinned && v.data && v.data.pinned && page === 0) r.pinned = v.data.pinned;
    v.data = r;
    v.title = r.seed.title || v.title;
    updateChrome();
    renderSimilar(v);
    if (!v.el.hidden) window.scrollTo(0, y);
    return;
  }
  if (token === v.token && v.data) { v.data.mix = "error"; v.data.outside = { items: [], end: true }; renderSimilar(v); }
}

// 🎯 The seed itself, first in its section on page 1: play, rate, edit (or add)
// and open in YouTube. No ♥ (a song can't be linked to itself), no ↪ (it's the seed).
function pinnedRow(v, p) {
  p._render = pinnedRow;
  p.song_key = p.song_key || (KG.videoId(p.karaoke_url) ? `yt:${KG.videoId(p.karaoke_url)}` : "");
  const inCat = !!p.catalog_id;
  const badge = adding[p.song_key] ? el("span", { class: "badge adding", text: "adding…" })
    : inCat && p.status ? el("span", { class: `badge ${p.status}`, text: p.status }) : null;
  const alts = p.alternatives || [];
  const altBox = el("div", { class: "alts", hidden: true }, alts.map((a) => el("button", { type: "button",
    onclick: () => reseed(v, a.song_id, a.title) }, a.title, el("span", { class: "sub", text: ` ${[a.film, a.year].filter(Boolean).join(" · ")}` }))));
  const row = el("div", { class: "item pinned" },
    el("div", { class: "pin", text: "🎯 Your song" }),
    el("div", { class: "top" }, primaryUrl(p) ? titleLink(v, p) : el("div", { class: "title", text: p.title }), badge),
    el("div", { class: "sub", text: [p.film, p.year, singers(p.singers)].filter(Boolean).join(" · ") }),
    p.karaoke === "none" ? el("div", { class: "nokaraoke" },
      el("span", { class: "badge warn", text: "No karaoke track found" }),
      el("button", { type: "button", class: "btn small secondary", onclick: () => openYouTube(p.search_url) }, "Search on YouTube")) : null,
    alts.length ? el("button", { type: "button", class: "link small", onclick: () => { altBox.hidden = !altBox.hidden; } }, "Not this one?") : null,
    altBox,
    el("div", { class: "bottom" },
      el("div", { class: "meta" }, energyBar(p.energy), p.tempo_class ? el("span", { text: p.tempo_class }) : null),
      el("div", { class: "actions" },
        p.song_key || inCat ? el("button", { type: "button", class: "rate", "aria-label": "Rating", onclick: () => openRating(v, p) },
          p.my_rating ? `⭐${p.my_rating}` : "☆") : null,
        p.song_key || inCat ? el("button", { type: "button", title: inCat ? "Edit" : "Add to catalog", "aria-label": "Edit",
          onclick: () => openEdit(v, p) }, "✎") : null,
        primaryUrl(p) ? el("button", { type: "button", title: "Open in YouTube", "aria-label": "Open in YouTube",
          onclick: () => openYouTube(primaryUrl(p)) }, "↗") : null)));
  p.el = row;
  return row;
}

function reseed(v, seed, title) {
  v.seed = seed; v.url = ""; v.karaoke = ""; v.title = title; v.data = null;
  updateChrome();
  loadPage(v, 0);
}

function renderSimilar(v) {
  const d = v.data;
  const pin = v.page === 0 && d.pinned ? d.pinned : null;
  const sub = [d.seed.film, d.seed.year, d.seed.in_catalog ? "in your catalog" : "not in catalog"].filter(Boolean).join(" · ");
  const cat = d.catalog;
  const catList = el("div", { class: "list" },
    cat.items.length ? cat.items.map((it) => renderItem(v, it)) : el("div", { class: "empty",
      text: v.page > 0 ? "No more catalog matches" : (d.seed.in_catalog || d.mix !== "loading"
        ? "No catalog matches" : "Waiting for the YouTube Mix…") }));
  if (cat.items.length && cat.end) catList.append(el("div", { class: "empty", text: "No more catalog matches" }));
  if (pin && pin.group === "catalog") catList.prepend(pinnedRow(v, pin));
  let outList;
  if (!d.outside) {
    outList = el("div", { class: "list" }, el("div", { class: "empty loading" }, "Loading YouTube Mix… "));
  } else {
    const o = d.outside;
    outList = el("div", { class: "list" }, o.items.length ? o.items.map((it) => renderItem(v, it))
      : el("div", { class: "empty", text: d.mix === "error" ? "YouTube Mix unavailable right now" : "End of YouTube Mix" }));
    if (o.items.length && o.end) outList.append(el("div", { class: "empty", text: "End of YouTube Mix" }));
    if (pin && pin.group === "outside") outList.prepend(pinnedRow(v, pin));
  }
  const mixDone = !!d.outside;
  const nextOff = cat.end && (!mixDone || d.outside.end);
  fill(v.el,
    el("h1", { text: d.seed.title || v.title }),
    el("div", { class: "seed-sub", text: sub }),
    el("div", { class: "section-title", text: "From your catalog" }),
    catList,
    el("div", { class: "section-title", text: "YouTube Mix (not in catalog)" }),
    outList,
    el("div", { class: "pager" },
      el("button", { class: "btn secondary", type: "button", disabled: v.page === 0, onclick: () => loadPage(v, v.page - 1) }, "‹ Previous"),
      el("button", { class: "btn", type: "button", disabled: nextOff, onclick: () => loadPage(v, v.page + 1) }, "Next ›")),
    (d.warnings || []).length ? el("p", { class: "hint", style: "margin:0 16px", text: d.warnings.join(" · ") }) : null,
    doneButton());
}

function energyBar(n) {
  const e = parseInt(n, 10);
  if (!e) return null;
  return el("span", { class: "energy", title: `energy ${e}/5` }, [1, 2, 3, 4, 5].map((i) => el("i", { class: i <= e ? "on" : "" })));
}

function renderItem(v, it) {
  const inCat = !!it.catalog_id;
  const badge = adding[it.song_key] ? el("span", { class: "badge adding", text: "adding…" })
    : inCat ? (it.status ? el("span", { class: `badge ${it.status}`, text: it.status }) : null)
      : it.karaoke === "none" ? el("span", { class: "badge", text: "no karaoke track" }) : null;
  const why = el("div", { class: "why", hidden: true, text: it.reasons || "No ranking reasons recorded." });
  const row = el("div", { class: "item" },
    el("div", { class: "top" },
      titleLink(v, it),
      badge),
    el("div", { class: "sub", text: [it.film, it.year, singers(it.singers)].filter(Boolean).join(" · ") }),
    why,
    el("div", { class: "bottom" },
      el("div", { class: "meta" }, energyBar(it.energy), it.tempo_class ? el("span", { text: it.tempo_class }) : null),
      el("div", { class: "actions" },
      el("button", { type: "button", class: `like${it.liked ? " on" : ""}`, "aria-label": "Like",
        onclick: () => toggleLike(v, it) }, it.liked ? "♥" : "♡"),
      el("button", { type: "button", class: "rate", "aria-label": "Rating",
        onclick: () => openRating(v, it) }, it.my_rating ? `⭐${it.my_rating}` : "☆"),
      el("button", { type: "button", "aria-label": "Similar", title: "Similar",
        onclick: () => push(similarView({ seed: it.catalog_id || it.title,
          url: it.catalog_id ? "" : (it.video_url || ""), title: it.title, event: v.event,
          karaoke: it.catalog_id ? "" : (it.karaoke_url || "") })) }, "↪"),
      v.event ? el("button", { type: "button", class: `add${it.inEvent ? " on" : ""}`, title: `Add to ${v.event.name}`,
        "aria-label": "Add to event", onclick: () => addToEvent(v, it) }, it.inEvent ? "✓" : "＋") : null,
      el("button", { type: "button", "aria-label": "Edit", title: "Edit", onclick: () => openEdit(v, it) }, "✎"),
      el("button", { type: "button", "aria-label": "Why", title: "Why",
        onclick: () => { why.hidden = !why.hidden; } }, "ⓘ"))));
  it.el = row;
  return row;
}

function refresh(v, it) {
  if (it.el && it.el.isConnected) it.el.replaceWith((it._render || renderItem)(v, it));
}

async function addToEvent(v, it) {
  if (it.inEvent) return;
  try {
    await api(`/api/events/${encodeURIComponent(v.event.id)}/songs`, { method: "POST", body: {
      song_key: it.catalog_id || it.song_key, title: it.title, film: it.film,
      karaoke_url: it.karaoke_url, video_url: it.video_url } });
    it.inEvent = true;
    refresh(v, it);
    haptic();
    toast(`Added to ${v.event.name} ✓`);
  } catch (e) {
    if (e.status === 409) { it.inEvent = true; refresh(v, it); toast(e.message); } else fail(e);
  }
}

// --- like ---------------------------------------------------------------------------------

async function toggleLike(v, it, value) {
  const seedKey = v.data && v.data.seed.key;
  if (!seedKey) return toast("Wait for the YouTube Mix to load first");
  const liked = value === undefined ? !it.liked : value;
  const before = it.liked;
  it.liked = liked;
  refresh(v, it);
  haptic();
  try {
    await api("/api/like", { method: "POST", body: { seed_key: seedKey, song_key: it.song_key, liked,
      seed_title: v.data.seed.title, song_title: it.title } });
    toast(liked ? "Saved ♥ ✓" : "Unliked ✓", () => toggleLike(v, it, before));
  } catch (e) { it.liked = before; refresh(v, it); fail(e); }
}

// --- rating ---------------------------------------------------------------------------------

let ratingCtx = null;

function openRating(v, it) {
  ratingCtx = { v, it };
  $("rating-title").textContent = `Rating · ${it.title}`;
  const wheel = $("wheel");
  if (!wheel.childElementCount) {
    for (let i = 10; i >= 1; i--) wheel.append(el("div", { "data-v": i, onclick: () => setWheel(i, true) }, String(i)));
    wheel.addEventListener("scroll", () => {
      clearTimeout(wheel._t);
      wheel._t = setTimeout(markWheel, 60);
    });
  }
  $("rating").hidden = false;
  updateChrome();
  setWheel(parseInt(it.my_rating, 10) || 8, false);
}

function wheelValue() {
  const w = $("wheel");
  return 10 - Math.round(w.scrollTop / 44);
}

function markWheel() {
  const val = wheelValue();
  for (const d of $("wheel").children) d.classList.toggle("on", +d.dataset.v === val);
  $("rating-set").textContent = `Set ⭐${val}`;
}

function setWheel(val, save) {
  $("wheel").scrollTop = (10 - val) * 44;
  markWheel();
  if (save) saveRating(val);
}

$("rating-set").onclick = () => saveRating(wheelValue());
$("rating-clear").onclick = () => saveRating(null);
$("rating").addEventListener("click", (e) => { if (e.target.id === "rating") closeSheet("rating"); });

async function saveRating(value) {
  if (!ratingCtx) return;
  const { v, it } = ratingCtx;
  closeSheet("rating");
  await applyRating(v, it, value, true);
}

async function applyRating(v, it, value, withUndo) {
  const before = it.my_rating ? parseInt(it.my_rating, 10) : null;
  if (value === before) return;
  haptic();
  try {
    const r = await api("/api/rating", { method: "POST", body: { song_key: it.catalog_id || it.song_key, rating: value } });
    if (r.needs_add) {
      if (r.adding) return toast("Still adding this song… try again in a moment");
      if (value === null) return;
      if (!(await confirmBox(`Add "${it.title}" to your catalog with ⭐${value}?`))) return;
      return addSong(v, it, { title: it.title, film: it.film, karaoke_url: it.karaoke_url }, value);
    }
    it.my_rating = r.my_rating;
    it.status = r.status;
    refresh(v, it);
    const msg = value === null ? "Rating cleared ✓" : `⭐${value} saved ✓ · ${r.status}`;
    toast(msg, withUndo ? () => applyRating(v, it, before, false) : null);
  } catch (e) { fail(e); }
}

// --- add + jobs -------------------------------------------------------------------------------

async function pollJob(id) {
  for (let i = 0; i < 150; i++) {
    await sleep(i < 5 ? 1500 : 3000);
    try {
      const j = await api(`/api/jobs/${id}`);
      if (j.status !== "running") return j;
    } catch (e) { if (e.status === 401 || e.status === 404) return { status: "error", error: e.message }; }
  }
  return { status: "error", error: "timed out" };
}

async function addSong(v, it, fields, rating) {
  let r;
  try {
    r = await api("/api/song/add", { method: "POST", body: { song_key: it.song_key, fields, rating } });
  } catch (e) { fail(e); return e; }
  adding[it.song_key] = r.job_id;
  refresh(v, it);
  toast("Adding to catalog… (about a minute)");
  pollJob(r.job_id).then((j) => {
    delete adding[it.song_key];
    if (j.status === "done") {
      it.catalog_id = j.song_id;
      if (rating) it.my_rating = String(rating);
      it.status = rating ? (rating >= 6 ? "ready" : "practicing") : "wishlist";
      toast(`Added ✓ ${it.title}`);
    } else toast(`⚠️ Add failed: ${j.error || "unknown error"}`);
    refresh(v, it);
  });
  return null;
}

// --- edit sheet -----------------------------------------------------------------------------------

const MULTI = ["genre", "mood", "theme", "situation"];
const SELECTS = ["voice_type", "tempo_class", "status"];
const LABELS = { music_director: "Music director", voice_type: "Voice type", tempo_class: "Tempo",
  my_rating: "Rating (1–10)", my_key_change: "Key change (semitones)", karaoke_url: "Karaoke YouTube link",
  karaoke_alt_urls: "Alternate links (; separated)", singers: "Singers (; separated)" };
let editCtx = null;

async function getVocab() {
  if (!vocabCache) vocabCache = await api("/api/vocab");
  return vocabCache;
}

async function openEdit(v, it) {
  let voc, s;
  try {
    [voc, s] = await Promise.all([getVocab(), api(`/api/song/${encodeURIComponent(it.catalog_id || it.song_key)}?${qs({
      title: it.title, film: it.film, karaoke_url: it.karaoke_url })}`)]);
  } catch (e) { return fail(e); }
  editCtx = { v, it, inCatalog: s.in_catalog, row: s.row, inputs: {}, chips: {} };
  $("edit-title").textContent = s.in_catalog ? `Edit · ${s.row.title}` : "Add to catalog";
  $("edit-save").textContent = s.in_catalog ? "Save" : "Add to catalog";
  $("edit-reanalyse").hidden = !s.in_catalog;
  $("edit-error").hidden = true;
  const body = $("edit-body");
  fill(body, ...voc.editable.map((f) => field(f, s.row[f] || "", voc)));
  $("edit").hidden = false;
  $("edit").querySelector(".sheet").scrollTop = 0;
  updateChrome();
}

function field(name, value, voc) {
  const label = LABELS[name] || name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const err = el("div", { class: "error", hidden: true });
  let control;
  if (MULTI.includes(name)) {
    const on = new Set(value.split(";").filter(Boolean));
    control = el("div", { class: "chips" }, voc.multi[name].map((opt) => el("button", { type: "button",
      class: `chip${on.has(opt) ? " on" : ""}`, onclick: (e) => e.currentTarget.classList.toggle("on") }, opt)));
    editCtx.chips[name] = control;
  } else if (SELECTS.includes(name) || name === "energy" || name === "my_rating") {
    const opts = SELECTS.includes(name) ? voc.enums[name] : name === "energy" ? ["1", "2", "3", "4", "5"]
      : ["10", "9", "8", "7", "6", "5", "4", "3", "2", "1"];
    control = el("select", { name }, el("option", { value: "" }, "—"),
      opts.map((o) => el("option", { value: o, selected: String(value) === o }, o)));
    editCtx.inputs[name] = control;
  } else if (name === "notes") {
    control = el("textarea", { name });
    control.value = value;
    editCtx.inputs[name] = control;
  } else {
    const input = el("input", { name, type: ["year", "my_key_change"].includes(name) ? "number" : "text",
      inputmode: name === "year" ? "numeric" : null, autocomplete: "off" });
    input.value = value.replace && name.endsWith("_urls") ? value.replace(/;/g, "; ") : value;
    editCtx.inputs[name] = input;
    control = input;
    if (name === "karaoke_url") {
      const cands = el("div", { class: "cands" });
      control = el("div", {}, el("div", { class: "inline" }, input,
        el("button", { type: "button", class: "btn secondary", onclick: () => findKaraoke(cands) }, "Find")), cands);
    }
  }
  const wrap = el("div", { class: "field", "data-field": name }, el("label", { text: label }), control, err);
  return wrap;
}

async function findKaraoke(box) {
  const title = editCtx.inputs.title.value.trim();
  if (!title) return toast("Type the title first");
  fill(box, el("div", { class: "empty loading" }));
  try {
    const r = await api(`/api/karaoke/find?${qs({ title, film: editCtx.inputs.film.value.trim() })}`);
    fill(box, ...(r.candidates.length ? r.candidates.map((c) => el("button", { type: "button",
      onclick: () => { editCtx.inputs.karaoke_url.value = c.url; fill(box); } },
      `${c.title} · ${c.channel} · ${Math.round(c.score)}${c.source === "index" ? " · Anil Chauhan" : ""}`))
      : [el("div", { class: "empty", text: "No karaoke video found" })]));
  } catch (e) { fill(box); fail(e); }
}

function collect() {
  const out = {};
  for (const [k, input] of Object.entries(editCtx.inputs)) {
    let val = input.value.trim();
    if (k.endsWith("_urls") || k === "singers" || k === "music_director" || k === "lyricist")
      val = val.split(/\s*;\s*/).filter(Boolean).join(";");
    out[k] = val;
  }
  for (const [k, box] of Object.entries(editCtx.chips))
    out[k] = [...box.querySelectorAll(".chip.on")].map((c) => c.textContent).join(";");
  return out;
}

function showErrors(errs) {
  for (const f of $("edit-body").querySelectorAll(".field")) {
    const msg = errs[f.dataset.field];
    f.classList.toggle("bad", !!msg);
    const e = f.querySelector(".error");
    e.hidden = !msg;
    e.textContent = msg ? `${f.dataset.field.replace(/_/g, " ")}: ${msg}` : "";
  }
  const other = Object.entries(errs).filter(([k]) => !editCtx.inputs[k] && !editCtx.chips[k]).map(([k, m]) => `${k === "_" ? "" : k + ": "}${m}`);
  $("edit-error").hidden = !other.length;
  $("edit-error").textContent = other.join(" · ");
  const first = $("edit-body").querySelector(".field.bad");
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
}

$("edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!editCtx) return;
  const { v, it, inCatalog, row } = editCtx;
  const all = collect();
  showErrors({});
  const save = $("edit-save");
  save.disabled = true;
  try {
    if (inCatalog) {
      const changed = Object.fromEntries(Object.entries(all).filter(([k, val]) => val !== (row[k] || "")));
      if (!Object.keys(changed).length) { closeSheet("edit"); return; }
      const r = await api(`/api/song/${encodeURIComponent(row.song_id)}`, { method: "PUT", body: { fields: changed } });
      Object.assign(it, { title: r.row.title, film: r.row.film, year: r.row.year, singers: r.row.singers,
        status: r.row.status, energy: r.row.energy, tempo_class: r.row.tempo_class, my_rating: r.row.my_rating,
        karaoke_url: r.row.karaoke_url });
      refresh(v, it);
      closeSheet("edit");
      toast("Saved ✓");
      if (r.karaoke_changed && await confirmBox("Karaoke link changed. Re-analyse tempo, key and energy from the new track?"))
        reanalyse(row.song_id);
    } else {
      const fields = Object.fromEntries(Object.entries(all).filter(([, val]) => val !== ""));
      const rating = fields.my_rating ? parseInt(fields.my_rating, 10) : null;
      delete fields.my_rating;
      const err = await addSong(v, it, fields, rating);
      if (err) { if (err.body && err.body.field_errors) showErrors(err.body.field_errors); return; }
      closeSheet("edit");
    }
  } catch (err) {
    if (err.body && err.body.field_errors) showErrors(err.body.field_errors);
    else { $("edit-error").hidden = false; $("edit-error").textContent = err.message; }
  } finally { save.disabled = false; }
});

$("edit-cancel").onclick = () => closeSheet("edit");
$("edit-reanalyse").onclick = () => { if (editCtx && editCtx.inCatalog) reanalyse(editCtx.row.song_id); };

async function reanalyse(songId) {
  try {
    const r = await api(`/api/song/${encodeURIComponent(songId)}/reanalyse`, { method: "POST", body: {} });
    toast("Re-analysing… (about a minute)");
    const j = await pollJob(r.job_id);
    const note = (j.steps || []).map((s) => s.note).filter(Boolean).pop() || "";
    toast(j.status === "done" ? `Re-analysed ✓ ${note}` : `⚠️ Re-analyse failed: ${j.error || note}`);
  } catch (e) {
    if (e.body && e.body.field_errors && !$("edit").hidden) showErrors(e.body.field_errors); else fail(e);
  }
}

// --- player ----------------------------------------------------------------------------------------
// Songs play inside the app (an overlay with a YouTube player; ▾ keeps it playing
// in a bar at the bottom). Fallback logic lives in player.js (unit-tested).

const KG = window.KGPlayer;
const safeStorage = () => { try { return window.localStorage; } catch (_) { return null; } };
const embedCache = new KG.EmbedCache(safeStorage());
let ytReady = null;
let playerCtx = null;   // {it, v, karaoke, original, tab, current}

function loadYT() {
  if (!ytReady) ytReady = new Promise((resolve, reject) => {
    if (window.YT && window.YT.Player) return resolve(window.YT);
    const t = setTimeout(() => { ytReady = null; reject(new Error("timeout")); }, KG.READY_TIMEOUT_MS);
    window.onYouTubeIframeAPIReady = () => { clearTimeout(t); resolve(window.YT); };
    const s = el("script", { src: "https://www.youtube.com/iframe_api" });
    s.onerror = () => { clearTimeout(t); ytReady = null; reject(new Error("load")); };
    document.head.append(s);
  });
  return ytReady;
}

// Our own iframe, so the referrer policy and allow list apply from the first load.
function mountYT(id, host, h) {
  const box = $("yt");
  const iframe = el("iframe", { src: KG.embedSrc(id, host, location.origin), title: "YouTube player",
    allow: "autoplay; encrypted-media; fullscreen; picture-in-picture", allowfullscreen: true,
    referrerpolicy: "strict-origin-when-cross-origin", frameborder: "0" });
  fill(box, iframe);
  const p = new window.YT.Player(iframe, { events: {
    onReady: () => { h.onReady(); try { p.playVideo(); } catch (_) { /* iOS may want a tap on the player */ } },
    onError: (e) => h.onError(e.data),
    onStateChange: (e) => h.onStateChange(e.data) } });
  return {
    playVideo: () => p.playVideo && p.playVideo(),
    pauseVideo: () => p.pauseVideo && p.pauseVideo(),
    destroy: () => { try { p.destroy(); } catch (_) { /* gone */ } fill(box); },
  };
}

const player = new KG.PlayerController({
  cache: embedCache, hosts: new KG.HostStore(safeStorage()), mount: mountYT,
  setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t),
  onState: onPlayerState,
});

const FALLBACK_TEXT = {
  timeout: "The player didn't load. Open the video in YouTube instead.",
  load: "The YouTube player couldn't load here. Open the video in YouTube instead.",
  100: "This video isn't available on YouTube any more (removed or private).",
  "no-video": "No video link for this song.",
};

function onPlayerState(state, info) {
  if (!playerCtx) return;
  $("player-loading").hidden = state !== "loading";
  if (state === "fallback") {
    $("player-frame").hidden = true;
    $("player-fallback").hidden = false;
    $("player-fallback-text").textContent = FALLBACK_TEXT[info.reason] || "This video can't play inside the app.";
    $("player-open-big").hidden = !playerCtx.current;
    if (embedCache.has(info.videoId) && playerCtx.v) refresh(playerCtx.v, playerCtx.it);  // the row gets ↗ now
  }
}

function watchUrl(url) {
  const id = KG.videoId(url);
  return id ? `https://www.youtube.com/watch?v=${id}` : url;
}

// Leaves Telegram for the YouTube app or browser; the mini app itself never navigates away.
function openYouTube(url) {
  if (!url) return;
  player.pause();
  if (WA && WA.openLink) WA.openLink(watchUrl(url)); else window.open(watchUrl(url), "_blank", "noopener");
}

function primaryUrl(it) { return it.karaoke_url || it.video_url || ""; }
function blocked(url) { return !!url && embedCache.has(KG.videoId(url)); }

// The song title link used in every list: plays in the app, or opens YouTube
// straight away (↗) when we already know the video can't play embedded.
function titleLink(v, it) {
  const url = primaryUrl(it);
  const ext = blocked(url) && (!it.video_url || it.video_url === url || blocked(it.video_url));
  return el("a", { class: "title", href: "#", onclick: (e) => {
    e.preventDefault();
    if (ext) openYouTube(url); else openPlayer(it, v);
  } }, it.title, ext ? el("span", { class: "ext", title: "Opens in YouTube" }, " ↗") : null);
}

function openPlayer(it, v) {
  playerCtx = { it, v, karaoke: it.karaoke_url || "", original: it.video_url || "" };
  const kt = document.querySelector('[data-tab="karaoke"]');
  kt.disabled = !playerCtx.karaoke;
  kt.textContent = playerCtx.karaoke ? "Karaoke" : "No karaoke";
  $("player").hidden = false;
  $("player").classList.remove("mini");
  document.body.classList.remove("has-mini");
  $("player-fs").hidden = !(document.fullscreenEnabled || document.webkitFullscreenEnabled ||
    (WA && WA.requestFullscreen && WA.isVersionAtLeast && WA.isVersionAtLeast("8.0")));
  updateChrome();
  playTab(playerCtx.karaoke ? "karaoke" : "original");
}

async function playTab(tab) {
  const ctx = playerCtx;
  ctx.tab = tab;
  for (const b of document.querySelectorAll(".player-tabs .tab")) b.classList.toggle("on", b.dataset.tab === tab);
  $("player-fallback").hidden = true;
  $("player-frame").hidden = false;
  $("player-loading").hidden = false;
  const label = ctx.it.title + (tab === "original" ? " · original" : " · karaoke");
  $("player-title").textContent = label;
  $("mini-title").textContent = label;
  let url = ctx[tab];
  ctx.current = url;
  setOpenButtons(url);
  if (!url && tab === "original") {
    $("player-title").textContent = "Finding the original video…";
    try {
      const r = await api(`/api/original?${qs({ title: ctx.it.title, film: ctx.it.film })}`);
      url = ctx.original = ctx.current = r.url;
    } catch (e) { url = ""; }
    if (ctx !== playerCtx || ctx.tab !== tab) return;
    $("player-title").textContent = label;
    setOpenButtons(url);
  }
  const id = KG.videoId(url);
  if (id && !embedCache.has(id)) {
    // Server-side oEmbed check in parallel: 401/403 = embedding disabled.
    api(`/api/embeddable?${qs({ url })}`).then((r) => {
      if (r.embeddable === false && ctx === playerCtx && ctx.current === url) player.notEmbeddable(id);
    }).catch(() => {});
  }
  try {
    if (id && !embedCache.has(id)) await loadYT();
  } catch (e) {
    if (ctx === playerCtx && ctx.current === url) { player.stop(); onPlayerState("fallback", { reason: e.message, videoId: id }); }
    return;
  }
  if (ctx !== playerCtx || ctx.current !== url) return;
  player.play(url);
}

function setOpenButtons(url) {
  $("player-open").disabled = !url;
  $("player-open").onclick = () => openYouTube(url);
  $("player-open-big").onclick = () => openYouTube(url);
}

function closePlayer() {
  player.stop();   // pauses and removes the player
  playerCtx = null;
  $("player").hidden = true;
  $("player").classList.remove("mini");
  document.body.classList.remove("has-mini");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  updateChrome();
}

// ▾: keep the song playing in a bar at the bottom while you like or rate.
function minimisePlayer() {
  if (!playerCtx) return;
  if (player.state !== "ready") return closePlayer();   // nothing playing: just close
  $("player").classList.add("mini");
  document.body.classList.add("has-mini");
  updateChrome();
}

function expandPlayer() {
  $("player").classList.remove("mini");
  document.body.classList.remove("has-mini");
  updateChrome();
}

for (const b of document.querySelectorAll(".player-tabs .tab")) b.onclick = () => { if (!b.disabled && playerCtx) playTab(b.dataset.tab); };
$("player-close").onclick = closePlayer;
$("player-min").onclick = minimisePlayer;
$("mini-expand").onclick = expandPlayer;
$("mini-close").onclick = closePlayer;
$("player-fs").onclick = () => {
  const f = $("player-frame");
  if (f.requestFullscreen) f.requestFullscreen().catch(() => {});
  else if (f.webkitRequestFullscreen) f.webkitRequestFullscreen();
  else if (WA && WA.requestFullscreen) WA.requestFullscreen();
};
$("player").addEventListener("click", (e) => { if (e.target.id === "player" && !$("player").classList.contains("mini")) closePlayer(); });

// --- Event Planner (spec §5) ---------------------------------------------------------------------

const fmtMin = (sec) => `${Math.round((sec || 0) / 60)} min`;
const eventPath = (id, rest = "") => `/api/events/${encodeURIComponent(id)}${rest}`;
let eventVocab = null;

async function getEventVocab() {
  if (!eventVocab) eventVocab = await api("/api/events/vocab");
  return eventVocab;
}

function statusBadge(status) {
  return el("span", { class: `badge ev-${status}`, text: status });
}

// Events list: Upcoming and Past.
function eventsView() {
  const v = { kind: "events", title: "Events" };
  v.render = async () => {
    fill(v.el, el("h1", { text: "📅 Events" }), el("div", { class: "empty loading" }));
    let r;
    try { r = await api("/api/events"); } catch (e) { if (e.status !== 401) v.el.append(el("div", { class: "notice", text: e.message })); return; }
    const row = (e, past) => el("div", { class: "item ev-row", role: "button", tabindex: 0,
      onclick: () => push(eventView(e.id)) },
      el("div", { class: "top" }, el("div", { class: "title", text: e.name }), statusBadge(e.status)),
      el("div", { class: "sub", text: [e.date, e.time, e.theme.replace(/;/g, ", ")].filter(Boolean).join(" · ") }),
      el("div", { class: "bottom" },
        el("div", { class: "meta", text: `${e.songs} song${e.songs === 1 ? "" : "s"} · ${e.estimated ? "≈ " : ""}${fmtMin(e.total_sec)}` }),
        past ? el("button", { type: "button", class: `btn small${e.needs_feedback ? "" : " secondary"}`,
          onclick: (ev) => { ev.stopPropagation(); push(feedbackView(e.id)); } }, e.needs_feedback ? "Give feedback" : "Feedback") : null));
    fill(v.el,
      el("h1", { text: "📅 Events" }),
      el("button", { class: "btn wide", type: "button", onclick: () => push(eventFormView(null)) }, "＋ New event"),
      el("div", { class: "section-title", text: "Upcoming" }),
      el("div", { class: "list" }, r.upcoming.length ? r.upcoming.map((e) => row(e, false)) : el("div", { class: "empty", text: "Nothing planned yet." })),
      el("div", { class: "section-title", text: "Past" }),
      el("div", { class: "list" }, r.past.length ? r.past.map((e) => row(e, true)) : el("div", { class: "empty", text: "No past events." })),
      doneButton());
  };
  v.onShow = () => v.render();
  return v;
}

// New event / edit details.
function eventFormView(existing) {
  const v = { kind: "event-form", title: existing ? "Event details" : "New event" };
  v.render = async () => {
    let voc;
    try { voc = await getEventVocab(); } catch (e) { return fail(e); }
    const m = existing ? existing.meta : { name: "", date: "", time: "", theme: "", instructions: "",
      duration_min: "", song_count: "", event_type: "", venue: "", audience: "" };
    const inputs = {};
    const err = {};
    const field = (name, label, control) => {
      err[name] = el("div", { class: "error", hidden: true });
      return el("div", { class: "field", "data-field": name }, el("label", { text: label }), control, err[name]);
    };
    const input = (name, attrs = {}) => { const i = el("input", { name, autocomplete: "off", ...attrs }); i.value = m[name] || ""; inputs[name] = i; return i; };
    const themeOn = new Set((m.theme || "").split(";").filter(Boolean));
    const known = new Set([...voc.situations, ...voc.themes]);
    const chips = el("div", { class: "chips" }, [...new Set([...voc.situations, ...voc.themes])].map((t) =>
      el("button", { type: "button", class: `chip${themeOn.has(t) ? " on" : ""}`, onclick: (e) => e.currentTarget.classList.toggle("on") }, t)));
    const freeTheme = el("input", { placeholder: "Other theme (free text)", autocomplete: "off" });
    freeTheme.value = [...themeOn].filter((t) => !known.has(t)).join("; ");
    const type = el("select", { name: "event_type" }, el("option", { value: "" }, "—"),
      voc.event_types.map((t) => el("option", { value: t, selected: m.event_type === t }, t)));
    inputs.event_type = type;
    const instr = el("textarea", { name: "instructions", rows: 3,
      placeholder: "e.g. 45 minutes, mostly 70s–80s, 2 duets, start peppy, end with a singalong, nothing too high" });
    instr.value = m.instructions || "";
    inputs.instructions = instr;
    const save = el("button", { class: "btn wide", type: "submit" }, existing ? "Save" : "Create draft");
    const formErr = el("p", { class: "error", hidden: true });
    const form = el("form", { class: "form-view", novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      const fields = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()]));
      fields.theme = [...chips.querySelectorAll(".chip.on")].map((c) => c.textContent)
        .concat(freeTheme.value.split(/\s*;\s*/).filter(Boolean)).join(";");
      for (const [k, box] of Object.entries(err)) { box.hidden = true; box.closest(".field").classList.remove("bad"); }
      formErr.hidden = true;
      save.disabled = true;
      save.textContent = fields.instructions ? "Reading the instructions…" : "Saving…";
      try {
        const r = existing ? await api(eventPath(existing.id), { method: "PUT", body: { fields } })
          : await api("/api/events", { method: "POST", body: fields });
        haptic();
        if (existing) { pop(); toast("Saved ✓"); }
        else { views.pop().el.remove(); push(eventView(r.id)); toast("Draft saved ✓"); }
      } catch (ex) {
        const fe = ex.body && ex.body.field_errors;
        if (fe) for (const [k, msg] of Object.entries(fe)) {
          const box = err[k];
          if (box) { box.hidden = false; box.textContent = msg; box.closest(".field").classList.add("bad"); }
          else { formErr.hidden = false; formErr.textContent = `${k}: ${msg}`; }
        } else { formErr.hidden = false; formErr.textContent = ex.message; }
      } finally { save.disabled = false; save.textContent = existing ? "Save" : "Create draft"; }
    } },
      field("name", "Name", input("name", { placeholder: "Diwali 2026" })),
      el("div", { class: "two" }, field("date", "Date", input("date", { type: "date" })),
        field("time", "Time", input("time", { type: "time" }))),
      field("event_type", "Kind of event", type),
      field("theme", "Theme", el("div", {}, chips, freeTheme)),
      field("instructions", "Instructions", instr),
      el("div", { class: "two" }, field("duration_min", "Duration (min)", input("duration_min", { type: "number", inputmode: "numeric" })),
        field("song_count", "Number of songs", input("song_count", { type: "number", inputmode: "numeric" }))),
      field("venue", "Venue", input("venue")),
      field("audience", "Audience", input("audience", { placeholder: "e.g. friends, 40-55, love classic Bollywood" })),
      formErr, save);
    fill(v.el, el("h1", { text: v.title }), form);
  };
  return v;
}

function energyCurve(values) {
  if (!values.length) return null;
  const w = 120, h = 26, step = values.length > 1 ? w / (values.length - 1) : 0;
  const pts = values.map((e, i) => `${(i * step).toFixed(1)},${(h - 3 - ((e || 3) - 1) / 4 * (h - 6)).toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `-3 0 ${w + 6} ${h}`);
  svg.setAttribute("class", "curve");
  svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  return svg;
}

function summaryBar(sm) {
  const target = sm.target_min ? ` / ${sm.target_min} min` : "";
  const over = sm.target_min && sm.total_sec / 60 > sm.target_min * 1.1;
  const parts = [el("b", { class: over ? "over" : "", text: `${sm.estimated ? "≈ " : ""}${fmtMin(sm.total_sec)}${target}` }),
    el("span", { text: `${sm.songs}${sm.song_count ? "/" + sm.song_count : ""} songs` }),
    el("span", { text: `${sm.duets}${sm.duets_wanted ? "/" + sm.duets_wanted : ""} duet${sm.duets === 1 && !sm.duets_wanted ? "" : "s"}` })];
  return el("div", { class: "ev-summary" }, ...parts, energyCurve(sm.energy));
}

// Event page: playlist + suggestions + YouTube ideas.
function eventView(id) {
  const v = { kind: "event", id, title: "Event", data: null, sugPage: 0, ideasPage: 0, ideasOpen: false };
  v.event = { id, name: "this event" };
  v.render = () => loadEvent(v);
  v.onShow = () => loadEvent(v, true);
  return v;
}

async function loadEvent(v, keepScroll) {
  const y = window.scrollY;
  if (!v.data) fill(v.el, el("h1", { text: "Event" }), el("div", { class: "empty loading" }));
  try { v.data = await api(eventPath(v.id)); } catch (e) {
    if (e.status !== 401) fill(v.el, el("div", { class: "notice", text: e.message }));
    return;
  }
  v.title = v.data.meta.name;
  v.event = { id: v.id, name: v.data.meta.name };
  updateChrome();
  renderEvent(v);
  if (keepScroll) window.scrollTo(0, y);
}

function asItem(it) {
  it.song_key = it.song_key || it.key;
  return it;
}

function eventRow(v, it, kind) {
  asItem(it);
  it._render = (vv, x) => eventRow(vv, x, kind);
  const editableEv = v.data.editable;
  // The status badge already says practicing/wishlist: show only the other warnings.
  const warn = (it.warnings || []).filter((w) => !(it.status && w.startsWith(it.status)))
    .map((w) => el("span", { class: "badge warn", text: w }));
  const badge = adding[it.song_key] ? el("span", { class: "badge adding", text: "adding…" })
    : it.catalog_id && it.status ? el("span", { class: `badge ${it.status}`, text: it.status })
      : !it.catalog_id ? el("span", { class: "badge", text: it.karaoke === "none" ? "no karaoke track" : "outside" }) : null;
  const why = el("div", { class: "why", hidden: true, text: it.reasons || "" });
  const acts = [];
  if (kind !== "playlist" && editableEv)
    acts.push(el("button", { type: "button", class: "add", title: "Add to playlist", "aria-label": "Add",
      onclick: () => addSuggestion(v, it, kind) }, "＋"));
  acts.push(el("button", { type: "button", class: "rate", "aria-label": "Rating", onclick: () => openRating(v, it) },
    it.my_rating ? `⭐${it.my_rating}` : "☆"));
  acts.push(el("button", { type: "button", title: "Similar", "aria-label": "Similar", onclick: () => push(similarView({
    seed: it.catalog_id || it.title, url: it.catalog_id ? "" : (it.video_url || ""), title: it.title,
    karaoke: it.catalog_id ? "" : (it.karaoke_url || ""), event: editableEv ? v.event : null })) }, "↪"));
  acts.push(el("button", { type: "button", title: "Edit", "aria-label": "Edit", onclick: () => openEdit(v, it) }, "✎"));
  if (it.reasons) acts.push(el("button", { type: "button", title: "Why", "aria-label": "Why", onclick: () => { why.hidden = !why.hidden; } }, "ⓘ"));
  if (editableEv) acts.push(el("button", { type: "button", class: "x", title: kind === "playlist" ? "Remove" : "Not for this event",
    "aria-label": kind === "playlist" ? "Remove" : "Not for this event", onclick: () => dismiss(v, it, kind) }, "✕"));
  const row = el("div", { class: `item${kind === "playlist" ? " pl" : ""}`, "data-key": it.key || it.song_key },
    el("div", { class: "top" },
      kind === "playlist" && editableEv ? el("span", { class: "handle", title: "Drag to reorder", "aria-label": "Drag to reorder" }, "≡") : null,
      kind === "playlist" ? el("span", { class: "num", text: `${v.data.playlist.indexOf(it) + 1}.` }) : null,
      titleLink(v, it), badge),
    el("div", { class: "sub", text: [it.film, it.year, singers(it.singers), it.duration_sec ? fmtMin(+it.duration_sec).replace(" min", "′") : ""].filter(Boolean).join(" · ") }),
    warn.length ? el("div", { class: "warns" }, warn) : null,
    why,
    el("div", { class: "bottom" },
      el("div", { class: "meta" }, energyBar(it.energy), it.tempo_class ? el("span", { text: it.tempo_class }) : null,
        it.voice_type && it.voice_type.startsWith("duet") ? el("span", { text: "duet" }) : null),
      el("div", { class: "actions" }, acts)));
  it.el = row;
  return row;
}

function renderEvent(v) {
  const d = v.data;
  const m = d.meta;
  const sub = [m.date, m.time, m.theme.replace(/;/g, ", "), m.event_type].filter(Boolean).join(" · ");
  const chips = el("div", { class: "chips ev-chips" }, d.chips.map((c) => el("span", { class: "chip on" }, c.label,
    d.editable ? el("button", { type: "button", class: "chip-x", "aria-label": `Remove ${c.label}`,
      onclick: () => removeChip(v, c.id) }, "×") : null)));
  const list = el("div", { class: "list playlist" }, d.playlist.length ? d.playlist.map((it) => eventRow(v, it, "playlist"))
    : el("div", { class: "empty", text: "Empty. Add songs from the suggestions below, the search box, or ↪ Similar." }));
  if (d.editable && d.playlist.length > 1) makeSortable(list, (from, to) => moveSong(v, from, to));
  const search = el("input", { type: "search", placeholder: "Add from catalog: search a title", autocomplete: "off" });
  const results = el("div", { class: "list results", hidden: true });
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = search.value.trim();
      if (q.length < 2) { results.hidden = true; return; }
      try {
        const r = await api(`/api/search?${qs({ q })}`);
        const inList = new Set(d.playlist.map((x) => x.catalog_id || x.key));
        fill(results, ...(r.rows.length ? r.rows.map((row) => el("div", { class: "item" },
          el("div", { class: "top" }, el("div", { class: "title", text: row.title }),
            inList.has(row.song_id) ? el("span", { class: "badge", text: "in playlist" })
              : el("button", { type: "button", class: "btn small", onclick: async () => {
                await addSuggestion(v, { song_key: row.song_id, title: row.title }, "search");
                search.value = ""; results.hidden = true; } }, "＋ Add")),
          el("div", { class: "sub", text: [row.film, row.year, singers(row.singers)].filter(Boolean).join(" · ") })))
          : [el("div", { class: "empty", text: "No catalog match." })]));
        results.hidden = false;
      } catch (e) { if (e.status !== 401) fail(e); }
    }, 300);
  });
  const sugBox = el("div", { class: "list" }, el("div", { class: "empty loading" }));
  const ideasBox = el("div", { class: "list" }, el("div", { class: "empty loading" }, "Collecting YouTube Mixes… "));
  const ideas = el("details", { class: "ideas", open: v.ideasOpen || null },
    el("summary", { class: "section-title" }, "YouTube ideas (outside catalog)"), ideasBox);
  ideas.addEventListener("toggle", () => { v.ideasOpen = ideas.open; if (ideas.open) loadIdeas(v, ideasBox, v.ideasPage); });
  const buttons = [];
  if (d.editable && d.playlist.length > 2) buttons.push(el("button", { class: "btn secondary", type: "button", onclick: () => autoOrder(v) }, "⇅ Auto-order"));
  if (d.status === "draft") buttons.push(el("button", { class: "btn", type: "button", onclick: () => createEvent(v) }, "✓ Create"));
  if (d.past || d.status === "performed") buttons.push(el("button", { class: "btn", type: "button", onclick: () => push(feedbackView(v.id)) }, "Feedback"));
  fill(v.el,
    el("h1", { text: m.name }),
    el("div", { class: "seed-sub" }, sub, " ", statusBadge(d.status),
      d.editable ? el("button", { type: "button", class: "link", onclick: () => push(eventFormView(d)) }, "✎ Details") : null),
    m.instructions ? el("p", { class: "hint ev-instr", text: `“${m.instructions}”` }) : null,
    d.chips.length || m.instructions ? el("div", { class: "ev-chip-row" }, chips,
      d.editable && m.instructions ? el("button", { type: "button", class: "link", onclick: () => reparse(v) }, "↻ Re-read") : null) : null,
    summaryBar(d.summary),
    el("div", { class: "section-title" }, `Playlist${d.editable && d.playlist.length > 1 ? " · drag ≡ to reorder" : ""}`),
    list,
    buttons.length ? el("div", { class: "pager" }, buttons) : null,
    d.editable ? el("div", { class: "section-title", text: "Suggestions" }) : null,
    d.editable ? el("div", { class: "search" }, search) : null,
    d.editable ? results : null,
    d.editable ? sugBox : null,
    d.editable ? ideas : null,
    doneButton());
  if (d.editable) loadSuggestions(v, sugBox, 0);
  if (d.editable && v.ideasOpen) loadIdeas(v, ideasBox, v.ideasPage);
}

async function loadSuggestions(v, box, page) {
  v.sugPage = page;
  try {
    const r = await api(eventPath(v.id, `/suggestions?${qs({ page })}`));
    v.suggestions = r.items;
    fill(box, ...(r.items.length ? r.items.map((it) => eventRow(v, it, "suggestion")) : [el("div", { class: "empty", text: "No more catalog suggestions." })]),
      el("div", { class: "pager" },
        el("button", { class: "btn secondary", type: "button", disabled: page === 0, onclick: () => loadSuggestions(v, box, page - 1) }, "‹ Back"),
        el("button", { class: "btn secondary", type: "button", disabled: r.end, onclick: () => loadSuggestions(v, box, page + 1) }, "More ›")));
  } catch (e) { if (e.status !== 401) fill(box, el("div", { class: "empty", text: e.message })); }
}

async function loadIdeas(v, box, page) {
  v.ideasPage = page;
  fill(box, el("div", { class: "empty loading" }, "Collecting YouTube Mixes… "));
  for (let i = 0; i < 6; i++) {
    let r;
    try { r = await api(eventPath(v.id, `/ideas?${qs({ page })}`)); } catch (e) {
      if (e.status !== 401) fill(box, el("div", { class: "empty", text: e.message }));
      return;
    }
    if (r.status === "loading") continue;
    fill(box, ...(r.items.length ? r.items.map((it) => eventRow(v, it, "idea"))
      : [el("div", { class: "empty", text: r.note || (r.status === "error" ? "YouTube Mix unavailable right now" : "No more ideas.") })]),
      el("div", { class: "pager" },
        el("button", { class: "btn secondary", type: "button", disabled: page === 0, onclick: () => loadIdeas(v, box, page - 1) }, "‹ Back"),
        el("button", { class: "btn secondary", type: "button", disabled: r.end, onclick: () => loadIdeas(v, box, page + 1) }, "More ›")));
    return;
  }
}

async function evAction(v, fn, okText) {
  try {
    const r = await fn();
    if (r && r.id) { v.data = r; renderEventKeepScroll(v); }
    if (okText) toast(okText);
    haptic();
    return r;
  } catch (e) { fail(e); return null; }
}

function renderEventKeepScroll(v) {
  const y = window.scrollY;
  renderEvent(v);
  window.scrollTo(0, y);
}

function addSuggestion(v, it, kind) {
  return evAction(v, () => api(eventPath(v.id, "/songs"), { method: "POST", body: {
    song_key: it.catalog_id || it.song_key, title: it.title, film: it.film || "",
    karaoke_url: it.karaoke_url || "", video_url: it.video_url || "" } }), `Added ${it.title} ✓`);
}

async function dismiss(v, it, kind) {
  if (kind === "playlist") {
    const r = await evAction(v, () => api(eventPath(v.id, `/songs/${encodeURIComponent(it.key)}`), { method: "DELETE" }),
      null);
    if (r) toast(`Removed ${it.title}`, () => addSuggestion(v, it, "undo"));
    return;
  }
  try {
    await api(eventPath(v.id, "/hide"), { method: "POST", body: { song_key: it.catalog_id || it.song_key } });
    it.el.remove();
    toast(`Not for ${v.data.meta.name}`);
  } catch (e) { fail(e); }
}

function moveSong(v, from, to) {
  const keys = v.data.playlist.map((x) => x.key);
  const [k] = keys.splice(from, 1);
  keys.splice(to, 0, k);
  evAction(v, () => api(eventPath(v.id, "/order"), { method: "PUT", body: { keys } }));
}

function autoOrder(v) {
  const before = v.data.playlist.map((x) => x.key);
  evAction(v, () => api(eventPath(v.id, "/autoorder"), { method: "POST", body: {} })).then((r) => {
    if (r) toast("Ordered by the energy arc ✓", () => evAction(v, () => api(eventPath(v.id, "/order"), { method: "PUT", body: { keys: before } })));
  });
}

function removeChip(v, chip) {
  evAction(v, () => api(eventPath(v.id, "/chips/remove"), { method: "POST", body: { chip } }));
}

function reparse(v) {
  toast("Reading the instructions…");
  evAction(v, () => api(eventPath(v.id, "/parse"), { method: "POST", body: {} }), "Instructions re-read ✓");
}

async function createEvent(v) {
  const outside = v.data.playlist.filter((x) => !x.catalog_id && x.key.startsWith("yt:"));
  if (outside.length && await confirmBox(`${outside.length} song${outside.length === 1 ? " is" : "s are"} outside your catalog. Add ${outside.length === 1 ? "it" : "them"} to the catalog first, so feedback can be tracked?`)) {
    for (const it of outside) await addSong(v, asItem(it), { title: it.title, karaoke_url: it.karaoke_url }, null);
  }
  const r = await evAction(v, () => api(eventPath(v.id, "/create"), { method: "POST", body: {} }));
  if (r) toast(r.sent ? "Planned ✓ (sent to the chat)" : "Planned ✓");
}

// Drag to reorder (pointer events, so it works with touch inside Telegram).
function makeSortable(list, onDrop) {
  list.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest(".handle");
    if (!handle) return;
    e.preventDefault();
    const rows = [...list.querySelectorAll(":scope > .item")];
    const row = handle.closest(".item");
    const from = rows.indexOf(row);
    const rects = rows.map((r) => r.getBoundingClientRect());
    const startY = e.clientY;
    let to = from;
    row.classList.add("dragging");
    handle.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const dy = ev.clientY - startY;
      row.style.transform = `translateY(${dy}px)`;
      const mid = rects[from].top + rects[from].height / 2 + dy;
      to = rects.findIndex((r) => mid < r.bottom);
      if (to < 0) to = rows.length - 1;
      rows.forEach((r, i) => {
        if (r === row) return;
        let shift = 0;
        if (from < to && i > from && i <= to) shift = -rects[from].height;
        if (from > to && i < from && i >= to) shift = rects[from].height;
        r.style.transform = shift ? `translateY(${shift}px)` : "";
      });
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      rows.forEach((r) => { r.style.transform = ""; });
      row.classList.remove("dragging");
      if (to !== from) { haptic("medium"); onDrop(from, to); }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

// Feedback after the event.
function feedbackView(id) {
  const v = { kind: "feedback", id, title: "Feedback", data: null };
  v.render = async () => {
    fill(v.el, el("h1", { text: "Feedback" }), el("div", { class: "empty loading" }));
    if (!v.id) {
      try { v.id = (await api("/api/events/latest-past")).id; } catch (e) { return; }
      if (!v.id) { fill(v.el, el("div", { class: "notice", text: "No past event yet." })); return; }
    }
    try { v.data = await api(eventPath(v.id)); } catch (e) {
      if (e.status !== 401) fill(v.el, el("div", { class: "notice", text: e.message }));
      return;
    }
    v.title = `Feedback · ${v.data.meta.name}`;
    updateChrome();
    renderFeedback(v);
  };
  return v;
}

function saveFeedback(v, body, quiet) {
  return api(eventPath(v.id, "/feedback"), { method: "PUT", body }).then((r) => {
    v.data = r;
    if (!quiet) toast("Saved ✓");
    return r;
  }).catch((e) => { fail(e); return null; });
}

function feedbackRow(v, it, n) {
  asItem(it);
  it._render = (vv, x) => feedbackRow(vv, x, n);
  const outcome = el("div", { class: "seg" }, ["standout", "good", "weak"].map((o) => el("button", {
    type: "button", class: `seg-b ${o}${it.outcome === o ? " on" : ""}`, disabled: it.skipped || null,
    onclick: async () => { it.outcome = it.outcome === o ? "" : o; refresh(v, it); haptic(); await saveFeedback(v, { song_key: it.key, outcome: it.outcome }, true); } },
  { standout: "🌟 Standout", good: "👍 Good", weak: "😐 Weak" }[o])));
  const skip = el("label", { class: "skip" }, el("input", { type: "checkbox", checked: it.skipped || null,
    onchange: async (e) => { it.skipped = e.target.checked; if (it.skipped) it.outcome = ""; refresh(v, it); await saveFeedback(v, { song_key: it.key, skipped: it.skipped, outcome: it.outcome }, true); } }), " Skipped");
  const comment = el("input", { type: "text", placeholder: "How did it go?", autocomplete: "off" });
  comment.value = it.comment || "";
  let t = null;
  comment.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { it.comment = comment.value; saveFeedback(v, { song_key: it.key, comment: comment.value }, true); }, 800); });
  const row = el("div", { class: `item fb${it.skipped ? " skipped" : ""}` },
    el("div", { class: "top" }, el("span", { class: "num", text: `${n}.` }),
      titleLink(v, it),
      el("button", { type: "button", class: "rate", onclick: () => openRating(v, it) }, it.my_rating ? `⭐${it.my_rating}` : "☆")),
    el("div", { class: "sub", text: [it.film, it.year, singers(it.singers)].filter(Boolean).join(" · ") }),
    outcome, el("div", { class: "fb-line" }, comment, skip));
  it.el = row;
  return row;
}

function renderFeedback(v) {
  const d = v.data;
  const area = (key, label, placeholder) => {
    const ta = el("textarea", { rows: 3, placeholder });
    ta.value = (d[key] || []).join("\n");
    let t = null;
    ta.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => saveFeedback(v, { [key]: ta.value.split("\n") }, true), 1000); });
    return el("div", { class: "field" }, el("label", { text: label }), ta);
  };
  const finish = el("button", { class: "btn wide", type: "button", onclick: async () => {
    finish.disabled = true;
    try {
      const r = await api(eventPath(v.id, "/finish"), { method: "POST", body: {} });
      v.data = r;
      renderFeedback(v);
      toast(r.text || "Feedback saved ✓");
      haptic("heavy");
    } catch (e) { fail(e); } finally { finish.disabled = false; }
  } }, d.status === "performed" ? "Save feedback again" : "Finish feedback");
  fill(v.el,
    el("h1", { text: d.meta.name }),
    el("div", { class: "seed-sub" }, [d.meta.date, d.meta.time].filter(Boolean).join(" · "), " ", statusBadge(d.status)),
    el("div", { class: "section-title", text: "Songs, in running order" }),
    el("div", { class: "list" }, d.playlist.length ? d.playlist.map((it, i) => feedbackRow(v, it, i + 1)) : el("div", { class: "empty", text: "No songs in this event." })),
    el("div", { class: "form-view" },
      area("requests", "Audience requests (one per line, added to your wishlist)", "e.g. Yeh Dosti"),
      area("notes", "Overall notes", "What worked, what to change")),
    el("p", { class: "hint", style: "margin:0 16px", text: "Finish updates times performed, crowd score and last performed, links songs that went well back to back, and sends one line to the chat. You can change feedback later and finish again." }),
    finish,
    doneButton());
}


// --- boot -------------------------------------------------------------------------------------------

async function boot() {
  if (WA) {
    document.documentElement.setAttribute("data-tg", "");
    WA.ready();
    WA.expand();
    if (WA.BackButton) WA.BackButton.onClick(onBack);
    // Vertical swipes scroll lists and drag playlist rows instead of closing the app.
    try { if (WA.disableVerticalSwipes && WA.isVersionAtLeast("7.7")) WA.disableVerticalSwipes(); } catch (_) { /* old client */ }
  }
  // Best effort: a summary line when Telegram hides or closes the app without Done.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { player.pause(); closeSession(true); }
  });
  try { if (WA && WA.onEvent) WA.onEvent("deactivated", () => player.pause()); } catch (_) { /* old client */ }
  window.addEventListener("pagehide", () => closeSession(true));

  push(homeView());
  const code = new URLSearchParams(location.search).get("start") || (WA && WA.initDataUnsafe && WA.initDataUnsafe.start_param) || "";
  if (!code) return;
  try {
    const s = await api(`/api/start/${encodeURIComponent(code)}`);
    if (s.view === "similar") push(similarView({ seed: s.seed, url: s.url, title: s.seed || "YouTube link" }));
    else if (s.view === "events") push(eventsView());
    else if (s.view === "new_event") push(eventFormView(null));
    else if (s.view === "event") { push(eventsView()); push(eventView(s.seed)); }
    else if (s.view === "feedback") { push(eventsView()); push(feedbackView(s.seed)); }
  } catch (e) { if (e.status !== 401) toast(e.message); }
}

boot();
