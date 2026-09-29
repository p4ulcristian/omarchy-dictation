# Changelog

## 3.1.0

Added:
- `start <tag>` on the control socket: a recording that another program shows
  itself. The levels socket says `recording <tag>`, and the waveform pill stays
  hidden until `idle`. Iris's desk card uses it to listen inside its own card.
- `duck_playback`: other apps' playback goes down to this share of its volume
  while you hold the key (0.3 = 30%), and back to where it was when you let
  go. Off by default (1).
  Streams an app opens while ducked, and apps with no stream left when you
  let go, come back too: WirePlumber would otherwise keep the ducked volume
  as the app's own.

## 3.0.0

The model is now NVIDIA Canary-1B-v2 instead of Whisper large-v3: better on
Hungarian (FLEURS WER 12.9% against 14.1%), about 0.1-0.3 s per clip, and
4.9 GB of VRAM instead of 7.4 GB.

Changed:
- Whisper picked one language up front and, when it picked wrong, typed a
  translation instead of what you said. Canary writes each clip out in every
  language in `languages` and keeps the version it is most confident in; a
  translation scores far lower than the real transcript.
- `languages = []` now means English only (Canary cannot detect the
  language). Canary knows 25 European languages; the list is in
  `iris_dictation/config.py`.

To update: `git pull`, run `./install.sh` again (it downloads the new model,
3.7 GB), and delete `~/.local/share/iris-dictation/models/whisper-large-v3`.

## 2.3.0

Fixed:
- Recordings longer than 30 seconds lost everything after 30 s (Whisper
  hears 30 s at a time). Long recordings are now cut at pauses into pieces
  of at most 28 s and transcribed piece by piece.

## 2.2.0

Removed:
- The vocabulary (`~/.config/iris-dictation/vocabulary.toml`), the
  `iris-dictation fix` command and `iris-dictation-learn`. Whisper
  large-v3's own transcript is typed as it is. Delete the vocabulary file
  and `~/.local/bin/iris-dictation-learn` if you have them.

## 2.1.0

Removed:
- The second pass by a local language model (`fix_model`, `fix_url`,
  `fix_timeout`): with Whisper large-v3 it had almost nothing left to fix.
  Remove those settings from your config.toml.
- The `words` list in the vocabulary; only `[heard]` is used.

The `[heard]` vocabulary, `iris-dictation fix` and `iris-dictation-learn`
stay.

## 2.0.0

One model: Whisper large-v3 (full, fp16) on an NVIDIA GPU. About 0.2-0.4 s
per clip and ~5 GB of VRAM; more accurate than large-v3-turbo, especially on
accents.

Removed:
- The other models and modes: Whisper large-v3-turbo, Parakeet, the CPU
  install (`install.sh --cpu`), the `model`, `quantization`, `device` and
  `threads` settings, and the `--cpu`/`--cuda`/`--fp32` daemon flags. Remove
  those settings from your config.toml; unknown settings are logged and
  ignored.

To update: `git pull`, run `./install.sh` again (it downloads the new model,
3.1 GB), and delete `~/.local/share/iris-dictation/models/whisper-large-v3-turbo`
and `.../parakeet-tdt-0.6b-v3` if you have them.

## 1.4.0

Added:
- `iris-dictation-learn`: an AI agent reviews your recent dictations and
  proposes vocabulary entries, you accept each with y/n, and the accepted
  ones are checked against the dictations they came from.
- `iris-dictation fix <text>`: what the second pass makes of a text.

Changed:
- `[heard]` vocabulary entries always apply, with or without `fix_model`,
  so a taught fix is certain.
- The second pass judges each of the model's changes on its own and keeps
  the allowed ones, instead of rejecting the whole answer for one bad change.
- Vocabulary words brought in by the model keep their listed spelling.
- Changes to case or punctuation alone are ignored; Whisper's text is kept.

## 1.3.0

Added:
- An optional second pass by a small local language model
  (`fix_model`): fixes words Whisper misheard from your private vocabulary
  (`~/.config/iris-dictation/vocabulary.toml`), drops filler sounds, and
  applies self-corrections. A guardrail rejects any other change. See the
  README.

Removed:
- Format mode from 1.2.0 (`format_key`, `start-format`, `format <text>`, the
  `format` overlay event). It was too unreliable with a small model; the
  second pass replaces it.

## 1.2.0

Added:
- Format mode (removed again in 1.3.0).

## 1.1.0

To update: `git pull`, run `./install.sh` again (with `--cpu` if you
installed that way), and on Omarchy restart the shell (`omarchy-restart-shell`)
so it loads the new overlay.

Changed:
- The overlay keeps one look from start to finish: a still dot stays on screen
  while transcribing and next to the result, and while waiting the waves
  ease from following your voice into a slow breath instead of switching to
  a different full-width animation.
- `iris-dictation transcribe <file>` replies with the text instead of typing
  it into the focused window. `iris-dictation-selftest` prints its results
  the same way.
- `--cpu` installs the CPU build of onnxruntime: about 150 MB instead of
  2.4 GB of CUDA libraries. The GPU build is now pinned.
- Removed `$XDG_RUNTIME_DIR/iris-dictation/state`, which nothing in
  iris-dictation read. Use `iris-dictation status`, or follow the state live
  on `levels.sock` (see PROTOCOL.md).

Fixed:
- The overlay stopped appearing for good after iris-dictation restarted: it
  retried the connection once, before the model had loaded. It now retries
  every 2 seconds until the daemon answers.
- `transcribe` with a missing or broken file crashed the daemon.
- `transcribe` with a relative path looked in the daemon's directory.
- `notify = false` silenced only one of the three notifications.
- The daemon refused to start when no keyboard was plugged in; it now waits
  for one.
- Unknown settings in `config.toml` (typos) are logged instead of silently
  ignored.

Added:
- PROTOCOL.md, documenting both sockets.
- Tests (`.venv/bin/python -m pytest`).

## 1.0.0

First release.
