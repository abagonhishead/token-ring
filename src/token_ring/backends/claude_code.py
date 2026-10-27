"""Claude Code (https://claude.com/claude-code).

Conversations (the operator, new conversations by phone) are headless `claude -p` processes with
stream-json in and out, one per conversation, living as long as the call.

Sessions are interactive `claude` processes running in tmux (with Remote Control, they're the
sessions you also use from the Claude app). A second process can't safely open the same
conversation, so the call types into the session's tmux pane like someone at the terminal, reads
replies from its transcript (~/.claude/projects) and dialogs from the screen.

[backends.claude-code]
command = ["claude"]
permission_mode = "default"    # for phone conversations: default, acceptEdits, auto, ...
model = ""                     # "" = Claude Code's default
extra_args = []
operator_dir = ""              # where the operator runs ("" = <data_dir>/operator)
directories = ["~/repos/app"]  # sessions by directory (key = directory name)
directories_file = ""          # also take them from a JSON file {"directories": [path | {"path"}]}
discover = true                # also any interactive Claude Code session running in tmux
terminal_hint = "Answer it at the terminal or in the Claude app."  # {key} {label}
"""
import glob
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from ..text import Phrases, spoken_label
from . import Backend, Conversation, Session

log = logging.getLogger("token-ring")

CLAUDE_HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
SESSIONS = os.path.join(CLAUDE_HOME, "sessions")
PROJECTS = os.path.join(CLAUDE_HOME, "projects")
MAX_TEXT = 2000
POLL_S = 0.3
DIALOG_EVERY_S = 1.0
AGENT_GRACE_S = 4 * 3600  # how long a conversation may keep working after its call ends


