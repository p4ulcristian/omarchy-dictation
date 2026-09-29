"""Push-to-talk dictation daemon.

Holds the speech model resident so a key press costs nothing but the audio
and about a tenth of a second of inference per second of speech.

Flow: hold the key -> record -> release -> transcribe -> type into the
focused window.

Everything that happens (key, control socket) arrives as an event tuple
(kind, *args) on one queue, and the main loop handles them in order.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import sys
import time
import wave

import evdev
import numpy as np

from . import audio, languages
from . import config as cfgmod
from . import output as out
from .audio import HotRecorder, Recorder
from .keys import KeyWatcher
from .mute import PlaybackDucker, StreamMuter
from .sockets import LEVELS_SOCKET, SOCKET_NAME, ControlServer, LevelServer
from .text import is_silence_phrase, tidy_short

log = logging.getLogger("iris-dictation")

NOTIFY_TAG = "iris-dictation-status"
# Its files are in cfg.model_path; the name tells onnx-asr how to load them.
MODEL = "nemo-canary-1b-v2"


def notify(summary: str, body: str = "", timeout: int = 2000) -> None:
    """One notification slot that replaces itself, so nothing piles up."""
    try:
        subprocess.Popen(
            [
                "notify-send",
                "-a", "iris-dictation",
                "-t", str(timeout),
                "-h", f"string:x-canonical-private-synchronous:{NOTIFY_TAG}",
                "-h", f"string:x-dunst-stack-tag:{NOTIFY_TAG}",
                summary,
                body,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class Daemon:
    def __init__(self, cfg: cfgmod.Config) -> None:
        self.cfg = cfg
        self.events: queue.Queue = queue.Queue()
        self.state = "idle"
        if cfg.preroll_ms > 0:
            self.recorder = HotRecorder(cfg.sample_rate, cfg.max_seconds,
                                        cfg.audio_source, cfg.preroll_ms)
        else:
            self.recorder = Recorder(cfg.sample_rate, cfg.max_seconds,
                                     cfg.audio_source)
        self.muter = StreamMuter(cfg.mute_apps)
        self.ducker = PlaybackDucker(cfg.duck_playback)
        self.model = None
        self.levels = LevelServer(str(cfgmod.runtime_dir() / LEVELS_SOCKET))
        self.recorder.listener = lambda chunk: self.levels.send(f"level {audio.level(chunk):.3f}")

    def notify(self, summary: str, body: str = "", timeout: int = 2000) -> None:
        if self.cfg.notify:
            notify(summary, body, timeout)

    def set_state(self, state: str) -> None:
        self.state = state
        self.levels.send(state)

    def load_model(self) -> None:
        import onnx_asr
        import onnxruntime as ort

        t0 = time.time()
        # CUDA and cuDNN come from pip wheels, not the system. Load them
        # before the first session so onnxruntime finds them.
        ort.preload_dlls()
        self.model = onnx_asr.load_model(
            MODEL, path=os.path.expanduser(self.cfg.model_path),
            # Grow the memory pool only by what is asked for, not in doubling
            # steps: about 300 MB less VRAM held, same speed.
            providers=[("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"}),
                       "CPUExecutionProvider"])
        log.info("model loaded in %.2fs", time.time() - t0)
        langs = self.cfg.languages or ["en"]
        languages.install(self.model, langs)
        log.info("languages: %s", ", ".join(langs))

        # Warm up so the first real press does not pay for lazy allocation.
        t0 = time.time()
        self.model.recognize(np.zeros(self.cfg.sample_rate, dtype=np.float32),
                             sample_rate=self.cfg.sample_rate)
        log.info("warmup %.2fs", time.time() - t0)

    def on_down(self, tag: str = "") -> None:
        if self.state != "idle":
            log.debug("ignoring key down while %s", self.state)
            return
        self.state = "recording"
        # A tagged recording ("start iris") is shown by whoever asked for it.
        self.levels.send(f"recording {tag}" if tag else "recording")
        self.muter.mute()
        try:
            self.recorder.start()
            self.ducker.duck()
        except Exception as exc:
            log.exception("could not start recording")
            self.muter.restore()
            self.ducker.restore()
            self.set_state("idle")
            self.notify("Dictation error", str(exc))

    def on_up(self, box: queue.Queue | None = None) -> None:
        """Stop recording and type the text, or, for a caller waiting on the
        control socket (stop-return), put it in `box` instead."""
        text = ""
        if self.state != "recording":
            log.debug("ignoring key up while %s", self.state)
        else:
            self.set_state("transcribing")
            samples = self.recorder.stop()
            self.muter.restore()
            self.ducker.restore()
            seconds = len(samples) / self.cfg.sample_rate
            if seconds < self.cfg.min_seconds:
                log.info("too short (%.2fs), ignored", seconds)
                self.set_state("idle")
            else:
                text = self.finish(samples, self.cfg.sample_rate, type_it=box is None)
        if box:
            box.put(text)

    def on_file(self, path: str, box: queue.Queue) -> None:
        """Transcribe a wav from disk and reply with the text."""
        try:
            with wave.open(path) as w:
                rate = w.getframerate()
                raw = w.readframes(w.getnframes())
        except (OSError, wave.Error) as exc:
            log.warning("cannot read %s: %s", path, exc)
            box.put("")
            return
        self.set_state("transcribing")
        box.put(self.finish(audio.to_float(raw), rate, type_it=False))

    def finish(self, samples: np.ndarray, rate: int, type_it: bool) -> str:
        """Transcribe, clean up, optionally type. Returns the text ("" for
        nothing heard) and always leaves the daemon idle."""
        seconds = len(samples) / rate
        t0 = time.time()
        try:
            text = self.recognize(samples, rate)
        except Exception as exc:
            log.exception("transcription failed")
            self.set_state("idle")
            self.notify("Dictation error", str(exc), timeout=4000)
            return ""
        peak = audio.loudest_rms(samples)
        if peak < self.cfg.silence_rms and is_silence_phrase(text):
            log.info("silence phrase %r (peak rms %.4f), ignored", text, peak)
            text = ""
        elapsed = time.time() - t0
        log.info("%.1fs audio (peak rms %.4f) -> %.2fs infer (rtf %.3f): %r",
                 seconds, peak, elapsed, elapsed / max(seconds, 0.01), text)
        text = tidy_short(text, self.cfg.short_words)
        if not text:
            self.levels.send("nothing")
            self.set_state("idle")
            return ""
        self.levels.send(f"text {text}")
        if type_it:
            self.deliver(text)
        self.set_state("idle")
        return text

    def recognize(self, samples: np.ndarray, rate: int) -> str:
        # Canary is trained on clips up to 40 s; a longer recording goes in pieces.
        texts = (self.model.recognize(piece, sample_rate=rate) or ""
                 for piece in audio.split_at_pauses(samples, rate))
        return " ".join(t.strip() for t in texts if t.strip())

    def deliver(self, text: str) -> None:
        payload = text + (" " if self.cfg.trailing_space else "")
        # Typing a long result takes seconds (wtype, ~220 chars/s); the overlay
        # shows a progress bar for it.
        self.levels.send(f"typing {len(payload) if self.cfg.output == 'type' else 0}")
        method = out.deliver(payload, self.cfg.output, self.cfg.output_fallback)
        if method == "clipboard" and self.cfg.output != "clipboard":
            self.notify("Dictation: on clipboard", "Could not type it, press ctrl+v", timeout=4000)

    def run(self) -> int:
        self.load_model()

        keycode = getattr(evdev.ecodes, self.cfg.key, None)
        if keycode is None:
            log.error("unknown key %s", self.cfg.key)
            return 2
        watcher = KeyWatcher(keycode, self.cfg.devices, self.events)
        if watcher.prepare() == 0:
            # Not fatal: the watcher keeps looking, so a keyboard plugged in
            # later still works, and start/stop over the socket work anyway.
            log.error("no readable keyboard exposes %s yet; is this user in the "
                      "input group or granted uaccess on /dev/input?",
                      self.cfg.key)
        watcher.start()
        control = ControlServer(str(cfgmod.runtime_dir() / SOCKET_NAME),
                                self.events, lambda: self.state)
        control.start()
        self.levels.start()

        if isinstance(self.recorder, HotRecorder):
            self.recorder.open()
            log.info("microphone held open for %d ms of pre-roll",
                     self.cfg.preroll_ms)

        self.set_state("idle")
        log.info("ready, hold %s to dictate", self.cfg.key)

        handlers = {"down": self.on_down, "up": self.on_up, "file": self.on_file}
        try:
            while True:
                kind, *args = self.events.get()
                if kind == "quit":
                    break
                handlers[kind](*args)
        except KeyboardInterrupt:
            pass
        finally:
            self.muter.restore()
            self.ducker.restore()
            watcher.stop()
            control.stop()
            if isinstance(self.recorder, HotRecorder):
                self.recorder.close()
        return 0


def _raise_interrupt(*_) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in argv else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # systemctl stop/restart sends SIGTERM. Turn it into the same clean exit
    # as ctrl+c so a stream muted mid-dictation gets unmuted.
    signal.signal(signal.SIGTERM, _raise_interrupt)
    return Daemon(cfgmod.load()).run()


if __name__ == "__main__":
    raise SystemExit(main())
