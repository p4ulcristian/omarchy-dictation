# Socket protocol

The daemon listens on two Unix sockets in `$XDG_RUNTIME_DIR/iris-dictation/`,
both readable by your user only. Other programs build on these, so they only
ever grow: existing commands and events keep their meaning.

## `iris-dictation.sock`: commands

One command per connection: connect, send the command, shut down the write
side, read the reply until the daemon closes the connection. `iris-dictation
<command>` does exactly this.

| command | reply | what it does |
|---|---|---|
| `start` | `ok` | start recording, as if the key went down |
| `start <tag>` | `ok` | the same, for a program that shows the recording itself: the events say `recording <tag>` and the waveform overlay stays hidden until `idle`. A tag is lowercase letters, digits and `-` |
| `stop` | `ok` | stop, transcribe, type the text |
| `stop-return` | the text | stop and transcribe, reply with the text instead of typing it; empty when nothing was heard |
| `toggle` | `ok` | `start` when idle, `stop` when recording |
| `status` | `idle`, `recording` or `transcribing` | |
| `transcribe <path>` | the text | transcribe a wav file (absolute path) and reply with the text; nothing is typed; empty when nothing was heard or the file can't be read |
| `ping` | `pong` | |
| `quit` | `ok` | shut the daemon down |

Anything else replies `unknown command: <verb>`. `stop-return` and
`transcribe` wait for the transcription, up to 60 seconds.

A shell example:

```sh
printf 'stop-return' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/iris-dictation/iris-dictation.sock
```

## `levels.sock`: events

Connect and read. The daemon sends one line per event to every connected
client and never reads anything from them. The waveform overlay
(`overlay/DictationWave.qml`) is the client that ships with iris-dictation.

| line | meaning |
|---|---|
| `recording` | recording started |
| `recording <tag>` | recording started by `start <tag>`: its owner shows it, the waveform overlay doesn't |
| `level <0..1>` | voice loudness, about every 20 ms while recording (`-54 dB` = 0, `-40 dB` = 1) |
| `transcribing` | recording stopped, the model is running |
| `text <text>` | what was heard (newlines replaced by spaces) |
| `nothing` | nothing was heard |
| `typing <n>` | typing `<n>` characters now, at about 220 per second; `0` means an instant paste |
| `idle` | done, ready for the next press |

A normal dictation is `recording`, `level …` (many), `transcribing`,
`text …`, `typing …`, `idle`. A `stop-return` or `transcribe` has no
`typing` line. Clients should ignore lines they don't know.
