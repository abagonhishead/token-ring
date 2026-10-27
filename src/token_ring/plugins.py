"""Loading speech-to-text engines, text-to-speech engines and AI backends by name.

A `type` (or `engine`) in the config is one of:
  - a built-in name ("openai", "piper", "claude-code", ...),
  - the name of an installed plugin, registered as a Python entry point in the group
    token_ring.stt, token_ring.tts, token_ring.spotters, token_ring.vad or token_ring.backends,
  - "package.module:ClassName".
"""
import importlib
from importlib.metadata import entry_points

BUILTIN = {
    "stt": {
        "openai": "token_ring.stt.openai:OpenAISTT",
        "http": "token_ring.stt.http:HTTPSTT",
        "command": "token_ring.stt.command:CommandSTT",
        "vosk": "token_ring.stt.vosk:VoskSTT",
        "fake": "token_ring.stt.fake:FakeSTT",
    },
    "tts": {
        "piper": "token_ring.tts.piper:PiperTTS",
        "openai": "token_ring.tts.openai:OpenAITTS",
        "command": "token_ring.tts.command:CommandTTS",
        "tone": "token_ring.tts.tone:ToneTTS",
    },
    "spotters": {
        "vosk": "token_ring.spotter:VoskSpotter",
        "off": "token_ring.spotter:NoSpotter",
    },
    "vad": {
        "energy": "token_ring.vad:EnergyVAD",
        "silero": "token_ring.vad:SileroVAD",
    },
    "backends": {
        "claude-code": "token_ring.backends.claude_code:ClaudeCode",
        "echo": "token_ring.backends.echo:Echo",
    },
}


def load(kind, name):
    """The class for a plugin of kind ("stt", "tts", "spotters", "vad", "backends") called name."""
    target = BUILTIN[kind].get(name)
    if not target:
        found = [e for e in entry_points(group=f"token_ring.{kind}") if e.name == name]
        if found:
            return found[0].load()
        target = name
    if ":" not in target:
        known = sorted(BUILTIN[kind]) + sorted(e.name for e in entry_points(group=f"token_ring.{kind}"))
        raise SystemExit(f"unknown {kind} {name!r}; known: {', '.join(known)}, or module:Class")
    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)


def load_class(target):
    """A class named "package.module:ClassName"."""
    module, _, cls = target.partition(":")
    return getattr(importlib.import_module(module), cls)
