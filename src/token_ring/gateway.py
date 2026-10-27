"""The gateway: runs beside the AI ([gateway] backend) and gives the bridge, and sessions on the
same machine, an HTTP API to it.

Interrupting is allowed (it stops the current turn and leaves background work running), but
nothing here cancels background work: when a call ends, its conversations finish what they're
doing before they exit. Permission prompts are never answered by phone (a command read aloud
can't be checked); other dialogs (the AI's questions, notices) can be, with the keypad.

  GET  /info                     {"assistant": what the AI is called, "host": this machine}
  POST /whoami                   {"pids": [ancestors, nearest first], "session": key or ""}:
                                 which session a local command runs under ({"key": k | null})

Conversations for one call (the operator, and new conversations started by phone):

  GET  /agents/events?dir=<key>  start one and stream its events: "operator", or a session key
                                 to start a new conversation in its directory. First event is
                                 {"agent": id}; then the backend's events (backends/__init__.py),
                                 {"connect": {"key" | "new": ...}} (the operator putting the
                                 caller through; `token-ring connect`), {"hangup": true}, and
                                 {"ping": 1}. The conversation is closed when the client goes
  POST /agents/<id>/send         {"text": "..."}
  POST /agents/<id>/interrupt    stop the current turn (background work keeps running)
  POST /agents/<id>/connect      {"key": k} or {"new": k}: from `token-ring connect`
  POST /agents/<id>/hangup       end the call once what's been said is spoken

Existing sessions:

  GET  /sessions                 [{"key", "name", "label", "status"}]
  POST /sessions/<key>/send      {"text"}: send it (with PREFIX), clearing unsent text first. 409
                                 with the dialog if one is showing
  POST /sessions/<key>/keys      {"key": "1".."9" | "Tab" | "Escape"}: a digit answers the dialog
                                 and Tab finishes a multi-select, never for a permission prompt;
                                 Escape interrupts, only while busy with no dialog. Returns
                                 {"dialog": the new one}
  POST /sessions/<key>/clear     empty its input of unsent text: {"cleared": text}
  POST /sessions/<key>/hangup    local only: end the call to that session once what's been said
                                 is spoken (`token-ring hangup`)
  GET  /sessions/<key>/events    the session's events for as long as the client stays connected,
                                 plus {"hangup": true} and {"ping": 1}

Outbound calls (`token-ring call`):

  POST /calls                    {"key", "message"}, local only: ring the phone; when answered
                                 the message is read out and the caller is talking to that
                                 session ("operator" if it isn't one). Returns {"id"}
  GET  /calls/<id>               {"status": queued | ringing | answered | no answer | failed}
  GET  /outbound                 the bridge's feed of calls to place: {"call": id, "key", "label",
                                 "message", "host", "assistant"}, {"ping": 1}
  POST /calls/<id>/status        {"status"}: the bridge reporting progress

Notifications ([notify]): when a session leaves voicemail, and every remind_every_minutes while
any is unheard, the user gets a text message on the phone (via = "phone": the bridge sends it
through Asterisk; the outbound feed carries {"text", "id"}) and/or an ntfy notification.

  POST /texts/<id>               {"status": "sent" | "unreachable" | "failed"}: the bridge
                                 reporting on a text
  POST /texts/reply              {"from", "to", "body"}: the user texted the PBX. Any text stops
                                 reminders until there's new voicemail (Asterisk can't pass on
                                 Linphone's read receipts, so a reply stands in for one); texts
                                 to [gateway] text_operator go to the text operator, whose
                                 replies are texted back from that extension
  POST /sessions/<key>/callback  {"text"}, local only: ask the session to ring the user back
                                 (`token-ring callback`); typed in when it's free, like voicemail

Voicemail (text: read out in the sender's voice). Messages for the user are in
<data_dir>/voicemail/inbox, theirs for sessions in outbox until delivered:

  GET  /voicemail                {"messages": [{"id", "from", "label", "text", "at", "heard"}],
                                 "new": n}, oldest first
  POST /voicemail                {"key", "message"}, local only (`token-ring voicemail`)
  POST /voicemail/<id>/heard     mark it heard (kept as saved)
  POST /voicemail/<id>/delete    delete it
  POST /voicemail/to/<key>       {"text"}: from the phone, a message for that session; sent with
                                 VM_PREFIX when it's up with no dialog showing

Only addresses in [gateway] allowed may connect; every request needs "Authorization: Bearer
<token>" if a token is set. With tls_cert and tls_key it serves HTTPS, reloading the certificate
when it changes.
"""
import glob
import hmac
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, plugins
from .text import spoken_label

