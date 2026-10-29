"""The PBX's web page: what's connected and configured, the logs, and the configuration.

  token-ring-web     serve it on [web] port (HTTPS with [web] cert_file and key_file, if present)

It runs beside Asterisk and the bridge (docker compose: the web service), and reads:
  - Asterisk through its manager interface, with its own login ([web] ami_user, a section in the
    bridge's ami_secret_file with read = system,call,reporting,
    write = system,call,reporting,config,command and displayconnects = no)
  - the logs: Asterisk's full.log and the bridge's log file ([web] asterisk_log, bridge_log), and
    the gateway's (GET /logs, with the bridge's gateway login)
  - the configuration: Asterisk's files in [web] asterisk_dir and token-ring's config file.
Editing (the page's Advanced switch) writes those files, keeping the previous version in
<dir>/.backup, and reloads what changed. The allowed callers (who may call in on the trunks) are
edited in their own box: GET/PUT /api/callers, {"callers": [{"number", "name"}]}, kept in
Asterisk's callers.conf.

There's no login yet: keep it to your LAN or VPN.
"""
import asyncio
import csv
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import tomllib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import bridge, config, contacts, tts
from .audio import pitch_hz, to_wav

log = logging.getLogger("token-ring")

SECRET_KEY = re.compile(r"(secret|password|passwd|token|api_key)$", re.I)
# What to reload after saving an Asterisk file (anything else: "core reload")
RELOADS = {
    "pjsip.conf": "module reload res_pjsip.so",
    "extensions.conf": "dialplan reload",
    "callers.conf": "dialplan reload",
    "cdr.conf": "module reload cdr",
    "cdr_custom.conf": "module reload cdr_custom.so",
    "manager.conf": "manager reload",
    "logger.conf": "logger reload",
    "rtp.conf": "module reload res_rtp_asterisk.so",
    "modules.conf": None,   # needs a restart
    "asterisk.conf": None,
}


