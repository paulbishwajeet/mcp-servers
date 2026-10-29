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
              "tabs.ultimate-guitar.com",
              "hindigeetmala.net", "www.hindigeetmala.net", "geetmanjusha.com",
              "www.geetmanjusha.com", "lyricsindia.net", "www.lyricsindia.net",
              "musixmatch.com", "www.musixmatch.com"}
# SCHEMA.md's lyrics_source values; sites without their own value are "other-web".
SCHEMA_SOURCE = {"genius": "genius", "ultimate-guitar": "ultimate-guitar", "smule": "smule"}
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
    all_words = set(_words(text))
    title_found = round(sum(w in all_words for w in title_words) / len(title_words), 2) \
        if title_words else None
    return {"line_count": len(lines), "devanagari_ratio": ratio,
            "title_words_in_first_lines": title_hit, "title_words_found": title_found,
            "looks_like_lyrics": bool(looks)}


def _candidate(source: str, url: str, text: str, title: str, chords: str = "",
               status: str = "ok", note: str = "") -> dict:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r\n", "\n")).strip()
    checks = lyrics_checks(text, title)
    # Passing also needs most of the title's words somewhere in the text, so a
    # mislabelled page (a different song under this title) doesn't win.
    title_ok = checks["title_words_found"] is None or checks["title_words_found"] >= 0.5
    passes = (checks["looks_like_lyrics"] and title_ok
              and checks["devanagari_ratio"] <= HINGLISH_MAX_DEVANAGARI)
    return {"source": source, "schema_source": SCHEMA_SOURCE.get(source, "other-web"),
            "url": url, "status": status, "text": text, "chords": chords,
            "checks": checks, "passes": bool(passes), "note": note}


def _attempt(source: str, status: str, note: str, url: str = "") -> dict:
    return {"source": source, "schema_source": SCHEMA_SOURCE.get(source, "other-web"),
            "url": url, "status": status, "text": "", "chords": "",
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

# --- Indian lyrics sites ------------------------------------------------------------------

def _br_text(el) -> str:
    for br in el.find_all("br"):
        br.replace_with("\n")
    return re.sub(r"[ \t]*\r?\n[ \t]*", "\n", el.get_text()).strip()


def _deva_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 0.0


def _ranked_links(pairs: list[tuple], title: str, max_pages: int = 3) -> list[str]:
    """(url, label[, bonus]) tuples -> urls of the best title matches, best first.
    The bonus (e.g. +10 when the result's film matches) only reorders results
    that already clear MIN_TITLE_SIMILARITY on the title."""
    scored, seen = [], set()
    for url, label, *extra in pairs:
        if url in seen:
            continue
        seen.add(url)
        sim = _similarity(title, label)
        # Prefer labels holding every title word ("Pal Pal Dil Ke Paas, Tum Rahti
        # Ho") over shorter ones that only share most of them ("Pal Pal Dil Ke").
        label_words = set(_words(label))
        sim += 5 if all(w in label_words for w in _words(title)) else -5
        if "version" in label.lower() and "version" not in title.lower():
            sim -= 10
        if sim >= MIN_TITLE_SIMILARITY:
            scored.append((sim + (extra[0] if extra else 0), url))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:max_pages]]


def _try_pages(source: str, urls: list[str], extract, title: str) -> list[dict]:
    out = []
    for url in urls:
        try:
            text = extract(fetch(url).text)
        except FetchError as e:
            out.append(_attempt(source, e.code, str(e), url))
            continue
        if not text:
            out.append(_attempt(source, "needs_js", "No lyrics in the server HTML.", url))
            continue
        cand = _candidate(source, url, text, title)
        out.append(cand)
        if cand["passes"]:
            break
    return out


