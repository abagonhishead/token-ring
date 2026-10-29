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


def test_voice_groups(conf):
    conf("""
[voices]
operator = "@ops"
default = "@team"
disabled = ["piper/b", "piper/x"]
[voices.groups]
ops = ["piper/x"]
team = ["piper/a", "piper/b", "piper/c"]
[voices.sessions]
pinned = "piper/p"
off = "piper/x"
""")
    from token_ring.tts import default_voice, in_use
    assert voice_for("operator") == default_voice()   # its group's only voice is off
    assert {voice_for("s") for _ in range(60)} == {"piper/a", "piper/c"}   # random, never b
    assert voice_for("pinned") == "piper/p"
    assert voice_for("off") in ("piper/a", "piper/c")   # its voice is off: the default
    assert set(in_use()) == {"piper/a", "piper/c", "piper/p"}


def test_call_keeps_its_voice(conf):
    from token_ring import bridge
    conf('[voices]\ndefault = "@g"\n[voices.groups]\ng = ["piper/a", "piper/b", "piper/c", "piper/d"]\n')
    call = bridge.Call.__new__(bridge.Call)
    call.voices = {}
    first = call.voice_of("alpha")
    assert all(call.voice_of("alpha") == first for _ in range(20))


def test_introduce(conf):
    conf('[voices.names]\n"piper/a" = "Ann"\n"piper/b" = "Bob"\n')
    assert persona("piper/a") == "Ann" and persona("piper/b") == "Bob"   # no introduce: all named do
    conf('[voices]\nintroduce = ["piper/a"]\n[voices.names]\n"piper/a" = "Ann"\n"piper/b" = "Bob"\n')
    assert persona("piper/a") == "Ann" and persona("piper/b") is None
