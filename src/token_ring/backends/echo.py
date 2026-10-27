"""A backend with no AI: says back what it's sent. For trying out the phone side and for tests,
and the smallest example of a backend.

[backends.echo]
sessions = ["alpha", "beta"]   # pretend sessions to put the caller through to

Say "ask me a question" to a session and it shows a multiple-choice dialog; "ask permission"
shows a permission prompt.
"""
import threading
import time

from . import Backend, Conversation, Session

_lock = threading.Lock()
_state = {}  # session key -> {"status", "dialog", "events": [lists of followers' queues]}


class EchoConversation(Conversation):
    def send(self, text):
        self.events.put({"status": "busy"})
        if text.lower().rstrip(".!") in ("hang up", "goodbye"):
            self.events.put({"say": "Goodbye."})
            self.events.put({"hangup": True})
        elif text.lower().startswith("connect me to ") and self.label == "operator":
            self.events.put({"connect": {"key": text[14:].strip(" .").lower()}})
        else:
            self.events.put({"say": f"You said: {text}"})
        self.events.put({"status": "idle"})


QUESTION = {"kind": "question", "question": "Which colour?", "options": [["1", "Red"], ["2", "Blue"]],
            "checked": [], "summary": "", "count": 1, "index": 1}
PERMISSION = {"kind": "permission", "question": "Do you want to run rm -rf build?",
              "options": [["1", "Yes"], ["2", "No"]], "checked": [], "summary": "", "count": 1, "index": 1}


class EchoSession(Session):
    def state(self):
        with _lock:
            return _state.setdefault(self.key, {"status": "idle", "dialog": None, "followers": []})

    def emit(self, *events):
        for q in self.state()["followers"]:
            q.extend(events)

    def status(self):
        return self.state()["status"]

    def dialog(self):
        return self.state()["dialog"]

    def send(self, text):
        s = self.state()
        said = text.lower()
        if "ask me a question" in said:
            s["dialog"] = dict(QUESTION)
            self.emit({"dialog": s["dialog"]})
        elif "ask permission" in said:
            s["dialog"] = dict(PERMISSION)
            self.emit({"dialog": s["dialog"]})
        else:
            self.emit({"status": "busy"}, {"say": f"{self.key} heard: {text}"}, {"status": "idle"})
        return True

    def press(self, key):
        s = self.state()
        d, s["dialog"] = s["dialog"], None
        label = dict(d["options"]).get(key, key) if d else key
        self.emit({"dialog": None}, {"say": f"You picked {label}."})

    def interrupt(self):
        return True

    def follow(self):
        q = []
        self.state()["followers"].append(q)
        try:
            while True:
                out, q[:] = list(q), []
                yield out
                time.sleep(0.1)
        finally:
            self.state()["followers"].remove(q)


class Echo(Backend):
    assistant = "Echo"

    def keys(self):
        return list(self.conf.get("sessions", ["alpha", "beta"]))

    def sessions(self):
        return [{"key": k, "name": k, "status": EchoSession(k).status()} for k in self.keys()]

    def session(self, key):
        return EchoSession(key) if key in self.keys() else None

    def can_start(self, key):
        return key in self.keys()

    def start(self, agent_id, key, prompt, label):
        return EchoConversation(agent_id, label)
