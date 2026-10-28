"""Allowed values and column definitions, mirrored from the wiki's SCHEMA.md.

SCHEMA.md is the authority. If it changes, update this file to match;
tests/test_vocab.py parses SCHEMA.md's tables and fails when the two drift.
"""

SONG_COLUMNS = [
    "song_id", "title", "film", "year", "singers", "music_director", "lyricist",
    "language", "voice_type", "era", "bpm", "tempo_class", "musical_key", "energy",
    "duration_sec", "genre", "mood", "emotion", "theme", "situation", "singalong",
    "karaoke_url", "karaoke_alt_urls", "karaoke_channel", "lyrics_file",
    "lyrics_source", "lyrics_url", "added_from", "added_on", "status",
    "my_key_change", "notes", "times_performed", "crowd_score", "last_performed",
    "confidence", "needs_review",
]

REVIEW_COLUMNS = ["logged_at", "song_id", "source_ref", "problem", "details", "resolved"]

INDEX_COLUMNS = ["video_id", "title", "url", "duration_sec", "indexed_on"]

# --- single-value enums -------------------------------------------------------

LANGUAGE = ["hindi", "urdu", "punjabi", "bengali", "english", "mixed"]
VOICE_TYPE = ["solo-male", "solo-female", "duet-mf", "duet-mm", "duet-ff", "group"]
ERA = ["pre-1960", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]
TEMPO_CLASS = ["slow", "medium", "fast"]
LYRICS_SOURCE = ["genius", "smule", "ultimate-guitar", "other-web", "neel", "transliterated"]
ADDED_FROM = ["local-mp3", "youtube-link", "song-name"]
STATUS = ["ready", "practicing", "wishlist", "retired"]
CONFIDENCE = ["high", "medium", "low"]

# --- multi-value enums ----------------------------------------------------------

GENRE = [
    "romantic-ballad", "sad", "peppy", "dance-party", "disco", "qawwali", "sufi", "ghazal",
    "classical-based", "folk", "bhangra-punjabi", "bhajan-devotional", "patriotic",
    "cabaret", "item", "rock", "lullaby", "comedy",
]
MOOD = [
    "romantic", "happy", "playful", "sad", "longing", "nostalgic", "energetic", "calm",
    "devotional", "philosophical", "celebratory", "inspirational",
]
THEME = [
    "love", "separation", "friendship", "rain-monsoon", "nature", "travel-journey",
    "night-moon", "youth-college", "family-mother", "wedding", "festival", "patriotism",
    "life-philosophy", "dance", "beauty-praise", "farewell",
]
SITUATION = [
    "wedding", "sangeet", "anniversary", "birthday", "farewell", "reunion", "holi",
    "diwali", "navratri", "independence-day", "valentines", "new-year", "house-party",
    "opener", "closer", "late-evening", "rainy-day", "road-trip", "antakshari",
]
# SCHEMA.md: emotion is "free text but short"; these are its examples, not a
# closed list. Values are checked for shape (short slug), not membership.
EMOTION_SUGGESTED = [
    "first-love", "heartbreak", "separation", "yearning", "friendship-bond", "teasing",
    "gratitude", "hope", "pride", "loneliness", "devotion", "joy-of-life",
]

# --- review.csv ---------------------------------------------------------------------

REVIEW_PROBLEM = [
    "unsure-match", "no-lyrics", "lyrics-mismatch", "no-karaoke-link", "dead-link",
    "analysis-failed", "metadata-conflict",
]

# --- events -------------------------------------------------------------------------

OUTCOME_SCORES = {"standout": 3, "good": 2, "weak": 1}

# --- column typing ------------------------------------------------------------------

ENUM_FIELDS = {
    "language": LANGUAGE,
    "voice_type": VOICE_TYPE,
    "era": ERA,
    "tempo_class": TEMPO_CLASS,
    "lyrics_source": LYRICS_SOURCE,
    "added_from": ADDED_FROM,
    "status": STATUS,
    "confidence": CONFIDENCE,
}
MULTI_ENUM_FIELDS = {"genre": GENRE, "mood": MOOD, "theme": THEME, "situation": SITUATION}
MULTI_FREE_FIELDS = ["singers", "music_director", "lyricist", "emotion", "karaoke_alt_urls"]

# Every column whose SCHEMA.md row lists backticked allowed values, with the
# values SCHEMA.md lists. Used by the drift test.
SCHEMA_VALUE_LISTS = ENUM_FIELDS | MULTI_ENUM_FIELDS | {"emotion": EMOTION_SUGGESTED}

INT_FIELDS = {  # column -> (min, max); None = unbounded
    "year": (1900, 2100),
    "energy": (1, 5),
    "duration_sec": (0, None),
    "singalong": (1, 5),
    "my_key_change": (-12, 12),
    "times_performed": (0, None),
}
NUMBER_FIELDS = {"bpm": (20.0, 320.0), "crowd_score": (1.0, 3.0)}
DATE_FIELDS = ["added_on", "last_performed"]
BOOL_FIELDS = ["needs_review"]
URL_FIELDS = ["karaoke_url", "lyrics_url"]
# Written unquoted in the CSV; every other field is double-quoted.
NUMERIC_COLUMNS = set(INT_FIELDS) | set(NUMBER_FIELDS)

# Values SCHEMA.md forbids for "unknown" (empty means unknown).
UNKNOWN_PLACEHOLDERS = {"n/a", "na", "none", "?", "null", "unknown", "-"}


def era_for_year(year: int) -> str:
    if year < 1960:
        return "pre-1960"
    return f"{year // 10 * 10}s"
