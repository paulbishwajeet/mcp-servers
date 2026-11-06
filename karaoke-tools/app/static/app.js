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
  $("views").replaceChildren(el("div", { class: "notice" },
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
}

function updateChrome() {
  const titles = views.filter((v) => v.kind === "similar").map((v) => v.title);
  $("crumbs").hidden = titles.length < 2;
  $("crumbs").textContent = titles.slice(-3).join(" › ");
  if (WA && WA.BackButton) {
    if (views.length > 1 || overlayOpen()) WA.BackButton.show(); else WA.BackButton.hide();
  }
}

function overlayOpen() { return !$("player").hidden || !$("rating").hidden || !$("edit").hidden; }

function onBack() {
  if (!$("player").hidden) return closePlayer();
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
          results.replaceChildren(...(r.rows.length ? r.rows.map((row) => el("button", { type: "button",
            class: "item", onclick: () => push(similarView({ seed: row.song_id, title: row.title })) },
            el("div", { class: "title", text: row.title }),
            el("div", { class: "sub", text: [row.film, row.year, singers(row.singers)].filter(Boolean).join(" · ") })))
            : [el("div", { class: "empty", text: "No catalog match. Press search to use it as a seed anyway." })]));
          results.hidden = false;
        } catch (e) { if (e.status !== 401) fail(e); }
      }, 300);
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    v.el.replaceChildren(
      el("h1", { text: "🎵 KaraokeGayak" }),
      el("form", { class: "search", onsubmit: (e) => { e.preventDefault(); go(); } },
        input, el("button", { class: "btn", type: "submit" }, "Similar")),
      results,
      el("div", { class: "section-title", text: "Recent seeds" }),
      recent,
      doneButton());
    try {
      const r = await api("/api/home");
      recent.replaceChildren(...(r.recent.length ? r.recent.map((s) => el("button", { type: "button",
        onclick: () => push(similarView({ seed: s.seed, url: s.url, title: s.title || s.seed })) },
        el("div", { text: s.title || s.seed || s.url })))
        : [el("div", { class: "empty", text: "No seeds yet. Search for a song above." })]));
    } catch (e) { if (e.status !== 401) recent.replaceChildren(el("div", { class: "empty", text: e.message })); }
  };
  return v;
}

// --- Suggest Similar -----------------------------------------------------------------------------

function similarView({ seed = "", url = "", title = "" }) {
  const v = { kind: "similar", seed, url, title: title || seed || url, page: 0, data: null, token: 0 };
  v.render = () => loadPage(v, 0);
  return v;
}

async function loadPage(v, page) {
  const token = ++v.token;
  v.page = page;
  v.el.replaceChildren(el("h1", { text: v.title }), el("div", { class: "empty loading" }));
  let r;
  try {
    r = await api(`/api/similar?${qs({ seed: v.seed, url: v.url, page })}`);
  } catch (e) {
    if (e.status === 401) return;
    v.el.replaceChildren(el("h1", { text: v.title }), el("div", { class: "notice", text: e.message }),
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
    v.data = r;
    v.title = r.seed.title || v.title;
    updateChrome();
    renderSimilar(v);
    if (!v.el.hidden) window.scrollTo(0, y);
    return;
  }
  if (token === v.token && v.data) { v.data.mix = "error"; v.data.outside = { items: [], end: true }; renderSimilar(v); }
}

function renderSimilar(v) {
  const d = v.data;
  const sub = [d.seed.film, d.seed.year, d.seed.in_catalog ? "in your catalog" : "not in catalog"].filter(Boolean).join(" · ");
  const cat = d.catalog;
  const catList = el("div", { class: "list" },
    cat.items.length ? cat.items.map((it) => renderItem(v, it)) : el("div", { class: "empty",
      text: v.page > 0 ? "No more catalog matches" : (d.seed.in_catalog || d.mix !== "loading"
        ? "No catalog matches" : "Waiting for the YouTube Mix…") }));
  if (cat.items.length && cat.end) catList.append(el("div", { class: "empty", text: "No more catalog matches" }));
  let outList;
  if (!d.outside) {
    outList = el("div", { class: "list" }, el("div", { class: "empty loading" }, "Loading YouTube Mix… "));
  } else {
    const o = d.outside;
    outList = el("div", { class: "list" }, o.items.length ? o.items.map((it) => renderItem(v, it))
      : el("div", { class: "empty", text: d.mix === "error" ? "YouTube Mix unavailable right now" : "End of YouTube Mix" }));
    if (o.items.length && o.end) outList.append(el("div", { class: "empty", text: "End of YouTube Mix" }));
  }
  const mixDone = !!d.outside;
  const nextOff = cat.end && (!mixDone || d.outside.end);
  v.el.replaceChildren(
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
      el("a", { class: "title", href: "#", onclick: (e) => { e.preventDefault(); openPlayer(it); } }, it.title),
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
          url: it.catalog_id ? "" : (it.video_url || ""), title: it.title })) }, "↪"),
      el("button", { type: "button", "aria-label": "Edit", title: "Edit", onclick: () => openEdit(v, it) }, "✎"),
      el("button", { type: "button", "aria-label": "Why", title: "Why",
        onclick: () => { why.hidden = !why.hidden; } }, "ⓘ"))));
  it.el = row;
  return row;
}

