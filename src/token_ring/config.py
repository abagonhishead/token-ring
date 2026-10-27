"""Configuration: one TOML file shared by every part of token-ring.

The file is $TOKEN_RING_CONFIG, else ~/.config/token-ring/config.toml, else
/etc/token-ring/config.toml. `config.example.toml` documents every setting. get() re-reads the
file when it changes, so voices, keywords and pronunciations apply from the next phrase or call.
"""
import copy
import logging
import os
import threading
import tomllib

log = logging.getLogger("token-ring")

DEFAULTS = {
    "user": {"name": "the caller"},
    "bridge": {
        "host": "127.0.0.1",
        "echo_port": 9093,
        "operator_port": 9094,
        "voicemail_port": 9097,
        "phone": "PJSIP/100",          # what outbound calls ring
        "caller_id": "Claude",         # shown on outbound calls as "<caller_id>: <session>"
        "spool": "/var/spool/asterisk/outgoing",
        "staging": "/var/spool/asterisk/token-ring",  # same filesystem as spool
        "ring_seconds": 30,
        "gateway_url": "http://127.0.0.1:9096",
        "gateway_token": "",           # or gateway_token_file, or $TOKEN_RING_TOKEN
        "gateway_token_file": "~/.config/token-ring/token",
        "gateway_ca_file": "",         # CA bundle for an https gateway_url with a private CA
        "answer_wait_seconds": 4.0,    # calls from a session: wait this long for a hello
        "pip_every_seconds": 3.0,      # soft pip while the session works in silence
        "ami_host": "127.0.0.1",       # Asterisk's manager interface, for text messages
        "ami_port": 5038,
        "ami_user": "token-ring",
        "ami_secret": "",              # or ami_secret_file (a manager.conf section with secret =)
        "ami_secret_file": "/run/secrets/manager_auth.conf",
        "text_from": "201",            # who texts appear to come from
    },
    "gateway": {
        "host": "127.0.0.1",
        "port": 9096,
        "allowed": ["127.0.0.0/8", "::1/128"],  # who may connect (the bridge, local sessions)
        "token": "",
        "token_file": "~/.config/token-ring/token",
        "tls_cert": "",                # both set: serve HTTPS (reloaded when the files change)
        "tls_key": "",
        "data_dir": "~/.local/share/token-ring",
        "call_gap_seconds": 120,       # a session may ring at most this often
        "backend": "claude-code",
        "labels": {},                  # session key -> how it's said ("rc" = "general use")
        "operator_prompt": "",         # appended to the operator's built-in instructions
        "session_prompt": "",          # appended to new phone sessions' instructions
    },
    "backends": {},
    "stt": {"engine": "openai"},
    "tts": {"default": "piper", "engines": {"piper": {"type": "piper"}}},
    "voices": {"operator": "", "pool": [], "sessions": {}, "names": {}, "rates": {}, "rate": 1.0},
    "keywords": {"engine": "vosk", "model": "", "phrases": {
        "stop": ["stop", "hang on", "wait", "shush", "quiet"],
        "interrupt": ["cancel that"],
    }},
    "pronunciations": {},
    "notify": {"via": [], "remind_every_minutes": 60, "preview": True, "preview_chars": 160, "ntfy": {}},
}


def path():
    for p in (os.environ.get("TOKEN_RING_CONFIG"), "~/.config/token-ring/config.toml",
              "/etc/token-ring/config.toml"):
        if p and os.path.exists(os.path.expanduser(p)):
            return os.path.expanduser(p)
    return None


def merge(base, over):
    """base with over's values on top (tables merged key by key, everything else replaced)."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "config": None}


def get():
    """The current configuration (defaults with the file on top). Cheap to call often."""
    p = path()
    try:
        mtime = os.path.getmtime(p) if p else None
    except OSError:
        mtime = None
    with _lock:
        if _cache["config"] is not None and (_cache["path"], _cache["mtime"]) == (p, mtime):
            return _cache["config"]
        data = {}
        if p:
            try:
                with open(p, "rb") as f:
                    data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as e:
                if _cache["config"] is not None:
                    log.warning("%s: %s; keeping the previous configuration", p, e)
                    return _cache["config"]
                raise SystemExit(f"{p}: {e}")
        _cache.update(path=p, mtime=mtime, config=merge(DEFAULTS, data))
        return _cache["config"]


def section(name):
    return get()[name]


def expand(p):
    return os.path.expanduser(p) if p else p


def secret(table, key):
    """A secret from table[key], else the file at table[key + "_file"], else $TOKEN_RING_TOKEN
    (for tokens)."""
    if table.get(key):
        return table[key]
    f = expand(table.get(f"{key}_file", ""))
    if f and os.path.exists(f):
        with open(f) as fh:
            return fh.read().strip()
    return os.environ.get("TOKEN_RING_TOKEN", "")
