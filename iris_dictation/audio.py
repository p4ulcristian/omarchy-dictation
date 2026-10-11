"""Microphone capture via parec (PipeWire's PulseAudio interface), and the
loudness measures taken from it."""

from __future__ import annotations

import json
import logging
import select
import subprocess
import threading
import time
from collections.abc import Callable

import numpy as np

log = logging.getLogger("iris-dictation")

CHUNK = 4096
# A microphone that is switched off, or a phone that is not streaming,
# delivers exact zeros. Anything above this (one 16-bit step) picked up sound.
DEAD_RMS = 3e-5
# A stream that lost its microphone goes quiet without ending. After this
# many seconds with nothing, HotRecorder opens it again.
STALL_SECONDS = 3
# Called with each raw s16le chunk while a recording is running (about every
# 20 ms, as parec delivers them), for the live level meter.
Listener = Callable[[bytes], None]


_sources: tuple[float, dict[str, str]] = (float("-inf"), {})


def sources() -> dict[str, str]:
    """PipeWire's sources right now, name -> description. Asked at most once
    a second."""
    global _sources
    at, known = _sources
    if time.monotonic() - at > 1:
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sources"],
                                 capture_output=True, text=True, timeout=2).stdout
            known = {s["name"]: s.get("description") or s["name"] for s in json.loads(out)}
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            known = {}
        _sources = (time.monotonic(), known)
    return known


def _parec(sample_rate: int, source: str, exact: bool = False) -> subprocess.Popen:
    cmd = [
        "parec",
        "--format=s16le",
        f"--rate={sample_rate}",
        "--channels=1",
        "--latency-msec=20",
        "--client-name=iris-dictation",
    ]
    if source:
        cmd.append(f"--device={source}")
    if exact:
        # Without this PipeWire moves the stream to the default microphone
        # when its own goes away.
        cmd.append("--property=node.dont-reconnect=true")
    # Unbuffered: a read returns what parec has, not a full CHUNK.
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)