def num(value):
    """An AMI number, which may be "N/A" or missing."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def web_conf():
    return config.section("web")


def ami_login():
    c = web_conf()
    return c["ami_user"], bridge.ami_secret(c["ami_user"])


def ami(coro):
    return asyncio.run(coro)


async def ami_command(command):
    resp = await bridge.ami_action({"Action": "Command", "Command": command}, login=ami_login())
    if resp.get("Response") != "Success":
        raise RuntimeError(resp.get("Message", f"{command} failed"))
    return "\n".join(resp.get("Output", []))


# Status: everything the overview shows

class AMI:
    """One manager connection for a run of actions, one at a time."""

    async def __aenter__(self):
        self.reader, self.writer = await bridge.ami_connect(login=ami_login())
        return self

    async def __aexit__(self, *exc):
        bridge.ami_send(self.writer, {"Action": "Logoff"})
        self.writer.close()

    async def action(self, fields, timeout=10):
        bridge.ami_send(self.writer, fields)
        return await asyncio.wait_for(bridge.ami_read(self.reader), timeout)

    async def list(self, fields, timeout=10):
        first = await self.action(fields, timeout)
        if first.get("Response") != "Success":
            msg = first.get("Message", "").lower()
            if msg.startswith("no ") or "not found" in msg or "did not find" in msg:
                return []
            raise RuntimeError(f"{fields['Action']}: {first.get('Message', 'failed')}")
        events = []
        while True:
            ev = await asyncio.wait_for(bridge.ami_read(self.reader), timeout)
            if ev.get("EventList") == "Complete" or ev.get("Event", "").endswith("Complete"):
                return events
            events.append(ev)


async def asterisk_status():
    async with AMI() as conn:
        return await gather_status(conn)


async def gather_status(conn):
    async def attempt(coro):
        try:
            return await coro
        except Exception as e:
            return e
    lists = {
        "endpoints": {"Action": "PJSIPShowEndpoints"},
        "contacts": {"Action": "PJSIPShowContacts"},
        "subscriptions": {"Action": "PJSIPShowSubscriptionsInbound"},
        "channels": {"Action": "CoreShowChannels"},
        "dialplan": {"Action": "ShowDialPlan"},
        "registrations": {"Action": "PJSIPShowRegistrationsOutbound"},
    }
    names = list(lists)
    raw = {n: await attempt(conn.list(lists[n])) for n in names}
    core = await attempt(conn.action({"Action": "CoreStatus"}))
    settings = await attempt(conn.action({"Action": "CoreSettings"}))
    transports = await attempt(conn.action({"Action": "Command", "Command": "pjsip show transports"}))
    transports = "\n".join(transports.get("Output", [])) if isinstance(transports, dict) else ""
    out = {"errors": [f"{n}: {r}" for n, r in raw.items() if isinstance(r, Exception)]}
    raw = {n: ([] if isinstance(r, Exception) else r) for n, r in raw.items()}

    details = [await attempt(conn.list({"Action": "PJSIPShowEndpoint", "Endpoint": e["ObjectName"]}))
               for e in raw["endpoints"]]
    registrations = {r.get("ObjectName", ""): r for r in raw["registrations"]  # (not its AuthDetail: the password)
                     if r.get("Event") == "OutboundRegistrationDetail"}
    endpoints, trunks = [], []
    for e, d in zip(raw["endpoints"], details):
        d = [] if isinstance(d, Exception) else d
        ep = next((x for x in d if x.get("Event") == "EndpointDetail"), {})
        aor = next((x for x in d if x.get("Event") == "AorDetail"), {})
        identify = [x for x in d if x.get("Event") == "IdentifyDetail"]
        reg = registrations.get(e["ObjectName"])
        if reg or identify:  # a trunk: registers out to a provider, or its calls are known by address
            status = next((x for x in d if x.get("Event") == "ContactStatusDetail"), {})
            m = re.match(r"sips?:([^@]+)@", (reg or {}).get("ClientUri", ""))
            trunks.append({
                "name": e["ObjectName"], "number": m.group(1) if m else ep.get("FromUser", ""),
                "server": re.sub(r"^sips?:", "", (reg or {}).get("ServerUri", "")),
                "registration": (reg or {}).get("Status", "") or ("" if reg else "not registering"),
                "status": status.get("Status", ""), "rtt_ms": round(num(status.get("RoundtripUsec")) / 1000),
                "context": ep.get("Context", ""), "codecs": ep.get("Allow", "").strip("()").replace("|", ", "),
                "transport": ep.get("Transport", ""), "state": e.get("DeviceState", ""),
                "channels": num(e.get("ActiveChannels")),
                "match": ", ".join(re.sub(r"/255\.255\.255\.255", "", x.get("Match", "")) for x in identify),
            })
            continue
        endpoints.append({   # never the AuthDetail: it has the password
            "name": e["ObjectName"], "callerid": ep.get("Callerid", ""), "state": e.get("DeviceState", ""),
            "channels": num(e.get("ActiveChannels")), "context": ep.get("Context", ""),
            "transport": ep.get("Transport", ""), "encryption": ep.get("MediaEncryption", "no"),
            "codecs": ep.get("Allow", "").strip("()").replace("|", ", "),
            "messages": ep.get("MessageContext", ""), "mailboxes": aor.get("Mailboxes", ""),
            "max_contacts": aor.get("MaxContacts", ""),
        })
    contacts = []
    for c in raw["contacts"]:
        uri = c.get("Uri", "")
        m = re.search(r"transport=(\w+)", uri, re.I)
        contacts.append({
            "endpoint": c.get("Endpoint", ""), "ip": c.get("ViaAddr", ""), "port": c.get("ViaPort", ""),
            "transport": (m.group(1) if m else "udp").upper(), "status": c.get("Status", ""),
            "rtt_ms": round(num(c.get("RoundtripUsec")) / 1000), "user_agent": c.get("UserAgent", ""),
            "expires": num(c.get("ExpirationTime")), "push": "pn-prid" in uri,
        })
    dialplan = {}
    for d in raw["dialplan"]:
        if d.get("Registrar") != "pbx_config" or not d.get("Extension"):
            continue
        ctx = dialplan.setdefault(d["Context"], {})
        ctx.setdefault(d["Extension"], []).append({"priority": d.get("Priority", ""), "app": d.get("Application", ""),
                                                   "data": d.get("AppData", "")})
    out.update(
        endpoints=endpoints, trunks=trunks, contacts=contacts,
        subscriptions=[{"endpoint": s.get("Endpoint", ""), "resource": s.get("Resource", ""),
                        "event": s.get("Event", s.get("SubscriptionType", "")), "expiry": s.get("Expiry", "")}
                       for s in raw["subscriptions"]],
        channels=[{"channel": c.get("Channel", ""), "caller": c.get("CallerIDNum", ""), "exten": c.get("Exten", ""),
                   "context": c.get("Context", ""), "app": c.get("Application", ""), "data": c.get("ApplicationData", ""),
                   "duration": c.get("Duration", ""), "state": c.get("ChannelStateDesc", "")} for c in raw["channels"]],
        dialplan=[{"context": ctx, "extensions": [{"exten": x, "steps": steps} for x, steps in exts.items()]}
                  for ctx, exts in dialplan.items()],
        transports=[{"name": m.group(1), "protocol": m.group(2), "bind": m.group(3)}
                    for m in re.finditer(r"Transport:\s+(\S+)\s+(\w+)\s+\d+\s+\d+\s+(\S+)",
                                         transports if isinstance(transports, str) else "")
                    if not m.group(1).startswith("<")],
        asterisk={} if isinstance(core, Exception) else {
            "version": "" if isinstance(settings, Exception) else settings.get("AsteriskVersion", ""),
            "started": f"{core.get('CoreStartupDate', '')} {core.get('CoreStartupTime', '')}",
            "reloaded": f"{core.get('CoreReloadDate', '')} {core.get('CoreReloadTime', '')}",
            "calls": num(core.get("CoreCurrentCalls")), "processed": num(core.get("CoreProcessedCalls")),
        },
    )
    mailbox = bridge.mailbox()
    try:
        mwi = await conn.list({"Action": "MWIGet", "Mailbox": mailbox})
        out["mwi"] = [{"mailbox": m.get("Mailbox", ""), "new": num(m.get("NewMessages")),
                       "old": num(m.get("OldMessages"))} for m in mwi]
    except Exception:
        out["mwi"] = []
    return out


def listening_ports():
    """TCP ports listening on this host (the web page runs with host networking)."""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh)
                for line in fh:
                    parts = line.split()
                    if parts[3] == "0A":
                        ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        except (OSError, StopIteration, IndexError, ValueError):
            pass
    return ports


def reachable(url, timeout=1.5):
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        return False
    try:
        with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout):
            return True
    except OSError:
        return False


def voice_label(want):
    """How a [voices] entry reads: a voice's name, or a group's voices that are on."""
    if not want.startswith("@"):
        return voice_name(want)
    members = [s for s in config.section("voices").get("groups", {}).get(want[1:], []) if tts.enabled(s)]
    return either(members, want[1:]) if members else f"{want[1:]} (no voices on)"


def voice_name(spec):
    return config.section("voices").get("names", {}).get(spec) or spec.split("/", 1)[-1]


def either(specs, what):
    """"Poppy or Spike", "Ann, Bob or Cee", or "<what> (5 voices)"."""
    names = [voice_name(s) for s in specs]
    if len(names) > 3:
        return f"{what} ({len(names)} voices)"
    return " or ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else names[0]


def port_voices(c):
    """{bridge port: ["voice: ...", ...]}: who speaks on calls to it."""
    v, b = c["voices"], c["bridge"]
    operator = voice_label(v.get("operator") or tts.default_voice())
    pool = [s for s in v.get("pool") or [] if tts.enabled(s)]
    sessions = (voice_label(v["default"]) if v.get("default") else either(pool, "pool") if pool
                else "the operator's")
    out = {b["echo_port"]: [f"voice: {operator}"], b["voicemail_port"]: [f"voice: {operator}"],
           b["operator_port"]: [f"voice: {operator}", f"sessions: {sessions}"]}
    for app in b.get("apps", {}).values():
        if app.get("port"):
            out[app["port"]] = [f"voice: {voice_label(app['voice']) if app.get('voice') else operator}"]
    return out


def services():
    """The bridge's ports, the gateway, speech-to-text and text-to-speech."""
    c = config.get()
    b = c["bridge"]
    up = listening_ports()
    ports = {"echo (601)": b["echo_port"], "operator (200)": b["operator_port"],
             "voicemail (201)": b["voicemail_port"],
             **{f"{name} app": app.get("port") for name, app in b.get("apps", {}).items()}}
    voices = port_voices(c)
    out = {"bridge": [{"name": n, "port": p, "up": p in up, "voices": voices.get(p, [])} for n, p in ports.items() if p]}
    gw = {"url": b["gateway_url"], "up": False, "detail": ""}
    try:
        code, body = bridge.gateway().call("GET", "/info")
        gw.update(up=code == 200, detail=", ".join(f"{k}: {v}" for k, v in body.items() if v))
    except Exception as e:
        gw["detail"] = str(e)
    out["gateway"] = gw
    stt = c["stt"]
    stt_url = stt.get("url", "")
    out["stt"] = {"engine": stt.get("engine", ""), "url": stt_url,
                  "up": reachable(stt_url) if stt_url.startswith("http") else None}
    out["tts"] = [{"name": n, "type": e.get("type", n), "url": e.get("url", ""),
                   "up": reachable(e["url"]) if str(e.get("url", "")).startswith("http") else None}
                  for n, e in c["tts"].get("engines", {}).items()]
    out["vad"] = c["vad"].get("engine", "energy")
    out["keywords"] = c["keywords"].get("engine", "")
    out["notify"] = c["notify"].get("via", [])
    return out


