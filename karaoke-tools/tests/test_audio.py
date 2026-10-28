"""audio.py on synthesized signals (no files needed), plus scan_folder and
analyze_audio end to end on WAV/MP3 files written into a temp SEED_ROOT."""

import shutil

import numpy as np
import pytest
import soundfile as sf

import audio

SR = audio.SR


def click_track(bpm=120.0, seconds=30):
    y = np.zeros(int(SR * seconds), np.float32)
    t = np.arange(int(0.03 * SR)) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 80)).astype(np.float32)
    step = SR * 60.0 / bpm
    i = 0.0
    while int(i) + len(click) < len(y):
        y[int(i):int(i) + len(click)] += click
        i += step
    return 0.8 * y


def chord_progression(seconds=24):
    """C - F - G - C, triads plus an octave-down doubling."""
    prog = [[60, 64, 67], [65, 69, 72], [67, 71, 74], [60, 64, 67]]
    seg = int(seconds * SR / len(prog))
    t = np.arange(seg) / SR
    out = []
    for chord in prog:
        s = sum(np.sin(2 * np.pi * 440 * 2 ** ((m - 69) / 12) * t)
                + 0.5 * np.sin(2 * np.pi * 440 * 2 ** ((m - 81) / 12) * t) for m in chord)
        out.append(s * np.minimum(1, t * 20))
    y = np.concatenate(out).astype(np.float32)
    return 0.2 * y / np.abs(y).max()


@pytest.fixture(scope="module")
def click_result():
    return audio.analyze_array(click_track())


def test_click_track_bpm(click_result):
    bpm = click_result["bpm"]
    assert abs(bpm - 120) <= 2 or any(abs(c - 120) <= 2 for c in click_result["bpm_candidates"])
    assert click_result["bpm_candidates"][1:] == [round(bpm / 2, 1), round(bpm * 2, 1)]


def test_c_major_progression_key():
    r = audio.analyze_array(chord_progression())
    assert r["musical_key"] == "C major"
    assert r["key_confidence"] > 0.5


def test_silence_low_energy():
    r = audio.analyze_array(np.zeros(SR * 10, np.float32))
    assert r["energy"] == 1
    assert r["bpm"] is None and r["bpm_candidates"] == []
    assert r["musical_key"] == ""


def test_energy_mapping_monotonic():
    assert audio.energy_level(0.0, 0.0) == 1
    assert audio.energy_level(0.6, 2.0) == 5
    levels = [audio.energy_level(p, s) for p, s in
              [(0.2, 1.2), (0.3, 1.4), (0.38, 1.6), (0.45, 1.75), (0.55, 1.95)]]
    assert levels == sorted(levels) and levels[0] < levels[-1]


def test_intro_seconds():
    hop = 512 / SR
    rms = np.concatenate([np.full(int(5 / hop), 0.001), np.full(int(20 / hop), 0.2)])
    assert abs(audio.intro_seconds(rms, hop) - 5.0) < 0.3
    assert audio.intro_seconds(np.zeros(100), hop) == 0.0


def test_estimate_key_minor():
    # A natural minor emphasis: A, C, E strong.
    chroma = np.array([0.6, 0.1, 0.3, 0.1, 0.6, 0.3, 0.1, 0.4, 0.1, 1.0, 0.1, 0.3])
    assert audio.estimate_key(chroma)["musical_key"] in ("A minor", "C major")


# --- files ------------------------------------------------------------------------------

def test_analyze_audio_wav_in_seed(wiki, tmp_path):
    seed = tmp_path / "seed"
    sf.write(seed / "click.wav", click_track(seconds=20), SR)
    r = audio.analyze_audio("click.wav")  # relative to SEED_ROOT
    assert r["success"], r
    assert r["duration_sec"] == 20
    assert r["decoder"] == "soundfile"


@pytest.mark.skipif(not shutil.which("ffmpeg") and not shutil.which("/usr/local/bin/ffmpeg"),
                    reason="ffmpeg needed to make an mp3")
def test_scan_folder_mp3s_and_duplicates(wiki, tmp_path):
    import subprocess
    seed = tmp_path / "seed"
    (seed / "Kishore").mkdir()
    wav = tmp_path / "x.wav"
    sf.write(wav, click_track(seconds=5), SR)
    ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
    mp3 = seed / "Kishore" / "pal pal.mp3"
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", str(wav), "-metadata",
                    "title=Pal Pal Dil Ke Paas", "-metadata", "artist=Kishore Kumar",
                    str(mp3)], check=True)
    shutil.copy(mp3, seed / "copy.mp3")
    (seed / "notes.txt").write_text("not audio")
    r = audio.scan_folder()
    assert r["success"] and r["total"] == 2
    files = {f["path"]: f for f in r["files"]}
    assert files["Kishore/pal pal.mp3"]["id3_title"] == "Pal Pal Dil Ke Paas"
    assert files["Kishore/pal pal.mp3"]["id3_artist"] == "Kishore Kumar"
    assert 4.5 < files["copy.mp3"]["duration_sec"] < 6
    assert files["copy.mp3"]["duplicate"] and files["copy.mp3"]["fingerprint"]
    assert r["duplicates"] == [["Kishore/pal pal.mp3", "copy.mp3"]]
    page = audio.scan_folder(limit=1)
    assert len(page["files"]) == 1 and page["next_offset"] == 1
    assert audio.scan_folder(offset=1, limit=1)["next_offset"] is None
    assert audio.scan_folder("Kishore")["total"] == 1
    a = audio.analyze_audio(str(mp3))
    assert a["success"] and a["duration_sec"] == 5


def test_scan_folder_needs_seed_root(wiki, monkeypatch):
    monkeypatch.delenv("SEED_ROOT")
    assert audio.scan_folder()["error_code"] == "seed_root_not_set"


def test_analyze_rejects_non_audio(wiki, tmp_path):
    (tmp_path / "seed" / "notes.txt").write_text("x")
    assert audio.analyze_audio("notes.txt")["error_code"] == "unsupported_format"
    assert audio.analyze_audio("missing.mp3")["error_code"] == "not_found"