MAX_TEXT = 2000
PING_EVERY_S = 5.0
VM_RETRY_S = 30
NOTIFY_RETRY_S = 60
TEXT_OPERATOR_IDLE_S = 600
TEXT_WAIT_S = 20

log = logging.getLogger("token-ring")


def gateway_conf():
    return config.section("gateway")


def user():
    return config.section("user")["name"]


def voice_style():
    return (f"You're talking to {user()} on a phone call. Their messages come from speech-to-text, "
            "so expect misheard words, especially names, and ask if something is unclear. Everything "
            "you write is read aloud by text-to-speech: answer in a few short spoken sentences, with "
            "no Markdown, lists, tables, code blocks, URLs or long paths. Before anything that will "
            "take a while, say in a sentence what you're doing; they hear a soft pip while you work. "
            "If they ask you to stop or cancel, use your judgement about what's safe to stop: never "
            "kill background tasks or throw away work in progress unless they confirm that's what "
            "they mean.")


def operator_prompt():
    return voice_style() + f"""

You're the phone operator for this machine's sessions. You can do general tasks, and you can \
put {user()} through to a session, which ends their conversation with you for this call (they \
call back to reach you):
- `token-ring list` lists the sessions (key, status, name).
- `token-ring connect <key>` connects them to that session's existing conversation (it waits \
for a session that's just been started).
- `token-ring connect --new <key>` starts a new conversation for them in that session's \
directory, alongside the existing one.
- `token-ring clear <key>` empties a session's input of unsent text.
- `token-ring hangup` ends the call once you've finished speaking: say goodbye first. Do it when \
they say they're done or ask you to hang up.
Tell them who you're putting them through to before you do it.
""" + gateway_conf().get("operator_prompt", "")


def session_prompt():
    return voice_style() + """

This is a new conversation started by phone. It's saved, and can be resumed later from this \
directory.
""" + gateway_conf().get("session_prompt", "")


def prefix():
    """Starts every phone message typed into a session, so it knows to answer for the ear."""
    return ("[Phone call. This message came through speech-to-text, so expect misheard words; "
            "reply in a few short spoken sentences, no Markdown] ")


def vm_prefix():
    return (f"[Voicemail from {user()}, through speech-to-text, so expect misheard words. If they "
            "want an answer, leave them one with `token-ring voicemail` (voicemail skill), or ring "
            "them with `token-ring call` if it's urgent] ")


def text_operator_prompt():
    return f"""You're the phone operator for this machine's sessions, reached by text message from \
{user()}'s phone. Whatever you write is sent back to them as one text message when you finish, so \
keep it to a sentence or two of plain text: no Markdown, lists or code. Mostly they'll want a \
session to ring them back:
- `token-ring list` lists the sessions (key, status, name).
- `token-ring callback <key> "<what they want to talk about>"` asks that session to ring them; \
it's passed on as soon as the session is free, and the session rings when it's ready.
If it's unclear which session they mean, ask. Say briefly what you did.
""" + gateway_conf().get("operator_prompt", "")


def callback_prefix():
    return (f"[Message from {user()}, sent by text through the phone operator: they'd like you to "
            "ring them back. Ring with `token-ring call \"...\"`, opening with what it's about, once "
            "you're ready; if it's late or it can wait, leave a voicemail instead] ")


def clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:MAX_TEXT]


