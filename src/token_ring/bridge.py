"""The bridge: runs beside Asterisk, which connects calls to it with AudioSocket
(Dial(AudioSocket/127.0.0.1:<port>/<uuid>/c(slin16))). The port picks the mode:

  echo_port      Speech echo: detect each utterance, transcribe it, say back what was heard.
  operator_port  Talk to the AI through the gateway. The call starts with the operator (an agent
                 that looks after the sessions and can put you through): say "connect me to
                 <session>", "new session in <session>" or "list sessions", or press 0 for the
                 list. Once you're in a session, everything goes to it: hang up and call again for
                 the operator. In a session, digits answer its multiple-choice questions (# finishes
                 a multi-select, 0 repeats the question, * the instructions); otherwise * stops the
                 speech and interrupts the current turn (background work keeps running).
                 Permission prompts are never answered by phone: the call says where to answer it
                 and hangs up. Trigger words ([keywords]) cut in while the AI is speaking.
                 Outbound: sessions can ring the phone (`token-ring call`). The gateway feeds the
                 requests here; each becomes an Asterisk call file that rings [bridge] phone and,
                 answered, lands in [token-ring-callback] and comes back to this port with the
                 request's UUID, so the call opens with the message and talks to the session that
                 rang.
  voicemail_port Voicemail: messages sessions left (each read in that session's voice), and
                 messages for sessions (transcribed, then delivered by the gateway).

Speech is transcribed by the [stt] engine and every phrase is synthesised live by the
[tts] engines (see config.example.toml).

AudioSocket framing: 1 byte kind, 2 bytes big-endian length, payload.
"""
import asyncio
import base64
import logging
import os
import re
import threading
import time
import uuid

from . import config, plugins, spotter, stt, tts, vad
from .audio import FRAME_BYTES, FRAME_MS, RATE, frame_db, tone
from .http import Client
from .text import match_session, pronounce, speakable, spoken_when
from .tts import persona, voice_for

KIND_HANGUP, KIND_UUID, KIND_DTMF, KIND_SLIN16, KIND_ERROR = 0x00, 0x01, 0x03, 0x12, 0xFF

ECHO_GREETING = "Hello. Say something, and I'll tell you what I heard."

# Utterance detection on 20 ms frames (energy-based)
START_DB = 15        # dB above the noise floor that counts as speech
START_FRAMES = 3     # consecutive loud frames to start an utterance
END_SILENCE_MS = 700
PREROLL_MS = 300
MIN_SPEECH_MS = 300
MAX_UTTERANCE_S = 20
REACH_WAIT_S = 15    # texts: how long to wait for a phone that's missed a qualify check
VOICEMAIL_S = 3.5    # calls to the fallback: talking longer than this when answering is voicemail

log = logging.getLogger("token-ring")

PIP = tone(520, 60, level=0.06)   # while a session is working and silent
TICK = tone(1000, 40, level=0.08)  # a key was heard
ACK = tone(880, 70, gap_ms=60) + tone(1175, 70)


def bridge_conf():
    return config.section("bridge")


# What Whisper and its kind tend to "hear" in noise or silence: dropped when that's all there is.
# [stt] ignore adds more (compared without case or punctuation).
NOT_SPEECH = {"you", "thank you", "thank you so much", "thank you very much", "thanks for watching",
              "thank you for watching", "thanks for watching and see you next time", "please subscribe",
              "subtitles by the amaraorg community", "uh", "um", "hmm", "mm", "mhm", "ah", "oh"}


def not_speech(text):
    """Whether a transcript is only something speech-to-text makes up from noise."""
    t = re.sub(r"[^\w\s']", "", text.lower()).strip()
    extra = {re.sub(r"[^\w\s']", "", x.lower()).strip() for x in config.section("stt").get("ignore", [])}
    return t in NOT_SPEECH or t in extra


def transcribe(pcm):
    """Text of one utterance and the seconds it took. Runs in a thread."""
    t0 = time.perf_counter()
    text = stt.get().transcribe(pcm)
    return text, time.perf_counter() - t0


def gateway():
    c = bridge_conf()
    return Client(c["gateway_url"], config.secret(c, "gateway_token"), config.expand(c.get("gateway_ca_file")))


OUTBOUND = {}  # AudioSocket UUID -> outbound call request from the gateway, until answered
# AudioSocket UUID of a call ringing [bridge] phone_fallback -> None, or a future told whether a
# person answered it (not its voicemail)
FALLBACK = {}


async def choose_phone(what):
    """What to ring: [bridge] phone, or phone_fallback (a mobile through a SIP trunk, say) when it's
    set and the phone isn't reachable, after REACH_WAIT_S for one that's missed a qualify check
    (a softphone asleep). Returns (channel, ring_seconds or None, whether it's the fallback)."""
    c = bridge_conf()
    tech, _, endpoint = c["phone"].partition("/")
    if c.get("phone_fallback") and tech.upper() == "PJSIP":
        try:
            deadline = time.monotonic() + REACH_WAIT_S
            while not await reachable(endpoint) and time.monotonic() < deadline:
                await asyncio.sleep(2)
            if not await reachable(endpoint):
                log.info("%s: the phone isn't reachable: ringing the fallback", what)
                return c["phone_fallback"], c["fallback_ring_seconds"], True
        except Exception as e:
            log.warning("%s: couldn't check the phone (%s); ringing it anyway", what, e)
    return c["phone"], None, False


def ring(call, channel, ring_seconds):
    """Have Asterisk ring channel for an outbound call request; returns the UUID it'll come back
    with. A call file is written beside the spool and moved in, so Asterisk never reads a partial
    file."""
    c = bridge_conf()
    call_uuid = str(uuid.uuid4())
    label = re.sub(r"[^A-Za-z0-9 ._-]", "", call["label"])[:30]
    who = re.sub(r"[^A-Za-z0-9 ._-]", "", call.get("assistant") or c["caller_id"])[:20]
    body = (f"Channel: {channel}\nCallerID: \"{who}: {label}\" <600>\nWaitTime: {ring_seconds}\n"
            f"MaxRetries: 0\nContext: token-ring-callback\nExtension: s\nPriority: 1\n"
            f"Setvar: TRUUID={call_uuid}\n")
    os.makedirs(c["staging"], exist_ok=True)
    tmp = os.path.join(c["staging"], f"{call_uuid}.call")
    with open(tmp, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o640)
    OUTBOUND[call_uuid] = call
    os.rename(tmp, os.path.join(c["spool"], f"{call_uuid}.call"))
    return call_uuid


async def outbound_loop():
    """Take outbound call requests from the gateway for as long as the bridge runs."""
    loop = asyncio.get_running_loop()
    while True:
        stop = threading.Event()
        done = asyncio.Event()

        def emit(ev):
            try:
                loop.call_soon_threadsafe(on_outbound, ev, done)
            except RuntimeError:  # the loop has closed (shutting down)
                stop.set()
        threading.Thread(target=gateway().follow, args=("/outbound", emit, stop, log), daemon=True).start()
        await done.wait()  # the stream dropped: reconnect
        await asyncio.sleep(10)


