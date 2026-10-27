"""Speech-to-text engines. An engine turns one utterance (16 kHz, 16-bit mono PCM) into text.

Write your own by subclassing STT and naming it in the config as "module:Class", or register it
as an entry point in the token_ring.stt group.
"""
import threading

from .. import config, plugins


class STT:
    """conf is the [stt] table. transcribe() is called from worker threads, possibly several at
    once (dictation keeps listening while earlier sentences are transcribed): lock inside if the
    engine can't cope."""

    def __init__(self, conf):
        self.conf = conf

    def transcribe(self, pcm):
        raise NotImplementedError

    def describe(self):
        return type(self).__name__


_lock = threading.Lock()
_engine = {"conf": None, "stt": None}


def get():
    """The configured engine (rebuilt when [stt] changes)."""
    conf = config.section("stt")
    with _lock:
        if _engine["conf"] != conf:
            _engine.update(conf=conf, stt=plugins.load("stt", conf.get("engine", "openai"))(conf))
        return _engine["stt"]
