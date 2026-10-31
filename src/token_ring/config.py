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
        "phone_fallback": "",          # rung instead when the phone isn't reachable, e.g. a mobile
                                       # through a SIP trunk: "PJSIP/07700900123@trunk"
        "fallback_ring_seconds": 20,   # less than the mobile takes to divert to its voicemail
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
        "text_from": "201",            # who voicemail texts appear to come from
        "log_file": "",                # also log here (the web page reads it)
        "mailbox": "",                 # for the voicemail light ("": <phone's endpoint>@token-ring)
        "presence_every_seconds": 5.0, # session lines' presence (Custom:session<line>); 0: off
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
        "text_operator": "200",        # texts to this extension go to the operator ("" for none)
        "lines": "301-399",            # each session's direct line, from these ("" for none)
    },
    "backends": {},
    "stt": {"engine": "openai"},
    "tts": {"default": "piper", "engines": {"piper": {"type": "piper"}}},
    "voices": {"operator": "", "default": "", "pool": [], "groups": {}, "disabled": [], "sessions": {},
               "names": {}, "rates": {}, "rate": 1.0},
    "keywords": {"engine": "vosk", "model": "", "phrases": {
        "stop": ["stop", "hang on", "wait", "shush", "quiet"],
        "interrupt": ["cancel that"],
    }},
    "vad": {"engine": "energy", "model": "", "threshold": 0.5, "near_db": 12, "min_level_db": -100,
            "noise_rise_db": 0, "record_dir": "", "record_keep": 200},
    "pronunciations": {},
    "notify": {"via": [], "remind_every_minutes": 60, "preview": True, "preview_chars": 160, "ntfy": {}},
    "web": {
        "listen": "0.0.0.0",
        "port": 8443,
        "cert_file": "/tls/fullchain.pem",   # HTTPS when both exist
        "key_file": "/tls/privkey.pem",
        "ami_user": "token-ring-web",        # its own manager login (in [bridge] ami_secret_file)
        "asterisk_dir": "/etc/asterisk",
        "asterisk_log": "/var/log/asterisk/full.log",
        "calls_log": "/var/log/asterisk/cdr-custom/calls.csv",   # cdr_custom.conf: the call log
        "calls_keep_days": 90,                    # older call records are dropped (0: keep all)
        "bridge_log": "/var/log/token-ring/bridge.log",
        "rotate_mb": 5,
    },
    "contacts": {
        "url": "",                     # the CardDAV server as phones reach it ("": no contacts)
        "local_url": "https://127.0.0.1:5232",
        "users_file": "/run/secrets/contacts_users",
        "owner": "token-ring",
        "directory": {"200": "Operator", "201": "Voicemail"},
    },
    "provision": {
        "name": "",                    # the host name phones register to (in the certificate)
        "sip_port": 5061,
        "transport": "tls",
        "media_encryption": "srtp",    # "" to leave the phone's setting alone
        "media_encryption_mandatory": True,
        "listen": "0.0.0.0",
        "port": 9443,
        "cert_file": "/tls/fullchain.pem",
        "key_file": "/tls/privkey.pem",
        "auth_file": "/secrets/pjsip_auth.conf",
        "wait_minutes": 10,
        "voicemail": "",               # what the voicemail button dials ("": [bridge] voicemail_extension)
        "names": {},                   # extension -> the account's display name
        "presence_list": "presence",   # the phones' presence list (pjsip_presence.conf, which the
                                       # web page keeps); "": none
    },
}


IMAGE_DIR = "/usr/share/token-ring"   # in the containers: the image's config.toml
LIVE_DIR = "/etc/token-ring"          # the one in use (a volume, which the web page edits)


def seed(image_dir=IMAGE_DIR, live_dir=LIVE_DIR):
    """Containers: bring the image's files (config.toml) into the live directory. A file that's
    missing, or still what an earlier image had (not edited since), takes the image's; one edited
    here is kept, and the image's version is left beside it as <name>.image-new. Records what each
    image had in <live_dir>/.image. A file with no record (mounted from outside) is never touched.
    Returns {name: "seeded" | "updated" | "kept edit" | "unchanged"}."""
    out = {}
    if not os.path.isdir(image_dir):
        return out
    record = os.path.join(live_dir, ".image")
    for name in sorted(os.listdir(image_dir)):
        src, live, base = (os.path.join(image_dir, name), os.path.join(live_dir, name),
                           os.path.join(record, name))
        if not os.path.isfile(src):
            continue
        try:
            new = open(src, "rb").read()
            have = open(live, "rb").read() if os.path.exists(live) else None
            was = open(base, "rb").read() if os.path.exists(base) else None
            if have is not None and was is None:
                out[name] = "unchanged"   # not ours to manage
                continue
            os.makedirs(record, exist_ok=True)
            if have == new:
                out[name] = "unchanged"   # (perhaps edited here to just what the image now has)
            elif have is None or have == was:
                if have != new:
                    with open(live, "wb") as f:
                        f.write(new)
                out[name] = "seeded" if have is None else ("updated" if have != new else "unchanged")
            else:
                out[name] = "kept edit"
                if new != was:
                    with open(live + ".image-new", "wb") as f:
                        f.write(new)
                    log.warning("%s: edited here, and the image has a new version (%s.image-new)", live, name)
            with open(base, "wb") as f:
                f.write(new)
        except OSError as e:
            log.warning("config seed %s: %s", name, e)
    return out


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