def _end(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()


def to_float(raw: bytes) -> np.ndarray:
    """Raw s16le bytes as float32 samples in [-1, 1]."""
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def level(chunk: bytes) -> float:
    """Loudness of one raw chunk as 0..1 on a dB scale: -54 dB (the silence
    on a typical headset) is 0, -40 dB (normal talking on a quiet mic) is 1."""
    x = to_float(chunk)
    if len(x) == 0:
        return 0.0
    db = 20 * np.log10(float(np.sqrt(np.mean(x * x))) + 1e-9)
    return min(1.0, max(0.0, (db + 54) / 14))


def rms(chunk: bytes) -> float:
    x = to_float(chunk)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def split_at_pauses(samples: np.ndarray, rate: int, max_seconds: float = 28.0,
                    search_seconds: float = 6.0, window: int = 480) -> list[np.ndarray]:
    """Cut a recording into pieces of at most `max_seconds`, each cut at the
    quietest 30 ms in the last `search_seconds` before the limit, so it falls
    between words."""
    pieces, start, limit = [], 0, int(max_seconds * rate)
    while len(samples) - start > limit:
        hi = start + limit
        lo = hi - int(search_seconds * rate)
        frames = samples[lo:hi][: (hi - lo) // window * window].reshape(-1, window)
        cut = lo + int(np.argmin((frames ** 2).mean(axis=1))) * window + window // 2
        pieces.append(samples[start:cut])
        start = cut
    pieces.append(samples[start:])
    return pieces


def loudest_rms(samples: np.ndarray, window: int = 480) -> float:
    """RMS of the loudest 30 ms (at 16 kHz) in the clip."""
    n = len(samples) // window * window
    if n == 0:
        return 0.0
    frames = samples[:n].reshape(-1, window)
    return float(np.sqrt((frames ** 2).mean(axis=1)).max())


class Recorder:
    """Records raw s16le mono into memory until stop() is called.

    parec is spawned on start and killed on stop, so the microphone is only
    open while the key is held. Spawn cost is a few tens of milliseconds,
    which lands well inside the gap between pressing a key and speaking.

    `exact` records from `source` or not at all: without it PipeWire falls
    back to the default microphone when the source is missing.
    """

    def __init__(self, sample_rate: int, max_seconds: int, source: str = "",
                 exact: bool = False) -> None:
        self.sample_rate = sample_rate
        self.source = source
        self.exact = exact
        self.max_bytes = sample_rate * 2 * max_seconds
        self._proc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self._thread: threading.Thread | None = None
        self.listener: Listener | None = None

    def start(self) -> None:
        self._chunks = []
        if self.exact and self.source and self.source not in sources():
            return
        self._proc = _parec(self.sample_rate, self.source, self.exact)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        total = 0
        while True:
            data = proc.stdout.read(CHUNK)
            if not data:
                break
            self._chunks.append(data)
            if self.listener:
                self.listener(data)
            total += len(data)
            if total >= self.max_bytes:
                break

    def stop(self) -> np.ndarray:
        """Stop capture and return float32 samples in [-1, 1]."""
        proc, self._proc = self._proc, None
        _end(proc)
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        raw = b"".join(self._chunks)
        self._chunks = []
        return to_float(raw)


class HotRecorder:
    """Keeps parec running and holds a rolling pre-roll buffer.

    Spawning parec on each key press costs about 155 ms before the first
    sample arrives, which clips the start of a sentence if you speak the
    instant you press. This variant keeps the stream open, so a press starts
    capturing immediately and also keeps the last preroll_ms of audio that
    was already in flight.

    The cost is that the microphone is open the whole time the daemon runs,
    so it is off by default.

    `exact` records from `source` or not at all, as in Recorder, and waits
    for a source that is missing or went away to come back.
    """

    def __init__(self, sample_rate: int, max_seconds: int, source: str = "",
                 preroll_ms: int = 300, exact: bool = False) -> None:
        self.sample_rate = sample_rate
        self.max_bytes = sample_rate * 2 * max_seconds
        self.source = source
        self.exact = exact
        self.preroll_bytes = int(sample_rate * 2 * preroll_ms / 1000)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._ring = bytearray()
        self._capture: bytearray | None = None
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self.listener: Listener | None = None

    def open(self) -> None:
        """Start the persistent capture stream. Call once at daemon start."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_flag.set()
        _end(self._proc)

    def _pump(self) -> None:
        while not self._stop_flag.is_set():
            if self.exact and self.source and self.source not in sources():
                self._stop_flag.wait(STALL_SECONDS)
                continue
            proc = self._proc = _parec(self.sample_rate, self.source, self.exact)
            self._read(proc)
            # parec died or went quiet, e.g. the device was unplugged. Reopen.
            _end(proc)
            self._stop_flag.wait(0.5)

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        while not self._stop_flag.is_set():
            if self.exact and not select.select([proc.stdout], [], [], STALL_SECONDS)[0]:
                return
            data = proc.stdout.read(CHUNK)
            if not data:
                return
            with self._lock:
                if self._capture is not None:
                    if len(self._capture) < self.max_bytes:
                        self._capture += data
                    if self.listener:
                        self.listener(data)
                else:
                    self._ring += data
                    if len(self._ring) > self.preroll_bytes:
                        del self._ring[: len(self._ring) - self.preroll_bytes]

    def start(self) -> None:
        self.open()
        with self._lock:
            self._capture = bytearray(self._ring)
            self._ring = bytearray()

    def stop(self) -> np.ndarray:
        with self._lock:
            raw = bytes(self._capture or b"")
            self._capture = None
            self._ring = bytearray()
        return to_float(raw)


class Mics:
    """Several microphones in order of preference, as one recorder.

    All of them record at once, and the first in the list that picked up
    any sound is the one kept. So a headset that is switched off, or a phone
    that is not streaming, is passed over without anyone choosing.
    """

    def __init__(self, recorders: list[Recorder] | list[HotRecorder]) -> None:
        self.recorders = recorders
        self.listener: Listener | None = None
        # Called with the source name when the microphone in use changes
        # during a recording.
        self.on_pick: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        self._best: int | None = None
        for i, recorder in enumerate(recorders):
            recorder.listener = lambda chunk, i=i: self._heard(i, chunk)

    def _heard(self, i: int, chunk: bytes) -> None:
        with self._lock:
            picked = (self._best is None or i < self._best) and rms(chunk) > DEAD_RMS
            if picked:
                self._best = i
            best = self._best
        if picked and self.on_pick:
            self.on_pick(self.recorders[i].source)
        if i == best and self.listener:
            self.listener(chunk)

    def open(self) -> None:
        for recorder in self.recorders:
            recorder.open()

    def close(self) -> None:
        for recorder in self.recorders:
            recorder.close()

    def start(self) -> None:
        with self._lock:
            self._best = None
        for recorder in self.recorders:
            recorder.start()

    def stop(self) -> np.ndarray:
        clips = [recorder.stop() for recorder in self.recorders]
        with self._lock:
            best = self._best
        if best is None:
            log.info("no microphone picked up any sound")
            return np.zeros(0, dtype=np.float32)
        return clips[best]