# Logs

def log_paths(source):
    c = web_conf()
    return {"asterisk": config.expand(c["asterisk_log"]), "bridge": config.expand(c["bridge_log"])}.get(source)


def tail(path, lines):
    """The last lines of a file (reading back from the end)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = size = f.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= lines:
                step = min(65536, end)
                end -= step
                f.seek(end)
                data = f.read(step) + data
            return data.decode(errors="replace").splitlines()[-lines:], size
    except OSError:
        return [], 0


def follow_file(path, start, emit, stop):
    """Emit lines added to path after offset start, through rotation, until stop is set."""
    pos, ino = start, None
    try:
        ino = os.stat(path).st_ino
    except OSError:
        pass
    buf = b""
    while not stop.is_set():
        try:
            st = os.stat(path)
            if st.st_ino != ino or st.st_size < pos:   # rotated or truncated: start the new file
                ino, pos, buf = st.st_ino, 0, b""
            if st.st_size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(st.st_size - pos)
                pos += len(data)
                buf += data
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    emit(line.decode(errors="replace"))
        except OSError:
            pass
        stop.wait(0.5)


def rotate_loop():
    """Keep Asterisk's full.log small: rotate it past [web] rotate_mb, keep three old ones. And
    keep the call log to [web] calls_keep_days."""
    while True:
        try:
            path = config.expand(web_conf()["asterisk_log"])
            if os.path.getsize(path) > web_conf()["rotate_mb"] * 1_000_000:
                ami(ami_command("logger rotate"))
                log.info("rotated %s", path)
            for old in os.listdir(os.path.dirname(path)):
                m = re.fullmatch(re.escape(os.path.basename(path)) + r"\.(\d+)", old)
                if m and int(m.group(1)) >= 3:
                    os.remove(os.path.join(os.path.dirname(path), old))
        except Exception as e:
            log.debug("rotate: %s", e)
        try:
            prune_calls()
        except Exception as e:
            log.warning("call log: %s", e)
        time.sleep(60)


# Configuration

def asterisk_dir():
    return config.expand(web_conf()["asterisk_dir"])


def config_file(kind, name):
    """The path of an editable file, or None if it isn't one."""
    if kind == "asterisk":
        if not re.fullmatch(r"[\w.-]+\.conf", name):
            return None
        return os.path.join(asterisk_dir(), name)
    if kind == "token-ring" and name == "config.toml":
        return config.path() or os.path.join(config.LIVE_DIR, "config.toml")
    return None


