from token_ring.tts import persona, rate_for, split, voice_for

VOICES = '''
[tts]
default = "piper"
[tts.engines.piper]
type = "piper"
voice = "en_GB-alan-medium"
[voices]
operator = "piper/en_GB-semaine-medium:obadiah"
pool = ["piper/en_GB-semaine-medium:poppy", "piper/en_GB-semaine-medium:spike", "kokoro/bf_emma"]
sessions = { pinned = "kokoro/bf_isabella" }
names = { "piper/en_GB-semaine-medium:poppy" = "Poppy" }
rate = 0.9
rates = { "piper/en_GB-semaine-medium" = 1.0, "kokoro/bf_emma" = 1.2 }
'''


def test_voices(conf):
    conf(VOICES)
    assert voice_for("operator") == "piper/en_GB-semaine-medium:obadiah"
    assert voice_for("pinned") == "kokoro/bf_isabella"
    assert voice_for("abc") == voice_for("abc")  # stable
    assert {voice_for(f"s{i}") for i in range(40)} == {
        "piper/en_GB-semaine-medium:poppy", "piper/en_GB-semaine-medium:spike", "kokoro/bf_emma"}
    assert persona("piper/en_GB-semaine-medium:poppy") == "Poppy"
    assert rate_for("piper/en_GB-semaine-medium:spike") == 1.0
    assert rate_for("kokoro/bf_emma") == 1.2
    assert rate_for("other/x") == 0.9
    assert split("en_GB-alan-medium") == ("piper", "en_GB-alan-medium")


def test_default_voice(conf):
    conf('[tts.engines.piper]\nvoice = "en_GB-alan-medium"\n')
    assert voice_for("operator") == "piper/en_GB-alan-medium"
