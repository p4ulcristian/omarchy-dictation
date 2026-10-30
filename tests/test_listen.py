"""The client for iris-listen, against a fake server."""

import io
import json
import wave

import httpx
import numpy as np

from iris_dictation.listen import Listen, wav


def test_wav_is_16_bit_mono_at_the_rate():
    samples = np.linspace(-1, 1, 1600, dtype=np.float32)
    with wave.open(io.BytesIO(wav(samples, 16000))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 1600)


def test_recognize_sends_the_token_language_and_vocabulary(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret\n")
    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": "Hello Omarchy."})

    ears = Listen("http://listen.test/", str(token), ["en"], ["Omarchy", "WireGuard"], 5)
    ears.client = httpx.Client(transport=httpx.MockTransport(answer))
    assert ears.recognize(np.zeros(1600, dtype=np.float32), 16000) == "Hello Omarchy."
    assert seen["auth"] == "Bearer secret"
    assert seen["url"] == "http://listen.test/v1/audio/transcriptions"
    assert b'name="language"\r\n\r\nen' in seen["body"]
    assert b'name="prompt"\r\n\r\nOmarchy, WireGuard' in seen["body"]


def test_several_languages_leave_the_choice_to_the_model(tmp_path):
    token = tmp_path / "token"
    token.write_text("t")
    assert Listen("http://x", str(token), ["hu", "en"], [], 5).language == ""


def test_an_error_names_iris_listen(tmp_path):
    token = tmp_path / "token"
    token.write_text("wrong")
    ears = Listen("http://x", str(token), [], [], 5)
    ears.client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text=json.dumps({"detail": "a bearer token is needed"}))))
    try:
        ears.recognize(np.zeros(160, dtype=np.float32), 16000)
    except RuntimeError as exc:
        assert "iris-listen: HTTP 401" in str(exc)
    else:
        raise AssertionError("no error")