def image_copy(kind, name):
    base = asterisk_dir() if kind == "asterisk" else config.LIVE_DIR
    return os.path.join(base, ".image", name)


def redact(kind, text):
    if kind == "asterisk":
        return re.sub(r"(?im)^(\s*(?:secret|password)\s*=\s*)\S.*$", r"\1••••••", text)
    return re.sub(r"(?m)^(\s*[\w-]*?(?:secret|password|passwd|token|api_key)\s*=\s*)\"[^\"]*\"",
                  r'\1"••••••"', text)


def list_config():
    files = []
    d = asterisk_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".conf"))
    except OSError:
        names = []
    for kind, name in [("token-ring", "config.toml")] + [("asterisk", n) for n in names]:
        p = config_file(kind, name)
        try:
            text = open(p, "rb").read()
        except OSError:
            continue
        base = image_copy(kind, name)
        files.append({"kind": kind, "name": name, "size": len(text), "empty": not text.strip(),
                      "edited": os.path.exists(base) and open(base, "rb").read() != text,
                      "image_new": os.path.exists(p + ".image-new"), "backups": len(backups(kind, name))})
    return files


def backups(kind, name):
    d = os.path.join(os.path.dirname(config_file(kind, name)), ".backup")
    try:
        return sorted((n for n in os.listdir(d) if n.startswith(name + ".")), reverse=True)
    except OSError:
        return []


