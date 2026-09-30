"""The daemon's state machine with a fake model, microphone and output."""

import queue

import numpy as np
import pytest

from iris_dictation import config, daemon


class FakeRecorder:
    listener = None

    def __init__(self, seconds, amplitude=0.1):
        self.samples = np.full(int(16000 * seconds), amplitude, dtype=np.float32)

    def start(self):
        pass

    def stop(self):
        return self.samples


class FakeModel:
    def __init__(self, text):
        self.text = text

    def recognize(self, samples, sample_rate):
        return self.text


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    typed = []
    monkeypatch.setattr(daemon.out, "deliver", lambda text, *a: typed.append(text) or "type")

    def build(text="This is a test.", seconds=2.0, amplitude=0.1):
        d = daemon.Daemon(config.Config(mute_apps=[], tail_ms=0))
        d.recorder = FakeRecorder(seconds, amplitude)
        d.model = FakeModel(text)
        sent = []
        d.levels.send = sent.append
        return d, typed, sent
    return build


def test_press_and_release_types_the_text(make):
    d, typed, sent = make()
    d.on_down()
    assert d.state == "recording"
    d.on_up()
    assert d.state == "idle"
    assert typed == ["This is a test. "]
    assert sent == ["recording", "transcribing", "text This is a test.", "typing 16", "idle"]


def test_stop_return_replies_instead_of_typing(make):
    d, typed, _ = make()
    box = queue.Queue()
    d.on_down()
    d.on_up(box)
    assert box.get_nowait() == "This is a test."
    assert typed == []


def test_release_without_press_replies_empty(make):
    d, _, _ = make()
    box = queue.Queue()
    d.on_up(box)
    assert box.get_nowait() == ""


def test_too_short_is_ignored(make):
    d, typed, sent = make(seconds=0.1)
    d.on_down()
    d.on_up()
    assert typed == []
    assert sent[-1] == "idle"


def test_silence_phrase_on_a_quiet_clip_is_dropped(make):
    d, typed, sent = make(text="Thank you.", amplitude=0.001)
    d.on_down()
    d.on_up()
    assert typed == []
    assert "nothing" in sent


def test_thank_you_said_out_loud_is_kept(make):
    d, typed, _ = make(text="Thank you.", amplitude=0.1)
    d.on_down()
    d.on_up()
    assert typed == ["thank you "]


class FakeFixer:
    def fix(self, text):
        return text.replace("post gress", "Postgres")


def test_typed_results_are_fixed(make):
    d, typed, sent = make(text="Please move it to post gress.")
    d.fixer = FakeFixer()
    d.on_down()
    d.on_up()
    assert typed == ["Please move it to Postgres. "]
    assert "text Please move it to Postgres." in sent


def test_stop_return_is_not_fixed(make):
    d, _, _ = make(text="Please move it to post gress.")
    d.fixer = FakeFixer()
    box = queue.Queue()
    d.on_down()
    d.on_up(box)
    assert box.get_nowait() == "Please move it to post gress."


def test_next_press_records_while_the_last_result_waits(make):
    d, typed, sent = make()
    d.jobs = queue.Queue()               # a worker that hasn't got to it yet
    d.on_down()
    d.on_up()
    assert d.state == "transcribing"
    d.on_down()
    assert d.state == "recording"
    d.type_result(d.jobs.get_nowait())   # typed while recording
    assert typed == ["This is a test. "]
    assert d.state == "recording"
    assert sent[-1] == "recording"       # the waveform isn't interrupted
    d.on_up()
    d.type_result(d.jobs.get_nowait())
    assert typed == ["This is a test. ", "This is a test. "]
    assert d.state == "idle"
