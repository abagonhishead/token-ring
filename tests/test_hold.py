"""Hold ("hold" ... "continue") and the ready tone, on an Operator call without a phone."""
import asyncio

from token_ring import bridge


def operator(spoken, tones, speak_s=0.3):
    c = bridge.Operator(None, None)
    c.uuid = "test"

    async def speak(text, label, spec):
        spoken.append(text)
        await asyncio.sleep(speak_s)
        c.last_sound = bridge.time.monotonic()

    async def play(pcm):
        tones.append(pcm)
    c.speak, c.play = speak, play
    return c


def test_hold_pauses_and_continue_resumes(conf):
    conf("")
    spoken, tones = [], []

    async def main():
        c = operator(spoken, tones)
        speaker = asyncio.create_task(c.speaker())
        c.say("One.", "reply")
        c.say("Two.", "reply")
        await asyncio.sleep(0.1)
        c.on_keyword("hold", "hold")              # mid "One."
        await asyncio.sleep(0)
        assert c.held and tones == [bridge.HOLD]
        c.say("Three.", "reply")                  # arrives while on hold: waits
        await asyncio.sleep(0.5)
        assert spoken == ["One."] and not c.talking
        c.on_keyword("stop", "stop")              # on hold, only "continue" counts
        c.on_keyword("interrupt", "cancel that")
        assert c.held and c.resume is not None
        c.on_keyword("continue", "continue")
        await asyncio.sleep(1.2)
        assert tones == [bridge.HOLD, bridge.RESUME]
        assert spoken == ["One.", "One.", "Two.", "Three."]   # "One." again from the start
        speaker.cancel()
    asyncio.run(main())


def test_nothing_heard_on_hold(conf):
    conf("")

    async def main():
        c = operator([], [])
        c.hold()
        loud = bridge.tone(300, 20, level=0.3)[:bridge.FRAME_BYTES]
        for _ in range(100):
            c.hear(loud)
        assert c.utterance is None
        await asyncio.sleep(0)
    asyncio.run(main())


def test_hold_by_speech_to_text_and_key_ends_it(conf):
    conf("")
    tones = []

    async def main():
        c = operator([], tones)
        c.key = "operator"
        await c.on_text("Hold.")
        assert c.held
        await c.on_dtmf("5")                      # any key: off hold, and nothing else
        assert not c.held and c.menu is None and c.queue.empty()
        await asyncio.sleep(0.05)
        assert tones == [bridge.HOLD, bridge.RESUME]
    asyncio.run(main())


def test_ready_tone(conf):
    conf("[bridge]\npip_every_seconds = 60\n")
    spoken, tones = [], []

    async def main():
        c = operator(spoken, tones, speak_s=0.2)
        c.targets["s"] = {"kind": "session", "label": "s", "status": "idle", "stop": None}
        c.key = "s"
        speaker = asyncio.create_task(c.speaker())
        pips = asyncio.create_task(c.pip_loop())
        c.say("Hello.", "greeting")
        await asyncio.sleep(0.6)
        assert tones == [bridge.READY]            # once it's been said: the caller's turn
        c.sent_at = bridge.time.monotonic()
        await asyncio.sleep(0.3)                  # sent, the session not busy yet: no tone
        c.targets["s"].update(status="busy", status_at=bridge.time.monotonic())
        await asyncio.sleep(0.3)
        c.say("Done.", "reply")
        c.targets["s"].update(status="idle", status_at=bridge.time.monotonic())
        await asyncio.sleep(0.6)
        assert tones == [bridge.READY, bridge.READY]   # not until the reply had been said
        pips.cancel()
        speaker.cancel()
    asyncio.run(main())
