import asyncio
import os
import threading

import pytest

from token_ring import bridge, vad
from token_ring.audio import from_wav, tone


def call(conf_text, conf):
    conf(conf_text)
    c = object.__new__(bridge.Call)
    c.uuid, c.noise_db, c.voice_db, c.vad, c.level_dbs = "test", -60.0, None, vad.EnergyVAD({}), []
    return c


def test_near_gate(conf):
    c = call("[vad]\nnear_db = 12\n", conf)
    assert c.is_speech(b"", -40, True)          # nothing known yet: anything well above the noise
    c.voice_db = -20
    assert c.is_speech(b"", -25, True)          # the caller
    assert not c.is_speech(b"", -40, True)      # a TV across the room, 20 dB quieter
    assert c.is_speech(b"", -36, False)         # trailing off mid-utterance is allowed a little more


def test_near_gate_off(conf):
    c = call("[vad]\nnear_db = 0\n", conf)
    c.voice_db = -20
    assert c.is_speech(b"", -40, True)


def test_voice_level_rises_at_once_falls_slowly(conf):
    c = call("", conf)
    c.learned_voice(-40)                        # first heard: maybe the TV
    c.learned_voice(-20)                        # then the caller
    assert c.voice_db == -20
    c.learned_voice(-30)
    assert c.voice_db == pytest.approx(-22)


def test_not_speech(conf):
    conf('[stt]\nignore = ["Like and subscribe!"]\n')
    assert bridge.not_speech("Thank you.")
    assert bridge.not_speech(" you ")
    assert bridge.not_speech("like and subscribe")
    assert not bridge.not_speech("Okay.")
    assert not bridge.not_speech("Thank you, that's all for today.")


def test_unknown_engine_falls_back(conf):
    conf('[vad]\nengine = "no.such:Thing"\n')
    assert type(vad.create()) is vad.EnergyVAD


@pytest.mark.skipif(not os.environ.get("SILERO_MODEL"), reason="set SILERO_MODEL to a silero_vad.onnx")
def test_silero_quiet_is_not_speech():
    v = vad.SileroVAD({"model": os.environ["SILERO_MODEL"]})
    frame = bytes(640)
    assert not any(v.speech(frame, 30, True) for _ in range(50))


def test_min_level(conf):
    c = call("[vad]\nmin_level_db = -30\n", conf)
    assert not c.is_speech(b"", -35, True)      # the TV, before the caller has said anything
    assert c.is_speech(b"", -20, True)
    assert c.is_speech(b"", -33, False)         # a little leeway once speaking


def test_utterance_ends_on_its_loudest_part(conf):
    c = call("[vad]\nnear_db = 12\n", conf)
    c.level_dbs = [-15.0] * 20                  # the caller has been speaking
    assert not c.is_speech(b"", -35, False)     # the TV behind them counts as a pause


def test_noise_rises_slowly(conf):
    c = call("[vad]\nnoise_rise_db = 6\n", conf)
    for _ in range(10):                         # 200 ms of the start of speech, not yet recognised
        c.track_noise(-20)
    assert c.noise_db == pytest.approx(-58.8)   # up 1.2 dB, not most of the way to -20
    c.noise_db = -40
    c.track_noise(-80)
    assert c.noise_db == pytest.approx(-42)     # down as fast as before


def test_noise_rise_unlimited(conf):
    c = call("", conf)
    c.track_noise(-20)
    assert c.noise_db == pytest.approx(-58)


def test_record_utterance(conf, tmp_path):
    conf(f'[vad]\nrecord_dir = "{tmp_path / "rec"}"\nrecord_keep = 1\n')
    heard = []

    async def main():
        c = bridge.Call(None, None)
        c.uuid = "abcdef0123"

        async def got(pcm):
            heard.append(pcm)
            c.transcribing = False
        c.heard = got
        quiet, loud = bytes(bridge.FRAME_BYTES), tone(300, 20, level=0.3)[:bridge.FRAME_BYTES]
        for frames in ([quiet] * 150 + [loud] * 50 + [quiet] * 50) * 2:   # 3 s quiet, 1 s speech, 1 s quiet
            c.hear(frames)
            await asyncio.sleep(0)
    asyncio.run(main())
    assert len(heard) == 2
    saved = list((tmp_path / "rec").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("-abcdef01.wav")   # only the newest kept
    pcm = from_wav(saved[0].read_bytes())
    assert len(pcm) == len(heard[1]) + bridge.RECORD_BEFORE_S * bridge.RATE * 2   # and the 2 s before


def test_any_key_cuts_speech_short(conf):
    conf("")
    said = []

    async def main():
        c = object.__new__(bridge.Operator)
        c.uuid, c.dialog, c.menu, c.key, c.rereading = "test", None, None, "operator", 0.0
        c.queue, c.current, c.voices = asyncio.Queue(), None, {}
        c.stop_synth = threading.Event()

        async def play(pcm):
            pass
        c.play = play
        c.say("A long reply from the operator.", "reply")
        for _ in range(3):
            await c.on_dtmf("2")
        while not c.queue.empty():
            said.append(c.queue.get_nowait()[0])
    asyncio.run(main())
    assert said == ["Press 0 to hear the list of sessions."]   # the reply cut short, one hint