class Gateway:
    def __init__(self):
        c = gateway_conf()
        self.data_dir = config.expand(c["data_dir"])
        self.vm_dir = os.path.join(self.data_dir, "voicemail")
        self.backend_conf = None
        self.backend_obj = None
        self.agents = {}            # id -> Conversation
        self.followers = {}         # session key -> [threading.Event] per call following it
        self.calls = {}             # id -> {"key", "label", "message", "status", "at"}
        self.outbound = []          # queues of connected bridges (normally one)
        self.lock = threading.Lock()
        self.vm_lock = threading.Lock()
        self.vm_deliver = threading.Event()
        self.notify_now = threading.Event()
        self.texts = {}             # id -> {"done": threading.Event, "status"}: texts the bridge is sending
        self.text_op = None         # the text operator: {"agent", "ended", "last", "ready"}

    @property
    def backend(self):
        """The configured backend (rebuilt if its config changes)."""
        c = config.get()
        name = c["gateway"]["backend"]
        conf = c["backends"].get(name, {})
        with self.lock:
            if self.backend_obj is None or self.backend_conf != (name, conf):
                self.backend_obj = plugins.load("backends", name)(conf, self)
                self.backend_conf = (name, conf)
            return self.backend_obj

    def sessions(self):
        return [dict(s, label=spoken_label(s["key"])) for s in self.backend.sessions()]

    def label(self, key):
        return "the operator" if key == "operator" else spoken_label(key)

    def known(self, key):
        return any(s["key"] == key for s in self.backend.sessions())

    # Voicemail

    def vm_save(self, box, item):
        os.makedirs(os.path.join(self.vm_dir, box), exist_ok=True)
        path = os.path.join(self.vm_dir, box, f"{item['id']}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(item, f, indent=1)
        os.replace(path + ".tmp", path)

    def vm_list(self, box):
        items = []
        for f in glob.glob(os.path.join(self.vm_dir, box, "*.json")):
            try:
                with open(f) as fh:
                    items.append(json.load(fh))
            except (OSError, ValueError):
                pass
        return sorted(items, key=lambda m: m["at"])

    def vm_deliver_loop(self):
        """Send the user's messages to their sessions once each is up with no dialog showing."""
        while True:
            self.vm_deliver.wait(VM_RETRY_S)
            self.vm_deliver.clear()
            for item in self.vm_list("outbox"):
                if item.get("delivered"):
                    continue
                try:
                    s = self.backend.session(item["to"])
                    if not s or s.dialog():
                        continue  # down, or waiting on the user: try again later
                    with self.vm_lock:
                        prefix = callback_prefix() if item.get("kind") == "callback" else vm_prefix()
                        if not s.send(clean(prefix + item["text"])):
                            continue
                        item["delivered"] = time.time()
                        self.vm_save("outbox", item)
                    log.info("voicemail %s delivered to %s", item["id"], item["to"])
                except Exception as e:
                    log.warning("voicemail %s: %s", item["id"], e)

    def vm_update(self, msg_id, change):
        """Re-read an inbox message, change(item) it and save it (under the lock, so a "heard" from
        the phone isn't lost). Returns the item, or None if it's gone."""
        path = os.path.join(self.vm_dir, "inbox", f"{msg_id}.json")
        with self.vm_lock:
            try:
                with open(path) as f:
                    item = json.load(f)
            except (OSError, ValueError):
                return None
            change(item)
            self.vm_save("inbox", item)
            return item

    # Notifications: a text to the phone (through the bridge) or ntfy when a session leaves
    # voicemail, and a reminder every so often while any is unheard. The inbox is the queue: each
    # message keeps "notified": {channel: time} once that channel has told the user about it.

    def notify_loop(self):
        while True:
            self.notify_now.wait(NOTIFY_RETRY_S)
            self.notify_now.clear()
            try:
                self.notify_pending()
            except Exception as e:
                log.warning("notifications: %s", e)

    def notify_pending(self):
        conf = config.section("notify")
        channels = conf.get("via", [])
        if not channels:
            return
        unheard = [m for m in self.vm_list("inbox") if not m.get("heard")]
        state = self.notify_state()
        for via in channels:
            fresh = [m for m in unheard if via not in m.get("notified", {})]
            st = state.setdefault(via, {})
            every = conf.get("remind_every_minutes", 60) * 60
            # Reminders only while the last text hasn't been answered: a reply to it (any reply)
            # is the nearest thing to a read receipt that Asterisk can pass on
            answered = st.get("replied", 0) >= st.get("last", 0) > 0
            if fresh:
                text = self.notice(fresh, conf)
            elif unheard and every and not answered and time.time() - st.get("last", 0) >= every:
                text = self.reminder(unheard)
            else:
                continue
            if not self.send_notification(via, text, conf):
                continue
            now = time.time()
            for m in fresh:
                self.vm_update(m["id"], lambda item: item.setdefault("notified", {}).__setitem__(via, now))
            st["last"] = now
            self.save_notify_state(state)

    def notify_state(self):
        try:
            with open(os.path.join(self.data_dir, "notify.json")) as f:
                state = json.load(f)
            return {k: v for k, v in state.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            return {}

    def save_notify_state(self, state):
        path = os.path.join(self.data_dir, "notify.json")
        os.makedirs(self.data_dir, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(path + ".tmp", path)

    def text_reply(self, sender, body, to=""):
        """The user texted the PBX: counts as having read the last text (no more reminders until
        there's new voicemail). A text to the operator's extension goes to the text operator."""
        with self.lock:
            state = self.notify_state()
            state.setdefault("phone", {})["replied"] = time.time()
            self.save_notify_state(state)
        log.info("text from %s to %s: %r", sender, to, body[:80])
        if to and to == self.text_operator_ext() and body.strip():
            self.operator_text(body)

    # The text operator: an operator conversation for texts to [gateway] text_operator, kept for
    # TEXT_OPERATOR_IDLE_S after the last text so a back-and-forth works. What it says in a turn
    # goes back as one text, from that extension.

    def operator_text(self, body):
        with self.lock:
            op = self.text_op
            start = op is None or op["ended"]
            if start:
                op = self.text_op = {"agent": None, "ended": False, "last": time.time(), "ready": threading.Event()}
            op["last"] = time.time()
        if start:
            agent_id = uuid.uuid4().hex[:12]
            try:
                op["agent"] = self.backend.start(agent_id, "operator", text_operator_prompt(), "operator (text)")
            except Exception as e:
                op["ended"] = True
                op["ready"].set()
                log.warning("text operator: couldn't start: %s", e)
                self.text_phone("Sorry, the operator couldn't start.", sender=self.text_operator_ext())
                return
            with self.lock:
                self.agents[agent_id] = op["agent"]
            op["ready"].set()
            threading.Thread(target=self.text_operator_run, args=(op, agent_id), daemon=True).start()
        op["ready"].wait(30)
        if op["agent"]:
            op["agent"].send(clean(f"[Text message from {user()}] {body}"))

    @staticmethod
    def text_operator_ext():
        return str(gateway_conf().get("text_operator", ""))

    def text_operator_run(self, op, agent_id):
        agent, said = op["agent"], []
        try:
            while True:
                try:
                    ev = agent.events.get(timeout=5)
                except queue.Empty:
                    if time.time() - op["last"] > TEXT_OPERATOR_IDLE_S:
                        break
                    continue
                if "say" in ev:
                    said.append(ev["say"].strip())
                elif ev.get("status") == "idle" and said:
                    reply = re.sub(r"\s+", " ", " ".join(said)).strip()[:1000]
                    said = []
                    if not self.text_phone(reply, sender=self.text_operator_ext()):
                        log.warning("text operator: couldn't text the reply %r", reply[:80])
                elif ev.get("status") == "down":
                    break
        finally:
            op["ended"] = True
            with self.lock:
                self.agents.pop(agent_id, None)
            agent.close()
            log.info("text operator: finished")

    def notice(self, fresh, conf):
        dial = self.dial()
        if len(fresh) == 1:
            m = fresh[0]
            preview = f": {m['text'][:conf.get('preview_chars', 160)]}" if conf.get("preview", True) else "."
            return f"New voicemail from {m['label']}{preview}{dial}"
        return f"{len(fresh)} new voicemails, from {self.who(fresh)}.{dial}"

    def reminder(self, unheard):
        n = len(unheard)
        return f"You have {n} new voicemail{'' if n == 1 else 's'}, from {self.who(unheard)}.{self.dial()}"

    @staticmethod
    def who(messages):
        labels = list(dict.fromkeys(m["label"] for m in messages))
        return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"

    @staticmethod
    def dial():
        ext = config.section("bridge").get("voicemail_extension", "")
        return f" Dial {ext} to listen." if ext else ""

    def send_notification(self, via, text, conf):
        if via == "phone":
            return self.text_phone(text)
        if via == "ntfy":
            return self.ntfy(text, conf.get("ntfy", {}))
        log.warning("notify: unknown channel %r", via)
        return False

    def text_phone(self, text, sender=None):
        """A text message to the phone, sent by the bridge through Asterisk (from sender, else
        [bridge] text_from). True once it's gone to a registered phone."""
        text_id = uuid.uuid4().hex[:12]
        pending = {"done": threading.Event(), "status": None}
        with self.lock:
            if not self.outbound:
                return False
            self.texts[text_id] = pending
            for q in self.outbound:
                q.put({"text": text, "id": text_id, **({"from": sender} if sender else {})})
        pending["done"].wait(TEXT_WAIT_S)
        with self.lock:
            self.texts.pop(text_id, None)
        log.info("text %s: %s (%r)", text_id, pending["status"] or "no reply from the bridge", text[:60])
        return pending["status"] == "sent"

    def ntfy(self, text, c):
        """POST to an ntfy topic (https://ntfy.sh, or your own server), which keeps it until the
        app collects it."""
        headers = {"Title": c.get("title", "Voicemail"), "Tags": c.get("tags", "telephone_receiver")}
        token = os.environ.get(c.get("token_env", ""), "") if c.get("token_env") else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(c["url"], data=text.encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            log.warning("ntfy: %s", e)
            return False

    # Outbound calls

    def place_call(self, key, message):
        """Queue an outbound call; returns (id, None) or (None, why not)."""
        gap = gateway_conf()["call_gap_seconds"]
        now = time.monotonic()
        assistant, label = self.backend.assistant, self.label(key)  # (backend takes the lock)
        with self.lock:
            if not self.outbound:
                return None, "the bridge isn't connected"
            for c in self.calls.values():  # never reported back (bridge restarted mid-call): give up
                if c["status"] in ("queued", "ringing") and now - c["at"] > gap:
                    c["status"] = "failed"
            if any(c["status"] in ("queued", "ringing") for c in self.calls.values()):
                return None, "another call is being placed"
            if any(c["key"] == key and now - c["at"] < gap for c in self.calls.values()):
                return None, f"{key} already rang in the last {gap // 60} minutes"
            call_id = uuid.uuid4().hex[:12]
            self.calls[call_id] = {"key": key, "label": label, "message": message, "status": "queued", "at": now}
            for q in self.outbound:
                q.put({"call": call_id, "key": key, "label": label, "message": message,
                       "host": socket.gethostname().split(".")[0], "assistant": assistant})
        log.info("call %s: %s rings with %r", call_id, key, message)
        return call_id, None


G = None  # the Gateway


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        return ipaddress.ip_address(self.client_address[0]).is_loopback

    def allowed(self):
        c = gateway_conf()
        nets = [ipaddress.ip_network(n) for n in c["allowed"]]
        if not any(ipaddress.ip_address(self.client_address[0]) in n for n in nets):
            self.reply(403, {"error": "forbidden"})
            return False
        token = config.secret(c, "token")
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            self.reply(401, {"error": "unauthorised"})
            return False
        return True

    def route(self):
        m = re.fullmatch(r"/sessions/([A-Za-z0-9_-]+)/(send|keys|clear|hangup|events)", self.path)
        return (m.group(1), m.group(2)) if m else (None, None)

    def body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 16384:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def emit(ev):
            self.wfile.write(json.dumps(ev).encode() + b"\n")
            self.wfile.flush()
        return emit

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/info":
            return self.reply(200, {"assistant": G.backend.assistant, "host": socket.gethostname().split(".")[0]})
        if self.path == "/sessions":
            return self.reply(200, G.sessions())
        if self.path == "/outbound":
            return self.outbound()
        if self.path == "/voicemail":
            messages = G.vm_list("inbox")
            return self.reply(200, {"messages": messages, "new": sum(not m["heard"] for m in messages)})
        m = re.fullmatch(r"/calls/([0-9a-f]+)", self.path)
        if m:
            call = G.calls.get(m.group(1))
            return self.reply(200, {"status": call["status"]}) if call else self.reply(404, {"error": "no such call"})
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/agents/events":
            key = urllib.parse.parse_qs(url.query).get("dir", [""])[0]
            if key == "operator":
                return self.run_agent(key, operator_prompt(), "operator")
            if not G.backend.can_start(key):
                return self.reply(404, {"error": f"can't start a conversation in {key}"})
            return self.run_agent(key, session_prompt(), f"new {spoken_label(key)} session")
        key, action = self.route()
        if action != "events":
            return self.reply(404, {"error": "not found"})
        s = G.backend.session(key)
        if not s:
            return self.reply(404, {"error": f"{key} isn't running"})
        self.follow(s)

    def do_POST(self):
        if not self.allowed():
            return
        body = self.body()
        m = re.fullmatch(r"/agents/([0-9a-f]+)/(send|interrupt|connect|hangup)", self.path)
        if m:
            return self.agent_post(m.group(1), m.group(2), body)
        if self.path.startswith("/voicemail") or re.fullmatch(r"/sessions/[A-Za-z0-9_-]+/callback", self.path):
            return self.voicemail_post(body)  # (callbacks are delivered like voicemail)
        if self.path == "/whoami":
            if not self.local():
                return self.reply(403, {"error": "local only"})
            key = body.get("session") if G.known(body.get("session")) else None
            pids = [p for p in body.get("pids", []) if isinstance(p, int)]
            return self.reply(200, {"key": key or G.backend.identify(pids)})
        if self.path == "/calls":
            if not self.local():
                return self.reply(403, {"error": "only sessions on this machine can ring"})
            key, message = body.get("key"), body.get("message")
            if not isinstance(message, str) or not message.strip():
                return self.reply(400, {"error": "message required"})
            if key != "operator" and not G.known(key):
                key = "operator"
            call_id, why = G.place_call(key, re.sub(r"\s+", " ", message).strip()[:600])
            return self.reply(200, {"id": call_id}) if call_id else self.reply(409, {"error": why})
        if self.path == "/texts/reply":  # the user texted the PBX (the bridge passes it on)
            threading.Thread(target=G.text_reply, args=(str(body.get("from", "")), str(body.get("body", "")),
                                                         str(body.get("to", ""))), daemon=True).start()
            return self.reply(200, {"ok": True})
        m = re.fullmatch(r"/texts/([0-9a-f]+)", self.path)
        if m:  # the bridge reporting on a text message
            with G.lock:
                pending = G.texts.get(m.group(1))
            if pending:
                pending["status"] = body.get("status")
                pending["done"].set()
            return self.reply(200, {"ok": True}) if pending else self.reply(404, {"error": "no such text"})
        m = re.fullmatch(r"/calls/([0-9a-f]+)/status", self.path)
        if m:
            status = body.get("status")
            with G.lock:
                call = G.calls.get(m.group(1))
                if call and status in ("ringing", "answered", "no answer", "failed"):
                    call["status"] = status
            log.info("call %s: %s", m.group(1), status)
            return self.reply(200, {"ok": True}) if call else self.reply(404, {"error": "no such call"})
        key, action = self.route()
        if action == "hangup":
            if not self.local():
                return self.reply(403, {"error": "only the session itself can hang up"})
            with G.lock:
                calls = list(G.followers.get(key, []))
            for ev in calls:
                ev.set()
            log.info("%s: hang up (%d call(s))", key, len(calls))
            return self.reply(200, {"calls": len(calls)}) if calls else self.reply(409, {"error": "not on a call"})
        if action not in ("send", "keys", "clear"):
            return self.reply(404, {"error": "not found"})
        s = G.backend.session(key)
        if not s:
            return self.reply(404, {"error": f"{key} isn't running"})
        if action == "keys":
            return self.keys(s, body.get("key"))
        d = s.dialog()
        if action == "clear":
            if d:
                return self.reply(409, {"error": "a dialog is open"})
            ok, old = s.clear()
            return self.reply(200, {"cleared": old}) if ok else self.reply(500, {"error": "couldn't clear the input"})
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return self.reply(400, {"error": "text required"})
        if d:
            return self.reply(409, {"dialog": d})
        with G.vm_lock:
            if not s.send(prefix() + clean(text)):
                return self.reply(500, {"error": "couldn't clear the input"})
        log.info("%s <- %r", key, text)
        return self.reply(200, {"ok": True})

    def keys(self, s, k):
        """Answer a dialog (never a permission prompt) or interrupt a busy session."""
        d = s.dialog()
        if k == "Escape":
            # Only while busy: on an idle prompt or a dialog, Escape means something else
            if d or s.status() != "busy":
                return self.reply(409, {"error": "not busy", "dialog": d})
            s.interrupt()
        elif not d or d["kind"] == "permission":
            return self.reply(409, {"error": "no dialog that can be answered by phone", "dialog": d})
        elif k == "Tab" and d["kind"] != "multi":
            return self.reply(400, {"error": "Tab only finishes a multi-select"})
        elif k != "Tab" and not (isinstance(k, str) and any(n == k for n, _ in d["options"])):
            return self.reply(400, {"error": "not one of the options"})
        else:
            s.press(k)
        log.info("%s <- key %s (%s)", s.key, k, d["kind"] if d else "interrupt")
        self.reply(200, {"dialog": s.dialog()})

    def voicemail_post(self, body):
        if self.path == "/voicemail":  # a session leaving the user a message
            if not self.local():
                return self.reply(403, {"error": "only sessions on this machine can leave messages"})
            key, text = body.get("key"), body.get("message")
            if not isinstance(text, str) or not text.strip():
                return self.reply(400, {"error": "message required"})
            if key != "operator" and not G.known(key):
                key = "operator"
            item = {"id": uuid.uuid4().hex[:12], "from": key, "label": G.label(key),
                    "text": re.sub(r"\s+", " ", text).strip()[:MAX_TEXT], "at": time.time(), "heard": False}
            G.vm_save("inbox", item)
            G.notify_now.set()
            log.info("voicemail %s from %s: %r", item["id"], key, item["text"][:80])
            return self.reply(200, {"id": item["id"]})
        m = re.fullmatch(r"/sessions/([A-Za-z0-9_-]+)/callback", self.path)
        if m:  # `token-ring callback`: ask a session to ring the user back (local only)
            if not self.local():
                return self.reply(403, {"error": "local only"})
            key, text = m.group(1), body.get("text")
            if not G.known(key):
                return self.reply(404, {"error": f"no session {key}"})
            if not isinstance(text, str) or not text.strip():
                return self.reply(400, {"error": "text required"})
            item = {"id": uuid.uuid4().hex[:12], "to": key, "kind": "callback", "text": text.strip()[:MAX_TEXT],
                    "at": time.time()}
            G.vm_save("outbox", item)
            G.vm_deliver.set()
            log.info("callback %s for %s: %r", item["id"], key, item["text"][:80])
            return self.reply(200, {"id": item["id"]})
        m = re.fullmatch(r"/voicemail/to/([A-Za-z0-9_-]+)", self.path)
        if m:  # the user leaving a session a message, from the phone
            key, text = m.group(1), body.get("text")
            if not G.known(key):
                return self.reply(404, {"error": f"no session {key}"})
            if not isinstance(text, str) or not text.strip():
                return self.reply(400, {"error": "text required"})
            item = {"id": uuid.uuid4().hex[:12], "to": key, "text": text.strip()[:MAX_TEXT], "at": time.time()}
            G.vm_save("outbox", item)
            G.vm_deliver.set()
            log.info("voicemail %s for %s: %r", item["id"], key, item["text"][:80])
            return self.reply(200, {"id": item["id"]})
        m = re.fullmatch(r"/voicemail/([0-9a-f]+)/(heard|delete)", self.path)
        if not m:
            return self.reply(404, {"error": "not found"})
        path = os.path.join(G.vm_dir, "inbox", f"{m.group(1)}.json")
        if not os.path.exists(path):
            return self.reply(404, {"error": "no such message"})
        if m.group(2) == "delete":
            os.remove(path)
        else:
            with open(path) as f:
                item = json.load(f)
            item["heard"] = True
            G.vm_save("inbox", item)
        return self.reply(200, {"ok": True})

    def outbound(self):
        """Feed calls to place to the bridge for as long as it stays connected."""
        q = queue.Queue()
        with G.lock:
            G.outbound.append(q)
        emit = self.stream()
        log.info("bridge connected from %s", self.client_address[0])
        try:
            while True:
                try:
                    emit(q.get(timeout=PING_EVERY_S))
                except queue.Empty:
                    emit({"ping": 1})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        finally:
            with G.lock:
                G.outbound.remove(q)
            log.info("bridge disconnected")

    def run_agent(self, key, prompt, label):
        """Start a conversation and stream its events; it's closed when the client goes."""
        agent_id = uuid.uuid4().hex[:12]
        try:
            agent = G.backend.start(agent_id, key, prompt, label)
        except Exception as e:
            log.warning("couldn't start a conversation in %s: %s", key, e)
            return self.reply(500, {"error": str(e)})
        with G.lock:
            G.agents[agent_id] = agent
        emit = self.stream()
        try:
            emit({"agent": agent_id, "label": label})
            while True:
                try:
                    ev = agent.events.get(timeout=PING_EVERY_S)
                except queue.Empty:
                    ev = {"ping": 1}
                emit(ev)
                if ev.get("status") == "down":
                    return
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        finally:
            with G.lock:
                G.agents.pop(agent_id, None)
            agent.close()

    def agent_post(self, agent_id, action, body):
        with G.lock:
            agent = G.agents.get(agent_id)
        if not agent:
            return self.reply(404, {"error": "no such agent"})
        if action == "send":
            text = body.get("text", "")
            if not isinstance(text, str) or not text.strip():
                return self.reply(400, {"error": "text required"})
            agent.send(clean(text))
        elif action == "interrupt":
            agent.interrupt()
        elif action == "hangup":
            agent.events.put({"hangup": True})
            log.info("agent %s: hang up", agent_id)
        else:  # connect: the operator (via `token-ring connect`) putting the caller through
            target = {k: body[k] for k in ("key", "new") if isinstance(body.get(k), str)}
            key = next(iter(target.values()), None)
            if len(target) != 1 or not G.known(key) or ("new" in target and not G.backend.can_start(key)):
                return self.reply(404, {"error": f"no session {key}"})
            agent.events.put({"connect": target})
            log.info("agent %s: connect %s", agent_id, target)
        self.reply(200, {"ok": True})

    def follow(self, s):
        """Stream the session's events until the client goes."""
        emit = self.stream()
        hangup, g = threading.Event(), G
        with g.lock:
            g.followers.setdefault(s.key, []).append(hangup)
        log.info("%s: following", s.key)
        events = s.follow()
        last_ping = 0.0
        try:
            for batch in events:
                if hangup.is_set():
                    hangup.clear()
                    emit({"hangup": True})
                for ev in batch:
                    emit(ev)
                now = time.monotonic()
                if now - last_ping >= PING_EVERY_S:
                    last_ping = now
                    emit({"ping": 1})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        finally:
            events.close()
            with g.lock:
                g.followers[s.key].remove(hangup)
            log.info("%s: stopped following", s.key)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        super().__init__(addr, Handler)
        self.tls, self.tls_mtime = None, None

    def get_request(self):
        sock, addr = super().get_request()
        c = gateway_conf()
        cert, key = config.expand(c.get("tls_cert")), config.expand(c.get("tls_key"))
        if not (cert and key):
            return sock, addr
        mtime = os.path.getmtime(cert)
        if mtime != self.tls_mtime:  # renewed: load the new certificate
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            self.tls, self.tls_mtime = ctx, mtime
        return self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def finish_request(self, request, client_address):
        if isinstance(request, ssl.SSLSocket):
            try:
                request.do_handshake()
            except (ssl.SSLError, OSError):
                return
        super().finish_request(request, client_address)


def run():
    global G
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    c = gateway_conf()
    G = Gateway()
    log.info("backend %s; %d session(s)", c["backend"], len(G.sessions()))
    if not config.secret(c, "token"):
        log.warning("no token set: any allowed address can use the gateway")
    threading.Thread(target=G.vm_deliver_loop, daemon=True).start()
    G.vm_deliver.set()  # anything left pending from before a restart
    threading.Thread(target=G.notify_loop, daemon=True).start()
    G.notify_now.set()
    server = Server((c["host"], c["port"]))
    log.info("listening on %s://%s:%d, allowed %s", "https" if c.get("tls_cert") else "http",
             c["host"], c["port"], " ".join(c["allowed"]))
    server.serve_forever()


if __name__ == "__main__":
    run()