def key_of(path):
    """A session's key: its directory's name."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", os.path.basename(path.rstrip("/")))


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def live_sessions():
    """{cwd: metadata} for running interactive sessions in tmux (Claude Code records each in
    ~/.claude/sessions/<pid>.json, with "tmux": "<session>:@<window>.%<pane>")."""
    found = {}
    for f in glob.glob(f"{SESSIONS}/*.json"):
        try:
            with open(f) as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            continue
        if s.get("kind") == "interactive" and s.get("tmux") and alive(s.get("pid", 0)):
            found[s["cwd"]] = s
    return found


def tmux_socket(pid):
    """The tmux server socket a process runs under, from its TMUX variable ("" if unreadable:
    tmux's default)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            for var in f.read().split(b"\0"):
                if var.startswith(b"TMUX="):
                    return var[5:].decode().split(",")[0]
    except OSError:
        pass
    return ""


def transcript(meta):
    return os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", meta["cwd"]), f"{meta['sessionId']}.jsonl")


def entries(path, offset):
    """New complete JSONL entries in path from offset; returns (entries, new offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    out = []
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out, offset + end


def events_of(entry):
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return
    content = (entry.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if block.get("type") == "text" and block.get("text", "").strip():
            yield {"say": block["text"]}
        elif block.get("type") == "tool_use":
            yield {"tool": block.get("name", "")}


OPTION = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*$")
CHECKBOX = re.compile(r"^\[(.)\]\s*(.*)$")
# AskUserQuestion's built-in extras, which make no sense on the phone
EXTRAS = re.compile(r"^(?:Type something|Chat about this)\b", re.I)
PERMISSION_WORDS = re.compile(r"Do you want to|proceed|allow|permission|Tab to amend", re.I)


def parse_dialog(screen):
    """The selection dialog on a captured screen, or None (see backends/__init__.py for the
    shape). kind is "question" or "multi" (Claude's AskUserQuestion: footer "Enter to select"),
    "review" (its submit page), "notice" ("Enter to confirm" with nothing asking for permission)
    or "permission": anything else, including every tool permission prompt."""
    lines = [l for l in screen.splitlines() if l.strip()]
    tail = lines[-30:]
    footer = " ".join(tail[-3:])
    # The dialog's own options: the last run numbered down to 1 (a numbered list in the reply above
    # the dialog would otherwise look like options too)
    idx = []
    for i in range(len(tail) - 1, -1, -1):
        m = OPTION.match(tail[i])
        if m and (not idx or int(m.group(1)) == int(OPTION.match(tail[idx[0]]).group(1)) - 1):
            idx.insert(0, i)
            if m.group(1) == "1":
                break
    if idx and OPTION.match(tail[idx[0]]).group(1) != "1":
        idx = []
    review = any("Ready to submit your answers?" in l for l in tail) and idx and idx[-1] >= len(tail) - 2
    if not idx or not (review or "Esc to cancel" in footer or "Enter to confirm" in footer):
        return None
    # The question is above the first option, below the last rule line or tab header
    above = tail[:idx[0]]
    start = max([i + 1 for i, l in enumerate(above) if l.strip().startswith(("─", "╌", "←"))] or [0])
    tabs = next((l for l in reversed(above) if l.strip().startswith("←")), "")
    count = len(re.findall(r"[☐☒]", tabs)) or 1
    index = min(len(re.findall("☒", tabs)) + 1, count)
    text = [l.strip() for l in above[start:]]
    asks = [l for l in text if l.endswith("?")]
    question = asks[-1] if asks else (text[0] if text else "")
    options, checked = [], []
    for i in idx:
        n, label = OPTION.match(tail[i]).groups()
        box = CHECKBOX.match(label)
        if box:
            label = box.group(2)
            if box.group(1).strip():
                checked.append(n)
        if not EXTRAS.match(label):
            options.append([n, label])
    if review:
        kind = "review"
    elif "Enter to select" in footer:
        kind = "multi" if any(CHECKBOX.match(OPTION.match(tail[i]).group(2)) for i in idx) else "question"
    elif "Enter to confirm" in footer and not PERMISSION_WORDS.search(" ".join(tail[start:])):
        kind = "notice"
    else:
        kind = "permission"
    summary = " ".join(l.lstrip("●→ ").strip() for l in text if l.startswith(("●", "→"))) if review else ""
    return {"kind": kind, "question": question, "options": options, "checked": checked,
            "summary": summary, "count": count, "index": index}


def parse_draft(screen):
    """Text in the prompt box of a screen captured with attributes (-e), or "". The box is the
    line starting with ❯ below a rule line. An empty box can show a dim placeholder suggestion
    (Try "fix lint errors"), so dim text is dropped."""
    screen = re.sub(r"\x1b\[2m.*?\x1b\[(?:0|22)?m", "", screen)  # dim (placeholder) runs
    screen = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", screen)       # all other attributes
    lines = [l for l in screen.splitlines() if l.strip()]
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].startswith("❯") and lines[i - 1].lstrip().startswith("─"):
            box = [lines[i][1:]]
            for l in lines[i + 1:]:
                if l.lstrip().startswith("─"):
                    break
                box.append(l)
            return " ".join(s.strip() for s in box).strip()
    return ""


class TmuxSession(Session):
    def __init__(self, key, cwd, meta, backend):
        super().__init__(key)
        self.cwd, self.meta, self.backend = cwd, meta, backend
        self.socket = tmux_socket(meta["pid"])
        self.pane = meta["tmux"].rsplit(".", 1)[-1]  # %<pane>: unique on its tmux server

    def tmux(self, *args):
        cmd = ["tmux"] + (["-S", self.socket] if self.socket else [])
        return subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=5)

    def screen(self, attributes=False):
        return self.tmux("capture-pane", "-p", *(["-e"] if attributes else []), "-t", self.pane).stdout

    def keys(self, *keys, literal=False):
        self.tmux("send-keys", "-t", self.pane, *(["-l"] if literal else []), *keys)

    def status(self):
        meta = live_sessions().get(self.cwd)
        return meta.get("status", "unknown") if meta else "down"

    def dialog(self):
        d = parse_dialog(self.screen())
        if d and d["kind"] == "permission":
            d["answer_at"] = self.backend.conf.get(
                "terminal_hint", "Answer it at the terminal or in the Claude app.").format(
                key=self.key, label=spoken_label(self.key))
        return d

    def draft(self):
        return parse_draft(self.screen(attributes=True))

    def clear(self):
        old = self.draft()
        for _ in range(30):  # a line at a time: end of line, delete to its start, join lines
            if not self.draft():
                break
            self.keys("C-e", "C-u", "BSpace")
            time.sleep(0.1)
        if old:
            log.info("%s: cleared unsent text %r (Ctrl+Y in the session pastes it back)", self.key, old)
        return not self.draft(), old

    def send(self, text):
        if not self.clear()[0]:  # typing would add to it
            return False
        self.keys(re.sub(r"[\x00-\x1f\x7f]+", " ", text), literal=True)
        time.sleep(0.2)  # let the input box take the text before Enter
        self.keys("Enter")
        return True

    def press(self, key):
        self.keys(key)
        time.sleep(0.5)

    def rewind_open(self):
        """Whether the rewind menu (double Escape on an idle prompt) is on screen."""
        return any("Restore the code and/or conversation" in l for l in self.screen().splitlines()[-20:])

    def interrupt(self):
        """Escape stops the current turn; background tasks keep running (never Ctrl+C)."""
        self.keys("Escape")
        time.sleep(0.5)
        # If the turn had just ended, Escape can land on an idle prompt and, with another Escape,
        # open the rewind menu: close it again (one Escape cancels it)
        for _ in range(3):
            if not self.rewind_open():
                break
            log.info("%s: closing the rewind menu", self.key)
            self.keys("Escape")
            time.sleep(0.5)
        return True

    def follow(self):
        path, offset = None, 0
        status, shown = None, None
        last_dialog = 0.0
        while True:
            out = []
            meta = live_sessions().get(self.cwd)
            if not meta:
                yield [{"status": "down"}]
                return
            p = transcript(meta)
            if p != path:  # first time, or the session restarted with a new conversation
                offset = os.path.getsize(p) if path is None and os.path.exists(p) else 0
                path = p
            new, offset = entries(path, offset)
            for e in new:
                out.extend(events_of(e))
            if meta.get("status") != status:
                status = meta.get("status")
                out.append({"status": status})
            now = time.monotonic()
            if now - last_dialog >= DIALOG_EVERY_S:
                last_dialog = now
                d = self.dialog()
                if d != shown:
                    shown = d
                    out.append({"dialog": d})
            yield out
            time.sleep(POLL_S)


class Agent(Conversation):
    """A headless `claude -p` for one call."""

    def __init__(self, agent_id, label, cwd, prompt, conf):
        super().__init__(agent_id, label)
        self.cwd = cwd
        self.phrases = Phrases()
        self.session_id = None
        cmd = list(conf.get("command", ["claude"])) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-mode", conf.get("permission_mode", "default"),
            "--append-system-prompt", prompt]
        if conf.get("model"):
            cmd += ["--model", conf["model"]]
        cmd += list(conf.get("extra_args", []))
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=dict(os.environ, TOKEN_RING_AGENT=agent_id),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.lock = threading.Lock()
        threading.Thread(target=self.read, daemon=True).start()
        log.info("agent %s: started in %s (pid %d)", self.id, cwd, self.proc.pid)

    def write(self, obj):
        with self.lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def send(self, text):
        text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:MAX_TEXT]
        self.write({"type": "user", "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None})
        self.events.put({"status": "busy"})
        log.info("agent %s <- %r", self.id, text)

    def interrupt(self):
        """Stop the current turn; background tasks keep running."""
        self.write({"type": "control_request", "request_id": uuid.uuid4().hex,
                    "request": {"subtype": "interrupt"}})
        log.info("agent %s: interrupt", self.id)

    def say(self, phrases):
        for p in phrases:
            self.events.put({"say": p})

    def read(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("type")
            if kind == "system" and e.get("subtype") == "init" and not self.session_id:
                self.session_id = e.get("session_id")
                log.info("agent %s: session %s", self.id, self.session_id)
            elif kind == "stream_event" and not e.get("parent_tool_use_id"):
                ev = e.get("event", {})
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    self.say(self.phrases.feed(ev["delta"]["text"]))
                elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") == "tool_use":
                    self.events.put({"tool": ev["content_block"].get("name", "")})
                elif ev.get("type") == "content_block_stop":
                    self.say(self.phrases.flush())
            elif kind == "result":
                self.say(self.phrases.flush())
                self.events.put({"status": "idle"})
        self.events.put({"status": "down"})

    def close(self):
        """No more input, but let it finish its work and exit by itself (killing it could throw
        away a long task). Only stopped if it's still going after AGENT_GRACE_S."""
        with self.lock:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        log.info("agent %s: call ended; resume with: cd %s && claude --resume %s",
                 self.id, self.cwd, self.session_id)
        try:
            self.proc.wait(AGENT_GRACE_S)
        except subprocess.TimeoutExpired:
            log.warning("agent %s: still running after %d s; stopping it", self.id, AGENT_GRACE_S)
            self.proc.terminate()
        log.info("agent %s: exited (%s)", self.id, self.proc.returncode)


class ClaudeCode(Backend):
    assistant = "Claude"

    def directories(self):
        """{key: path} of the configured directories, then running sessions (if discover)."""
        paths = list(self.conf.get("directories", []))
        f = os.path.expanduser(self.conf.get("directories_file", ""))
        if f:
            try:
                with open(f) as fh:
                    paths += [e if isinstance(e, str) else e["path"] for e in json.load(fh)["directories"]]
            except (OSError, ValueError, KeyError) as e:
                log.warning("%s: %s", f, e)
        out = {}
        for p in paths:
            p = os.path.realpath(os.path.expanduser(p))
            out.setdefault(key_of(p), p)
        if self.conf.get("discover", True):
            for cwd in live_sessions():
                out.setdefault(key_of(cwd), cwd)
        return out

    def sessions(self):
        live = live_sessions()
        out = []
        for key, path in self.directories().items():
            s = live.get(path)
            out.append({"key": key, "name": s.get("name", key) if s else key,
                        "status": s.get("status", "unknown") if s else "down"})
        return out

    def session(self, key):
        path = self.directories().get(key)
        meta = live_sessions().get(path) if path else None
        return TmuxSession(key, path, meta, self) if meta else None

    def can_start(self, key):
        return key in self.directories()  # only these: they're trusted, and -p skips the trust check

    def start(self, agent_id, key, prompt, label):
        if key == "operator":
            cwd = os.path.expanduser(self.conf.get("operator_dir") or os.path.join(self.gateway.data_dir, "operator"))
            os.makedirs(cwd, exist_ok=True)
        else:
            cwd = self.directories()[key]
        return Agent(agent_id, label, cwd, prompt, self.conf)

    def identify(self, pids):
        for pid in pids:
            try:
                with open(f"{SESSIONS}/{int(pid)}.json") as f:
                    cwd = json.load(f)["cwd"]
            except (OSError, ValueError, KeyError):
                continue
            key = key_of(cwd)
            return key if self.directories().get(key) == os.path.realpath(cwd) else None
        return None
