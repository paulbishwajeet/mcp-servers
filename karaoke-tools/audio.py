"""Local audio: scan SEED_ROOT for MP3s, and analyze one file with librosa.

Audio paths must resolve inside SEED_ROOT (read-only MP3 folder) or
MEDIA_ROOT (temp downloads). librosa is imported lazily: its first import
JIT-compiles numba code and takes several seconds, which we don't want on
server startup.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import numpy as np

from common import PathError, fail, ffmpeg_path, log, media_root, ok, resolve_inside, seed_root

SR = 22050
MAX_ANALYZE_SEC = 240
FINGERPRINT_BYTES = 1 << 20
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".webm", ".aac"}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Schmuckler key profiles (Krumhansl & Kessler 1982), C as tonic.
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

# Energy 1-5 calibration. Karaoke masters are uniformly loud (mean RMS
# -8 to -16 dBFS) with dense onsets, so loudness and onset rate barely
# separate ballads from dance numbers. Two features did, on 14 karaoke
# tracks hand-labelled from "Sajni" (soft) to "Aaj Ki Raat" (dance):
#   percussive: share of the signal in the percussive part (HPSS) of a
#               60 s excerpt, 0.20 -> 0, 0.55 -> 1
#   strength:   mean onset strength, 1.2 -> 0, 1.95 -> 1
# combined = mean of the two; energy = 1 + number of ENERGY_STEPS reached.
# Silence (mean RMS < 1e-5) is always 1. This is a measurement for the agent
# to sanity-check against what it knows about the song, not ground truth;
# the raw values are returned so the thresholds can be re-tuned.
PERCUSSIVE_RANGE = (0.20, 0.55)
STRENGTH_RANGE = (1.2, 1.95)
ENERGY_STEPS = (0.20, 0.40, 0.60, 0.80)


# --- scan_folder -------------------------------------------------------------------

def fingerprint(path: Path, size: int | None = None) -> str:
    size = path.stat().st_size if size is None else size
    h = hashlib.sha1(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(FINGERPRINT_BYTES))
    return h.hexdigest()


def _id3(path: Path) -> dict:
    out = {"duration_sec": None, "id3_title": "", "id3_artist": "", "id3_album": ""}
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is None:
            return out
        if getattr(f, "info", None) and getattr(f.info, "length", None):
            out["duration_sec"] = round(float(f.info.length), 1)
        tags = f.tags or {}
        for key, field in (("title", "id3_title"), ("artist", "id3_artist"), ("album", "id3_album")):
            value = tags.get(key)
            if value:
                out[field] = "; ".join(str(v) for v in value)[:200]
    except Exception as e:  # corrupt tags must not break the batch
        log(f"[scan] tags for {path.name}: {e!r}")
    return out


def _list_mp3s(base: Path, root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(".mp3"):
                continue
            p = Path(dirpath) / name
            try:  # a symlinked file must still land inside SEED_ROOT
                resolve_inside(p, [root])
            except PathError:
                continue
            if p.is_file():
                found.append(p)
    return sorted(found, key=lambda p: str(p.relative_to(root)).lower())


def scan_folder(subpath: str = "", offset: int = 0, limit: int = 50) -> dict:
    root = seed_root()
    if root is None:
        return fail("seed_root_not_set", "SEED_ROOT is not set.")
    if not root.is_dir():
        return fail("not_found", f"SEED_ROOT {root} is not a folder.")
    try:
        base = resolve_inside(subpath or ".", [root], base=root)
    except PathError as e:
        return fail("path_not_allowed", str(e))
    if not base.is_dir():
        return fail("not_found", f"'{subpath}' is not a folder under SEED_ROOT.")
    try:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        return fail("invalid_input", "offset and limit must be whole numbers.")

    files = _list_mp3s(base, root)
    sizes = {}
    for p in files:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            sizes[p] = -1

    # Exact duplicates must share a size, so only fingerprint size collisions.
    by_size: dict[int, list[Path]] = {}
    for p, s in sizes.items():
        by_size.setdefault(s, []).append(p)
    prints: dict[Path, str] = {}
    for group in by_size.values():
        if len(group) > 1:
            for p in group:
                try:
                    prints[p] = fingerprint(p, sizes[p])
                except OSError:
                    pass
    by_print: dict[str, list[str]] = {}
    for p, fp in prints.items():
        by_print.setdefault(fp, []).append(str(p.relative_to(root)))
    duplicates = [sorted(g) for g in by_print.values() if len(g) > 1]
    dup_paths = {path for g in duplicates for path in g}

    items = []
    for p in files[offset:offset + limit]:
        rel = str(p.relative_to(root))
        item = {"path": rel, "file_name": p.name, "size_bytes": sizes[p]}
        try:
            item["fingerprint"] = prints.get(p) or fingerprint(p, sizes[p])
        except OSError as e:
            item["fingerprint"] = ""
            item["error"] = f"unreadable: {e}"
        item |= _id3(p)
        item["duplicate"] = rel in dup_paths
        items.append(item)

    next_offset = offset + limit if offset + limit < len(files) else None
    return ok(seed_root=str(root), subpath=subpath, total=len(files), offset=offset,
              limit=limit, next_offset=next_offset, files=items, duplicates=duplicates)


# --- analyze_audio ---------------------------------------------------------------------

def resolve_audio_path(path: str) -> Path:
    """Absolute path, or relative to SEED_ROOT. Must stay inside SEED_ROOT or MEDIA_ROOT."""
    roots = [r for r in (seed_root(), media_root()) if r is not None]
    p = resolve_inside(path, roots, base=seed_root())
    return p


def _load_with_ffmpeg(path: Path) -> np.ndarray:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("soundfile could not decode this file and ffmpeg was not found "
                           "(set FFMPEG_PATH=/usr/local/bin/ffmpeg).")
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(MAX_ANALYZE_SEC),
         "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def load_audio(path: Path) -> tuple[np.ndarray, str]:
    """Mono float32 at 22.05 kHz, first 240 s. soundfile (libsndfile >= 1.1
    reads MP3) first; ffmpeg via its explicit path as the fallback."""
    import librosa
    try:
        y, _ = librosa.load(str(path), sr=SR, mono=True, duration=MAX_ANALYZE_SEC)
        return y, "soundfile"
    except Exception as e:
        log(f"[analyze] soundfile could not read {path.name} ({e!r}); trying ffmpeg")
    return _load_with_ffmpeg(path), "ffmpeg"


def file_duration(path: Path) -> float | None:
    try:
        import mutagen
        f = mutagen.File(path)
        if f is not None and f.info and f.info.length:
            return float(f.info.length)
    except Exception:
        pass
    try:
        import soundfile
        return float(soundfile.info(str(path)).duration)
    except Exception:
        return None


def estimate_key(chroma_mean: np.ndarray) -> dict:
    """Krumhansl-Schmuckler: correlate the mean chroma with all 24 rotated profiles."""
    if not np.any(chroma_mean > 0) or np.allclose(chroma_mean, chroma_mean[0]):
        return {"musical_key": "", "key_confidence": 0.0, "key_alternatives": []}
    scores = []
    for tonic in range(12):
        for mode, profile in (("major", KS_MAJOR), ("minor", KS_MINOR)):
            r = float(np.corrcoef(chroma_mean, np.roll(profile, tonic))[0, 1])
            scores.append((r, f"{NOTE_NAMES[tonic]} {mode}"))
    scores.sort(reverse=True)
    best_r, best = scores[0]
    # Confidence: how far the winner beats the runner-up, scaled so a 0.1
    # correlation margin is "confident" (relative major/minor often tie).
    margin = best_r - scores[1][0]
    confidence = round(float(np.clip(max(best_r, 0) * min(1.0, margin / 0.1 + 0.3), 0, 1)), 2)
    return {"musical_key": best, "key_confidence": confidence,
            "key_alternatives": [{"key": k, "r": round(r, 3)} for r, k in scores[1:4]]}


def _unit(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0, 1)) if np.isfinite(x) else 0.0


def energy_level(percussive: float, strength: float) -> int:
    combined = 0.5 * _unit(percussive, *PERCUSSIVE_RANGE) + 0.5 * _unit(strength, *STRENGTH_RANGE)
    return 1 + sum(combined >= step for step in ENERGY_STEPS)


def percussive_ratio(y: np.ndarray, sr: int) -> float:
    """RMS of the percussive component over RMS of the whole, on the middle 60 s."""
    import librosa
    n = 60 * sr
    start = max(0, (len(y) - n) // 2)
    seg = y[start:start + n]
    _, perc = librosa.effects.hpss(seg)
    total = float(np.sqrt(np.mean(seg ** 2)))
    return float(np.sqrt(np.mean(perc ** 2))) / total if total > 1e-9 else 0.0


def intro_seconds(rms: np.ndarray, hop_sec: float, sustain_sec: float = 2.0) -> float:
    """Seconds before sustained energy: the first frame from which at least
    80% of the next `sustain_sec` is above half the track's median RMS."""
    if rms.size == 0 or float(np.max(rms)) <= 1e-6:
        return 0.0
    threshold = 0.5 * float(np.median(rms[rms > 1e-6])) if np.any(rms > 1e-6) else 0.0
    above = rms > max(threshold, 1e-4)
    win = max(1, int(round(sustain_sec / hop_sec)))
    if above.size < win:
        return 0.0
    frac = np.convolve(above.astype(float), np.ones(win) / win, mode="valid")
    idx = np.flatnonzero(frac >= 0.8)
    if not idx.size:
        return 0.0
    first = idx[0] + int(np.argmax(above[idx[0]:]))  # the window may start a little early
    return round(float(first * hop_sec), 1)