def extract_hindigeetmala(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one('div.song [itemprop="text"]') or soup.select_one("div.song")
    return _br_text(el) if el else ""


def try_hindigeetmala(title: str, film: str, singers: str) -> list[dict]:
    url = "https://www.hindigeetmala.net/search.php?value=" + quote(title)
    try:
        soup = BeautifulSoup(fetch(url).text, "html.parser")
    except FetchError as e:
        return [_attempt("hindigeetmala", e.code, f"search: {e}", url)]
    pairs = [(urljoin(url, a["href"]), a.get_text(" ", strip=True) or a["href"].split("/")[-1])
             for a in soup.select('a[href^="/song/"]') if a.get_text(strip=True)]
    links = _ranked_links(pairs, title)
    if not links:
        return [_attempt("hindigeetmala", "no_match", f"No result similar to '{title}'.", url)]
    return _try_pages("hindigeetmala", links, extract_hindigeetmala, title)


def extract_geetmanjusha(html: str) -> str:
    """The page has the lyrics twice, Devanagari and Roman: take the Roman one."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = [_br_text(p) for p in soup.find_all("pre")]
    blocks = [b for b in blocks if len(b) > 80]
    if not blocks:
        return ""
    return min(blocks, key=_deva_ratio)


def try_geetmanjusha(title: str, film: str, singers: str) -> list[dict]:
    url = "https://geetmanjusha.com/search?term=" + quote(title)
    try:
        html = fetch(url).text
    except FetchError as e:
        return [_attempt("geetmanjusha", e.code, f"search: {e}", url)]
    pairs = []
    for href in re.findall(r'href="(https://geetmanjusha\.com/lyrics/[^"?#]+)"', html):
        slug = re.sub(r"^\d+-", "", href.rsplit("/", 1)[-1])
        latin = re.sub(r"[^\x00-\x7f].*$", "", slug).replace("-", " ").strip()
        pairs.append((href, latin))
    links = _ranked_links(pairs, title)
    if not links:
        return [_attempt("geetmanjusha", "no_match", f"No result similar to '{title}'.", url)]
    return _try_pages("geetmanjusha", links, extract_geetmanjusha, title)


_ITRANS = [(".Nh", "n"), (".N", "n"), (".n", "n"), (".D", "d"), (".h", ""), ("~N", "n"),
           ("~n", "n"), ("R^i", "ri"), ("Sh", "sh"), ("shh", "sh"), ("T", "t"), ("D", "d"),
           ("N", "n"), ("L", "l"), ("M", "n"), ("H", "h"), ("K", "kh"), ("G", "gh"),
           ("_", "")]


def itrans_to_hinglish(text: str) -> str:
    """lyricsindia.net writes lyrics in ITRANS ("jiivan miiThii", "aa.Nkho.n").
    Convert to everyday Hinglish ("jeevan meethi", "aankhon")."""
    for a, b in _ITRANS:
        text = text.replace(a, b)
    text = re.sub(r"ii\b", "i", text)
    text = text.replace("ii", "ee").replace("uu", "oo")
    text = re.sub(r"\.(?=[a-z])", "", text)  # leftover ITRANS dots inside words
    return text


def extract_lyricsindia(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("pre#ASCII") or soup.select_one("pre.lyrics")
    if not el:
        return ""
    lines = [ln.strip() for ln in itrans_to_hinglish(el.get_text().replace("\r", "")).split("\n")]
    return "\n".join(lines).strip()


def try_lyricsindia(title: str, film: str, singers: str) -> list[dict]:
    url = "https://lyricsindia.net/songs/search?txt=" + quote(title)
    try:
        soup = BeautifulSoup(fetch(url).text, "html.parser")
    except FetchError as e:
        return [_attempt("lyricsindia", e.code, f"search: {e}", url)]
    pairs = []
    for a in soup.select('a[href^="/songs/"]'):
        if not re.match(r"^/songs/\d+$", a["href"]):
            continue
        label = a.get_text(" ", strip=True)  # "देवनागरी - itrans title (Film)"
        roman = itrans_to_hinglish(label.split(" - ", 1)[-1])
        film_part = re.search(r"\(([^)]*)\)\s*$", roman)
        roman = re.sub(r"\([^)]*\)\s*$", "", roman)
        bonus = 10 if film and film_part and _similarity(film, film_part.group(1)) >= 80 else 0
        pairs.append((urljoin(url, a["href"]), roman, bonus))
    links = _ranked_links(pairs, title)
    if not links:
        return [_attempt("lyricsindia", "no_match", f"No result similar to '{title}'.", url)]
    return _try_pages("lyricsindia", links, extract_lyricsindia, title)


def _mxm_slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-")


def extract_musixmatch(html: str) -> str:
    """Musixmatch renders lyrics client-side for logged-out visitors. Use them
    only if a lyrics body is present in the page's __NEXT_DATA__ JSON."""
    soup = BeautifulSoup(html, "html.parser")
    nd = soup.select_one("script#__NEXT_DATA__")
    if not nd or not nd.string:
        return ""
    try:
        data = json.loads(nd.string)
    except ValueError:
        return ""
    stack = [data]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            lyr = o.get("lyrics")
            if isinstance(lyr, dict) and isinstance(lyr.get("body"), str) and len(lyr["body"]) > 80:
                return lyr["body"].strip()
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return ""


def try_musixmatch(title: str, film: str, singers: str) -> list[dict]:
    """Musixmatch search needs a login, so try the song URL pattern directly:
    /lyrics/<Artist>/<Title> for the first singer."""
    artist = (singers.split(";")[0] if singers else "").strip()
    if not artist:
        return [_attempt("musixmatch", "no_match", "Needs a singer to build the song URL "
                         "(search requires a login).")]
    url = f"https://www.musixmatch.com/lyrics/{_mxm_slug(artist)}/{_mxm_slug(title)}"
    try:
        text = extract_musixmatch(fetch(url).text)
    except FetchError as e:
        return [_attempt("musixmatch", "no_match" if e.code == "not_found" else e.code, str(e), url)]
    if not text:
        return [_attempt("musixmatch", "needs_js", "Lyrics are rendered by JavaScript for "
                         "logged-out visitors.", url)]
    return [_candidate("musixmatch", url, text, title)]


SOURCES = [("genius", try_genius), ("ultimate-guitar", try_ultimate_guitar),
           ("hindigeetmala", try_hindigeetmala), ("geetmanjusha", try_geetmanjusha),
           ("lyricsindia", try_lyricsindia), ("musixmatch", try_musixmatch),
           ("smule", try_smule)]


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
    elif host.endswith("hindigeetmala.net"):
        source, text = "hindigeetmala", extract_hindigeetmala(html)
    elif host.endswith("geetmanjusha.com"):
        source, text = "geetmanjusha", extract_geetmanjusha(html)
    elif host.endswith("lyricsindia.net"):
        source, text = "lyricsindia", extract_lyricsindia(html)
    elif host.endswith("musixmatch.com"):
        source, text = "musixmatch", extract_musixmatch(html)
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