def save_config(kind, name, text):
    """Write a config file in place (it may be a bind mount), keeping the old one in .backup.
    Returns what reloading it said."""
    path = config_file(kind, name)
    if kind == "token-ring":
        tomllib.loads(text)   # raises on a syntax error: nothing is written
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if old == text:
        return {"changed": False, "reload": ""}
    bdir = os.path.join(os.path.dirname(path), ".backup")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, f"{name}.{time.strftime('%Y%m%d-%H%M%S')}"), "w", encoding="utf-8") as f:
        f.write(old)
    for b in backups(kind, name)[20:]:
        os.remove(os.path.join(bdir, b))
    with open(path, "r+" if os.path.exists(path) else "w", encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
    log.info("saved %s %s", kind, name)
    if kind != "asterisk":
        return {"changed": True, "reload": "token-ring reads it again by itself"}
    return {"changed": True, **reload_asterisk(name)}


# Allowed callers: who may call in on the trunks. callers.conf (included by extensions.conf) has
# them as extensions of [allowed-callers], which the dialplan's [caller-allowed] checks the
# caller's number against. Numbers from your own country (the dialplan's COUNTRY_CODE) are kept
# in national form (0...), others as +<country code>..., as the dialplan turns caller IDs into.

CALLERS_FILE = "callers.conf"
CALLER_LINE = re.compile(r"^\s*exten\s*=>\s*([+\d]+)\s*,\s*1\s*,[^;\n]*(?:;\s*(.*?))?\s*$", re.M)


def normalise_number(number, country):
    n = re.sub(r"[\s().\-/]", "", number)
    if not re.fullmatch(r"\+?\d{3,17}", n):
        raise ValueError(f"{number!r} isn't a phone number")
    if n.startswith("+"):
        n = "00" + n[1:]
    if country and n.startswith("00" + country):
        n = "0" + n[2 + len(country):]
    elif country and n.startswith(country) and len(n) >= len(country) + 9:
        n = "0" + n[len(country):]    # the country code without the + (some networks send it so)
    if n.startswith("00"):
        n = "+" + n[2:]
    return n


async def country_code():
    try:
        resp = await bridge.ami_action({"Action": "Getvar", "Variable": "GLOBAL(COUNTRY_CODE)"}, login=ami_login())
        return re.sub(r"\D", "", resp.get("Value") or "")
    except Exception:
        return ""


def read_callers():
    path = os.path.join(asterisk_dir(), CALLERS_FILE)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return {"exists": False, "callers": []}
    return {"exists": True, "callers": [{"number": n, "name": name or ""} for n, name in CALLER_LINE.findall(text)]}


def write_callers(callers, country):
    seen, lines = set(), []
    for c in callers:
        number = normalise_number(str(c.get("number", "")), country)
        if number in seen:
            continue
        seen.add(number)
        name = " ".join(re.sub(r"[;\r\n]+", " ", str(c.get("name", ""))).split())
        lines.append(f"exten => {number},1,NoOp()" + (f" ; {name}" if name else ""))
    text = ("; Who may call in on the trunks: the dialplan's [caller-allowed] looks the caller's number up\n"
            "; here. Numbers from this country are in national form (0...), others +<country code>...\n"
            "; The web page's Allowed callers box writes this file: one number per line, its name after ;\n"
            "[allowed-callers]\n" + "".join(l + "\n" for l in lines))
    return save_config("asterisk", CALLERS_FILE, text)


# The call log: Asterisk's call records (cdr_custom.conf writes calls.csv, a line per call), newest
# first, with names from the allowed callers. Which calls are in, out or internal is worked out
# on the page, which knows the trunks.

CALL_FIELDS = ["start", "answer", "end", "duration", "billsec", "disposition", "src", "clid", "dst",
               "dcontext", "channel", "dstchannel", "lastapp", "uniqueid", "to", "screen"]


def read_calls(limit=100):
    path = config.expand(web_conf()["calls_log"])
    try:
        with open(path, "rb") as f:
            start = max(0, os.path.getsize(path) - 400 * (limit + 1))   # about enough lines, from the end
            f.seek(start)
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()[1 if start else 0:][-limit:]     # (from the middle: not the part-line)
    country = None
    names = {}
    calls = []
    for row in csv.reader(lines):
        if len(row) < len(CALL_FIELDS):
            continue
        c = dict(zip(CALL_FIELDS, row))
        for k in ("start", "answer", "end"):
            c[k] = float(c[k] or 0) or None
        for k in ("duration", "billsec"):
            c[k] = int(c[k] or 0)
        m = re.match(r'"?(.*?)"?\s*<[^>]*>$', c["clid"])
        c["caller_name"] = m.group(1) if m else ""
        # who's who: the caller, and for calls the PBX placed through a trunk, who it rang
        if country is None:
            country = ami(country_code())
            names = {x["number"]: x["name"] for x in read_callers()["callers"]}
        c["name"] = lookup_name(c["src"], country, names)
        rung = re.match(r"PJSIP/([^@]+)@", c["to"])
        c["to_number"] = rung.group(1) if rung else ""
        c["to_name"] = lookup_name(c["to_number"], country, names)
        calls.append(c)
    return calls[::-1]


def prune_calls(now=None):
    """Drop call records older than [web] calls_keep_days (0: keep them all). Returns how many."""
    days = web_conf()["calls_keep_days"]
    path = config.expand(web_conf()["calls_log"])
    if not days or not os.path.exists(path):
        return 0
    cutoff = (now or time.time()) - days * 86400

    def start_of(line):
        try:
            return float(next(csv.reader([line]))[0] or 0)
        except (ValueError, IndexError, StopIteration):
            return cutoff   # not a record: keep it
    with open(path, encoding="utf-8", errors="replace") as f:
        if start_of(f.readline()) >= cutoff:
            return 0        # the oldest is recent enough: nothing to do
    for _ in range(3):      # Asterisk appends a line per call: start again if one lands meanwhile
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        keep = [l for l in lines if start_of(l) >= cutoff]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        if os.path.getsize(path) == size:
            os.replace(tmp, path)
            log.info("call log: dropped %d records older than %d days", len(lines) - len(keep), days)
            return len(lines) - len(keep)
    os.remove(tmp)
    return 0


def lookup_name(number, country, names):
    try:
        return names.get(normalise_number(number, country), "") if number else ""
    except ValueError:
        return ""


def reload_asterisk(name):
    """Reload what a file belongs to; returns what Asterisk said, and the warnings it logged."""
    command = RELOADS.get(name, "core reload")
    if command is None:
        return {"reload": f"{name} only takes effect when Asterisk restarts", "restart": True}
    path = config.expand(web_conf()["asterisk_log"])
    _, before = tail(path, 0)
    said = ami(ami_command(command))
    time.sleep(1)
    lines = []
    try:
        with open(path, "rb") as f:
            f.seek(before)
            lines = [l for l in f.read().decode(errors="replace").splitlines()
                     if re.search(r"WARNING|ERROR", l)]
    except OSError:
        pass
    return {"reload": f"{command}: {said.strip() or 'done'}", "warnings": lines[-30:]}


# Voices ([voices] in config.toml): what each engine offers, and who speaks in which. The Voices
# tab edits the [voices] tables, rewriting just them (their comments go; the rest of the file
# is kept as it is).

VOICE_KEYS = ("operator", "default", "rate", "disabled", "introduce", "groups", "sessions", "names", "rates")
GROUP_NAME = re.compile(r"[\w-]{1,40}")


def read_voices():
    """[voices], as the Voices tab edits it. The older pool (sessions hashed onto it) shows as a
    group, "pool", that sessions use: saving makes that the default (a random one each call)."""
    v = config.section("voices")
    out = {"operator": v.get("operator", ""), "default": v.get("default", ""), "rate": v.get("rate", 1.0),
           "disabled": list(v.get("disabled", [])), "introduce": list(v.get("introduce", v.get("names", {}))),
           "groups": dict(v.get("groups", {})),
           "sessions": dict(v.get("sessions", {})), "names": dict(v.get("names", {})),
           "rates": dict(v.get("rates", {}))}
    if v.get("pool") and not out["default"]:
        out["groups"].setdefault("pool", list(v["pool"]))
        out["default"] = "@pool"
    return out


def voice_sessions():
    """The sessions the gateway knows ([{"key", "label", "line", "status"}]); [] if it's down."""
    try:
        code, body = bridge.gateway().call("GET", "/sessions")
        return [{"key": x["key"], "label": x.get("label", x["key"]), "line": x.get("line", ""),
                 "status": x.get("status", "")} for x in body] if code == 200 else []
    except Exception:
        return []


def toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if len(v) > 2:
        return "[\n" + "".join(f"  {toml_value(x)},\n" for x in v) + "]"
    return "[" + ", ".join(toml_value(x) for x in v) + "]"


def voices_toml(v):
    out = ["[voices]", "# Edited on the web page's Voices tab (saving there rewrites these tables, and their comments)"]
    out += [f"{k} = {toml_value(v[k])}" for k in ("operator", "default", "rate", "disabled", "introduce")]
    for table in ("groups", "sessions", "names", "rates"):
        out.append(f"[voices.{table}]")
        out += [f"{toml_key(k)} = {toml_value(x)}" for k, x in v[table].items()]
    return "\n".join(out) + "\n"


def check_voices(v):
    """The Voices tab's settings, checked and tidied; raises ValueError."""
    if not isinstance(v, dict):
        raise ValueError("voices required")
    v = {k: v.get(k) for k in VOICE_KEYS}
    text = lambda x: isinstance(x, str) and "\n" not in x and len(x) <= 200
    v["operator"], v["default"] = v["operator"] or "", v["default"] or ""
    v["disabled"] = v["disabled"] or []
    for k in ("groups", "sessions", "names", "rates"):
        v[k] = v[k] or {}
        if not isinstance(v[k], dict):
            raise ValueError(f"{k} isn't a table")
    try:
        v["rate"] = float(v["rate"] if v["rate"] is not None else 1.0)
        v["rates"] = {k: float(x) for k, x in v["rates"].items()}
    except (TypeError, ValueError):
        raise ValueError("speaking rates are numbers")
    if not all(0.3 <= r <= 3 for r in [v["rate"], *v["rates"].values()]):
        raise ValueError("speaking rates are between 0.3 and 3")
    for name, members in v["groups"].items():
        if not GROUP_NAME.fullmatch(name):
            raise ValueError(f"group names are letters, digits, - and _: {name!r}")
        if not isinstance(members, list) or not all(map(text, members)):
            raise ValueError(f"group {name}: a list of voices")
        v["groups"][name] = list(dict.fromkeys(members))
    for k in ("disabled", "introduce"):
        v[k] = v[k] or []
        if not isinstance(v[k], list) or not all(map(text, v[k])):
            raise ValueError(f"{k}: a list of voices")
    v["names"] = {k: " ".join(x.split()) for k, x in v["names"].items() if text(k) and text(x) and x.strip()}
    v["introduce"] = [s for s in dict.fromkeys(v["introduce"]) if s in v["names"]]
    for who, want in [("operator", v["operator"]), ("default", v["default"]), *v["sessions"].items()]:
        if not text(who) or not text(want):
            raise ValueError(f"{who}: a voice or @group")
        if want.startswith("@") and want[1:] not in v["groups"]:
            raise ValueError(f"{who}: there's no group {want[1:]!r}")
    v["sessions"] = {k: x for k, x in v["sessions"].items() if x}
    return v


def splice_table(text, name, section):
    """text (TOML) with table name and its subtables replaced by section (appended if missing)."""
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]", line)
        if not m:
            continue
        inside = m.group(1) == name or m.group(1).startswith(name + ".")
        if start is None and inside:
            start = i
        elif start is not None and end is None and not inside:
            end = i
        elif end is not None and inside:
            raise ValueError(f"[{name}] is split up in the file: put its tables together first")
    if start is None:
        return text.rstrip("\n") + "\n\n" + section
    end = len(lines) if end is None else end
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1   # comments just before the next table are that table's
    return "".join(lines[:start]) + section + ("\n" if end < len(lines) else "") + "".join(lines[end:])


