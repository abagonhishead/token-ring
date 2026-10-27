"""AI backends: what the gateway talks to.

A backend offers two things to a phone call:

- Conversations: new, for one call (the operator, or a new conversation in a session's
  directory). The gateway starts one when the call needs it and closes it when the call ends.
- Sessions: existing, long-lived conversations the caller can be put through to (for Claude
  Code, interactive sessions running in tmux). The call types into them like someone at the
  keyboard and follows their output.

Write your own by subclassing Backend (and Conversation, Session), naming it in the config as
[gateway] backend = "module:Class", or registering it as a token_ring.backends entry point. The
echo backend (backends/echo.py) is the smallest complete example.

Rules every backend must keep:
- Interrupting stops the current turn only. Nothing may cancel background work: closing a
  conversation when the call ends lets it finish what it's doing.
- Permission prompts are never answered by phone (a command read aloud can't be checked): report
  them as dialogs of kind "permission". When unsure what a dialog is, say "permission".

Events (dicts) a conversation puts on its queue, and a session's follow() yields:
  {"say": text}          something to say (streamed in phrases is best; Markdown is cleaned up)
  {"tool": name}         it started using a tool (informational)
  {"status": s}          "busy" (working: the caller hears soft pips), "idle", or "down" (gone)
  {"dialog": d | None}   sessions only: a multiple-choice dialog appeared, changed, or went:
      {"kind": "question" | "multi" | "review" | "notice" | "permission",
       "question": text, "options": [["1", label], ...], "checked": ["2", ...],
       "summary": text (review), "count": questions in the set, "index": this one (1-based),
       "answer_at": optional, what to tell the caller about answering a permission prompt}
"""
import queue


class Conversation:
    """A conversation for one call. events is a queue.Queue of event dicts."""

    def __init__(self, agent_id, label):
        self.id, self.label = agent_id, label
        self.events = queue.Queue()

    def send(self, text):
        raise NotImplementedError

    def interrupt(self):
        """Stop the current turn; background work keeps running."""

    def close(self):
        """The call has ended. No more input; let it finish its work. May block (it's called in
        the gateway's request thread for this call)."""


class Session:
    """An existing session, looked up by key for each request."""

    def __init__(self, key):
        self.key = key

    def status(self):
        """"idle", "busy" or "down"."""
        raise NotImplementedError

    def dialog(self):
        """The dialog it's showing (see above), or None."""
        return None

    def send(self, text):
        """Send a message as if typed. The gateway checks there's no dialog first. Any unsent
        text in the session's input should be cleared first (and logged). Returns True if sent."""
        raise NotImplementedError

    def press(self, key):
        """Answer the dialog: a digit picks that option (toggles it in a multi-select), "Tab"
        finishes a multi-select. The gateway has checked it's allowed."""
        raise NotImplementedError

    def interrupt(self):
        """Stop the current turn (only called while busy with no dialog); background work keeps
        running. Returns True if done."""
        return False

    def clear(self):
        """Empty the input of unsent text: (cleared, the text that was there)."""
        return True, ""

    def follow(self):
        """A generator of lists of new events, yielding about every 0.3 s (an empty list when
        there's nothing new) for as long as the gateway iterates it. Starts from now: earlier
        output isn't replayed. Must yield {"status": "down"} and stop when the session goes."""
        raise NotImplementedError


class Backend:
    """conf is the [backends.<name>] table; gateway is the Gateway (its prompts, data dir)."""

    assistant = "the assistant"  # what the AI is called on the phone ("Claude is asking you...")

    def __init__(self, conf, gateway):
        self.conf, self.gateway = conf, gateway

    def sessions(self):
        """The sessions a caller can reach: [{"key", "name", "status"}], status as for
        Session.status(). Keys are short and sayable ([A-Za-z0-9_-])."""
        return []

    def session(self, key):
        """The running session with that key, or None."""
        return None

    def can_start(self, key):
        """Whether a new conversation may be started in session key's directory."""
        return False

    def start(self, agent_id, key, prompt, label):
        """A new Conversation: the operator if key is "operator", otherwise a new conversation in
        that session's directory. prompt is the system prompt to add. agent_id goes in the
        agent's environment as TOKEN_RING_AGENT, so `token-ring connect` and `token-ring hangup`
        can act for it."""
        raise NotImplementedError

    def identify(self, pids):
        """The session key that a process with these ancestors (nearest first) belongs to, or
        None: `token-ring call` and friends use it to say which session they're from."""
        return None
