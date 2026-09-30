"""Microphone capture via parec (PipeWire's PulseAudio interface), and the
loudness measures taken from it."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable

import numpy as np

CHUNK = 4096
# Called with each raw s16le chunk while a recording is running (about every
# 20 ms, as parec delivers them), for the live level meter.
Listener = Callable[[bytes], None]


def _parec(sample_rate: int, source: str) -> subprocess.Popen:
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
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


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
    """

    def __init__(self, sample_rate: int, max_seconds: int, source: str = "") -> None:
        self.sample_rate = sample_rate
        self.source = source
        self.max_bytes = sample_rate * 2 * max_seconds
        self._proc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self._thread: threading.Thread | None = None
        self.listener: Listener | None = None

    def start(self) -> None:
        self._chunks = []
        self._proc = _parec(self.sample_rate, self.source)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        total = 0
        while True:
            data = proc.stdout.read1(CHUNK)   # whatever parec has, not a full CHUNK
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
    """

    def __init__(self, sample_rate: int, max_seconds: int, source: str = "",
                 preroll_ms: int = 300) -> None:
        self.sample_rate = sample_rate
        self.max_bytes = sample_rate * 2 * max_seconds
        self.source = source
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
        if self._proc is not None:
            return
        self._proc = _parec(self.sample_rate, self.source)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_flag.set()
        proc, self._proc = self._proc, None
        _end(proc)

    def _pump(self) -> None:
        while not self._stop_flag.is_set():
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            data = proc.stdout.read1(CHUNK)
            if not data:
                # parec died, e.g. the device was unplugged. Reopen.
                if self._stop_flag.is_set():
                    return
                time.sleep(0.5)
                self._proc = None
                self.open()
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