def write_voices(v):
    v = check_voices(v)
    path = config_file("token-ring", "config.toml")
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    new = splice_table(old, "voices", voices_toml(v))
    got = tomllib.loads(new).get("voices", {})
    if any(got.get(k, v[k]) != v[k] for k in VOICE_KEYS) or "pool" in got:
        raise ValueError("the [voices] tables didn't come out as meant: edit config.toml by hand")
    return save_config("token-ring", "config.toml", new)


def voice_sample(voice, rate, text):
    """WAV of text said in voice, at rate."""
    return to_wav(voice_pcm(voice, rate, text))


def voice_pcm(voice, rate, text):
    name, inner = tts.split(voice)
    chunks, stop = [], threading.Event()
    tts.engine(name).synthesize(text[:300], inner, rate, chunks.append, stop)
    return b"".join(chunks)


# Names for voices that haven't got one, by how the voice sounds: a low voice (below
# PITCH_SPLIT_HZ) gets a man's name. 153 Hz splits Piper's 109 VCTK speakers by their recorded
# gender (the men's at most 150 Hz, the women's at least 156).
PITCH_SPLIT_HZ = 153
FEMALE_NAMES = """Abigail Ada Alice Amelia Anna Beatrice Bethany Caitlin Charlotte Chloe Clara Daisy
Eleanor Eliza Ellie Emily Erin Esme Evie Fiona Florence Freya Georgia Grace Hannah Harriet Holly
Imogen Isla Ivy Jasmine Jessica Juliet Katie Lily Lucy Lydia Maisie Martha Matilda Megan Millie
Molly Nancy Niamh Olivia Penny Phoebe Rachel Rosie Ruby Sophie Stella Tilly Violet Willow Zara""".split()
MALE_NAMES = """Albert Alfie Angus Archie Arthur Barnaby Ben Callum Charlie Dan Declan Dominic Ed
Elliot Ewan Felix Finn Fraser Freddie George Gethin Harry Henry Hugh Isaac Jack Jacob Jamie Joe
Jonah Kit Leo Louis Luke Max Miles Nathan Ned Noah Oliver Oscar Owen Patrick Rhys Robin Rory Rufus
Sam Seb Stanley Theo Tobias Tom Will Wilf Zach""".split()


