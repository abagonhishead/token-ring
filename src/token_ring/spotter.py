"""Trigger words: hear "stop" or "cancel that" while the caller is being spoken to (barge-in),
within about half a second.

[keywords]
engine = "vosk"                    # built in; "off" to switch it off (the keypad's * still works)
model = "/opt/vosk/model"          # a Vosk model directory (the bridge image has small English)
[keywords.phrases]
stop = ["stop", "hang on", "wait"] # stop speaking
interrupt = ["cancel that"]        # stop speaking and interrupt the current turn, like *

Another engine (your own model, or a recogniser on another server) is a Spotter subclass named as
the engine ("module:Class") or registered as a token_ring.spotters entry point.
"""
import asyncio
import json
import logging
import os
import queue
import threading

from . import config, plugins
from .audio import FRAME_MS, RATE

log = logging.getLogger("token-ring")


class Spotter:
    """One per call. feed() gets every 20 ms frame of the caller's audio, with whether the
    caller's utterance detector thinks it's speech; on_hit(action, phrase) is to be called in the
    event loop (use self.hit from another thread). Only a phrase at the start of a burst of speech
    should count ("don't stop" isn't "stop")."""

    def __init__(self, conf, on_hit):
        self.conf, self.on_hit = conf, on_hit
        self.loop = asyncio.get_running_loop()
        self.keywords = {p.lower().strip(): action for action, phrases in conf.get("phrases", {}).items()
                         for p in phrases}

    def hit(self, action, phrase):
        self.loop.call_soon_threadsafe(self.on_hit, action, phrase)

    def feed(self, frame, loud):
        pass

    def close(self):
        pass


class NoSpotter(Spotter):
    pass


_models = {}
_models_lock = threading.Lock()


class VoskSpotter(Spotter):
    """Vosk with a grammar of just the trigger phrases, in its own thread. The recogniser is
    reset after RESET_QUIET_MS of quiet."""

    RESET_QUIET_MS = 300

    def __init__(self, conf, on_hit):
        super().__init__(conf, on_hit)
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        path = os.path.expanduser(conf.get("model") or "/opt/vosk/model")
        with _models_lock:
            if path not in _models:
                _models[path] = Model(path)
            self.model = _models[path]
        self.debug = os.environ.get("DEBUG_SPOTTER") == "1"
        self.frames = queue.Queue()
        threading.Thread(target=self.run, daemon=True).start()

    def feed(self, frame, loud):
        self.frames.put((frame, loud))

    def close(self):
        self.frames.put(None)

    def run(self):
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(self.model, RATE, json.dumps(list(self.keywords) + ["[unk]"]))
        fired = False  # one hit per burst of speech
        quiet_ms = 0
        last = ""
        while (item := self.frames.get()) is not None:
            frame, loud = item
            quiet_ms = 0 if loud else quiet_ms + FRAME_MS
            if quiet_ms == self.RESET_QUIET_MS:
                rec.Reset()
                fired, last = False, ""
            if rec.AcceptWaveform(frame):  # Vosk's own end of utterance
                fired, last = False, ""
                continue
            heard = json.loads(rec.PartialResult())["partial"]
            if self.debug and heard != last:
                log.info("spotter heard %r", heard)
            last = heard
            if fired or not heard:
                continue
            for phrase, action in self.keywords.items():
                if heard == phrase or heard.startswith(phrase + " "):
                    fired = True
                    self.hit(action, phrase)
                    break


def start(on_hit):
    """A spotter for a new call, as configured (NoSpotter if the engine can't start)."""
    conf = config.section("keywords")
    try:
        return plugins.load("spotters", conf.get("engine", "vosk"))(conf, on_hit)
    except Exception as e:
        log.warning("trigger words off: %s", e)
        return NoSpotter(conf, on_hit)
