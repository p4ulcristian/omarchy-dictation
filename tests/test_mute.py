import json

from iris_dictation import mute

STREAMS = [
    {"index": 1, "mute": False, "properties": {"application.name": "Chromium"}},
    {"index": 2, "mute": True, "properties": {"application.name": "Discord"}},
    {"index": 3, "mute": False, "properties": {"application.process.binary": "discord"}},
    {"index": 4, "mute": False, "properties": {"application.name": "OBS"}},
]


def fake_pactl(calls):
    def run(*args):
        calls.append(args)
        if args[:3] == ("-f", "json", "list"):
            return json.dumps(STREAMS)
        return ""
    return run


def test_mutes_matching_streams_and_restores_only_those(monkeypatch):
    calls = []
    monkeypatch.setattr(mute, "_pactl", fake_pactl(calls))
    m = mute.StreamMuter(["discord", "chromium"])
    m.mute()
    # Stream 2 was already muted by the user, so it is left alone.
    assert m.muted == ["1", "3"]
    calls.clear()
    m.restore()
    assert calls == [("set-source-output-mute", "1", "0"),
                     ("set-source-output-mute", "3", "0")]
    assert m.muted == []


def test_empty_list_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(mute, "_pactl", fake_pactl(calls))
    mute.StreamMuter([]).mute()
    assert calls == []


def test_a_hung_stream_leaves_the_others_ducked_and_restored(monkeypatch):
    # The apps of streams 1-3 stopped feeding them: pactl hangs on each until
    # its timeout. One after another, those hangs outlasted restore()'s wait,
    # and the duck went on turning streams down after they were put back.
    import threading
    import time

    playing = {1: [60000, 60000], 2: [40000], 3: [65536], 4: [65536, 65536], 5: [30000]}
    volumes = {i: list(v) for i, v in playing.items()}
    lock = threading.Lock()

    def run(*args):
        if args[:3] == ("-f", "json", "list"):
            with lock:
                return json.dumps([{"index": i, "properties": {"application.name": f"app{i}"},
                                    "volume": {str(c): {"value": v} for c, v in enumerate(vol)}}
                                   for i, vol in volumes.items()])
        _, idx, *values = args
        if idx in ("1", "2", "3"):
            time.sleep(0.3)
            raise TimeoutError("pactl timed out")
        with lock:
            volumes[int(idx)] = [int(v) for v in values]
        return ""

    monkeypatch.setattr(mute, "_pactl", run)
    monkeypatch.setattr(mute.PlaybackDucker, "_remember", classmethod(lambda cls, apps: None))
    d = mute.PlaybackDucker(0.3)
    t0 = time.monotonic()
    d.duck()
    d.restore()
    assert time.monotonic() - t0 < 0.6   # the hangs side by side (0.3 s), not one after another
    assert volumes == playing
