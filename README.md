# omarchy-dictation

Push-to-talk dictation for Hyprland. Hold Caps Lock, speak, let go, and the
text is typed into whatever window is focused. Everything runs locally: no
cloud, no account.

The program itself is called **iris-dictation**: `iris-dictation`, `iris-dictation.service`.

- **Fast.** NVIDIA Canary-1B-v2 on the GPU transcribes a short phrase in
  about 0.1 s and a long sentence in about 0.3 s, measured on an RTX 5060 Ti. The
  model stays loaded, so a key press never waits for it.
- **Your languages, never translated.** Canary knows 25 European languages.
  Tell it which ones you speak (`languages = ["hu", "en"]`): every clip is
  written out in each of them and the version the model is surest of wins,
  so switching language between presses just works.
- **Launcher-friendly.** Results of up to three words lose their trailing
  full stop and start lowercase, so "Firefox." arrives as `firefox`.
- **Quiet on calls.** While you hold the key, Discord's microphone stream is
  muted, so your call doesn't hear what you dictate.
- **Waveform overlay** on Omarchy: a live voice meter while you talk, then
  the text it heard.
- **Scriptable.** `iris-dictation start`/`stop` from anything, plus
  `stop-return`, which hands the transcript back instead of typing it.
  [omarchy-controller](https://github.com/p4ulcristian/omarchy-controller)
  uses these for hold-R1-to-dictate on a DualSense.

## Requirements

- Hyprland (Omarchy for the waveform overlay), PipeWire.
- An NVIDIA GPU with about 5 GB of free VRAM (6.5 GB at peak).
- `uv`, `wtype`, `wl-clipboard`, `libpulse` (for `parec`/`pactl`),
  `libnotify`:

```sh
sudo pacman -S uv wtype wl-clipboard libpulse libnotify
```

## Install

```sh
git clone https://github.com/p4ulcristian/omarchy-dictation ~/.local/share/omarchy-dictation
~/.local/share/omarchy-dictation/install.sh
```

The installer creates a Python environment inside the clone, downloads the
model (about 3.7 GB) to `~/.local/share/iris-dictation/models/`, links
`iris-dictation` and friends into `~/.local/bin`, adds the waveform overlay to
the Omarchy shell if there is one, and starts the `iris-dictation` systemd user
service.

Then free up Caps Lock (next section) and try it.

## Freeing Caps Lock

iris-dictation reads Caps Lock straight from the keyboard (evdev), so it works
whatever the key is mapped to. But if Caps Lock still toggles caps, or is
your Compose key (Omarchy's default), it will also do that every time you
dictate, and a half-typed Compose sequence swallows the first letters.

Map it to nothing in your Hyprland input config, and move Compose if you use
it:

```lua
-- ~/.config/hypr/input.lua (Omarchy)
hl.config({
  input = {
    kb_options = "caps:none,compose:menu,shift:both_capslock_cancel",
  },
})
```

Nothing but iris-dictation sees Caps Lock after that, games included. Prefer another
key? Set `key = "KEY_RIGHTALT"` (any evdev `KEY_*` name) in the config
instead and leave Caps Lock alone.

If the key does nothing at all, the daemon probably can't read
`/dev/input`: the log says so. Add yourself to the `input` group and log in
again.

## Use it

Hold the key, speak, release. From scripts or other tools:

```sh
iris-dictation start          # begin recording
iris-dictation stop           # stop, transcribe, type
iris-dictation stop-return    # stop, transcribe, print the text instead of typing
iris-dictation toggle
iris-dictation status         # idle | recording | transcribing
iris-dictation transcribe some.wav   # print the text, type nothing
```

```sh
systemctl --user restart iris-dictation
journalctl --user -u iris-dictation -f
iris-dictation-daemon --debug    # run in a terminal instead (stop the service first)
```

## Config

Optional: `~/.config/iris-dictation/config.toml`. Every setting and its default is
documented in [`iris_dictation/config.py`](iris_dictation/config.py). The common ones:

```toml
languages = ["hu", "en"]    # the languages you speak; [] = English
key = "KEY_CAPSLOCK"        # any evdev KEY_* name
output = "type"             # or "paste" (clipboard + paste shortcut), "clipboard"
mute_apps = ["discord", "vesktop", "webcord"]   # add "chromium" for Discord in a browser
duck_playback = 0.3         # other apps' sound at 30% while you talk; 1 = leave it
trailing_space = true
preroll_ms = 0              # >0 keeps the mic open to catch the first syllable
audio_source = ""           # a PipeWire source name; "" = default mic
```

**Noise-suppressed mic?** If your default input is a filtered virtual mic
(EasyEffects, RNNoise, PipeWire echo-cancel and the like), point
`audio_source` at the raw microphone instead. The filters are tuned for human
listeners and cut parts of your words out, while the model copes with
background noise, even a TV, on its own. Your calls keep the filtered mic.
`pactl list sources short` lists the names; the raw one usually starts with
`alsa_input.`.

## How it works

A resident daemon owns the microphone, the key and the model:

1. **Key down:** `parec` starts recording from PipeWire, and apps in
   `mute_apps` get their capture stream muted. The mic itself stays on for
   iris-dictation.
2. **Key up:** the clip goes through Canary (onnxruntime, CUDA). Canary has
   no language detection and translates when told the wrong language, so the
   audio is encoded once and decoded once per entry in `languages`; the
   decode with the highest mean token log-probability is kept
   (`iris_dictation/languages.py`). This reaches into onnx-asr internals, so
   the version is pinned in `requirements.txt`.
3. The text is cleaned up: a hallucinated "Thank you." on silent
   clips is dropped, and short results are tidied. Then `wtype` types it into
   the focused window. If typing fails, it goes on the clipboard with a
   notification, so a transcription is never lost.

The waveform overlay listens on `$XDG_RUNTIME_DIR/iris-dictation/levels.sock`, one
line per event: `recording`, `level 0.42`, `transcribing`, `text …`, `idle`.
Both sockets are documented in [PROTOCOL.md](PROTOCOL.md), for building your
own tools on them.

`iris-dictation-selftest` runs the clips in `testwav/` through the running daemon
and prints what it heard; nothing is typed.

## Development

```sh
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest
```

`tests/test_live.py` also runs the sample clips through the running daemon
(skipped when it isn't running). Nothing is typed, but the overlay shows each
result.

## Uninstall

```sh
systemctl --user disable --now iris-dictation
rm ~/.config/systemd/user/iris-dictation.service ~/.local/bin/iris-dictation-daemon \
   ~/.local/bin/iris-dictation ~/.local/bin/iris-dictation-selftest
rm ~/.config/omarchy/plugins/p4ulcristian.iris-dictation   # Omarchy only
rm -rf ~/.local/share/omarchy-dictation ~/.local/share/iris-dictation ~/.config/iris-dictation   # clone, models, config
```

On Omarchy, also remove `p4ulcristian.iris-dictation` from the `plugins` list
in `~/.config/omarchy/shell.json`.

## License

MIT
