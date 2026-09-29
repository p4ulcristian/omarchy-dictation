"""Mute other apps' microphone streams while dictating, and turn their playback
down (PlaybackDucker, below).

So the people in a Discord call do not hear what you dictate. This mutes the
app's capture stream in PipeWire, not the microphone itself, so iris-dictation still
records. The app's own mute icon does not change; it just receives silence.

Only streams this module muted are unmuted again, so a stream you had muted
yourself stays muted.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("iris-dictation")


def _pactl(*args: str) -> str:
    return subprocess.run(
        ["pactl", *args], capture_output=True, text=True, timeout=2, check=True
    ).stdout


class StreamMuter:
    def __init__(self, apps: list[str]) -> None:
        self.apps = [a.lower() for a in apps]
        self.muted: list[str] = []

    def _matches(self, props: dict) -> bool:
        names = (
            props.get("application.name", ""),
            props.get("application.process.binary", ""),
        )
        return any(app in name.lower() for app in self.apps for name in names)

    def mute(self) -> None:
        if not self.apps:
            return
        try:
            streams = json.loads(_pactl("-f", "json", "list", "source-outputs"))
        except Exception as exc:
            log.warning("could not list capture streams: %s", exc)
            return
        for s in streams:
            if s.get("mute") or not self._matches(s.get("properties", {})):
                continue
            idx = str(s["index"])
            try:
                _pactl("set-source-output-mute", idx, "1")
            except Exception as exc:
                log.warning("could not mute stream %s: %s", idx, exc)
                continue
            self.muted.append(idx)
            log.info("muted %s (stream %s)",
                     s["properties"].get("application.name"), idx)

    def restore(self) -> None:
        for idx in self.muted:
            try:
                _pactl("set-source-output-mute", idx, "0")
            except Exception:
                # The stream ended while muted (left the call). Nothing to undo.
                pass
        self.muted = []


class PlaybackDucker:
    """Turn other apps' playback down while you dictate, so the film or video
    doesn't talk over you, and back to exactly where it was when you let go.

    `level` is the share of its volume a stream keeps (0.3 = 30%); 1 turns
    this off. Ducking runs on a thread of its own so the recording starts at
    once; restore() waits for it first, however long it takes: a duck that
    finished after restore() would leave those streams down.

    WirePlumber remembers each app's volume by its name, the ducked one too:
    a stream the app opens while ducked (mpv starting the next file, the next
    sentence of a voice) starts at the ducked volume. So restore() also puts
    back any stream of a ducked app that is at the ducked volume, not only the
    streams it turned down. And an app with no stream left when you let go
    (the video closed) would open its next one at the ducked volume, so its
    remembered volume is set back through a short silent stream in its name.
    """

    def __init__(self, level: float) -> None:
        self.level = max(0.0, min(1.0, level))
        self.saved: dict[str, list[int]] = {}   # stream -> its channels' volumes before
        self.apps: dict[str, tuple[list[int], list[int]]] = {}   # app -> (volumes before, ducked)
        self.keys: dict[str, dict[str, str]] = {}   # app -> what WirePlumber knows it by
        self.thread: threading.Thread | None = None

    @staticmethod
    def _app(stream: dict) -> str:
        props = stream.get("properties", {})
        return props.get("application.name") or props.get("application.process.binary") or ""

    @staticmethod
    def _volumes(stream: dict) -> list[int]:
        return [c["value"] for c in stream.get("volume", {}).values()]

    @staticmethod
    def _streams() -> list[dict]:
        return json.loads(_pactl("-f", "json", "list", "sink-inputs"))

    def duck(self) -> None:
        if self.level >= 1 or self.thread:
            return
        self.thread = threading.Thread(target=self._duck, daemon=True)
        self.thread.start()

    def _duck(self) -> None:
        try:
            streams = self._streams()
        except Exception as exc:
            log.warning("could not list playback streams: %s", exc)
            return
        found = {str(s["index"]): (s, self._volumes(s)) for s in streams}
        found = {idx: (s, values) for idx, (s, values) in found.items() if values}
        ducked = _set_volumes({idx: [round(v * self.level) for v in values]
                               for idx, (_, values) in found.items()}, "duck")
        for idx, target in ducked.items():
            s, values = found[idx]
            self.saved[idx] = values
            if app := self._app(s):
                self.apps[app] = (values, target)
                props = s.get("properties", {})
                self.keys[app] = {k: props[k] for k in WP_KEYS if props.get(k)}

    def restore(self) -> None:
        if self.thread:
            self.thread.join()   # bounded: each pactl call in it has a timeout
            self.thread = None
        if not self.saved:
            return
        try:
            streams = self._streams()
        except Exception as exc:
            log.warning("could not list playback streams: %s", exc)
            streams = [{"index": idx} for idx in self.saved]   # the ones we know, at least
        targets, apps = {}, {}
        for s in streams:
            idx = str(s["index"])
            app = self._app(s)
            values = self.saved.get(idx)
            if values is None:
                # A stream opened while ducked: WirePlumber gave it the ducked volume.
                before, ducked = self.apps.get(app, (None, None))
                if before is None or not _near(self._volumes(s), ducked):
                    continue
                values = before
            targets[idx] = values
            apps[idx] = app
        restored = {apps[idx] for idx in _set_volumes(targets, "restore")}
        gone = {app: (before, self.keys.get(app, {})) for app, (before, _) in self.apps.items()
                if app not in restored}
        if gone:
            threading.Thread(target=self._remember, args=(gone,), daemon=True).start()
        self.saved = {}
        self.apps = {}
        self.keys = {}

    @classmethod
    def _remember(cls, apps: dict[str, tuple[list[int], dict[str, str]]]) -> None:
        """Set WirePlumber's remembered volume of apps that have no stream now,
        through a silent stream that WirePlumber takes for the app."""
        for app, (values, keys) in apps.items():
            name = f"iris-dictation stand-in {os.getpid()}"
            props = {**keys, "media.name": name}
            player = subprocess.Popen(
                ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", "48000",
                 "--channels", str(len(values)),
                 "-P", "{ %s }" % " ".join(f"{k} = {json.dumps(v)}" for k, v in props.items()),
                 "/dev/zero"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(20):
                    ours = [s for s in cls._streams()
                            if s.get("properties", {}).get("media.name") == name]
                    if ours:
                        _pactl("set-sink-input-volume", str(ours[0]["index"]), *map(str, values))
                        time.sleep(0.2)   # WirePlumber saves it
                        log.info("%s had no stream left: its volume is set back for the next one", app)
                        break
                    time.sleep(0.1)
                else:
                    log.warning("could not set back %s's volume: its stand-in stream didn't show up", app)
            except Exception as exc:
                log.warning("could not set back %s's volume: %s", app, exc)
            finally:
                player.kill()
                player.wait()


def _set_volumes(targets: dict[str, list[int]], verb: str) -> dict[str, list[int]]:
    """Set these streams' volumes all at once; the ones that were set.

    pactl waits for a stream to take a new volume, and one whose app stopped
    feeding it without closing it never does: that call hangs for its whole
    timeout. One at a time, a few of those held the rest back for seconds.
    """
    def one(idx: str) -> bool:
        try:
            _pactl("set-sink-input-volume", idx, *map(str, targets[idx]))
            return True
        except Exception as exc:
            log.warning("could not %s stream %s: %s", verb, idx, exc)
            return False
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return {idx: targets[idx] for idx, ok in zip(targets, pool.map(one, targets)) if ok}


# What WirePlumber remembers a stream's volume by (the first one it has).
WP_KEYS = ("application.id", "application.name", "media.role")


def _near(volumes: list[int], target: list[int]) -> bool:
    """The same volume, give or take the rounding of a trip through WirePlumber (1%)."""
    return len(volumes) == len(target) and all(abs(a - b) <= 655 for a, b in zip(volumes, target))
