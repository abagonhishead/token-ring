"""A small JSON-over-HTTP(S) client (the bridge and the token-ring command use it)."""
import json
import ssl
import urllib.error
import urllib.request


class Client:
    def __init__(self, url, token, ca_file="", verify=True):
        self.url, self.token = url.rstrip("/"), token
        self.ctx = None
        if self.url.startswith("https:"):
            self.ctx = ssl.create_default_context(cafile=ca_file or None)
            if not verify:  # loopback to our own gateway
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE

    def headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def call(self, method, path, body=None, timeout=15):
        """Returns (status code, decoded JSON)."""
        req = urllib.request.Request(self.url + path, method=method, headers=self.headers(),
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def follow(self, path, emit, stop, log=None):
        """Read a newline-delimited JSON event stream until stop is set, calling emit(event);
        emits {"lost": True} if the stream ends first. The server pings every few seconds, so
        stop is noticed promptly. Blocks: run it in a thread."""
        req = urllib.request.Request(self.url + path, headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=20, context=self.ctx) as resp:
                for line in resp:
                    if stop.is_set():
                        return
                    emit(json.loads(line))
        except Exception as e:
            if log:
                log.warning("event stream %s ended: %s", path, e)
        if not stop.is_set():
            emit({"lost": True})
