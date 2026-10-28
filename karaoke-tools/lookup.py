"""Wikipedia (no key) soundtrack lookup, to ground song metadata in a source
instead of model memory. Only en.wikipedia.org is contacted."""

import re

import requests
from bs4 import BeautifulSoup, Tag
from rapidfuzz import fuzz

from common import fail, ok

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "karaoke-tools/1.0 (personal MCP server for a karaoke repertoire; "
                         "python-requests)"}
TIMEOUT = 20
MAX_SECTION_CHARS = 6000
MUSIC_SECTION_RE = re.compile(r"soundtrack|music|songs|track ?list", re.I)
INFOBOX_FIELDS = {"music by": "music_by", "release date": "release_date",
                  "released": "release_date", "language": "language", "directed by": "directed_by",
                  "lyricist": "lyricist", "lyrics by": "lyricist", "label": "label"}


def _api(**params) -> dict:
    params |= {"format": "json", "formatversion": 2}
    resp = requests.get(API, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def html_to_text(html: str) -> str:
    """Readable text from a parsed section: tables as `cell | cell` rows,
    edit links and footnote markers removed."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select(".mw-editsection, sup.reference, .reference, style, .mw-empty-elt, "
                            ".navbox, .reflist, .hatnote"):
        junk.decompose()
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", c.get_text(" ")).strip() for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n")
    for li in soup.find_all("li"):
        li.insert_before("- ")
        li.append("\n")
    for block in soup.find_all(["p", "h2", "h3", "h4", "div", "ol", "ul"]):
        block.append("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_infobox(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("table.infobox")
    out = {}
    if not isinstance(box, Tag):
        return out
    hidden = [el for el in box.select("[style]")
              if "display:none" in el["style"].replace(" ", "").lower()]
    for junk in hidden + box.select("sup.reference, .noprint"):
        junk.decompose()
    for tr in box.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        key = INFOBOX_FIELDS.get(re.sub(r"\s+", " ", th.get_text(" ")).strip().lower())
        if key and key not in out:
            for br in td.find_all("br"):
                br.replace_with("; ")
            out[key] = re.sub(r"\s+", " ", td.get_text(" ")).strip(" ;")[:300]
    return out


def find_film_page(title: str, film: str, year: str) -> str | None:
    queries = []
    if film:
        queries += [f'"{film}" {year} Hindi film'.strip(), f"{film} {year} film".strip()]
    queries.append(f'"{title}" song {film}'.strip())
    film_n = _norm(film)
    for q in queries:
        hits = _api(action="query", list="search", srsearch=q, srlimit=8)["query"]["search"]
        best, best_score = None, 0.0
        for h in hits:
            page = h["title"]
            if page.lower().startswith(("list of", "category:")):
                continue
            base = _norm(re.sub(r"\(.*?\)", "", page))
            score = fuzz.ratio(base, film_n) if film_n else fuzz.partial_ratio(_norm(title), base)
            if "film" in page.lower():
                score += 10
            if year and year in page:
                score += 10
            if score > best_score:
                best, best_score = page, score
        if best and best_score >= 85:
            return best
    return None


def song_lookup(title: str, film: str = "", year: str = "") -> dict:
    title, film, year = (title or "").strip(), (film or "").strip(), str(year or "").strip()
    if not title and not film:
        return fail("invalid_input", "Give a title and/or film.")
    if year and not re.fullmatch(r"\d{4}", year):
        return fail("invalid_input", "year must be four digits.")
    try:
        page = find_film_page(title, film, year)
        if not page:
            return ok(found=False, note="No matching Wikipedia film article.")
        parsed = _api(action="parse", page=page, prop="sections|categories", redirects=1)["parse"]
        page = parsed.get("title", page)
        sections = parsed.get("sections", [])
        music = next((s for s in sections if MUSIC_SECTION_RE.search(s.get("line", ""))), None)
        lead = _api(action="parse", page=page, prop="text", section=0, redirects=1)
        infobox = parse_infobox(lead["parse"]["text"])
        text = ""
        if music:
            sec = _api(action="parse", page=page, prop="text", section=music["index"],
                       disabletoc=1, redirects=1)
            text = html_to_text(sec["parse"]["text"])
        cats = [c.get("category", "") for c in parsed.get("categories", [])]
        release_year = ""
        m = re.search(r"\((\d{4}) film\)", page)
        if m:
            release_year = m.group(1)
        else:
            years = [c[:4] for c in cats if re.fullmatch(r"\d{4}_films", c)]
            release_year = years[0] if years else ""
            if not release_year:
                m = re.search(r"\b(19|20)\d{2}\b", infobox.get("release_date", ""))
                release_year = m.group(0) if m else ""
    except (requests.RequestException, KeyError, ValueError) as e:
        return fail("network_error", f"Wikipedia lookup failed: {e}")
    mentioned = bool(title) and _norm(title) in _norm(text)
    return ok(found=True, page_title=page,
              page_url="https://en.wikipedia.org/wiki/" + page.replace(" ", "_"),
              release_year=release_year, infobox=infobox,
              section_title=music["line"] if music else "",
              soundtrack_text=text[:MAX_SECTION_CHARS], truncated=len(text) > MAX_SECTION_CHARS,
              song_mentioned=mentioned,
              note="" if music else "Article has no music/soundtrack section.")
