// Fallback logic of app/static/player.js with a mocked YouTube player.
// Run: node --test tests/js/   (tests/test_player_js.py runs it under pytest)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const P = require(path.join(__dirname, "../../app/static/player.js"));

function memStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m };
}

// A controller wired to fakes: players[] records each mount; timers run on demand.
function setup({ storage = memStorage(), now = () => 1000 } = {}) {
  const players = [];
  const states = [];
  const timers = new Map();
  let nextTimer = 1;
  const cache = new P.EmbedCache(storage, "kg.noEmbed", 30 * 86400e3, now);
  const hosts = new P.HostStore(storage);
  const ctl = new P.PlayerController({
    cache, hosts,
    mount: (id, host, handlers) => {
      const p = { id, host, handlers, destroyed: false, paused: 0,
        playVideo() {}, pauseVideo() { this.paused++; }, destroy() { this.destroyed = true; } };
      players.push(p);
      return p;
    },
    setTimer: (fn, ms) => { const t = nextTimer++; timers.set(t, { fn, ms }); return t; },
    clearTimer: (t) => timers.delete(t),
    onState: (s, info) => states.push([s, info.reason ?? null]),
  });
  const fireTimers = () => { for (const [t, { fn }] of [...timers]) { timers.delete(t); fn(); } };
  return { ctl, players, states, timers, fireTimers, cache, hosts, storage };
}

const URL1 = "https://www.youtube.com/watch?v=8H6cpx3oXYc";
const BLOCKED = "https://youtu.be/TnDmK0ujJ-0";

test("plays inline when the player becomes ready", () => {
  const { ctl, players, states, timers } = setup();
  assert.equal(ctl.play(URL1), "loading");
  assert.equal(players[0].id, "8H6cpx3oXYc");
  assert.equal(players[0].host, "https://www.youtube.com");
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].ms, 8000);
  players[0].handlers.onReady();
  assert.equal(ctl.state, "ready");
  assert.equal(timers.size, 0, "the ready timeout is cleared");
  assert.deepEqual(states, [["loading", null], ["ready", null]]);
});

for (const code of [100, 101, 150]) {
  test(`error ${code}: fallback and the video is remembered as not embeddable`, () => {
    const { ctl, players, cache } = setup();
    ctl.play(BLOCKED);
    players[0].handlers.onError(code);
    assert.equal(ctl.state, "fallback");
    assert.ok(players[0].destroyed);
    assert.ok(cache.has("TnDmK0ujJ-0"));
    // Next time: straight to the fallback (↗), no player at all.
    assert.equal(ctl.play(BLOCKED), "fallback");
    assert.equal(players.length, 1);
  });
}

test("error 153 retries once on youtube-nocookie.com, then remembers that host", () => {
  const { ctl, players, hosts, cache } = setup();
  ctl.play(URL1);
  players[0].handlers.onError(153);
  assert.equal(ctl.state, "loading");
  assert.equal(players.length, 2);
  assert.ok(players[0].destroyed);
  assert.equal(players[1].host, "https://www.youtube-nocookie.com");
  players[1].handlers.onReady();
  assert.equal(ctl.state, "ready");
  assert.equal(hosts.get(), "https://www.youtube-nocookie.com");
  assert.equal(cache.has("8H6cpx3oXYc"), false, "153 is about the page, not the video");
  ctl.play(URL1);
  assert.equal(players[2].host, "https://www.youtube-nocookie.com");
});

test("error 153 on both hosts: fallback, video not remembered", () => {
  const { ctl, players, cache } = setup();
  ctl.play(URL1);
  players[0].handlers.onError(153);
  players[1].handlers.onError(153);
  assert.equal(ctl.state, "fallback");
  assert.equal(players.length, 2);
  assert.equal(cache.has("8H6cpx3oXYc"), false);
});

test("no ready within 8 s: fallback, not remembered (a slow network isn't the video's fault)", () => {
  const { ctl, fireTimers, states, cache, players } = setup();
  ctl.play(URL1);
  fireTimers();
  assert.equal(ctl.state, "fallback");
  assert.deepEqual(states.at(-1), ["fallback", "timeout"]);
  assert.equal(cache.has("8H6cpx3oXYc"), false);
  // A late onReady from the dead player changes nothing.
  players[0].handlers.onReady();
  assert.equal(ctl.state, "fallback");
});

