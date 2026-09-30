"""The speech model: Qwen3-ASR-1.7B on the GPU, through transformers.

The best open model for English on the Open ASR Leaderboard (September 2026:
4.31% mean WER, against 5.71% for Canary-1B-v2). It is an audio encoder in
front of a small language model, so the text is written one token at a time:
about 15 ms per token on an RTX 5060 Ti, 0.3 s for a 7 s sentence.

Those 15 ms need a fixed-size KV cache and torch.compile with CUDA graphs;
plain generate() takes 25 ms a token. The cache never changes shape, so the
decoder step is compiled once, at warmup, and no clip length recompiles it.
"""

from __future__ import annotations

import logging
import os
import time

import numpy as np

log = logging.getLogger("iris-dictation")

RATE = 16000
# Prompt plus answer, in tokens. The prompt is ~50 tokens, 8 per second of
# audio (a 28 s piece: ~275) and the vocabulary; the answer 3-4 per second.
# ~115 KB of VRAM per token.
CACHE_TOKENS = 2048
MAX_NEW_TOKENS = 384


class Speech:
    def __init__(self, path: str, languages: list[str], vocabulary: list[str]) -> None:
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor, CompileConfig, StaticCache
        from transformers.models.qwen3_asr.processing_qwen3_asr import LANGUAGE_CODE_TO_NAME

        for lang in languages:
            if lang not in LANGUAGE_CODE_TO_NAME:
                raise ValueError(f"Qwen3-ASR does not know the language {lang!r}")
        # One language is forced; with several the model says which it heard.
        # Either way it writes down what was said: told "en" for Hungarian
        # speech, it still writes Hungarian, never a translation.
        langs = languages or ["en"]
        self.language = langs[0] if len(langs) == 1 else None
        # Names and terms go in as context, which the model leans towards.
        self.context = ", ".join(vocabulary) or None

        self.torch = torch
        path = os.path.expanduser(path)
        self.processor = AutoProcessor.from_pretrained(path)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            path, dtype=torch.bfloat16).to("cuda").eval()
        self.cache = StaticCache(config=self.model.config.get_text_config(),
                                 max_cache_len=CACHE_TOKENS)
        self.compile = CompileConfig(fullgraph=False, dynamic=False, mode="reduce-overhead")

    def warmup(self) -> None:
        """Compile the decoder (~6 s), so the first press doesn't."""
        rng = np.random.default_rng(0)
        for seconds in (1, 5):
            self.recognize(rng.normal(0, 0.01, RATE * seconds).astype(np.float32), RATE)

    def recognize(self, samples: np.ndarray, sample_rate: int) -> str:
        torch = self.torch
        if sample_rate != RATE:
            n = round(len(samples) * RATE / sample_rate)
            samples = np.interp(np.linspace(0, len(samples) - 1, n),
                                np.arange(len(samples)), samples).astype(np.float32)
        inputs = self.processor.apply_transcription_request(
            audio=samples, language=self.language, prompt=self.context,
        ).to(self.model.device, self.model.dtype)
        prompt = inputs["input_ids"].shape[1]
        with torch.inference_mode():
            self.cache.reset()
            out = self.model.generate(
                **inputs, do_sample=False, past_key_values=self.cache,
                compile_config=self.compile,
                max_new_tokens=min(MAX_NEW_TOKENS, CACHE_TOKENS - prompt))
        return self.processor.decode(out[:, prompt:], return_format="transcription_only")[0]


def load(path: str, languages: list[str], vocabulary: list[str]) -> Speech:
    t0 = time.time()
    speech = Speech(path, languages, vocabulary)
    log.info("model loaded in %.2fs", time.time() - t0)
    log.info("languages: %s", ", ".join(languages) if len(languages) > 1
             else languages[0] if languages else "en")
    t0 = time.time()
    speech.warmup()
    log.info("warmup %.2fs", time.time() - t0)
    return speech
