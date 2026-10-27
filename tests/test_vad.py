import os

import pytest

from token_ring import bridge, vad


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
