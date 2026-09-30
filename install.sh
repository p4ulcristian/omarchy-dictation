#!/usr/bin/env bash
# Install iris-dictation for the current user. Safe to run again.
# Needs an NVIDIA GPU with about 5 GB of free VRAM.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
SRC=$PWD
PLUGIN=p4ulcristian.iris-dictation

missing=()
for tool in uv wtype wl-copy parec pactl notify-send; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing: ${missing[*]}" >&2
  echo "On Arch: sudo pacman -S uv wtype wl-clipboard libpulse libnotify" >&2
  exit 1
fi

echo "Setting up the Python environment ..."
[ -d .venv ] || uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python -r requirements.txt
./bin/iris-dictation-fetch-model

mkdir -p ~/.local/bin ~/.config/systemd/user
for b in iris-dictation-daemon iris-dictation iris-dictation-selftest; do
  ln -sfn "$SRC/bin/$b" ~/.local/bin/$b
done

# The waveform overlay, if this is Omarchy.
SHELL_JSON=~/.config/omarchy/shell.json
if [ -f "$SHELL_JSON" ]; then
  mkdir -p ~/.config/omarchy/plugins
  ln -sfn "$SRC/overlay" ~/.config/omarchy/plugins/$PLUGIN
  python3 - "$SHELL_JSON" "$PLUGIN" <<'PY'
import json, sys
path, plugin = sys.argv[1:]
with open(path) as f:
    cfg = json.load(f)
plugins = cfg.setdefault("plugins", [])
if not any(p.get("id") == plugin for p in plugins):
    plugins.append({"id": plugin})
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("enabled the waveform overlay in", path)
PY
  # Make the running shell pick up the plugin now, not at next login.
  command -v omarchy-shell >/dev/null && omarchy-shell -q shell rescanPlugins || true
fi

cp systemd/iris-dictation.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iris-dictation
systemctl --user restart iris-dictation
echo
echo "Installed. Logs: journalctl --user -u iris-dictation -f"
echo "Caps Lock still does its normal job until you free it; see README, 'Freeing Caps Lock'."