def on_outbound(ev, done):
    if "lost" in ev:
        done.set()
    elif "text" in ev:
        asyncio.create_task(send_text(ev["id"], ev["text"], ev.get("from")))
    elif "mwi" in ev:
        asyncio.create_task(update_mwi(ev["id"], ev["mwi"].get("new", 0), ev["mwi"].get("old", 0)))
    elif "call" in ev:
        asyncio.create_task(place_call(ev))


async def place_call(call):
    """Ring the phone for an outbound call request (or the fallback: choose_phone)."""
    call_id = call["call"]
    channel, seconds, call["fallback"] = await choose_phone(f"outbound {call_id}")
    seconds = seconds or bridge_conf()["ring_seconds"]
    try:
        call_uuid = ring(call, channel, seconds)
    except OSError as e:
        log.warning("outbound %s: couldn't write the call file: %s", call_id, e)
        await report(call_id, "failed")
        return
    if call["fallback"]:
        FALLBACK[call_uuid] = None
    log.info("outbound %s from %s: ringing %s (%s)", call_id, call["key"],
             "the fallback" if call["fallback"] else "the phone", call_uuid[:8])
    await report(call_id, "ringing")

    def unanswered():
        if OUTBOUND.pop(call_uuid, None):
            log.info("outbound %s: no answer", call_id)
            asyncio.create_task(report(call_id, "no answer"))
    asyncio.get_running_loop().call_later(seconds + 15, unanswered)


# Text messages to the phone, through Asterisk's manager interface (AMI): only sent while the
# phone is registered and reachable, since Asterisk keeps no messages for later and doesn't say
# whether a phone took one. The user's texts back come in as UserEvents from the
# [token-ring-messages] dialplan context.

def ami_secret(user=None, secret="", secret_file=None):
    """The AMI password: [bridge] ami_secret, else the secret = in the manager.conf section for the
    user in [bridge] ami_secret_file (the first one if there's no such section)."""
    c = bridge_conf()
    user = user or c["ami_user"]
    raw = config.secret({"secret": secret if secret_file is not None else c.get("ami_secret"),
                         "secret_file": secret_file if secret_file is not None else c.get("ami_secret_file")}, "secret")
    sections = re.split(r"^\s*\[([^\]]+)\]\s*$", raw, flags=re.M)
    for name, body in zip(sections[1::2], sections[2::2]):
        if name.strip() == user:
            raw = body
            break
    m = re.search(r"^\s*secret\s*=\s*(\S+)", raw, re.M)
    return m.group(1) if m else raw.strip()


async def ami_read(reader):
    """One AMI message: {field: value}, with command output lines in "Output" (a list)."""
    msg = {"Output": []}
    while True:
        line = (await reader.readline()).decode(errors="replace")
        if not line:
            raise ConnectionError("AMI connection closed")
        line = line.rstrip("\r\n")
        if not line:
            if len(msg) > 1 or msg["Output"]:
                return msg
            continue
        key, _, value = line.partition(": ")
        if key == "Output":
            msg["Output"].append(value)
        else:
            msg[key] = value


async def ami_connect(events="off", login=None):
    """A logged-in AMI connection: the bridge's ([bridge] ami_user), or login = (user, secret)."""
    c = bridge_conf()
    user, secret = login or (c["ami_user"], ami_secret())
    reader, writer = await asyncio.wait_for(asyncio.open_connection(c["ami_host"], c["ami_port"]), 5)
    await reader.readline()  # "Asterisk Call Manager/..."
    ami_send(writer, {"Action": "Login", "Username": user, "Secret": secret, "Events": events})
    resp = await asyncio.wait_for(ami_read(reader), 5)
    if resp.get("Response") != "Success":
        writer.close()
        raise PermissionError(resp.get("Message", "AMI login failed"))
    return reader, writer


def ami_send(writer, fields):
    writer.write("".join(f"{k}: {v}\r\n" for k, v in fields.items()).encode() + b"\r\n")


async def ami_action(fields, timeout=10, login=None):
    reader, writer = await ami_connect(login=login)
    try:
        ami_send(writer, fields)
        return await asyncio.wait_for(ami_read(reader), timeout)
    finally:
        ami_send(writer, {"Action": "Logoff"})
        writer.close()


async def ami_list(fields, timeout=10, login=None):
    """An action that answers with a list of events (PJSIPShowContacts, ShowDialPlan, ...): the
    events, without the response and the "...Complete" one."""
    reader, writer = await ami_connect(login=login)
    try:
        ami_send(writer, fields)
        first = await asyncio.wait_for(ami_read(reader), timeout)
        if first.get("Response") != "Success":
            msg = first.get("Message", "").lower()
            if msg.startswith("no ") or "not found" in msg or "did not find" in msg:
                return []  # an empty list
            raise RuntimeError(f"{fields['Action']}: {first.get('Message', 'failed')}")
        events = []
        while True:
            ev = await asyncio.wait_for(ami_read(reader), timeout)
            if ev.get("EventList") == "Complete" or ev.get("Event", "").endswith("Complete"):
                return events
            events.append(ev)
    finally:
        ami_send(writer, {"Action": "Logoff"})
        writer.close()


async def reachable(endpoint):
    """Whether a PJSIP endpoint has a registered contact that answers its qualify checks."""
    out = (await ami_action({"Action": "Command", "Command": "pjsip show contacts"}))["Output"]
    return any(re.match(rf"\s*Contact:\s+{re.escape(endpoint)}/\S+\s+\S+\s+(Avail|NonQual)\b", l) for l in out)


async def send_text(text_id, text, sender=None):
    phone = bridge_conf()["phone"]
    status = "failed"
    try:
        tech, _, endpoint = phone.partition("/")
        if tech.upper() != "PJSIP":
            raise ValueError(f"texts need a PJSIP phone, not {phone}")
        # A phone on Wi-Fi sometimes misses a qualify check and comes back seconds later
        deadline = time.monotonic() + REACH_WAIT_S
        while not await reachable(endpoint) and time.monotonic() < deadline:
            await asyncio.sleep(2)
        if not await reachable(endpoint):
            status = "unreachable"
        else:
            resp = await ami_action({"Action": "MessageSend", "To": f"pjsip:{endpoint}",
                                     "From": sender or bridge_conf()["text_from"],
                                     "Base64Body": base64.b64encode(text.encode()).decode()})
            status = "sent" if resp.get("Response") == "Success" else "failed"
            if status == "failed":
                log.warning("text %s: %s", text_id, resp.get("Message"))
    except Exception as e:
        log.warning("text %s: %s", text_id, e)
    log.info("text %s to %s: %s", text_id, phone, status)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, gateway().call, "POST", f"/texts/{text_id}", {"status": status})
    except Exception as e:
        log.warning("text %s: couldn't report %s: %s", text_id, status, e)


def mailbox():
    """The phone's mailbox for MWI: [bridge] mailbox, else <its endpoint>@token-ring (pjsip.conf's
    mailboxes = on its AOR)."""
    c = bridge_conf()
    return c.get("mailbox") or c["phone"].partition("/")[2] + "@token-ring"


