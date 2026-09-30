"""End to end through the running daemon: the real model on the sample clips.
Skipped when the daemon is not running. Nothing is typed."""

import re
from pathlib import Path

import pytest

from iris_dictation import ctl

CLIPS = Path(__file__).parent.parent / "testwav"
# Words that must come through (see testwav/SOURCES.md).
EXPECTED = {
    "16k_fleurs_en_0.wav": ["thousands", "miles", "satellite"],
    "16k_fleurs_en_2.wav": ["fund", "global warming"],
    "16k_piper_hu.wav": ["magyarul", "működik"],
}


def daemon_running() -> bool:
    try:
        return ctl.send("ping", timeout=2) == "pong"
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not daemon_running(), reason="daemon not running")


@pytest.mark.parametrize("clip", sorted(EXPECTED))
def test_clip(clip):
    text = ctl.send(f"transcribe {CLIPS / clip}").lower()
    for word in EXPECTED[clip]:
        assert re.search(word, text), f"{word!r} not in {text!r}"


def test_missing_file_does_not_kill_the_daemon():
    assert ctl.send("transcribe /nonexistent.wav") == ""
    assert ctl.send("ping") == "pong"


def test_recording_longer_than_30_seconds_keeps_the_end(tmp_path):
    # ~45 s: the English clips back to back with short pauses. The model is
    # trained on up to 40 s, so the daemon splits and every sentence survives.
    import wave
    names = ["16k_fleurs_en_0.wav", "16k_fleurs_en_1.wav", "16k_fleurs_en_2.wav",
             "16k_fleurs_en_0.wav", "16k_fleurs_en_2.wav"]
    out = tmp_path / "long.wav"
    with wave.open(str(out), "wb") as o:
        for i, name in enumerate(names):
            with wave.open(str(CLIPS / name)) as w:
                if i == 0:
                    o.setparams(w.getparams())
                o.writeframes(w.readframes(w.getnframes()) + b"\0\0" * 8000)   # 0.5 s pause
    with wave.open(str(out)) as w:
        assert w.getnframes() / w.getframerate() > 40
    text = ctl.send(f"transcribe {out}").lower()
    assert "archipelago" in text                     # middle
    assert text.count("global warming") == 2         # the end, past 30 s
