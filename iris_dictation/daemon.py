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
import queue
import signal
import subprocess
import sys
import threading
import time
import wave

import evdev
import numpy as np

from . import audio
from . import config as cfgmod
from . import output as out
from .audio import HotRecorder, Recorder
from .fix import Fixer
from .keys import KeyWatcher
from .mute import PlaybackDucker, StreamMuter
from .sockets import LEVELS_SOCKET, SOCKET_NAME, ControlServer, LevelServer
from .text import is_silence_phrase, tidy_short

log = logging.getLogger("iris-dictation")

NOTIFY_TAG = "iris-dictation-status"


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
        self.fixer = None
        if cfg.fix == "claude":
            self.fixer = Fixer(cfg.vocabulary, cfg.fix_model, cfg.fix_timeout, cfg.fix_command)
        elif cfg.fix:
            log.warning("unknown fix %r, typing results as heard", cfg.fix)
        # Results to type are fixed and typed one after another on a worker
        # thread, so the next press can record in the meantime. run() starts
        # it; without it (the tests) they are handled in place.
        self.lock = threading.Lock()
        self.jobs: queue.Queue | None = None
        self.pending = 0

    def notify(self, summary: str, body: str = "", timeout: int = 2000) -> None:
        if self.cfg.notify:
            notify(summary, body, timeout)

    def set_state(self, state: str) -> None:
        self.state = state
        self.levels.send(state)

    def settle(self) -> None:
        """Idle, unless a recording runs or results are still being typed."""
        with self.lock:
            if self.state != "recording" and not self.pending:
                self.set_state("idle")

    def show(self, line: str) -> None:
        """An overlay line about a result, held back while the next
        recording already shows its waveform."""
        if self.state != "recording":
            self.levels.send(line)

    def load_model(self) -> None:
        from . import model

        self.model = model.load(self.cfg.model_path, self.cfg.languages, self.cfg.vocabulary)

    def on_down(self, tag: str = "") -> None:
        with self.lock:
            if self.state == "recording":
                log.debug("ignoring key down while recording")
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
            with self.lock:
                self.state = "transcribing"
            self.settle()
            self.notify("Dictation error", str(exc))

    def on_up(self, box: queue.Queue | None = None) -> None:
        """Stop recording and type the text, or, for a caller waiting on the
        control socket (stop-return), put it in `box` instead."""
        text = ""
        if self.state != "recording":
            log.debug("ignoring key up while %s", self.state)
        else:
            if self.cfg.tail_ms > 0:
                time.sleep(self.cfg.tail_ms / 1000)
            with self.lock:
                self.set_state("transcribing")
            samples = self.recorder.stop()
            self.muter.restore()
            self.ducker.restore()
            seconds = len(samples) / self.cfg.sample_rate
            if seconds < self.cfg.min_seconds:
                log.info("too short (%.2fs), ignored", seconds)
                self.settle()
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
        with self.lock:
            self.set_state("transcribing")
        box.put(self.finish(audio.to_float(raw), rate, type_it=False))

    def finish(self, samples: np.ndarray, rate: int, type_it: bool) -> str:
        """Transcribe and clean up. Text to type goes on to type_result();
        otherwise it is returned ("" for nothing heard)."""
        seconds = len(samples) / rate
        t0 = time.time()
        try:
            text = self.recognize(samples, rate)
        except Exception as exc:
            log.exception("transcription failed")
            self.settle()
            self.notify("Dictation error", str(exc), timeout=4000)
            return ""
        peak = audio.loudest_rms(samples)
        if peak < self.cfg.silence_rms and is_silence_phrase(text):
            log.info("silence phrase %r (peak rms %.4f), ignored", text, peak)
            text = ""
        elapsed = time.time() - t0
        log.info("%.1fs audio (peak rms %.4f) -> %.2fs infer (rtf %.3f): %r",
                 seconds, peak, elapsed, elapsed / max(seconds, 0.01), text)
        if type_it and text:
            with self.lock:
                self.pending += 1
            if self.jobs is None:
                self.type_result(text)
            else:
                self.jobs.put(text)
            return text
        text = tidy_short(text, self.cfg.short_words)
        self.show(f"text {text}" if text else "nothing")
        self.settle()
        return text

    def type_result(self, text: str) -> None:
        """Fix, tidy and type one result, then settle."""
        try:
            if self.fixer:
                text = self.fixer.fix(text)
            text = tidy_short(text, self.cfg.short_words)
            self.show(f"text {text}" if text else "nothing")
            if text:
                self.deliver(text)
        except Exception as exc:
            log.exception("typing failed")
            self.notify("Dictation error", str(exc), timeout=4000)
        finally:
            with self.lock:
                self.pending -= 1
            self.settle()

    def type_results(self) -> None:
        while True:
            self.type_result(self.jobs.get())

    def recognize(self, samples: np.ndarray, rate: int) -> str:
        # A long recording goes in pieces of under 30 s, cut between words,
        # which keeps the prompt inside the model's fixed-size cache.
        texts = (self.model.recognize(piece, sample_rate=rate) or ""
                 for piece in audio.split_at_pauses(samples, rate))
        return " ".join(t.strip() for t in texts if t.strip())

    def deliver(self, text: str) -> None:
        payload = text + (" " if self.cfg.trailing_space else "")
        # Typing a long result takes seconds (wtype, ~220 chars/s); the overlay
        # shows a progress bar for it.
        self.show(f"typing {len(payload) if self.cfg.output == 'type' else 0}")
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

        self.jobs = queue.Queue()
        threading.Thread(target=self.type_results, daemon=True).start()
        if self.fixer:
            self.fixer.start()
            log.info("fix: %s, %d vocabulary terms", self.cfg.fix_model, len(self.cfg.vocabulary))

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
            if self.fixer:
                self.fixer.close()
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