async def update_mwi(req_id, new, old):
    """Set the voicemail light (res_mwi_external): Asterisk keeps the counts and tells the phone,
    while its subscription lasts (the phone renews it)."""
    status = "failed"
    try:
        resp = await ami_action({"Action": "MWIUpdate", "Mailbox": mailbox(),
                                 "NewMessages": int(new), "OldMessages": int(old)})
        status = "sent" if resp.get("Response") == "Success" else "failed"
        if status == "failed":
            log.warning("mwi: %s", resp.get("Message"))
    except Exception as e:
        log.warning("mwi: %s", e)
    log.info("mwi %s: %d new, %d old: %s", mailbox(), new, old, status)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, gateway().call, "POST", f"/texts/{req_id}", {"status": status})
    except Exception as e:
        log.warning("mwi: couldn't report %s: %s", status, e)


async def text_replies_loop():
    """Pass the user's texts to the PBX on to the gateway, for as long as the bridge runs."""
    warned = False
    while True:
        try:
            reader, writer = await ami_connect(events="user")
            log.info("listening for texts (AMI)")
            warned = False
            while True:
                ev = await ami_read(reader)
                if ev.get("Event") == "UserEvent" and ev.get("UserEvent") == "TokenRingText":
                    body = base64.b64decode(ev.get("Body", "")).decode(errors="replace")
                    log.info("text from %s to %s: %r", ev.get("From"), ev.get("To"), body[:80])
                    await asyncio.get_running_loop().run_in_executor(
                        None, gateway().call, "POST", "/texts/reply",
                        {"from": ev.get("From", ""), "to": ev.get("To", ""), "body": body})
        except Exception as e:
            if not warned:
                log.warning("texts: no AMI connection (%s); retrying", e)
                warned = True
        await asyncio.sleep(10)


async def ring_app(port, caller_id, ring_seconds=30, call_uuid=None):
    """Ring the phone for a call app ([bridge.apps]): returns the AudioSocket UUID the call will
    arrive on the app's port with if it was answered, or None (no answer, busy, rejected). Waits
    for the outcome (Asterisk reports it as soon as it's known). If it rang the fallback
    (choose_phone), its voicemail answering counts as no answer."""
    call_uuid = call_uuid or str(uuid.uuid4())
    phone, fallback_seconds, fallback = await choose_phone(f"call app on {port}")
    if fallback:
        ring_seconds = min(ring_seconds, fallback_seconds)
        FALLBACK[call_uuid] = asyncio.get_running_loop().create_future()
    try:
        resp = await ami_action({"Action": "Originate", "Channel": phone, "Context": "token-ring-app",
                                 "Exten": "s", "Priority": "1", "Timeout": str(ring_seconds * 1000),
                                 "CallerID": caller_id, "Async": "false",
                                 "Variable": f"TRUUID={call_uuid},TRPORT={port}"}, timeout=ring_seconds + 15)
        if resp.get("Response") != "Success":
            return None
        if fallback:
            try:
                return call_uuid if await asyncio.wait_for(FALLBACK[call_uuid], 30) else None
            except asyncio.TimeoutError:
                return None
        return call_uuid
    finally:
        FALLBACK.pop(call_uuid, None)


async def report(call_id, status):
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, gateway().call, "POST", f"/calls/{call_id}/status", {"status": status})
    except Exception as e:
        log.warning("outbound %s: couldn't report %s: %s", call_id, status, e)


