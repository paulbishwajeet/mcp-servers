/* In-app YouTube playback for the KaraokeGayak Mini App: the fallback logic,
   kept apart from the DOM so it can be unit-tested with a mocked player
   (tests/js/player.test.js).

   - A video that reports error 100 (removed/private), 101 or 150 (embedding
     disabled) is remembered per video, so its rows open YouTube straight away.
   - Error 153 means YouTube got no referrer: retry once on the
     youtube-nocookie.com host (and keep using it if that works).
   - Any other error, or no "ready" within READY_TIMEOUT_MS, shows the fallback
     too, but isn't remembered: those are about the network, not the video. */
(function (root) {
  "use strict";

  const BLOCK_CODES = [100, 101, 150];     // the video itself can't play embedded
  const REFERRER_CODE = 153;               // missing referrer: try the other host
  const READY_TIMEOUT_MS = 8000;
  const HOSTS = ["https://www.youtube.com", "https://www.youtube-nocookie.com"];
  const CACHE_TTL_MS = 30 * 24 * 3600 * 1000;

  function videoId(url) {
    const m = /(?:v=|youtu\.be\/|\/shorts\/|\/embed\/|\/live\/)([A-Za-z0-9_-]{11})/.exec(url || "");
    return m ? m[1] : (/^[A-Za-z0-9_-]{11}$/.test(url || "") ? url : "");
  }

  // Videos that can't play inside the app. storage: localStorage-like, may throw.
  class EmbedCache {
    constructor(storage, key = "kg.noEmbed", ttlMs = CACHE_TTL_MS, now = () => Date.now()) {
      this.storage = storage; this.key = key; this.ttlMs = ttlMs; this.now = now;
      this.mem = this._load();
    }
    _load() {
      try { return JSON.parse((this.storage && this.storage.getItem(this.key)) || "{}") || {}; } catch (_) { return {}; }
    }
    _save() {
      try { this.storage && this.storage.setItem(this.key, JSON.stringify(this.mem)); } catch (_) { /* private mode */ }
    }
    has(id) {
      const hit = id && this.mem[id];
      if (!hit) return false;
      if (this.now() - hit.at > this.ttlMs) { delete this.mem[id]; this._save(); return false; }
      return true;
    }
    add(id, reason) {
      if (!id) return;
      this.mem[id] = { at: this.now(), reason: String(reason) };
      this._save();
    }
  }

  // Remembers which embed host works here (youtube.com, or nocookie after a 153).
  class HostStore {
    constructor(storage, key = "kg.embedHost") { this.storage = storage; this.key = key; }
    get() {
      try { const h = this.storage && this.storage.getItem(this.key); return HOSTS.includes(h) ? h : HOSTS[0]; } catch (_) { return HOSTS[0]; }
    }
    set(h) { try { this.storage && this.storage.setItem(this.key, h); } catch (_) { /* private mode */ } }
  }

  /* deps:
       mount(videoId, host, handlers) -> player {playVideo, pauseVideo, destroy}
         handlers: {onReady(), onError(code), onStateChange(state)}
       cache: EmbedCache, hosts: HostStore
       setTimer(fn, ms) / clearTimer(t)
       onState(state, info): "loading" | "ready" | "fallback" (info.reason, info.videoId) */
  class PlayerController {
    constructor(deps) {
      this.d = deps;
      this.token = 0;
      this.player = null;
      this.timer = null;
      this.videoId = "";
      this.host = "";
      this.retried = false;
      this.state = "idle";
    }

    play(url) {
      const id = videoId(url);
      this.stop();
      const tok = ++this.token;
      this.videoId = id;
      this.retried = false;
      if (!id) return this._fallback(tok, "no-video");
      if (this.d.cache.has(id)) return this._fallback(tok, "cached");
      this._mount(tok, this.d.hosts.get());
      return this.state;
    }

    _mount(tok, host) {
      this._destroy();
      this.host = host;
      this._set("loading");
      this.timer = this.d.setTimer(() => this._timeout(tok), READY_TIMEOUT_MS);
      try {
        this.player = this.d.mount(this.videoId, host, {
          onReady: () => this._ready(tok),
          onError: (code) => this._error(tok, code),
          onStateChange: (st) => this.d.onPlayerState && tok === this.token && this.d.onPlayerState(st),
        });
      } catch (e) {
        this._fallback(tok, "load");
      }
    }

    _ready(tok) {
      if (tok !== this.token || this.state !== "loading") return;
      this._clearTimer();
      if (this.retried) this.d.hosts.set(this.host);  // the other host works here: keep it
      this._set("ready");
    }

    _error(tok, code) {
      if (tok !== this.token || this.state === "fallback") return;
      this._clearTimer();
      code = Number(code);
      if (code === REFERRER_CODE && !this.retried) {
        this.retried = true;
        const other = HOSTS.find((h) => h !== this.host);
        return this._mount(tok, other);
      }
      if (BLOCK_CODES.includes(code)) this.d.cache.add(this.videoId, code);
      this._fallback(tok, code);
    }

    _timeout(tok) {
      if (tok !== this.token || this.state !== "loading") return;
      this._fallback(tok, "timeout");
    }

    // Server-side oEmbed said embedding is disabled (HTTP 401/403).
    notEmbeddable(id) {
      this.d.cache.add(id, "oembed");
      if (id === this.videoId && this.state !== "fallback") this._fallback(this.token, "oembed");
    }

    pause() {
      try { this.player && this.player.pauseVideo && this.player.pauseVideo(); } catch (_) { /* not ready */ }
    }

    stop() {
      this.token++;
      this._clearTimer();
      this._destroy();
      this.state = "idle";
    }

    _fallback(tok, reason) {
      if (tok !== this.token) return this.state;
      this._clearTimer();
      this._destroy();
      this._set("fallback", { reason });
      return this.state;
    }

    _destroy() {
      const p = this.player;
      this.player = null;
      try { p && p.destroy && p.destroy(); } catch (_) { /* already gone */ }
    }

    _clearTimer() {
      if (this.timer !== null) { this.d.clearTimer(this.timer); this.timer = null; }
    }

    _set(state, info = {}) {
      this.state = state;
      this.d.onState && this.d.onState(state, Object.assign({ videoId: this.videoId, host: this.host }, info));
    }
  }

  // The embed iframe: our own element so referrerpolicy and allow are set before
  // it loads (the IFrame API then attaches to it).
  function embedSrc(id, host, origin) {
    const q = new URLSearchParams({ enablejsapi: "1", playsinline: "1", rel: "0", modestbranding: "1", origin });
    return `${host}/embed/${id}?${q}`;
  }

  const api = { BLOCK_CODES, REFERRER_CODE, READY_TIMEOUT_MS, HOSTS, videoId, EmbedCache, HostStore,
    PlayerController, embedSrc };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KGPlayer = api;
})(typeof window !== "undefined" ? window : globalThis);
