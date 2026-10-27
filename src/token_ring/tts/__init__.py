"""Text-to-speech engines, and which voice says what.

A voice is "<engine>/<voice>", where engine names a table under [tts.engines] and voice means
whatever that engine takes (a Piper model, "model:speaker", an OpenAI voice name, ...). A voice
without "<engine>/" uses [tts] default.

Write your own engine by subclassing TTS and naming it as the engine's type ("module:Class"), or
register it as an entry point in the token_ring.tts group.
"""
import hashlib
import logging
import re
import threading
import time

from .. import config, plugins

log = logging.getLogger("token-ring")


class TTS:
    """One configured engine. synthesize() runs in a worker thread; it calls emit(pcm) with
    16 kHz 16-bit mono PCM as audio becomes available, and gives up promptly once stop (a
    threading.Event) is set. rate is the speaking rate: 1 is the voice's own pace, 1.2 is 20%
    faster."""

    def __init__(self, name, conf):
        self.name, self.conf = name, conf

    def synthesize(self, text, voice, rate, emit, stop):
        raise NotImplementedError

    def preload(self, voice):
        """Get ready to speak in voice (load a model, ...), so the first phrase isn't slow."""


SENTENCE = re.compile(r"(?<=[.!?;:])\s+")


def sentences(text):
    """Text split into sentences, for engines that synthesise a whole request before replying:
    the first sentence can start playing while the rest are made."""
    return [s for s in SENTENCE.split(text) if s.strip()]


_lock = threading.Lock()
_engines = {}  # name -> (conf, engine)


def engine(name):
    engines = config.section("tts").get("engines", {})
    if name not in engines:
        raise KeyError(f"no TTS engine {name!r} in [tts.engines]")
    conf = engines[name]
    with _lock:
        if name not in _engines or _engines[name][0] != conf:
            _engines[name] = (conf, plugins.load("tts", conf.get("type", name))(name, conf))
        return _engines[name][1]


def split(spec):
    """(engine name, voice) for a voice spec."""
    if "/" in spec:
        name, _, voice = spec.partition("/")
        return name, voice
    return config.section("tts").get("default", "piper"), spec


def voice_for(key):
    """The voice for a speaker: "operator" (also everything the PBX itself says), or a session
    key. A session not pinned in [voices] sessions gets one from the pool by hashing its key:
    random, but the same on every call. Voices that share a model ("model:speaker") are grouped
    so a model with a hundred speakers doesn't crowd out the rest."""
    v = config.section("voices")
    if key == "operator":
        return v.get("operator") or default_voice()
    if key in v.get("sessions", {}):
        return v["sessions"][key]
    pool = v.get("pool") or [v.get("operator") or default_voice()]
    groups = {}
    for spec in pool:
        groups.setdefault(spec.split(":")[0], []).append(spec)
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    group = groups[sorted(groups)[h % len(groups)]]
    return group[(h // len(groups)) % len(group)]


def default_voice():
    name = config.section("tts").get("default", "piper")
    return f"{name}/{config.section('tts')['engines'].get(name, {}).get('voice', '')}"


def persona(spec):
    """The name a voice introduces itself by ([voices] names), or None."""
    return config.section("voices").get("names", {}).get(spec)


def rate_for(spec):
    v = config.section("voices")
    rates = v.get("rates", {})
    return float(rates.get(spec, rates.get(spec.split(":")[0], v.get("rate", 1.0))))


def synthesize(text, spec, emit, stop):
    """Say text in voice spec, falling back to the operator's voice if that one fails."""
    name, voice = split(spec)
    try:
        return engine(name).synthesize(text, voice, rate_for(spec), emit, stop)
    except Exception as e:
        fallback = voice_for("operator")
        if spec == fallback or stop.is_set():
            raise
        log.warning("voice %s failed (%s); using %s", spec, e, fallback)
        name, voice = split(fallback)
        return engine(name).synthesize(text, voice, rate_for(fallback), emit, stop)


def preload(attempts=6, wait_s=10):
    """Load every configured voice, the operator's first (every call starts with it). Engines
    that are still starting (another container) are tried again a few times."""
    v = config.section("voices")
    specs = list(dict.fromkeys([voice_for("operator")] + list(v.get("pool", [])) + list(v.get("sessions", {}).values())))
    for attempt in range(attempts):
        failed = []
        for spec in specs:
            try:
                name, voice = split(spec)
                engine(name).preload(voice)
            except Exception as e:
                failed.append(spec)
                if attempt == attempts - 1:
                    log.warning("can't load voice %s: %s", spec, e)
        if not failed:
            return
        specs = failed
        time.sleep(wait_s)