def refine_bpm(tempo: float, beat_times: np.ndarray) -> float:
    """beat_track's tempo is quantized to whole autocorrelation lags (117.5 or
    123.0 near 120 at hop 512). The tracked beats alternate between the two
    neighbouring lags, so their mean spacing recovers the true tempo."""
    if tempo <= 0 or len(beat_times) < 8:
        return tempo
    ibi = np.diff(beat_times)
    med = float(np.median(ibi))
    steady = ibi[np.abs(ibi - med) < 0.25 * med]
    if steady.size < 6:
        return tempo
    refined = 60.0 / float(np.mean(steady))
    return refined if abs(refined - tempo) / tempo < 0.08 else tempo


def analyze_array(y: np.ndarray, sr: int = SR) -> dict:
    """Pure analysis of a mono signal (used directly by the tests)."""
    import librosa

    hop = 512
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    mean_rms = float(np.mean(rms)) if rms.size else 0.0
    rms_db = 20 * np.log10(mean_rms) if mean_rms > 1e-10 else float("-inf")
    seconds = len(y) / sr

    if mean_rms < 1e-5:  # silence: nothing to track
        bpm, onset_rate, percussive, strength = 0.0, 0.0, 0.0, 0.0
        key = {"musical_key": "", "key_confidence": 0.0, "key_alternatives": []}
    else:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=hop)
        bpm = refine_bpm(float(np.atleast_1d(tempo)[0]), beats * hop / sr)
        onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=hop)
        onset_rate = len(onsets) / seconds if seconds else 0.0
        strength = float(np.mean(onset_env))
        percussive = percussive_ratio(y, sr)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
        key = estimate_key(chroma.mean(axis=1))

    bpm = round(bpm, 1)
    candidates = [c for c in (bpm, round(bpm / 2, 1), round(bpm * 2, 1)) if bpm and 30 <= c <= 300]
    return {
        "bpm": bpm or None,
        "bpm_candidates": candidates,
        **key,
        "energy": energy_level(percussive, strength) if mean_rms >= 1e-5 else 1,
        "percussive_ratio": round(percussive, 3),
        "onset_strength": round(strength, 2),
        "rms_db": round(float(rms_db), 1) if np.isfinite(rms_db) else None,
        "onset_rate": round(onset_rate, 2),
        "intro_sec": intro_seconds(rms, hop / sr),
        "analyzed_sec": round(seconds, 1),
    }


def analyze_audio(path: str) -> dict:
    try:
        p = resolve_audio_path(path)
    except PathError as e:
        return fail("path_not_allowed", f"{e} Audio must be inside SEED_ROOT or MEDIA_ROOT.")
    if not p.is_file():
        return fail("not_found", f"No audio file at {path}.")
    if p.suffix.lower() not in AUDIO_EXTS:
        return fail("unsupported_format", f"Not an audio file: {p.name}.")
    try:
        y, decoder = load_audio(p)
    except Exception as e:
        return fail("decode_failed", str(e), path=str(p))
    if y.size < SR:  # under a second
        return fail("decode_failed", "Decoded audio is shorter than one second.", path=str(p))
    result = analyze_array(y, SR)
    duration = file_duration(p)
    return ok(path=str(p), duration_sec=int(round(duration)) if duration else
              int(round(result["analyzed_sec"])), decoder=decoder, **result)