def suggest_names(voices, taken, rng=None):
    """{voice: {"name", "pitch_hz", "sounds"}} for voices (a list), each name new: not in taken,
    nor given to another of them. Each voice says a sentence so its pitch can be measured."""
    import random
    rng = rng or random.Random()
    used = {n.lower() for n in taken}
    out = {}
    for voice in voices:
        try:
            hz = pitch_hz(voice_pcm(voice, 1.0, "Hello there, this is how my voice sounds today."))
        except Exception as e:
            out[voice] = {"error": str(e)}
            continue
        sounds = "male" if 0 < hz < PITCH_SPLIT_HZ else "female"
        pool = [n for n in (MALE_NAMES if sounds == "male" else FEMALE_NAMES) if n.lower() not in used]
        pool = pool or [n for n in MALE_NAMES + FEMALE_NAMES if n.lower() not in used] or ["Voice"]
        name = rng.choice(pool)
        used.add(name.lower())
        out[voice] = {"name": name, "pitch_hz": round(hz), "sounds": sounds}
    return out


# Contacts (contacts.py): the address books the phones read

def directory_source():
    """(phones [(extension, name)], sessions) for the directory."""
    st = ami(asterisk_status())
    phones = [(ep["name"], (re.match(r'\s*"([^"]*)"', ep.get("callerid", "")) or [None, ""])[1])
              for ep in st.get("endpoints", [])]
    return phones, voice_sessions()


def contacts_host():
    return config.section("provision").get("name") or urllib.parse.urlsplit(contacts.conf()["url"]).hostname or ""


def contacts_view():
    if not contacts.enabled():
        return {"enabled": False}
    contacts.ensure_books()
    c = contacts.conf()
    return {"enabled": True, "people": contacts.people(),
            "directory": sorted((contacts.parse(t) for t in contacts.cards("directory").values()),
                                key=lambda x: x["sips"][0] if x["sips"] else ""),
            "urls": {b: contacts.book_url(b) for b in contacts.BOOKS}, "reader": c["reader"]}


# The server

