"""token-ring: for sessions on the gateway's machine (and the phone operator).

  token-ring call "message"        ring the user; when they answer they hear the message and are
                                   talking to this session. Waits about a minute and prints the
                                   outcome (exit 0 only if answered)
  token-ring voicemail "message"   leave the user a voicemail, read out in this session's voice
  token-ring hangup                end the phone call to this session once what's been said is
                                   spoken (say goodbye first)
  token-ring list                  the sessions: key, status, name
  token-ring connect <key>         operator only: put the call through to that session (waits up
                                   to 90 s for one that's just been started)
  token-ring connect --new <key>   operator only: a new conversation in that session's directory
  token-ring clear <key>           empty that session's input of unsent text (printed)

The gateway is $TOKEN_RING_URL, else this machine's [gateway] port. Which session this is comes
from $TOKEN_RING_SESSION, else the gateway's backend (from the process tree); anything else
counts as the operator.
"""
import os
import sys
import time

from . import config
from .http import Client


def client():
    c = config.section("gateway")
    scheme = "https" if c.get("tls_cert") else "http"
    url = os.environ.get("TOKEN_RING_URL") or f"{scheme}://127.0.0.1:{c['port']}"
    return Client(url, config.secret(c, "token"), verify=False)


def ancestors():
    pids, pid = [], os.getppid()
    while pid > 1 and len(pids) < 64:
        pids.append(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def my_session(gw):
    code, body = gw.call("POST", "/whoami", {"pids": ancestors(), "session": os.environ.get("TOKEN_RING_SESSION", "")})
    return body.get("key") if code == 200 else None


def fail(code, body, what):
    sys.exit(f"{what}: {body.get('error', f'the gateway said {code}')}")


def main(argv=None):
    args = (sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    gw, cmd, rest = client(), args[0], args[1:]
    agent = os.environ.get("TOKEN_RING_AGENT")
    if cmd == "call" and len(rest) == 1 and rest[0].strip():
        if agent:
            sys.exit("You're already on the phone with them.")
        code, body = gw.call("POST", "/calls", {"key": my_session(gw) or "operator", "message": rest[0]})
        if code != 200:
            fail(code, body, "Not calling")
        deadline, status = time.monotonic() + 75, "queued"
        while time.monotonic() < deadline:
            time.sleep(2)
            status = gw.call("GET", f"/calls/{body['id']}")[1].get("status", "failed")
            if status in ("answered", "no answer", "failed"):
                break
        print({"answered": "Answered: they've heard the message and are now talking to this session.",
               "no answer": "No answer."}.get(status, f"Call {status}."))
        return 0 if status == "answered" else 1
    if cmd == "voicemail" and len(rest) == 1 and rest[0].strip():
        code, body = gw.call("POST", "/voicemail", {"key": my_session(gw) or "operator", "message": rest[0]})
        if code != 200:
            fail(code, body, "Not left")
        print("Left. They'll hear it when they next check their voicemail.")
        return 0
    if cmd == "hangup" and not rest:
        if agent:
            code, body = gw.call("POST", f"/agents/{agent}/hangup")
        else:
            key = my_session(gw)
            if not key:
                sys.exit("Can't tell which session this is.")
            code, body = gw.call("POST", f"/sessions/{key}/hangup")
        if code != 200:
            fail(code, body, "Not hanging up")
        print("Hanging up once your last words have been spoken.")
        return 0
    if cmd == "list" and not rest:
        for s in gw.call("GET", "/sessions")[1]:
            print(f"{s['key']:16} {s['status']:8} {s['name']}")
        return 0
    if cmd == "clear" and len(rest) == 1:
        code, body = gw.call("POST", f"/sessions/{rest[0]}/clear")
        if code != 200:
            fail(code, body, "Not cleared")
        print(f"Cleared: {body['cleared']!r}" if body["cleared"] else "The input was already empty.")
        return 0
    if cmd == "connect" and rest:
        new = rest[0] == "--new"
        rest = rest[1:] if new else rest
        if len(rest) != 1:
            sys.exit(__doc__)
        key = rest[0]
        if not agent:
            sys.exit("TOKEN_RING_AGENT isn't set: only the phone operator can connect a call")
        found = {s["key"]: s for s in gw.call("GET", "/sessions")[1]}
        if key not in found:
            sys.exit(f"no session {key!r}; there are: {', '.join(found)}")
        if not new:
            deadline = time.monotonic() + 90
            while found[key]["status"] == "down":
                if time.monotonic() > deadline:
                    sys.exit(f"{key} hasn't started")
                time.sleep(3)
                found = {s["key"]: s for s in gw.call("GET", "/sessions")[1]}
        code, body = gw.call("POST", f"/agents/{agent}/connect", {"new" if new else "key": key})
        if code != 200:
            fail(code, body, "Not connected")
        print(f"Connecting the call to {'a new conversation in ' if new else ''}{key}.")
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
