"""The bridge and the gateway together, with the echo backend, canned speech-to-text and beeps
for speech: a fake Asterisk rings the bridge over AudioSocket."""
import asyncio
import base64
import json
import os
import socket
import threading
import urllib.request
import uuid

import pytest

from token_ring import bridge, gateway
from token_ring.audio import FRAME_BYTES, tone


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def system(conf, tmp_path, monkeypatch):
    ports = {k: free_port() for k in ("gw", "echo", "op", "vm", "ami")}
    (tmp_path / "spool").mkdir()
    conf(f"""
[user]
name = "Tester"
[bridge]
echo_port = {ports['echo']}
operator_port = {ports['op']}
voicemail_port = {ports['vm']}
gateway_url = "http://127.0.0.1:{ports['gw']}"
gateway_token = "secret"
spool = "{tmp_path}/spool"
staging = "{tmp_path}/staging"
answer_wait_seconds = 0.3
voicemail_extension = "201"
ami_port = {ports['ami']}
ami_secret = "amisecret"
[notify]
via = ["phone"]
remind_every_minutes = 0.02
[gateway]
port = {ports['gw']}
token = "secret"
backend = "echo"
data_dir = "{tmp_path}/data"
[backends.echo]
sessions = ["alpha", "beta"]
[stt]
engine = "fake"
[tts]
default = "tone"
[tts.engines.tone]
type = "tone"
[voices]
names = {{ "tone/" = "Toney" }}
[keywords]
engine = "off"
""")
    gateway.G = gateway.Gateway()
    server = gateway.Server(("127.0.0.1", ports["gw"]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    said = []
    monkeypatch.setattr(bridge.Call, "say", lambda self, text, label, voice=None: (
        said.append(text), self.queue.put_nowait((text, label, voice or bridge.voice_for("operator")))))
    yield ports, said, tmp_path
    server.shutdown()


def replies(*texts):
    from token_ring import stt
    stt.get().replies[:] = list(texts)


class Caller:
    """A fake Asterisk channel: AudioSocket frames both ways."""

    def __init__(self, port, call_uuid=None):
        self.port, self.uuid = port, call_uuid or str(uuid.uuid4())
        self.hung_up = False
        self.heard_at = 0.0

    async def __aenter__(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.frame(bridge.KIND_UUID, uuid.UUID(self.uuid).bytes)
        self.drain = asyncio.create_task(self.listen())
        return self

    async def __aexit__(self, *exc):
        if not self.hung_up:
            self.frame(bridge.KIND_HANGUP)
        self.drain.cancel()
        self.writer.close()

    def frame(self, kind, payload=b""):
        self.writer.write(bytes([kind]) + len(payload).to_bytes(2, "big") + payload)

    async def listen(self):
        try:
            while True:
                kind, length = (lambda h: (h[0], int.from_bytes(h[1:3], "big")))(await self.reader.readexactly(3))
                await self.reader.readexactly(length)
                self.heard_at = asyncio.get_running_loop().time()
                if kind == bridge.KIND_HANGUP:
                    self.hung_up = True
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError):
            self.hung_up = True

    async def audio(self, pcm):
        for i in range(0, len(pcm), FRAME_BYTES):
            self.frame(bridge.KIND_SLIN16, pcm[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\0"))
            await self.writer.drain()
            await asyncio.sleep(0.002)

    async def speak(self):
        """Once the bridge has stopped talking: half a second of quiet, a second of "speech", then
        quiet to end the utterance."""
        loop = asyncio.get_running_loop()
        await asyncio.sleep(0.3)
        while loop.time() - self.heard_at < 0.3:
            await asyncio.sleep(0.05)
        await self.audio(b"\0" * 16000 + tone(300, 1000, level=0.3) + b"\0" * 32000)

    async def key(self, digit):
        self.frame(bridge.KIND_DTMF, digit.encode())
        await self.writer.drain()


async def until(check, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


def said_has(said, part):
    return lambda: any(part in s for s in said)


def gw(ports, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{ports['gw']}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


async def serve(ports):
    servers = [await asyncio.start_server(lambda r, w, cls=cls: cls(r, w).run(), "127.0.0.1", ports[k])
               for cls, k in ((bridge.Call, "echo"), (bridge.Operator, "op"), (bridge.Voicemail, "vm"))]
    return servers


def test_echo_line(system):
    ports, said, _ = system

    async def main():
        await serve(ports)
        replies("testing one two three")
        async with Caller(ports["echo"]) as c:
            await until(said_has(said, "Say something"))
            await c.speak()
            await until(said_has(said, "I heard: testing one two three"))
    asyncio.run(main())


def test_operator_connect_question_and_permission(system):
    ports, said, _ = system

    async def main():
        await serve(ports)
        replies("Connect me to alpha.", "Ask me a question.", "Ask permission.")
        async with Caller(ports["op"]) as c:
            await until(said_has(said, "Operator"))
            await c.speak()
            await until(said_has(said, "Connected to alpha"))
            await c.speak()
            await until(said_has(said, "Echo is asking you a multiple choice question. To answer, press 1, or 2."))
            await until(said_has(said, "Which colour? 1, Red. 2, Blue."))
            await c.key("1")
            await until(said_has(said, "You picked Red."))
            await c.speak()
            await until(said_has(said, "That needs answering at the session's terminal. Goodbye."))
            await until(lambda: c.hung_up)
    asyncio.run(main())


def test_voicemail_and_outbound(system):
    ports, said, tmp = system
    gw(ports, "POST", "/voicemail", {"key": "beta", "message": "The build passed."})
    assert gw(ports, "GET", "/voicemail")["new"] == 1

    async def main():
        await serve(ports)
        loop_task = asyncio.create_task(bridge.outbound_loop())
        async with Caller(ports["vm"]) as c:
            await until(said_has(said, "You have 1 new message"))
            await c.key("1")
            await until(said_has(said, "New message from beta, today at"))
            await until(said_has(said, "The build passed."))
        assert gw(ports, "GET", "/voicemail")["new"] == 0

        await until(lambda: gateway.G.outbound, timeout=5)  # the bridge's feed is connected
        call = gw(ports, "POST", "/calls", {"key": "alpha", "message": "All done."})
        await until(lambda: os.listdir(tmp / "spool"))
        body = (tmp / "spool" / os.listdir(tmp / "spool")[0]).read_text()
        assert "Context: token-ring-callback" in body and '"Echo: alpha"' in body
        call_uuid = body.split("TRUUID=")[1].strip()
        async with Caller(ports["op"], call_uuid) as c:
            await c.speak()  # "hello?"
            await until(said_has(said, "this is a call from Echo on"))
            await until(said_has(said, "All done."))
        assert gw(ports, "GET", f"/calls/{call['id']}")["status"] == "answered"
        loop_task.cancel()
    asyncio.run(main())


class FakeAMI:
    """Enough of Asterisk's manager interface for texts: logins, "pjsip show contacts",
    MessageSend, MWIUpdate, and UserEvents for texts from the phone."""

    def __init__(self):
        self.reachable = False
        self.sent = []
        self.froms = []
        self.listeners = []
        self.mwi = []
        self.originated = []
        self.answer = None  # async (Originate message): whoever answers an Originate

    async def handle(self, reader, writer):
        writer.write(b"Asterisk Call Manager/9.0.0\r\n")
        try:
            while True:
                msg = await bridge.ami_read(reader)
                action = msg.get("Action")
                if action == "Login":
                    ok = msg.get("Secret") == "amisecret"
                    writer.write(b"Response: Success\r\n\r\n" if ok else b"Response: Error\r\nMessage: Authentication failed\r\n\r\n")
                    if ok and msg.get("Events") == "user":
                        self.listeners.append(writer)
                elif action == "Command":
                    contact = (b"Output:  Contact:  100/sip:100@10.0.0.5:5060  1a2b3c  Avail  12.3\r\n"
                               if self.reachable else b"Output: No objects found.\r\n")
                    writer.write(b"Response: Success\r\nMessage: Command output follows\r\n" + contact + b"\r\n")
                elif action == "MessageSend":
                    self.sent.append(base64.b64decode(msg["Base64Body"]).decode())
                    self.froms.append(msg.get("From"))
                    writer.write(b"Response: Success\r\nMessage: Message successfully sent\r\n\r\n")
                elif action == "Originate":  # answered at once; the test plays whoever answered
                    self.originated.append(msg)
                    asyncio.create_task(self.answer(msg))
                    writer.write(b"Response: Success\r\nMessage: Originate successfully queued\r\n\r\n")
                elif action == "MWIUpdate":
                    self.mwi.append((msg["Mailbox"], int(msg["NewMessages"]), int(msg["OldMessages"])))
                    writer.write(b"Response: Success\r\nMessage: Mailbox updated\r\n\r\n")
                elif action == "Logoff":
                    writer.close()
                    return
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    def text_from_phone(self, body, to="201"):
        for w in self.listeners:
            w.write(("Event: UserEvent\r\nUserEvent: TokenRingText\r\nFrom: <sip:100@10.0.0.5>\r\n"
                     f"To: {to}\r\nBody: {base64.b64encode(body.encode()).decode()}\r\n\r\n").encode())


def test_voicemail_texts(system, monkeypatch):
    ports, said, tmp = system
    monkeypatch.setattr(gateway, "NOTIFY_RETRY_S", 0.2)
    ami = FakeAMI()

    async def main():
        await asyncio.start_server(ami.handle, "127.0.0.1", ports["ami"])
        tasks = [asyncio.create_task(bridge.outbound_loop()), asyncio.create_task(bridge.text_replies_loop())]
        threading.Thread(target=gateway.G.notify_loop, daemon=True).start()
        await until(lambda: gateway.G.outbound and ami.listeners, timeout=5)

        gw(ports, "POST", "/voicemail", {"key": "beta", "message": "The build passed."})
        await asyncio.sleep(1)
        assert ami.sent == []  # the phone isn't there: the notification waits
        ami.reachable = True
        await until(lambda: ami.sent)
        assert ami.sent[0] == "New voicemail from beta: The build passed. Dial 201 to listen."
        await until(lambda: len(ami.sent) >= 2)  # unheard: a reminder
        assert ami.sent[1] == "You have 1 new voicemail, from beta. Dial 201 to listen."

        ami.text_from_phone("ok")  # read it: no more reminders
        await asyncio.sleep(0.5)
        count = len(ami.sent)
        await asyncio.sleep(2)
        assert len(ami.sent) == count

        gw(ports, "POST", "/voicemail", {"key": "alpha", "message": "Deploy finished."})
        await until(lambda: any("from alpha: Deploy finished." in t for t in ami.sent))
        assert set(ami.froms) == {"201"}

        # Texting the operator: its reply comes back from 200
        ami.text_from_phone("Ask beta to ring me about the build.", to="200")
        await until(lambda: "200" in ami.froms)
        assert ami.sent[ami.froms.index("200")] == "You said: [Text message from Tester] Ask beta to ring me about the build."

        # A callback request reaches the session like voicemail, with its own prefix
        gw(ports, "POST", "/sessions/beta/callback", {"text": "the build"})
        from token_ring.backends.echo import EchoSession
        seen = []
        follower = EchoSession("beta").follow()
        next(follower)
        threading.Thread(target=gateway.G.vm_deliver_loop, daemon=True).start()
        gateway.G.vm_deliver.set()
        await until(lambda: seen.extend(next(follower)) or any("ring them back" in e.get("say", "") for e in seen))
        follower.close()
        for t in tasks:
            t.cancel()
    asyncio.run(main())


def test_voicemail_light(system, conf, tmp_path, monkeypatch):
    ports, said, tmp = system
    monkeypatch.setattr(gateway, "NOTIFY_RETRY_S", 0.2)
    text = (tmp_path / "config.toml").read_text().replace('via = ["phone"]', 'via = ["mwi"]')
    conf(text)
    ami = FakeAMI()

    async def main():
        await asyncio.start_server(ami.handle, "127.0.0.1", ports["ami"])
        task = asyncio.create_task(bridge.outbound_loop())
        threading.Thread(target=gateway.G.notify_loop, daemon=True).start()
        await until(lambda: gateway.G.outbound, timeout=5)

        await until(lambda: ami.mwi)  # the current state, once
        assert ami.mwi == [("100@token-ring", 0, 0)]
        gw(ports, "POST", "/voicemail", {"key": "beta", "message": "The build passed."})
        await until(lambda: ami.mwi[-1] == ("100@token-ring", 1, 0))
        msg_id = gw(ports, "GET", "/voicemail")["messages"][0]["id"]
        gw(ports, "POST", f"/voicemail/{msg_id}/heard")
        await until(lambda: ami.mwi[-1] == ("100@token-ring", 0, 1))
        gw(ports, "POST", f"/voicemail/{msg_id}/delete")
        await until(lambda: ami.mwi[-1] == ("100@token-ring", 0, 0))
        n = len(ami.mwi)
        await asyncio.sleep(1)
        assert len(ami.mwi) == n  # only on changes
        assert ami.sent == []
        task.cancel()
    asyncio.run(main())


def test_outbound_fallback(system, conf, tmp_path, monkeypatch):
    """The phone isn't reachable: the fallback is rung. A short hello gets the call; a long
    greeting is voicemail, and gets hung up on."""
    ports, said, tmp = system
    monkeypatch.setattr(bridge, "REACH_WAIT_S", 0.5)
    text = (tmp_path / "config.toml").read_text().replace(
        "[bridge]\n", '[bridge]\nphone_fallback = "PJSIP/07700900123@trunk"\n')
    conf(text)
    ami = FakeAMI()

    async def ring(key):
        for f in os.listdir(tmp / "spool"):
            os.remove(tmp / "spool" / f)
        call = gw(ports, "POST", "/calls", {"key": key, "message": "All done."})
        await until(lambda: os.listdir(tmp / "spool"), timeout=5)
        body = (tmp / "spool" / os.listdir(tmp / "spool")[0]).read_text()
        assert "Channel: PJSIP/07700900123@trunk" in body and "WaitTime: 20" in body
        return call["id"], body.split("TRUUID=")[1].strip()

    async def main():
        await serve(ports)
        await asyncio.start_server(ami.handle, "127.0.0.1", ports["ami"])
        task = asyncio.create_task(bridge.outbound_loop())
        await until(lambda: gateway.G.outbound, timeout=5)

        call_id, call_uuid = await ring("alpha")
        async with Caller(ports["op"], call_uuid) as c:
            await c.audio(b"\0" * 8000 + tone(300, 5000, level=0.3) + b"\0" * 32000)  # a greeting
            await until(lambda: gw(ports, "GET", f"/calls/{call_id}")["status"] == "no answer")
        assert not said_has(said, "All done.")()

        call_id, call_uuid = await ring("beta")
        async with Caller(ports["op"], call_uuid) as c:
            await c.speak()  # "hello?"
            await until(said_has(said, "this is a call from Echo on"))
            await until(said_has(said, "All done."))
        assert gw(ports, "GET", f"/calls/{call_id}")["status"] == "answered"
        task.cancel()
    asyncio.run(main())


def test_call_app_fallback(system, conf, tmp_path, monkeypatch):
    """A call app's call (ring_app) goes to the fallback too, and its voicemail is no answer."""
    ports, said, tmp = system
    monkeypatch.setattr(bridge, "REACH_WAIT_S", 0.5)
    text = (tmp_path / "config.toml").read_text().replace(
        "[bridge]\n", '[bridge]\nphone_fallback = "PJSIP/07700900123@trunk"\n')
    conf(text)
    ami = FakeAMI()

    def answered_with(pcm):
        async def answer(msg):
            call_uuid = dict(v.split("=", 1) for v in msg["Variable"].split(","))["TRUUID"]
            async with Caller(ports["echo"], call_uuid) as c:
                await c.audio(pcm)
                await asyncio.sleep(2)
        return answer

    async def main():
        await serve(ports)
        await asyncio.start_server(ami.handle, "127.0.0.1", ports["ami"])
        ami.answer = answered_with(b"\0" * 8000 + tone(300, 5000, level=0.3) + b"\0" * 32000)
        assert await bridge.ring_app(ports["echo"], '"Test" <700>', 45) is None
        assert ami.originated[-1]["Channel"] == "PJSIP/07700900123@trunk"
        assert ami.originated[-1]["Timeout"] == "20000"
        ami.answer = answered_with(b"\0" * 8000 + tone(300, 800, level=0.3) + b"\0" * 32000)
        assert await bridge.ring_app(ports["echo"], '"Test" <700>', 45)
        assert not bridge.FALLBACK
    asyncio.run(main())