class Handler(BaseHTTPRequestHandler):
    server_version = "token-ring-web"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length)) if length else {}
        except ValueError:
            return {}

    def sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(line):
            self.wfile.write(b"data: " + json.dumps(line).encode() + b"\n\n")
            self.wfile.flush()
        return emit

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self.reply(200, resources.files("token_ring").joinpath("web.html").read_text(), "text/html")
            if path == "/api/status":
                status = ami(asterisk_status())
                status["services"] = services()
                status["host"] = socket.gethostname()
                status["time"] = time.time()
                status["sessions"] = voice_sessions()
                return self.reply(200, status)
            if path == "/api/logs":
                return self.logs(q.get("source", "asterisk"), int(q.get("lines", 300)), q.get("follow") == "1")
            if path == "/api/config":
                return self.reply(200, {"files": list_config()})
            if path == "/api/calls":
                return self.reply(200, {"calls": read_calls(min(int(q.get("limit", 100)), 1000))})
            if path == "/api/callers":
                return self.reply(200, {**read_callers(), "country_code": ami(country_code())})
            if path == "/api/contacts":
                try:
                    return self.reply(200, contacts_view())
                except Exception as e:
                    return self.reply(200, {"enabled": True, "error": str(e)})
            if path == "/api/voices":
                voices, errors = tts.available()
                return self.reply(200, {"config": read_voices(), "available": voices, "errors": errors,
                                        "sessions": voice_sessions(), "operator_now": tts.voice_for("operator")})
            m = re.fullmatch(r"/api/config/(asterisk|token-ring)/([\w.-]+)", path)
            if m:
                kind, name = m.groups()
                p = config_file(kind, name)
                if not p or not os.path.exists(p):
                    return self.reply(404, {"error": "no such file"})
                text = open(p, encoding="utf-8", errors="replace").read()
                image = image_copy(kind, name)
                return self.reply(200, {
                    "kind": kind, "name": name, "path": p,
                    "content": text if q.get("raw") == "1" else redact(kind, text),
                    "image": (open(image, encoding="utf-8", errors="replace").read() if os.path.exists(image) else None)
                    if q.get("raw") == "1" else None,
                    "backups": backups(kind, name), "reload": RELOADS.get(name, "core reload") if kind == "asterisk" else None})
            m = re.fullmatch(r"/api/config/(asterisk|token-ring)/([\w.-]+)/backup/([\w.-]+)", path)
            if m:
                kind, name, b = m.groups()
                if b not in backups(kind, name):
                    return self.reply(404, {"error": "no such backup"})
                d = os.path.join(os.path.dirname(config_file(kind, name)), ".backup")
                return self.reply(200, {"content": open(os.path.join(d, b), encoding="utf-8").read()})
            return self.reply(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as e:
            log.warning("GET %s: %s", path, e)
            try:
                self.reply(500, {"error": str(e)})
            except OSError:
                pass

    def do_PUT(self):
        if urllib.parse.urlsplit(self.path).path == "/api/contacts":
            try:
                uid = contacts.save(self.body().get("contact") or {})
                return self.reply(200, {"uid": uid, **contacts_view()})
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                log.warning("save contact: %s", e)
                return self.reply(500, {"error": str(e)})
        if urllib.parse.urlsplit(self.path).path == "/api/voices":
            try:
                return self.reply(200, {**write_voices(self.body().get("voices")), "config": read_voices()})
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                log.warning("save voices: %s", e)
                return self.reply(500, {"error": str(e)})
        if urllib.parse.urlsplit(self.path).path == "/api/callers":
            callers = self.body().get("callers")
            if not isinstance(callers, list):
                return self.reply(400, {"error": "callers required"})
            try:
                return self.reply(200, {**write_callers(callers, ami(country_code())), **read_callers()})
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                log.warning("save callers: %s", e)
                return self.reply(500, {"error": str(e)})
        m = re.fullmatch(r"/api/config/(asterisk|token-ring)/([\w.-]+)", urllib.parse.urlsplit(self.path).path)
        if not m or not config_file(*m.groups()):
            return self.reply(404, {"error": "not found"})
        content = self.body().get("content")
        if not isinstance(content, str):
            return self.reply(400, {"error": "content required"})
        if "••••••" in content:
            return self.reply(400, {"error": "that's the masked copy: switch on Advanced and load the file again"})
        try:
            return self.reply(200, save_config(*m.groups(), content))
        except tomllib.TOMLDecodeError as e:
            return self.reply(400, {"error": f"not valid TOML: {e}"})
        except Exception as e:
            log.warning("save %s: %s", self.path, e)
            return self.reply(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/contacts/delete":
                uid = str(self.body().get("uid", ""))
                if not re.fullmatch(r"[\w.@-]{1,100}", uid):
                    return self.reply(400, {"error": "uid required"})
                contacts.delete("contacts", uid)
                return self.reply(200, contacts_view())
            if path == "/api/contacts/import-callers":
                have = {re.sub(r"\D", "", t) for p in contacts.people() for t in p["tels"]}
                added = 0
                for c in read_callers()["callers"]:
                    if re.sub(r"\D", "", c["number"]) not in have:
                        contacts.save({"name": c["name"] or c["number"], "tels": [c["number"]]})
                        added += 1
                return self.reply(200, {"added": added, **contacts_view()})
            if path == "/api/voices/names":
                b = self.body()
                voices, taken = b.get("voices"), b.get("taken") or []
                if not isinstance(voices, list) or not all(isinstance(v, str) for v in voices) or len(voices) > 50:
                    return self.reply(400, {"error": "voices: a list of up to 50"})
                return self.reply(200, {"names": suggest_names(voices, [str(t) for t in taken])})
            if path == "/api/voices/sample":
                b = self.body()
                if not isinstance(b.get("voice"), str) or not b["voice"]:
                    return self.reply(400, {"error": "voice required"})
                return self.reply(200, voice_sample(b["voice"], float(b.get("rate") or 1.0),
                                                    str(b.get("text") or "Hello. This is how I sound.")), "audio/wav")
            m = re.fullmatch(r"/api/asterisk/(reload|restart)", path)
            if m:
                if m.group(1) == "restart":
                    said = ami(ami_command("core restart gracefully"))
                    return self.reply(200, {"reload": said or "restarting when no calls are up"})
                what = self.body().get("file", "")
                return self.reply(200, reload_asterisk(what) if what else {"reload": ami(ami_command("core reload"))})
            return self.reply(404, {"error": "not found"})
        except Exception as e:
            log.warning("POST %s: %s", path, e)
            return self.reply(500, {"error": str(e)})

    def logs(self, source, lines, follow):
        emit = self.sse() if follow else None
        stop = threading.Event()
        try:
            if source == "gateway":
                if not follow:
                    got = []
                    gstop = threading.Event()

                    def take(ev):
                        if "line" in ev:
                            got.append(ev["line"])
                    bridge.gateway().follow(f"/logs?lines={lines}", take, gstop)
                    return self.reply(200, {"lines": got})

                def relay(ev):
                    if "line" in ev:
                        emit(ev["line"])
                    elif "lost" in ev:
                        emit("(the gateway's log stream ended)")
                        stop.set()
                    else:
                        emit(None)   # a ping: notices a closed browser
                bridge.gateway().follow(f"/logs?lines={lines}&follow=1", relay, stop)
                return
            path = log_paths(source)
            if not path:
                return self.reply(404, {"error": "no such log"})
            got, size = tail(path, lines)
            if not follow:
                return self.reply(200, {"lines": got})
            for line in got:
                emit(line)
            pinger = threading.Thread(target=self.ping, args=(emit, stop), daemon=True)
            pinger.start()
            follow_file(path, size, emit, stop)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, ValueError):
            stop.set()

    def ping(self, emit, stop):
        while not stop.wait(15):
            try:
                emit(None)
            except (OSError, ValueError):
                stop.set()


class Server(ThreadingHTTPServer):
    """Threaded, with the TLS handshake in each request's thread (a slow client can't hold up the
    rest)."""
    daemon_threads = True
    context = None

    def finish_request(self, request, client_address):
        if self.context:
            try:
                request.settimeout(10)
                request = self.context.wrap_socket(request, server_side=True)
                request.settimeout(None)
            except (ssl.SSLError, OSError):
                return
        super().finish_request(request, client_address)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    config.seed()
    c = web_conf()
    server = Server((c["listen"], c["port"]), Handler)
    cert, key = config.expand(c["cert_file"]), config.expand(c["key_file"])
    scheme = "http"
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        server.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server.context.load_cert_chain(cert, key)
        scheme = "https"
    threading.Thread(target=rotate_loop, daemon=True).start()
    if contacts.enabled():
        threading.Thread(target=contacts.directory_loop, args=(directory_source, contacts_host()), daemon=True).start()
    log.info("web page on %s://%s:%d (no login: keep it to your LAN or VPN)", scheme, c["listen"], c["port"])
    server.serve_forever()


if __name__ == "__main__":
    main()
