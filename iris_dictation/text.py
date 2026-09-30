"""Cleaning up what the model wrote."""

from __future__ import annotations

# What speech models write for a clip with nobody talking in it (learned from
# subtitle training data). Dropped only when the clip is also quiet, so saying
# "thank you" out loud still works.
SILENCE_PHRASES = {
    "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "thanks for watching and see you next time", "bye",
    "you", "yeah", "köszönöm", "köszönöm szépen", "köszönöm a figyelmet",
}


def is_silence_phrase(text: str) -> bool:
    norm = text.lower().strip().strip(".,!?…").strip()
    return norm in SILENCE_PHRASES or norm.startswith(("feliratok", "feliratozta"))


def tidy_short(text: str, max_words: int) -> str:
    """A one-to-few word result without the sentence dressing the model adds."""
    words = text.split()
    if not words or len(words) > max_words:
        return text
    text = text.rstrip(".,!?;:… ")
    first = text.split()[0] if text else ""
    if first and not (len(first) > 1 and first.isupper()):
        text = first.lower() + text[len(first):]     # "YouTube" -> "youtube"
    return text
