"""Several microphones: the first one that picked up sound is used."""

import numpy as np

from iris_dictation import config, daemon
from iris_dictation.audio import Mics

LOUD = (np.full(320, 3000, dtype=np.int16)).tobytes()
SILENT = bytes(640)


class FakeMic:
    listener = None

    def __init__(self, source, chunk):
        self.source = source
        self.chunk = chunk

    def start(self):
        pass

    def hear(self):
        self.listener(self.chunk)

    def stop(self):
        return np.frombuffer(self.chunk, dtype=np.int16).astype(np.float32) / 32768


def build(*chunks):
    mics = [FakeMic(f"mic{i}", chunk) for i, chunk in enumerate(chunks)]
    both = Mics(mics)
    picked, heard = [], []
    both.on_pick = picked.append
    both.listener = heard.append
    both.start()
    return mics, both, picked, heard


def test_a_silent_first_microphone_is_passed_over():
    mics, both, picked, heard = build(SILENT, LOUD)
    for mic in mics:
        mic.hear()
    assert picked == ["mic1"]
    assert heard == [LOUD]
    assert both.stop().max() > 0


def test_the_preferred_microphone_wins_once_it_hears_something():
    mics, both, picked, _ = build(LOUD, LOUD)
    mics[1].hear()
    mics[0].hear()
    mics[1].hear()
    assert picked == ["mic1", "mic0"]
    assert both.stop() is not None


def test_nothing_heard_anywhere_is_an_empty_clip():
    mics, both, picked, _ = build(SILENT, SILENT)
    for mic in mics:
        mic.hear()
    assert picked == []
    assert len(both.stop()) == 0


def test_the_daemon_names_the_microphone(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(daemon.audio, "sources", lambda: {"phone": "iPhone Mic"})
    d = daemon.Daemon(config.Config(mute_apps=[], audio_sources=["phone", "headset"]))
    assert isinstance(d.recorder, Mics)
    sent = []
    d.levels.send = sent.append
    d.recorder.on_pick("phone")
    d.recorder.on_pick("headset")
    assert sent == ["mic iPhone Mic", "mic headset"]
