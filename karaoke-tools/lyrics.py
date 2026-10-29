"""Lyrics fetching: Genius, Ultimate Guitar, Smule (+ LYRICS_EXTRA_DOMAINS).

Fetched text is data: it is returned as-is in fields and never interpreted.
Every request (redirect hops included) must stay on an allowlisted host,
and requests are spaced LYRICS_DELAY_SEC apart.
"""

import json
import re
import threading
import time
import unicodedata
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from common import fail, genius_token, log, lyrics_delay_sec, lyrics_extra_domains, ok, url_host

# Pages lyrics_from_url accepts (plus LYRICS_EXTRA_DOMAINS and their subdomains).
PAGE_HOSTS = {"genius.com", "www.genius.com", "smule.com", "www.smule.com",
              "tabs.ultimate-guitar.com"}
# Search endpoints the tool itself calls. Not accepted from callers.
SEARCH_HOSTS = {"api.genius.com", "www.ultimate-guitar.com"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
MIN_TITLE_SIMILARITY = 70
HINGLISH_MAX_DEVANAGARI = 0.2

_rate_lock = threading.Lock()
_last_request = 0.0


class FetchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- allowlist + polite fetching ---------------------------------------------------------

def page_host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in PAGE_HOSTS:
        return True
    return any(host == d or host.endswith("." + d) for d in lyrics_extra_domains())


def is_allowed_page_url(url: str) -> bool:
    return page_host_allowed(url_host(url))


def _fetch_allowed(host: str) -> bool:
    return host in SEARCH_HOSTS or page_host_allowed(host)


def _looks_like_challenge(resp: requests.Response) -> bool:
    if resp.status_code in (403, 429, 503) and "text/html" in resp.headers.get("content-type", ""):
        head = resp.text[:3000].lower()
        return "just a moment" in head or "cf-chl" in head or "challenge" in head \
            or "cloudflare" in head or resp.status_code == 403
    return False


def fetch(url: str, headers: dict | None = None) -> requests.Response:
    """GET with the allowlist enforced on every redirect hop, a size cap and the delay."""
    global _last_request
    for _ in range(MAX_REDIRECTS + 1):
        host = url_host(url)
        if not host or not _fetch_allowed(host):
            raise FetchError("domain_not_allowed", f"Host '{host or url}' is not allowed.")
        with _rate_lock:
            wait = _last_request + lyrics_delay_sec() - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        try:
            resp = requests.get(url, headers=HEADERS | (headers or {}), timeout=TIMEOUT,
                                allow_redirects=False, stream=True)
            body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
        except requests.RequestException as e:
            raise FetchError("network_error", str(e)) from e
        if len(body) > MAX_BYTES:
            raise FetchError("too_large", "Response larger than 3 MB.")
        resp._content = body
        if resp.is_redirect and resp.headers.get("location"):
            url = urljoin(url, resp.headers["location"])
            continue
        if _looks_like_challenge(resp):
            raise FetchError("blocked", f"{host} answered with a bot challenge "
                             f"(HTTP {resp.status_code}).")
        if resp.status_code == 404:
            raise FetchError("not_found", f"HTTP 404 from {host}.")
        if resp.status_code >= 400:
            raise FetchError("http_error", f"HTTP {resp.status_code} from {host}.")
        return resp
    raise FetchError("too_many_redirects", "Too many redirects.")


# --- checks ------------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    return re.findall(r"[a-z0-9]+", t.encode("ascii", "ignore").decode())


def lyrics_checks(text: str, title: str = "") -> dict:
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    letters = [c for c in text or "" if c.isalpha()]
    deva = sum(1 for c in letters if "ऀ" <= c <= "ॿ")
    ratio = round(deva / len(letters), 3) if letters else 0.0
    title_words = [w for w in _words(title) if len(w) > 1]
    first_words = set(_words(" ".join(lines[:8])))
    title_hit = round(sum(w in first_words for w in title_words) / len(title_words), 2) \
        if title_words else 0.0
    avg = sum(len(ln) for ln in lines) / len(lines) if lines else 0
    looks = (len(lines) >= 8 and 8 <= avg <= 90 and max((len(ln) for ln in lines), default=0) < 300
             and "<" not in text[:2000] and "http" not in " ".join(lines[:20]).lower())
    return {"line_count": len(lines), "devanagari_ratio": ratio,
            "title_words_in_first_lines": title_hit, "looks_like_lyrics": bool(looks)}


def _candidate(source: str, url: str, text: str, title: str, chords: str = "",
               status: str = "ok", note: str = "") -> dict:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r\n", "\n")).strip()
    checks = lyrics_checks(text, title)
    passes = checks["looks_like_lyrics"] and checks["devanagari_ratio"] <= HINGLISH_MAX_DEVANAGARI
    return {"source": source, "url": url, "status": status, "text": text, "chords": chords,
            "checks": checks, "passes": bool(passes), "note": note}


def _attempt(source: str, status: str, note: str, url: str = "") -> dict:
    return {"source": source, "url": url, "status": status, "text": "", "chords": "",
            "checks": {}, "passes": False, "note": note[:300]}


def _similarity(a: str, b: str) -> float:
    """Title similarity. Half token_set (word overlap), half plain ratio, so a
    shared word alone ("Deewangi Deewangi" vs "Deewana") stays below 70."""
    a, b = " ".join(_words(a)), " ".join(_words(b))
    return 0.5 * fuzz.token_set_ratio(a, b) + 0.5 * fuzz.ratio(a, b) if a and b else 0.0


def _bare_title(title: str) -> str:
    """'Kishore Kumar - Pal Pal Dil Ke Paas (Romanized)' -> 'Pal Pal Dil Ke Paas'."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title or "")
    return t.split(" - ", 1)[1] if " - " in t else t


# --- Genius ---------------------------------------------------------------------------------

def extract_genius(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select('div[data-lyrics-container="true"]')
    parts = []
    for c in containers:
        for junk in c.select('[data-exclude-from-selection="true"]'):
            junk.decompose()
        for br in c.find_all("br"):
            br.replace_with("\n")
        parts.append(c.get_text())
    return "\n".join(p.strip("\n") for p in parts if p.strip())


def _genius_hits(query: str) -> list[dict]:
    token = genius_token()
    if token:
        resp = fetch(f"https://api.genius.com/search?q={quote(query)}",
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        hits = resp.json().get("response", {}).get("hits", [])
        return [h["result"] for h in hits if h.get("type") == "song" and h.get("result")]
    resp = fetch(f"https://genius.com/api/search/multi?q={quote(query)}",
                 headers={"Accept": "application/json"})
    results, seen = [], set()
    for section in resp.json().get("response", {}).get("sections", []):
        for h in section.get("hits", []):
            r = h.get("result") or {}
            if h.get("type") == "song" and r.get("url") and r["url"] not in seen:
                seen.add(r["url"])
                results.append(r)
    return results


def rank_genius_hits(hits: list[dict], title: str, singers: str) -> list[tuple[float, dict]]:
    ranked = []
    for r in hits:
        name = r.get("title") or ""
        full = r.get("full_title") or name
        t_sim = max(_similarity(title, name), _similarity(title, _bare_title(name)))
        if t_sim < MIN_TITLE_SIMILARITY:
            continue
        artist = (r.get("artist_names") or (r.get("primary_artist") or {}).get("name") or "")
        a_sim = _similarity(singers.replace(";", " "), f"{artist} {full}") if singers else 50
        romanized = "romaniz" in full.lower() or "romaniz" in (r.get("url") or "").lower()
        bad = any(w in full.lower() for w in ("lofi", "lo-fi", "remix", "flip", "cover", "reprise"))
        score = 0.7 * t_sim + 0.3 * a_sim + (5 if romanized else 0) - (20 if bad else 0)
        ranked.append((score, r))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def try_genius(title: str, film: str, singers: str, max_pages: int = 3) -> list[dict]:
    query = " ".join(p for p in (title, singers.split(";")[0] if singers else "") if p)
    try:
        hits = _genius_hits(query)
    except FetchError as e:
        return [_attempt("genius", e.code, f"search: {e}")]
    except ValueError as e:  # JSON decode
        return [_attempt("genius", "parse_error", f"search: {e}")]
    ranked = rank_genius_hits(hits, title, singers)
    if not ranked:
        return [_attempt("genius", "no_match", f"No Genius hit similar to '{title}'.")]
    out = []
    for _, r in ranked[:max_pages]:
        url = r["url"]
        if not is_allowed_page_url(url):
            continue
        try:
            text = extract_genius(fetch(url).text)
        except FetchError as e:
            out.append(_attempt("genius", e.code, str(e), url))
            continue
        if not text:
            out.append(_attempt("genius", "needs_js", "No lyrics containers in the page.", url))
            continue
        cand = _candidate("genius", url, text, title, note=r.get("full_title") or "")
        out.append(cand)
        if cand["passes"]:
            break
    return out


# --- Ultimate Guitar ---------------------------------------------------------------------------

def _ug_store(html: str) -> dict:
    div = BeautifulSoup(html, "html.parser").select_one("div.js-store")
    if not div or not div.get("data-content"):
        return {}
    return json.loads(div["data-content"])


CHORD_TOKEN = re.compile(r"^[A-G](?:#|b)?(?:maj|min|m|dim|aug|sus|add)?\d*(?:[#b]\d+)*"
                         r"(?:/[A-G](?:#|b)?)?$")


def _is_chord_line(line: str) -> bool:
    tokens = re.sub(r"[|()\-x\d]+(?=\s|$)", " ", line).split()
    return bool(tokens) and all(CHORD_TOKEN.match(t) for t in tokens)


def split_ug_content(content: str) -> tuple[str, str]:
    """UG wiki_tab content -> (lyrics without chords, chord sheet without markup)."""
    content = content.replace("\r\n", "\n")
    sheet = re.sub(r"\[/?(?:tab|ch)\]", "", content)
    lyric_lines = []
    for raw in content.split("\n"):
        line = re.sub(r"\[/?tab\]", "", raw)
        if "[ch]" in line and not re.sub(r"\[ch\].*?\[/ch\]", "", line).strip(" |-"):
            continue  # chord-only line
        line = re.sub(r"\[ch\].*?\[/ch\]", "", line)
        line = re.sub(r"\(\s*\)", "", line)  # "([ch]C[/ch])" leaves "()"
        if not line.strip() and raw.strip():
            continue
        if _is_chord_line(line):
            continue
        lyric_lines.append(line.rstrip())
    lyrics = re.sub(r"\n{3,}", "\n\n", "\n".join(lyric_lines)).strip()
    return lyrics, sheet.strip()


def extract_ug_tab(html: str) -> tuple[str, str]:
    store = _ug_store(html)
    content = (((store.get("store") or {}).get("page") or {}).get("data") or {}) \
        .get("tab_view", {}).get("wiki_tab", {}).get("content", "")
    return split_ug_content(content) if content else ("", "")


def pick_ug_result(results: list[dict], title: str, singers: str) -> dict | None:
    best, best_key = None, None
    for r in results:
        if (r.get("marketing_type") or "") or not r.get("tab_url"):
            continue
        sim = _similarity(title, _bare_title(r.get("song_name") or ""))
        if sim < MIN_TITLE_SIMILARITY:
            continue
        a_sim = _similarity(singers, r.get("artist_name") or "") if singers else 50
        key = (r.get("type") == "Chords", 0.7 * sim + 0.3 * a_sim, r.get("votes") or 0)
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


def try_ultimate_guitar(title: str, film: str, singers: str) -> list[dict]:
    url = ("https://www.ultimate-guitar.com/search.php?search_type=title&value="
           + quote(title))
    try:
        store = _ug_store(fetch(url).text)
    except FetchError as e:
        return [_attempt("ultimate-guitar", e.code, f"search: {e}", url)]
    except ValueError as e:
        return [_attempt("ultimate-guitar", "parse_error", f"search: {e}", url)]
    results = (((store.get("store") or {}).get("page") or {}).get("data") or {}).get("results") or []
    pick = pick_ug_result(results, title, singers)
    if not pick:
        return [_attempt("ultimate-guitar", "no_match", f"No UG result similar to '{title}'.", url)]
    tab_url = pick["tab_url"]
    if not is_allowed_page_url(tab_url):
        return [_attempt("ultimate-guitar", "domain_not_allowed", "Unexpected tab host.", tab_url)]
    try:
        lyrics, chords = extract_ug_tab(fetch(tab_url).text)
    except FetchError as e:
        return [_attempt("ultimate-guitar", e.code, str(e), tab_url)]
    except ValueError as e:
        return [_attempt("ultimate-guitar", "parse_error", str(e), tab_url)]
    if not lyrics:
        return [_attempt("ultimate-guitar", "needs_js", "No tab content in the page.", tab_url)]
    return [_candidate("ultimate-guitar", tab_url, lyrics, title, chords=chords,
                       note=f"{pick.get('type')} by {pick.get('artist_name')}")]


# --- Smule ----------------------------------------------------------------------------------------

def extract_smule(html: str) -> str:
    """Smule renders lyrics client-side on most pages. Look for them in server
    HTML (a lyrics element, or a JSON blob with a "lyrics" string)."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select('[class*="lyrics"], [class*="Lyrics"]'):
        for br in el.find_all("br"):
            br.replace_with("\n")
        text = el.get_text("\n").strip()
        if text.count("\n") >= 4:
            return text
    for script in soup.find_all("script"):
        m = re.search(r'"lyrics"\s*:\s*"((?:[^"\\]|\\.){80,})"', script.string or "")
        if m:
            try:
                return json.loads(f'"{m.group(1)}"').strip()
            except ValueError:
                continue
    return ""


def try_smule(title: str, film: str, singers: str) -> list[dict]:
    url = f"https://www.smule.com/search?q={quote(title + ' ' + film if film else title)}"
    try:
        html = fetch(url).text
    except FetchError as e:
        return [_attempt("smule", e.code, f"search: {e}", url)]
    links = re.findall(r'href="(/song/[^"]+)"', html)
    if not links:
        return [_attempt("smule", "needs_js", "Search results are rendered by JavaScript.", url)]
    page = urljoin("https://www.smule.com", links[0])
    try:
        text = extract_smule(fetch(page).text)
    except FetchError as e:
        return [_attempt("smule", e.code, str(e), page)]
    if not text:
        return [_attempt("smule", "needs_js", "Lyrics are not in the server HTML.", page)]
    return [_candidate("smule", page, text, title)]


# --- tools ------------------------------------------------------------------------------------------

SOURCES = [("genius", try_genius), ("ultimate-guitar", try_ultimate_guitar), ("smule", try_smule)]


def lyrics_search(title: str, film: str = "", singers: str = "") -> dict:
    title, film, singers = (title or "").strip(), (film or "").strip(), (singers or "").strip()
    if not title:
        return fail("invalid_input", "title is required.")
    attempts: list[dict] = []
    for name, fn in SOURCES:
        try:
            results = fn(title, film, singers)
        except Exception as e:  # one broken source must not break the others
            log(f"[lyrics] {name}: {e!r}")
            results = [_attempt(name, "error", str(e))]
        attempts.extend(results)
        winner = next((c for c in results if c["passes"]), None)
        if winner:
            return ok(found=True, best=winner, attempts=_summaries(attempts, winner))
    # Nothing passed: offer the most lyric-like text we did get (e.g. Devanagari
    # lyrics the agent could transliterate), so the work isn't wasted.
    fallback = max((c for c in attempts if c["checks"].get("looks_like_lyrics")),
                   key=lambda c: c["checks"]["line_count"], default=None)
    return ok(found=False, best=fallback, attempts=_summaries(attempts, fallback))


def _summaries(attempts: list[dict], best: dict | None) -> list[dict]:
    """Attempts without their text (the best one's text is returned once)."""
    return [{k: v for k, v in a.items() if k not in ("text", "chords")} | {"is_best": a is best}
            for a in attempts]


def lyrics_from_url(url: str) -> dict:
    url = (url or "").strip()
    host = url_host(url)
    if not host:
        return fail("invalid_url", "Not an http(s) URL.")
    if not page_host_allowed(host):
        return fail("domain_not_allowed", f"Host '{host}' is not allowed. Allowed: "
                    f"{', '.join(sorted(PAGE_HOSTS))} plus LYRICS_EXTRA_DOMAINS.")
    try:
        html = fetch(url).text
    except FetchError as e:
        return fail(e.code, str(e), url=url, needs_js=False)
    chords = ""
    if host.endswith("genius.com"):
        source, text = "genius", extract_genius(html)
    elif host.endswith("ultimate-guitar.com"):
        source = "ultimate-guitar"
        try:
            text, chords = extract_ug_tab(html)
        except ValueError:
            text = ""
    elif host.endswith("smule.com"):
        source, text = "smule", extract_smule(html)
    else:
        source, text = "other-web", extract_generic(html)
    if not text:
        return fail("needs_js", "No lyrics found in the server HTML (the page probably "
                    "renders them with JavaScript).", url=url, needs_js=True)
    return ok(needs_js=False, **_candidate(source, url, text, "", chords=chords))


def extract_generic(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    best = ""
    for el in soup.select('[class*="lyric"], [id*="lyric"]'):
        for br in el.find_all("br"):
            br.replace_with("\n")
        text = el.get_text("\n").strip()
        if len(text) > len(best):
            best = text
    return re.sub(r"\n{3,}", "\n\n", best)