test("other errors (2, 5) fall back without remembering", () => {
  for (const code of [2, 5]) {
    const { ctl, players, cache } = setup();
    ctl.play(URL1);
    players[0].handlers.onError(code);
    assert.equal(ctl.state, "fallback");
    assert.equal(cache.has("8H6cpx3oXYc"), false);
  }
});

test("events from a previous song are ignored after switching", () => {
  const { ctl, players } = setup();
  ctl.play(URL1);
  ctl.play("https://www.youtube.com/watch?v=lsE__bt5FX8");
  players[0].handlers.onError(150);          // the old player
  assert.equal(ctl.state, "loading");
  assert.equal(ctl.d.cache.has("8H6cpx3oXYc"), false);
  players[1].handlers.onReady();
  assert.equal(ctl.state, "ready");
});

test("oEmbed says not embeddable: fallback now and remembered", () => {
  const { ctl, cache } = setup();
  ctl.play(BLOCKED);
  ctl.notEmbeddable("TnDmK0ujJ-0");
  assert.equal(ctl.state, "fallback");
  assert.ok(cache.has("TnDmK0ujJ-0"));
});

test("pause and stop", () => {
  const { ctl, players, timers } = setup();
  ctl.play(URL1);
  players[0].handlers.onReady();
  ctl.pause();
  assert.equal(players[0].paused, 1);
  ctl.stop();
  assert.ok(players[0].destroyed);
  assert.equal(ctl.state, "idle");
  assert.equal(timers.size, 0);
});

test("no video id: fallback without mounting", () => {
  const { ctl, players } = setup();
  assert.equal(ctl.play(""), "fallback");
  assert.equal(players.length, 0);
});

test("the cache persists in storage and expires after 30 days", () => {
  const storage = memStorage();
  let t = 1000;
  const a = new P.EmbedCache(storage, "kg.noEmbed", 30 * 86400e3, () => t);
  a.add("TnDmK0ujJ-0", 150);
  const b = new P.EmbedCache(storage, "kg.noEmbed", 30 * 86400e3, () => t);
  assert.ok(b.has("TnDmK0ujJ-0"), "a new page load still knows");
  t += 31 * 86400e3;
  assert.equal(b.has("TnDmK0ujJ-0"), false);
});

test("storage that throws (private mode) doesn't break playback", () => {
  const bad = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  const { ctl, players } = setup({ storage: bad });
  ctl.play(URL1);
  players[0].handlers.onError(101);
  assert.equal(ctl.state, "fallback");
  assert.ok(ctl.d.cache.has("8H6cpx3oXYc"), "still remembered for this session, in memory");
});

test("embed URL: inline, no related videos, our origin", () => {
  const src = P.embedSrc("8H6cpx3oXYc", "https://www.youtube.com", "https://kg.example.ts.net");
  const u = new URL(src);
  assert.equal(u.origin + u.pathname, "https://www.youtube.com/embed/8H6cpx3oXYc");
  for (const [k, v] of Object.entries({ enablejsapi: "1", playsinline: "1", rel: "0", modestbranding: "1",
    origin: "https://kg.example.ts.net" })) assert.equal(u.searchParams.get(k), v);
  assert.equal(u.searchParams.get("autoplay"), null, "no autoplay");
});

test("videoId parses the usual link shapes", () => {
  assert.equal(P.videoId("https://youtu.be/TnDmK0ujJ-0"), "TnDmK0ujJ-0");
  assert.equal(P.videoId("https://www.youtube.com/watch?v=lsE__bt5FX8&t=3"), "lsE__bt5FX8");
  assert.equal(P.videoId("https://music.youtube.com/watch?v=8H6cpx3oXYc"), "8H6cpx3oXYc");
  assert.equal(P.videoId("lsE__bt5FX8"), "lsE__bt5FX8");
  assert.equal(P.videoId("https://example.com/x"), "");
});