function refresh(v, it) {
  if (it.el && it.el.isConnected) it.el.replaceWith(renderItem(v, it));
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
  body.replaceChildren(...voc.editable.map((f) => field(f, s.row[f] || "", voc)));
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
  box.replaceChildren(el("div", { class: "empty loading" }));
  try {
    const r = await api(`/api/karaoke/find?${qs({ title, film: editCtx.inputs.film.value.trim() })}`);
    box.replaceChildren(...(r.candidates.length ? r.candidates.map((c) => el("button", { type: "button",
      onclick: () => { editCtx.inputs.karaoke_url.value = c.url; box.replaceChildren(); } },
      `${c.title} · ${c.channel} · ${Math.round(c.score)}${c.source === "index" ? " · Anil Chauhan" : ""}`))
      : [el("div", { class: "empty", text: "No karaoke video found" })]));
  } catch (e) { box.replaceChildren(); fail(e); }
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

let ytReady = null;
let ytPlayer = null;
let playerCtx = null;

function loadYT() {
  if (!ytReady) ytReady = new Promise((resolve, reject) => {
    window.onYouTubeIframeAPIReady = () => resolve(window.YT);
    const s = el("script", { src: "https://www.youtube.com/iframe_api" });
    s.onerror = () => { ytReady = null; reject(new Error("YouTube player didn't load")); };
    document.head.append(s);
  });
  return ytReady;
}

function videoId(url) {
  const m = /(?:v=|youtu\.be\/|\/shorts\/|\/embed\/|\/live\/)([A-Za-z0-9_-]{11})/.exec(url || "");
  return m ? m[1] : "";
}

function openPlayer(it) {
  playerCtx = { it, karaoke: it.karaoke_url || "", original: it.video_url || "" };
  const kt = document.querySelector('[data-tab="karaoke"]');
  kt.disabled = !playerCtx.karaoke;
  kt.textContent = playerCtx.karaoke ? "Karaoke" : "No karaoke";
  $("player").hidden = false;
  $("player-fs").hidden = !(document.fullscreenEnabled || document.webkitFullscreenEnabled ||
    (WA && WA.requestFullscreen && WA.isVersionAtLeast && WA.isVersionAtLeast("8.0")));
  updateChrome();
  playTab(playerCtx.karaoke ? "karaoke" : "original");
}

async function playTab(tab) {
  const ctx = playerCtx;
  for (const b of document.querySelectorAll(".player-tabs .tab")) b.classList.toggle("on", b.dataset.tab === tab);
  $("player-fallback").hidden = true;
  $("player-frame").hidden = false;
  $("player-title").textContent = ctx.it.title + (tab === "original" ? " · original" : " · karaoke");
  let url = ctx[tab];
  if (!url && tab === "original") {
    $("player-title").textContent = "Finding the original video…";
    try {
      const r = await api(`/api/original?${qs({ title: ctx.it.title, film: ctx.it.film })}`);
      url = ctx.original = r.url;
    } catch (e) { return fallback("", "Couldn't find the original video."); }
    if (ctx !== playerCtx) return;
    $("player-title").textContent = ctx.it.title + " · original";
  }
  const id = videoId(url);
  if (!id) return fallback(url, "No video link for this song.");
  ctx.current = url;
  api(`/api/embeddable?${qs({ url })}`).then((r) => {
    if (r.embeddable === false && ctx === playerCtx && ctx.current === url) fallback(url);
  }).catch(() => {});
  try {
    const YT = await loadYT();
    if (ctx !== playerCtx) return;
    if (!ytPlayer) {
      ytPlayer = new YT.Player("yt", { videoId: id, playerVars: { playsinline: 1, rel: 0, fs: 1, modestbranding: 1 },
        events: { onError: (e) => { if ([2, 5, 100, 101, 150].includes(e.data) && playerCtx) fallback(playerCtx.current); } } });
    } else ytPlayer.loadVideoById(id);
  } catch (e) { fallback(url, e.message); }
}

function fallback(url, text) {
  try { ytPlayer && ytPlayer.stopVideo && ytPlayer.stopVideo(); } catch (_) { /* not ready */ }
  $("player-frame").hidden = true;
  $("player-fallback").hidden = false;
  $("player-fallback-text").textContent = text || "The uploader doesn't allow playing this video inside Telegram.";
  $("player-open").hidden = !url;
  $("player-open").onclick = () => { if (WA && WA.openLink) WA.openLink(url); else window.open(url, "_blank", "noopener"); };
}

function closePlayer() {
  try { ytPlayer && ytPlayer.stopVideo && ytPlayer.stopVideo(); } catch (_) { /* not ready */ }
  playerCtx = null;
  $("player").hidden = true;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  updateChrome();
}

for (const b of document.querySelectorAll(".player-tabs .tab")) b.onclick = () => { if (!b.disabled && playerCtx) playTab(b.dataset.tab); };
$("player-close").onclick = closePlayer;
$("player-fs").onclick = () => {
  const f = $("player-frame");
  if (f.requestFullscreen) f.requestFullscreen().catch(() => {});
  else if (f.webkitRequestFullscreen) f.webkitRequestFullscreen();
  else if (WA && WA.requestFullscreen) WA.requestFullscreen();
};
$("player").addEventListener("click", (e) => { if (e.target.id === "player") closePlayer(); });

// --- boot -------------------------------------------------------------------------------------------

async function boot() {
  if (WA) {
    document.documentElement.setAttribute("data-tg", "");
    WA.ready();
    WA.expand();
    if (WA.BackButton) WA.BackButton.onClick(onBack);
  }
  // Best effort: a summary line when Telegram hides or closes the app without Done.
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") closeSession(true); });
  window.addEventListener("pagehide", () => closeSession(true));

  push(homeView());
  const code = new URLSearchParams(location.search).get("start") || (WA && WA.initDataUnsafe && WA.initDataUnsafe.start_param) || "";
  if (!code) return;
  try {
    const s = await api(`/api/start/${encodeURIComponent(code)}`);
    if (s.view === "similar") push(similarView({ seed: s.seed, url: s.url, title: s.seed || "YouTube link" }));
  } catch (e) { if (e.status !== 401) toast(e.message); }
}

boot();