class Call:
    greeting = ECHO_GREETING

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.uuid = "?"
        self.queue = asyncio.Queue()  # (text, label, voice) waiting to be said
        self.current = None           # the speak() task playing now
        self.stop_synth = threading.Event()
        self.transcribing = False
        self.dictating = False  # recording a voicemail: keep listening while transcribing
        self.saying = ""  # the phrase being spoken, for the trigger-word echo guard
        self.deaf_until = 0.0  # ignore caller audio until then (the rest of a trigger word)
        self.answering = None   # while listening to how a call we made was answered
        self.confirming = None  # set by a key press then
        self.last_sound = time.monotonic()
        # Utterance detection state
        self.vad = vad.create()
        self.voice_db = None   # the caller's voice level on this call, once they've said something
        self.level_dbs = []    # levels of the speech frames in the current utterance
        self.noise_db = -60.0
        self.loud_run = 0
        self.preroll = []
        self.utterance = None  # bytearray while the caller is speaking
        self.speech_ms = self.silence_ms = 0

    async def send(self, kind, payload=b""):
        self.writer.write(bytes([kind]) + len(payload).to_bytes(2, "big") + payload)
        await self.writer.drain()

    async def play(self, pcm):
        """Send PCM to the caller in real time."""
        next_at = time.monotonic()
        for i in range(0, len(pcm), FRAME_BYTES):
            await self.send(KIND_SLIN16, pcm[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0"))
            next_at += FRAME_MS / 1000
            await asyncio.sleep(max(0, next_at - time.monotonic()))
        self.last_sound = time.monotonic()

    async def speak(self, text, label, spec):
        """Synthesise text live and stream it to the caller in real time."""
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        t0 = time.perf_counter()
        self.stop_synth = stop = threading.Event()
        self.saying = text.lower()
        job = loop.run_in_executor(
            None, tts.synthesize, pronounce(text), spec,
            lambda pcm: loop.call_soon_threadsafe(queue.put_nowait, pcm), stop)
        job.add_done_callback(lambda _: loop.call_soon_threadsafe(queue.put_nowait, None))

        first, sent, next_at = None, 0, None
        buf = b""
        try:
            while True:
                pcm = await queue.get()
                if pcm is None:
                    break
                if first is None:
                    first = time.perf_counter() - t0
                    log.info("%s %s: first audio after %d ms (%s)", self.uuid, label, first * 1000, spec)
                    next_at = time.monotonic()
                buf += pcm
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    await self.send(KIND_SLIN16, frame)
                    sent += 1
                    next_at += FRAME_MS / 1000
                    await asyncio.sleep(max(0, next_at - time.monotonic()))
            if buf:
                await self.send(KIND_SLIN16, buf.ljust(FRAME_BYTES, b"\0"))
            log.info("%s %s: done, %.1f s of audio", self.uuid, label, sent * FRAME_MS / 1000)
        finally:
            stop.set()
            self.last_sound = time.monotonic()

    async def speaker(self):
        """Say queued phrases one after another."""
        while True:
            text, label, spec = await self.queue.get()
            self.current = asyncio.create_task(self.speak(text, label, spec))
            await asyncio.wait([self.current])

    def say(self, text, label, voice=None):
        """Queue text to be said, in voice (default: the operator's, which the PBX speaks in)."""
        self.queue.put_nowait((text, label, voice or voice_for("operator")))

    def hush(self):
        """Stop speaking and drop anything still queued."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.stop_synth.set()
        if self.current and not self.current.done():
            self.current.cancel()

    @property
    def talking(self):
        return (self.current is not None and not self.current.done()) or not self.queue.empty()

    def hear(self, frame):
        """Feed one 20 ms frame of caller audio to the utterance detector. Caller audio is ignored
        while we're talking or transcribing (no barge-in yet)."""
        if self.talking or (self.transcribing and not self.dictating) or time.monotonic() < self.deaf_until:
            self.utterance, self.loud_run = None, 0
            return
        db = frame_db(frame)
        if self.utterance is None:
            self.preroll = (self.preroll + [frame])[-(PREROLL_MS // FRAME_MS):]
            if self.is_speech(frame, db, True):
                self.loud_run += 1
                self.level_dbs.append(db)
                if self.loud_run >= START_FRAMES:
                    self.utterance = bytearray(b"".join(self.preroll))
                    self.speech_ms, self.silence_ms = self.loud_run * FRAME_MS, 0
            else:
                self.loud_run, self.level_dbs = 0, []
                self.noise_db = 0.95 * self.noise_db + 0.05 * db  # track the background level
            return
        self.utterance += frame
        if self.is_speech(frame, db, False):
            self.speech_ms += FRAME_MS
            self.silence_ms = 0
            self.level_dbs.append(db)
        else:
            self.silence_ms += FRAME_MS
        if self.silence_ms >= END_SILENCE_MS or len(self.utterance) >= MAX_UTTERANCE_S * RATE * 2:
            pcm, speech_ms = bytes(self.utterance), self.speech_ms
            levels = sorted(self.level_dbs)
            self.utterance, self.loud_run, self.preroll, self.level_dbs = None, 0, [], []
            if speech_ms >= MIN_SPEECH_MS:
                self.transcribing = True
                self.last_utterance = {"speech_ms": speech_ms, "level": levels[int(len(levels) * 0.9)]}
                asyncio.create_task(self.heard(pcm))

    def is_speech(self, frame, db, starting):
        """Speech by the detector ([vad]), loud enough ([vad] min_level_db), and near the caller's
        voice level: their level on this call once known, and within an utterance its loudest part
        so far, so it ends when they stop even with a TV going on behind."""
        if not self.vad.speech(frame, db - self.noise_db, starting):
            return False
        conf = config.section("vad")
        slack = 0 if starting else 5
        if db < conf.get("min_level_db", -100) - slack:
            return False
        ref = self.voice_db
        if not starting and len(self.level_dbs) >= 10:
            peak = sorted(self.level_dbs)[int(len(self.level_dbs) * 0.9)]
            ref = peak if ref is None else max(ref, peak)
        near = conf.get("near_db", 12)
        return not (near and ref is not None and db < ref - near - slack)

    def learned_voice(self, level):
        """The level of an utterance that turned out to be words: the caller's voice. Goes up at
        once (a TV heard first mustn't set it), down slowly."""
        if self.voice_db is None or level > self.voice_db:
            self.voice_db = level
        else:
            self.voice_db = 0.8 * self.voice_db + 0.2 * level
        log.debug("%s voice level %.1f dB", self.uuid, self.voice_db)

    async def heard(self, pcm):
        utterance = getattr(self, "last_utterance", {"speech_ms": 0, "level": None})
        try:
            log.info("%s utterance: %.1f s, level %.0f dB (noise %.0f dB, voice %s)", self.uuid,
                     len(pcm) / 2 / RATE, utterance["level"] or 0, self.noise_db,
                     "unknown" if self.voice_db is None else f"{self.voice_db:.0f} dB")
            try:
                text, took = await asyncio.get_running_loop().run_in_executor(None, transcribe, pcm)
            except Exception as e:  # network or engine failure: say so rather than go quiet
                log.warning("%s transcription failed: %s", self.uuid, e)
                self.say("Sorry, speech recognition isn't available right now.", "stt-error")
                return
            log.info("%s transcribed in %d ms: %r", self.uuid, took * 1000, text)
        finally:
            self.transcribing = False
        text = text.strip()
        if not text or not_speech(text):
            # noise, or what's made up from it: only ask again after a good second of speech
            if utterance["speech_ms"] < 1000 or text:
                log.info("%s ignored: %r", self.uuid, text)
                return
        elif utterance["level"] is not None:
            self.learned_voice(utterance["level"])
        await self.on_text("" if not_speech(text) else text)

    async def on_text(self, text):
        """Echo mode: say back what was heard."""
        self.say(f"I heard: {text}" if text else "Sorry, I didn't catch that.", "echo")

    async def start(self):
        """A call to the fallback (choose_phone) first checks it's a person who answered, not the
        fallback's voicemail; then on_start."""
        if self.call_uuid in FALLBACK:
            person = await self.answered_by_person()
            result = FALLBACK.get(self.call_uuid)
            if result is None:
                FALLBACK.pop(self.call_uuid, None)
            elif not result.done():
                result.set_result(person)
            if not person:
                await self.on_voicemail()
                return await self.hangup()
        await self.on_start()

    @staticmethod
    def listening():
        return {"floor": -60.0, "loud": 0, "spoke": False, "last": 0.0, "frames": 0, "first": 0, "last_frame": 0}

    def listen_answer(self, frame):
        """While waiting for whoever answered a call we made to speak: just listen for sound."""
        a = self.answering
        a["frames"] += 1  # time in the call's audio, for telling a hello from a greeting
        if not self.talking:
            db = frame_db(frame)
            if db > a["floor"] + START_DB:
                a["loud"] += 1
                if a["loud"] >= START_FRAMES:
                    if not a["spoke"]:
                        a["first"] = a["frames"] - START_FRAMES
                    a["spoke"], a["last"], a["last_frame"] = True, time.monotonic(), a["frames"]
            else:
                a["loud"] = 0
                a["floor"] = 0.95 * a["floor"] + 0.05 * db

    async def answered_by_person(self):
        """A call to the fallback (a mobile), answered: by a person, or by its voicemail? A person
        says a short hello and stops; voicemail talks for longer than VOICEMAIL_S. A key press is a
        person too. Silence gets one "Hello?"; if it's still silent, it's taken as voicemail (when
        unsure, don't talk to it). Times are in the call's audio (20 ms frames)."""
        a = self.answering = self.listening()
        self.confirming = asyncio.Event()
        person = False
        try:
            for attempt in range(2):
                start = time.monotonic()
                while True:
                    if self.confirming.is_set():
                        person = True
                        return True
                    if a["spoke"]:
                        if (a["last_frame"] - a["first"]) * 0.02 > VOICEMAIL_S:
                            return False
                        if a["frames"] - a["last_frame"] >= 50:  # a second's quiet after a hello
                            person = True
                            return True
                    elif time.monotonic() - start > 6:
                        break
                    await asyncio.sleep(0.05)
                if attempt == 0:
                    self.say("Hello?", "hello")
                    await self.finish_speaking()
            return False
        finally:
            log.info("%s answered by %s (%.1f s of speech)", self.uuid, "a person" if person else "voicemail or nobody",
                     max(0, a["last_frame"] - a["first"]) * 0.02 if a["spoke"] else 0)
            self.answering = self.confirming = None
            self.noise_db = a["floor"]

    async def on_voicemail(self):
        """A call to the fallback went to its voicemail (or nobody spoke): about to hang up."""

    async def on_start(self):
        self.say(self.greeting, "greeting")

    async def on_dtmf(self, digit):
        pass

    async def on_end(self):
        pass

    async def finish_speaking(self):
        """Wait until everything queued has been said."""
        await asyncio.sleep(0.05)
        while self.talking:
            await asyncio.sleep(0.05)

    async def hangup(self):
        """End the call once what's queued has been said."""
        await self.finish_speaking()
        await asyncio.sleep(0.5)
        try:
            await self.send(KIND_HANGUP)
        finally:
            self.writer.close()

    async def run(self):
        speaker = asyncio.create_task(self.speaker())
        try:
            while True:
                kind, length = (lambda h: (h[0], int.from_bytes(h[1:3], "big")))(
                    await self.reader.readexactly(3))
                payload = await self.reader.readexactly(length) if length else b""
                if kind == KIND_UUID:
                    self.call_uuid = str(uuid.UUID(bytes=payload)) if len(payload) == 16 else ""
                    self.uuid = payload.hex()[:8]
                    log.info("%s call connected (%s)", self.uuid, type(self).__name__)
                    # alongside reading audio: setting up may wait to hear the caller (wait_for_hello)
                    asyncio.create_task(self.start())
                elif kind == KIND_SLIN16:
                    if self.answering is not None:
                        self.listen_answer(payload)
                    else:
                        self.hear(payload)
                elif kind == KIND_DTMF:
                    digit = payload.decode(errors="replace")
                    log.info("%s DTMF %s", self.uuid, digit)
                    if self.confirming is not None:
                        self.confirming.set()
                    else:
                        asyncio.create_task(self.on_dtmf(digit))  # may call the gateway; keep reading audio
                elif kind == KIND_HANGUP:
                    break
                elif kind == KIND_ERROR:
                    log.warning("%s Asterisk reported an error", self.uuid)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.hush()
            speaker.cancel()
            await self.on_end()
            self.writer.close()
            log.info("%s call ended", self.uuid)


SWITCH = re.compile(r"^(?:please\s+)?(?:connect me|put me through|switch|go|take me|transfer me)"
                    r"(?:\s+(?:back|over|through))?\s+to\s+(?:the\s+)?(.+?)[.!?]*$", re.I)
NEW = re.compile(r"^(?:please\s+)?(?:start|open|begin|create|give me)?\s*(?:me\s+)?(?:a\s+)?"
                 r"new\s+(?:session|conversation|chat)\s+(?:in|on|for|with)\s+(?:the\s+)?(.+?)[.!?]*$", re.I)
LIST = re.compile(r"^(?:list|which|what)\s+(?:the\s+)?sessions\b", re.I)


class Operator(Call):
    """Talk to the AI through the gateway. The call starts with the operator, an agent that looks
    after the sessions and can put the caller through. Targets are the operator, new conversations
    started for this call in a session's directory ("agent"s, alive for the call), and the
    existing sessions ("session": followed through the gateway)."""

    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        self.targets = {}     # key -> {"kind", "label", "agent", "stop", "status", "pending"}
        self.key = None       # the target we're talking to
        self.sessions = []    # the existing sessions, from the gateway
        self.menu = None      # targets just read out, while waiting for a number
        self.dialog = None    # a dialog on the current session's screen that the keypad answers
        self.rereading = 0.0  # when * or 0 last asked for the dialog again
        self.pips = None
        self.spotter = None
        self.assistant = bridge_conf()["caller_id"]  # what the AI is called (the gateway says)

    @property
    def target(self):
        return self.targets.get(self.key)

    @property
    def label(self):
        return self.target["label"] if self.target else "the session"

    async def api(self, method, path, body=None):
        try:
            return await asyncio.get_running_loop().run_in_executor(None, gateway().call, method, path, body)
        except Exception as e:
            log.warning("%s gateway %s %s failed: %s", self.uuid, method, path, e)
            return None, {}

    async def refresh(self):
        code, sessions = await self.api("GET", "/sessions")
        if code == 200:
            self.sessions = sessions
        return code == 200

    def choices(self):
        """What the operator can put the caller through to: this call's new sessions, then the
        Remote Control sessions."""
        out = [{"key": k, "label": t["label"], "name": t["label"], "status": "idle"}
                for k, t in self.targets.items() if k.startswith("new:")]
        return out + self.sessions

    def stream(self, key, path):
        """Follow a gateway event stream in a thread; events go to on_event tagged with key."""
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def emit(ev):
            try:
                loop.call_soon_threadsafe(self.on_event, key, stop, ev)
            except RuntimeError:  # the loop has closed (shutting down)
                stop.set()
        threading.Thread(target=gateway().follow, args=(path, emit, stop, log), daemon=True).start()
        return stop

    def start_agent(self, key, dir_key, label):
        """A headless claude for this call (the operator, or a new session in dir_key)."""
        self.targets[key] = {"kind": "agent", "label": label, "agent": None, "status": None,
                             "pending": [], "stop": self.stream(key, f"/agents/events?dir={dir_key}")}

    def hear(self, frame):
        if self.spotter:
            self.spotter.feed(frame, frame_db(frame) > self.noise_db + START_DB - 5)
        super().hear(frame)

    async def wait_for_hello(self):
        """After a call from a session is answered: wait for whoever picked up to say something, then
        for a second's quiet, before talking. If nobody speaks within answer_wait_seconds, ask if anyone
        is there, wait once more, then carry on anyway. What they say isn't sent anywhere."""
        a = self.answering = self.listening()
        t0 = time.monotonic()
        wait = bridge_conf()["answer_wait_seconds"]
        try:
            for attempt in range(2):
                start = time.monotonic()
                while time.monotonic() - start < wait:
                    if a["spoke"] and time.monotonic() - a["last"] >= 1.0:
                        return
                    await asyncio.sleep(0.05)
                if a["spoke"]:  # still talking at the deadline: give them up to 6 s more
                    end = time.monotonic() + 6
                    while time.monotonic() < end and time.monotonic() - a["last"] < 1.0:
                        await asyncio.sleep(0.05)
                    return
                if attempt == 0:
                    self.say("Hello? Is anyone there?", "hello")
                    await asyncio.sleep(0.2)
                    while self.talking:
                        await asyncio.sleep(0.05)
        finally:
            log.info("%s answered: %s after %.1f s", self.uuid, "spoke" if a["spoke"] else "silence",
                     time.monotonic() - t0)
            self.answering = None
            self.noise_db = a["floor"]

    async def on_voicemail(self):
        call = OUTBOUND.pop(getattr(self, "call_uuid", ""), None)
        if call:
            log.info("%s outbound %s: voicemail or nobody", self.uuid, call["call"])
            asyncio.create_task(report(call["call"], "no answer"))

    def on_keyword(self, action, phrase):
        """A trigger word. Only acted on while speaking (barge-in); otherwise speech-to-text hears
        it like anything else."""
        working = self.target and self.target.get("status") == "busy"
        if phrase in self.saying or not (self.talking or (action == "interrupt" and working)):
            return  # our own voice echoing back, or nothing to cut in on
        log.info("%s trigger %r: %s", self.uuid, phrase, action)
        self.deaf_until = time.monotonic() + 1.0  # don't send the rest of the word to the session
        self.hush()
        if action == "interrupt":
            asyncio.create_task(self.interrupt())

    async def on_start(self):
        self.spotter = spotter.start(self.on_keyword)
        self.pips = asyncio.create_task(self.pip_loop())
        call = OUTBOUND.pop(getattr(self, "call_uuid", ""), None)
        await self.refresh()
        code, info = await self.api("GET", "/info")
        if code == 200 and info.get("assistant"):
            self.assistant = info["assistant"]
        if call:  # a session rang the phone: say why, then talk to that session
            log.info("%s outbound %s answered", self.uuid, call["call"])
            if call.get("fallback"):  # a person's already said hello (start)
                asyncio.create_task(report(call["call"], "answered"))
            else:
                asyncio.create_task(report(call["call"], "answered"))
                await self.wait_for_hello()
            who = "the operator" if call["key"] == "operator" else f"the {call['label']} session"
            await self.switch(call["key"], hello=f"Hello, this is a call from {self.assistant} on "
                                                f"{call.get('host', 'the computer')}, {who}.")
            self.say(speakable(call["message"]), "message", voice_for(call["key"]))
            return
        self.start_agent("operator", "operator", "operator")
        self.key = "operator"
        code, body = await self.api("GET", "/voicemail")
        new = body.get("new", 0) if code == 200 else 0
        ext = " ".join(str(bridge_conf().get("voicemail_extension", "")))
        dial = f": dial {ext} to hear them" if ext.strip() else ""
        waiting = f" You have {new} new voicemail{'' if new == 1 else 's'}{dial}." if new else ""
        name = persona(voice_for("operator"))
        self.say(f"Operator{f', {name} speaking' if name else ''}.{waiting} Go ahead.", "connect")

    async def switch(self, key, hello=None):
        """Talk to target key from now on (starting it if needed)."""
        old = self.target
        if old and old["kind"] == "session" and key != self.key:
            old["stop"].set()  # only the current session is followed
            del self.targets[self.key]
        self.menu = self.dialog = None
        if key == "operator" and key not in self.targets:
            self.start_agent("operator", "operator", "operator")
        elif key.startswith("new:") and key not in self.targets:
            dir_key = key[4:]
            session = next((s for s in self.sessions if s["key"] == dir_key), None)
            self.start_agent(key, dir_key, f"new {session['label'] if session else dir_key} session")
            hello = hello or f"New {session['label'] if session else dir_key} session. Go ahead."
        elif key not in self.targets:
            await self.refresh()
            session = next((s for s in self.sessions if s["key"] == key), None)
            if not session or session["status"] == "down":
                self.say(f"{session['label'] if session else key} isn't running.", "down")
                return
            self.targets[key] = {"kind": "session", "label": session["label"], "status": session["status"],
                                 "stop": self.stream(key, f"/sessions/{key}/events")}
        self.key = key
        log.info("%s talking to %s", self.uuid, key)
        self.say(hello or f"Connected to {self.label}.", "connect")
        name = persona(voice_for(key)) if key != "operator" else None
        if name:  # the session says hello in its own voice
            self.say(f"Hi, it's {name}.", "intro", voice_for(key))

    def on_event(self, key, stop, ev):
        t = self.targets.get(key)
        if stop.is_set() or not t or t["stop"] is not stop:
            return  # from a stream we've since left
        current = key == self.key
        if "agent" in ev:
            t["agent"] = ev["agent"]
            for text in t["pending"]:  # said before the agent was ready
                asyncio.create_task(self.api("POST", f"/agents/{t['agent']}/send", {"text": text}))
            t["pending"] = []
        elif "hangup" in ev and current:
            log.info("%s %s hung up", self.uuid, key)
            asyncio.create_task(self.hangup())  # after whatever is still being said
        elif "connect" in ev and current:
            c = ev["connect"]
            asyncio.create_task(self.switch(f"new:{c['new']}" if "new" in c else c["key"]))
        elif "status" in ev:
            t["status"] = ev["status"]
            if ev["status"] == "down":
                t["stop"].set()
                del self.targets[key]
                if current:
                    self.say(f"The {self.label_of(t)} has ended. Hang up and call back for the operator.", "down")
        elif "lost" in ev:
            del self.targets[key]
            if current:
                self.say(f"Lost the connection to the {self.label_of(t)}. Hang up and call back for the operator.", "lost")
        elif not current:
            return
        elif "say" in ev:
            text = speakable(ev["say"])
            if text:
                self.say(text, "reply", voice_for(key))
        elif "dialog" in ev:
            self.on_dialog(ev["dialog"])

    @staticmethod
    def label_of(t):
        return t["label"]

    def on_dialog(self, d):
        """A dialog appeared, changed or went. Permission prompts: SSH and hang up. Others are read
        out; ticking options in a multi-select isn't (the keypress tick confirms it)."""
        old, self.dialog = self.dialog, d
        if not d:
            return
        if d["kind"] == "permission":
            self.dialog = None
            asyncio.create_task(self.needs_terminal(d))
            return
        asking = ("question", "multi")
        if old and old["kind"] == "multi" and d["kind"] == "multi" and old["question"] == d["question"]:
            return  # just ticks changing: the keypress tick is confirmation enough
        self.hush()  # a question takes priority over whatever was being said
        if d["kind"] in asking and not (old and old["kind"] in asking + ("review",)):
            self.say(self.instructions(d), "dialog")  # the first question of a set
        self.say(self.question_text(d), "dialog", voice_for(self.key))  # the session asking, in its voice

    def instructions(self, d):
        """How to answer, said before the first question (again on *)."""
        n = d.get("count", 1)
        kind = "multi-select" if d["kind"] == "multi" and n == 1 else "multiple choice"
        what = f"a {kind} question" if n == 1 else f"{n} {kind} questions"
        nums = [o for o, _ in d["options"]]
        keys = nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + f", or {nums[-1]}"
        return (f"{self.assistant} is asking you {what}. To answer, press {keys}. To repeat a question, "
                f"press 0. To repeat these instructions, press star.")

    def question_text(self, d):
        """The question with its options (again on 0)."""
        options = " ".join(f"{n}, {label}." for n, label in d["options"])
        if d["kind"] == "review":
            return f"Your answers: {d['summary']}. Press 1 to submit, or 2 to cancel."
        if d["kind"] == "notice":
            return f"{self.label} is showing a notice. {d['question']} {options} Press a number."
        number = f"Question {d['index']} of {d['count']}. " if d.get("count", 1) > 1 else ""
        text = f"{number}{d['question']} {options}"
        if d["kind"] == "multi":
            ticked = [label for n, label in d["options"] if n in d["checked"]]
            text += (f" {' and '.join(ticked)} already ticked." if ticked else "")
            text += " Press numbers to tick, then hash when done."
        return text

    async def needs_terminal(self, dialog):
        """A dialog on a session's screen: those are answered at the terminal, so say so and
        hang up."""
        self.hush()
        where = dialog.get("answer_at") or "That needs answering at the session's terminal."
        self.say(f"{self.label} is asking: {dialog['question']} {where} Goodbye.", "dialog")
        await self.hangup()

    async def read_menu(self):
        self.hush()
        await self.refresh()
        self.menu = self.choices()[:9]
        items = " ".join(f"{i}, {s['label']}{', not running' if s['status'] == 'down' else ''}."
                         for i, s in enumerate(self.menu, 1))
        self.say(f"Sessions. {items} Press a number.", "menu")

    async def on_text(self, text):
        if not text:
            self.say("Sorry, I didn't catch that.", "unclear")
            return
        self.menu = None
        if self.key != "operator":  # in a session, everything is for the session
            return await self.send_text(text)
        if LIST.match(text):
            return await self.read_menu()
        m = NEW.match(text)
        if m:
            await self.refresh()
            target = match_session(m.group(1), self.sessions)
            if target:
                return await self.switch(f"new:{target['key']}")
            self.say("I couldn't tell where you want the new session. Press 0 to hear the list.", "unclear")
            return
        m = SWITCH.match(text)
        if m:
            await self.refresh()
            target = match_session(m.group(1), self.choices())
            if target:
                return await self.switch(target["key"])
            self.say("I couldn't tell which session you meant. Press 0 to hear the list.", "unclear")
            return
        await self.send_text(text)

    async def send_text(self, text):
        t = self.target
        if not t:
            self.say("You're not connected to anything. Hang up and call back for the operator.", "none")
            return
        if t["kind"] == "agent":
            if not t["agent"]:
                t["pending"].append(text)
                await self.play(ACK)
                return
            code, body = await self.api("POST", f"/agents/{t['agent']}/send", {"text": text})
        else:
            code, body = await self.api("POST", f"/sessions/{self.key}/send", {"text": text})
        if code == 200:
            await self.play(ACK)
        elif code == 409:
            self.dialog = None
            self.on_dialog(body.get("dialog") or {"kind": "permission", "question": ""})
        elif code == 404:
            self.say(f"{self.label} isn't running.", "down")
        else:
            self.say(f"Sorry, I couldn't reach {self.label}.", "rc-error")

    async def interrupt(self):
        """Stop speaking and interrupt the current turn; background tasks keep running."""
        self.hush()
        t = self.target
        if not t:
            return
        if t["kind"] == "agent":
            code = (await self.api("POST", f"/agents/{t['agent']}/interrupt"))[0] if t["agent"] else 200
        elif self.dialog:
            return  # Escape on a dialog would answer it
        else:
            code = (await self.api("POST", f"/sessions/{self.key}/keys", {"key": "Escape"}))[0]
        if code == 200:
            self.say("Interrupted.", "interrupt")

    async def press(self, key):
        """Answer the dialog on screen; the event stream reads out what it changes to."""
        code, body = await self.api("POST", f"/sessions/{self.key}/keys", {"key": key})
        if code == 200:
            await self.play(ACK)
        elif code == 409:  # it's gone or turned into something the phone mustn't answer
            self.dialog = None
            if body.get("dialog"):
                self.on_dialog(body["dialog"])
        else:
            self.say(f"Sorry, that didn't work: {body.get('error', 'no answer from the session')}.", "dialog")

    async def on_dtmf(self, digit):
        log.info("%s key %s: dialog %s, menu %s, queued %d", self.uuid, digit,
                 self.dialog and self.dialog["kind"], bool(self.menu), self.queue.qsize())
        await self.play(TICK)
        if digit in "0*" and self.dialog:  # 0: the question again; *: the instructions
            if self.rereading and time.monotonic() - self.rereading < 3 and self.talking:
                return  # already starting; another press would only restart it
            self.rereading = time.monotonic()
            self.hush()
            d = self.dialog
            if digit == "*" and d["kind"] in ("question", "multi"):
                self.say(self.instructions(d), "dialog")
            else:
                self.say(self.question_text(d), "dialog", voice_for(self.key))
        elif digit == "0" and self.key == "operator":
            await self.read_menu()
        elif digit == "#":
            if self.dialog and self.dialog["kind"] == "multi":
                self.hush()
                await self.press("Tab")  # done ticking: on to the review page
        elif digit == "*":
            await self.interrupt()
        elif digit.isdigit():
            n = int(digit)
            if self.menu:
                if 1 <= n <= len(self.menu):
                    self.hush()
                    await self.switch(self.menu[n - 1]["key"])
            elif self.dialog:
                if any(o == digit for o, _ in self.dialog["options"]):
                    self.hush()
                    await self.press(digit)
                else:
                    self.say("That isn't one of the options.", "dialog")
            elif self.key == "operator":
                self.say("Press 0 to hear the list of sessions.", "hint")

    async def pip_loop(self):
        """Soft pips while the current target is working and nothing else is being said."""
        while True:
            await asyncio.sleep(0.5)
            t = self.target
            if (t and t["status"] == "busy" and not self.talking and not self.transcribing
                    and self.utterance is None and time.monotonic() - self.last_sound >= bridge_conf()["pip_every_seconds"]):
                await self.play(PIP)

    async def on_end(self):
        if self.spotter:
            self.spotter.close()
        for t in self.targets.values():
            t["stop"].set()  # the gateway stops this call's agents when their streams close
        if self.pips:
            self.pips.cancel()


class Voicemail(Operator):
    """Voicemail. Plays the messages sessions have left (each in the voice of the session that
    left it) and records messages for sessions, transcribed and then delivered by the gateway.
    Keypad driven; a session can be named by voice when leaving one."""

    MENU = "To hear your messages, press 1. To leave a message for a session, press 2."
    AFTER = "To replay it, press 1. For the next message, 2. To delete it, 3. To reply, 4."
    END_SILENCE_S = 8       # a recording ends after this long without speech...
    NOTHING_S = 15          # ...or this long if nothing was said at all

    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        self.mode = "menu"  # menu, after (a message just played), pick, record, confirm
        self.messages = []
        self.index = 0
        self.to = None      # the session a message is being left for
        self.parts = {}     # recording: utterance number -> text
        self.seq = 0
        self.pending = 0    # utterances still being transcribed
        self.last_speech = 0.0
        self.watch = None

    async def on_start(self):
        await self.refresh()
        code, body = await self.api("GET", "/voicemail")
        if code != 200:
            self.say("Sorry, voicemail isn't available right now.", "vm")
            return
        new = [m for m in body["messages"] if not m["heard"]]
        saved = [m for m in body["messages"] if m["heard"]]
        self.messages = new + saved
        count = lambda n, what: f"{'no' if n == 0 else n} {what} message{'' if n == 1 else 's'}"
        name = persona(voice_for("operator"))
        self.say(f"Voicemail{f', {name} speaking' if name else ''}. You have {count(len(new), 'new')}"
                 f"{f', and {count(len(saved), 'saved')}' if saved else ''}.", "vm")
        self.say(self.MENU, "vm")

    def play_message(self):
        """Read out the current message: who and when in the PBX voice, the message in theirs."""
        m = self.messages[self.index]
        self.mode = "after"
        self.say(f"{'Saved message' if m['heard'] else 'New message'} from {m['label']}, "
                 f"{spoken_when(m['at'])}.", "vm")
        name = persona(voice_for(m["from"]))
        self.say((f"Hi, it's {name}. " if name else "") + speakable(m["text"]), "message", voice_for(m["from"]))
        self.say(self.AFTER, "vm")
        if not m["heard"]:
            m["heard"] = True
            asyncio.create_task(self.api("POST", f"/voicemail/{m['id']}/heard"))

    def next_message(self):
        self.index += 1
        if self.index < len(self.messages):
            self.play_message()
        else:
            self.mode = "menu"
            self.say("No more messages. " + self.MENU, "vm")

    def pick(self):
        self.mode = "pick"
        self.say("Who's it for? Say the session's name, or press 0 for a list.", "vm")

    async def record_for(self, key, label):
        self.to = (key, label)
        self.mode = "record-wait"
        self.parts, self.seq, self.pending = {}, 0, 0
        self.say(f"Leave your message for {label} after the tone. Press hash when you've finished.", "vm")
        while self.talking:
            await asyncio.sleep(0.1)
        await self.play(tone(1000, 350, level=0.1))
        self.mode, self.dictating = "record", True
        self.last_speech = time.monotonic()
        self.watch = asyncio.create_task(self.silence_watch())

    async def silence_watch(self):
        while self.mode == "record":
            await asyncio.sleep(0.5)
            quiet = time.monotonic() - self.last_speech
            if self.utterance is None and not self.pending and (
                    quiet > (self.END_SILENCE_S if self.parts else self.NOTHING_S)):
                await self.finish_recording()

    async def finish_recording(self):
        if self.mode != "record":
            return
        self.mode, self.dictating = "confirm", False
        while self.pending:  # let the last sentences finish transcribing
            await asyncio.sleep(0.1)
        text = " ".join(t for _, t in sorted(self.parts.items()) if t).strip()
        self.draft = text
        if not text:
            self.say("I didn't catch anything. To try again, press 2. To cancel, press 3.", "vm")
            return
        self.say(f"Your message for {self.to[1]}: {text}", "vm")
        self.say("To send it, press 1. To record it again, press 2. To cancel, press 3.", "vm")

    def hear(self, frame):
        if self.mode == "record" and self.utterance is not None:
            self.last_speech = time.monotonic()
        super().hear(frame)

    async def heard(self, pcm):
        if self.mode != "record":
            return await super().heard(pcm)
        seq = self.seq  # dictation: keep listening while each sentence is transcribed, in order
        self.seq += 1
        self.pending += 1
        self.transcribing = False
        try:
            text, _ = await asyncio.get_running_loop().run_in_executor(None, transcribe, pcm)
            self.parts[seq] = "" if not_speech(text) else text.strip()
            log.info("%s dictated %d: %r", self.uuid, seq, text)
        except Exception as e:
            log.warning("%s transcription failed: %s", self.uuid, e)
            self.parts[seq] = ""
        finally:
            self.pending -= 1
            self.last_speech = time.monotonic()

    async def on_text(self, text):
        if self.mode != "pick" or not text:
            return  # outside picking a session, speech isn't needed (and is ignored)
        await self.refresh()
        target = match_session(text, self.sessions)
        if target:
            await self.record_for(target["key"], target["label"])
        else:
            self.say("I couldn't tell which session you meant. Press 0 for a list.", "vm")

    async def on_dtmf(self, digit):
        await self.play(TICK)
        mode = self.mode
        if mode == "record":
            if digit == "#":
                await self.finish_recording()
            return
        self.hush()
        if digit == "*":  # say the options again
            self.say({"menu": self.MENU, "after": self.AFTER}.get(mode, "To send it, press 1. To record "
                     "it again, press 2. To cancel, press 3." if mode == "confirm" else self.MENU), "vm")
        elif mode == "menu" and digit == "1":
            if not self.messages:
                self.say("You have no messages. " + self.MENU, "vm")
            else:
                self.index = 0
                self.play_message()
        elif mode == "menu" and digit == "2":
            self.pick()
        elif mode == "after":
            m = self.messages[self.index]
            if digit == "1":
                self.play_message()
            elif digit == "2":
                self.next_message()
            elif digit == "3":
                await self.api("POST", f"/voicemail/{m['id']}/delete")
                del self.messages[self.index]
                self.say("Deleted.", "vm")
                self.index -= 1
                self.next_message()
            elif digit == "4":
                if m["from"] == "operator":
                    self.say("That one came from the operator, which can't take a reply. " + self.AFTER, "vm")
                else:
                    await self.record_for(m["from"], m["label"])
        elif mode == "pick":
            if digit == "0":
                await self.refresh()
                self.menu = [s for s in self.sessions if s["status"] != "down"][:9]
                self.say("Sessions. " + " ".join(f"{i}, {s['label']}." for i, s in enumerate(self.menu, 1))
                         + " Press a number.", "vm")
            elif digit.isdigit() and self.menu and 1 <= int(digit) <= len(self.menu):
                s = self.menu[int(digit) - 1]
                self.menu = None
                await self.record_for(s["key"], s["label"])
        elif mode == "confirm":
            if digit == "1" and self.draft:
                code, _ = await self.api("POST", f"/voicemail/to/{self.to[0]}", {"text": self.draft})
                self.mode = "menu"
                self.say((f"Sent. {self.to[1]} will get it as soon as it's free. " if code == 200
                          else "Sorry, that couldn't be sent. ") + self.MENU, "vm")
            elif digit == "2":
                await self.record_for(*self.to)
            elif digit == "3":
                self.mode = "menu"
                self.say("Cancelled. " + self.MENU, "vm")

    async def on_end(self):
        self.mode = "ended"
        await super().on_end()


async def main():
    c = bridge_conf()
    servers = [await asyncio.start_server(lambda r, w, cls=cls: cls(r, w).run(), c["host"], c[port])
               for cls, port in ((Call, "echo_port"), (Operator, "operator_port"), (Voicemail, "voicemail_port"))]
    log.info("listening on %s: echo %d, operator %d, voicemail %d; voice %s, STT %s, gateway %s",
             c["host"], c["echo_port"], c["operator_port"], c["voicemail_port"], voice_for("operator"),
             stt.get().describe(), c["gateway_url"])
    tasks = [outbound_loop(), text_replies_loop()]
    # Call apps: your own Call subclasses, each on its own port ([bridge.apps.<name>] port and
    # class = "module:Class"), with an optional background task (async classmethod background(conf))
    for name, app in c.get("apps", {}).items():
        cls = plugins.load_class(app["class"])

        def serve(r, w, cls=cls, app=app):
            call = cls(r, w)
            call.app_conf = app
            return call.run()
        servers.append(await asyncio.start_server(serve, c["host"], app["port"]))
        if hasattr(cls, "background"):
            tasks.append(cls.background(app))
        log.info("call app %s on %d", name, app["port"])
    threading.Thread(target=tts.preload, daemon=True).start()
    await asyncio.gather(*(srv.serve_forever() for srv in servers), *tasks)


def log_to_file(path, fmt):
    """Also log to path (rotated at 5 MB, two old ones kept), for the web page."""
    if not path:
        return
    import logging.handlers
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=2)
    except OSError as e:
        log.warning("can't log to %s: %s", path, e)
        return
    h.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(h)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")
    config.seed()
    log_to_file(config.expand(bridge_conf().get("log_file")), "%(asctime)s.%(msecs)03d %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
