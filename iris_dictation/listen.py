"""The ears: iris-listen, the speech model as a service, over HTTP.

The model (Qwen3-ASR-1.7B) runs in iris-listen, one copy on the GPU for every
program that needs speech turned into text. This sends it a clip as WAV and
gets the text back: about 0.1 s for a short phrase, 0.35 s for a 7 s sentence,
plus the trip. iris-listen cuts clips over 28 s itself.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import numpy as np


class Listen:
    def __init__(self, url: str, token_file: str, languages: list[str],
                 vocabulary: list[str], timeout: float) -> None:
        self.url = url.rstrip("/") + "/v1/audio/transcriptions"
        self.token_file = Path(token_file).expanduser()
        # One language is forced; with several the model says which it heard.
        # Either way it writes down what was said, never a translation.
        langs = languages or ["en"]
        self.language = langs[0] if len(langs) == 1 else ""
        # Names and terms go in as context, which the model leans towards.
        self.prompt = ", ".join(vocabulary)
        self.client = httpx.Client(timeout=timeout)

    def recognize(self, samples: np.ndarray, sample_rate: int) -> str:
        # Read each time: a token made or changed after the daemon started still works.
        token = self.token_file.read_text().strip()
        r = self.client.post(
            self.url,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("clip.wav", wav(samples, sample_rate), "audio/wav")},
            data={"language": self.language, "prompt": self.prompt},
        )
        if r.status_code != 200:
            raise RuntimeError(f"iris-listen: HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["text"]


def wav(samples: np.ndarray, sample_rate: int) -> bytes:
    """Float samples (-1..1) as a 16-bit mono WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()
