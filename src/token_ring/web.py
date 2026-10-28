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
<dir>/.backup, and reloads what changed.

There's no login yet: keep it to your LAN or VPN.
"""
import asyncio
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

from . import bridge, config

log = logging.getLogger("token-ring")

SECRET_KEY = re.compile(r"(secret|password|passwd|token|api_key)$", re.I)
# What to reload after saving an Asterisk file (anything else: "core reload")
RELOADS = {
    "pjsip.conf": "module reload res_pjsip.so",
    "extensions.conf": "dialplan reload",
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


def services():
    """The bridge's ports, the gateway, speech-to-text and text-to-speech."""
    c = config.get()
    b = c["bridge"]
    up = listening_ports()
    ports = {"echo (601)": b["echo_port"], "operator (200)": b["operator_port"],
             "voicemail (201)": b["voicemail_port"],
             **{f"{name} app": app.get("port") for name, app in b.get("apps", {}).items()}}
    out = {"bridge": [{"name": n, "port": p, "up": p in up} for n, p in ports.items() if p]}
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
    """Keep Asterisk's full.log small: rotate it past [web] rotate_mb, keep three old ones."""
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
                return self.reply(200, status)
            if path == "/api/logs":
                return self.logs(q.get("source", "asterisk"), int(q.get("lines", 300)), q.get("follow") == "1")
            if path == "/api/config":
                return self.reply(200, {"files": list_config()})
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
    log.info("web page on %s://%s:%d (no login: keep it to your LAN or VPN)", scheme, c["listen"], c["port"])
    server.serve_forever()


if __name__ == "__main__":
    main()
