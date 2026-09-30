import pytest

from iris_dictation.fix import plausible


@pytest.mark.parametrize("raw, fixed", [
    ("Show me a plan to move the API to the Kuber Nettie's cluster.",
     "Show me a plan to move the API to the Kubernetes cluster."),
    ("buyer guard you're gonna find it", "WireGuard you're gonna find it"),
    ("Is Whisper available on Linux?", "Is Whisper available on Linux?"),
    ("Flow Bispa.", "Wispr Flow."),
    ("plan", "plan"),
    ("Vireguard.", "WireGuard."),
    ("Tail scale.", "Tailscale."),
    ("Tail scale.", "Tailscale"),
    ("plan", "Plan."),
    ("I wanted to change the hyper land config.", "I wanted to change the Hyprland config."),
])
def test_fixes_pass(raw, fixed):
    assert plausible(raw, fixed)


@pytest.mark.parametrize("raw, fixed", [
    ("Is Whisper available on Linux?",
     "Yes, Whisper runs on Linux. You can install it with pip install openai-whisper."),
    ("Can you check dictation?", "I can't access your files."),
    ("Build it please.", ""),
    ("Build it please.", "Sure, building it now."),
    ("Is Whisper available on Linux?", "Yes, it is available."),
    ("Commit and push.", "Done."),
    ("Yes, commit and push please.", "I cannot commit."),
])
def test_answers_and_rewrites_fail(raw, fixed):
    assert not plausible(raw, fixed)
