"""The bridge and the gateway together, with the echo backend, canned speech-to-text and beeps
for speech: a fake Asterisk rings the bridge over AudioSocket."""
import asyncio
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
    ports = {k: free_port() for k in ("gw", "echo", "op", "vm")}
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
