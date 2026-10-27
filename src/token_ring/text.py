"""Turning text into something worth saying, and what the caller said into a session."""
import datetime
import difflib
import re
import time

from . import config


def pronounce(text):
    """Apply [pronunciations] ({"word" = "respelling"}; whole words, case-insensitive, longest
    first)."""
    fixes = config.section("pronunciations")
    for word, say in sorted(fixes.items(), key=lambda f: -len(f[0])):
        text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", say, text, flags=re.I)
    return text


def speakable(text):
    """Turn a Markdown reply into something worth reading aloud."""
    text = re.sub(r"```.*?(```|$)", " (code omitted.) ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") or re.fullmatch(r"[-*_=]{3,}", s):
            continue  # tables and rules
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        if s and not re.search(r"[.!?:;,]\)?$", s):
            s += "."  # a pause at the end of headings and list items
        lines.append(s)
    text = " ".join(lines)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+?(?=[.,;:!?)]*(?:\s|$))", "a link", text)
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


NUMBERS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
           "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def compact(s):
    """Lower-case letters and digits only, with spoken numbers as digits ("double oh" -> 00)."""
    words = re.findall(r"[a-z0-9]+", s.lower())
    out, double = [], False
    for w in words:
        if w == "double":
            double = True
            continue
        w = NUMBERS.get(w, w)
        out.append(w * 2 if double and w.isdigit() else w)
        double = False
    return "".join(out)


def match_session(spoken, sessions):
    """The session the caller named, or None if it's unclear. Sessions are {"key", "label",
    "name"}; each part of a hyphenated key counts too ("the CA" -> ca-personal)."""
    said = compact(re.sub(r"\b(?:the|my|a|session|conversation)\b", " ", spoken, flags=re.I))
    scores = []
    for s in sessions:
        names = {s["key"], s["label"], s["name"].split(":")[-1], *s["key"].split("-")}
        scores.append((max(difflib.SequenceMatcher(None, said, compact(n)).ratio() for n in names), s))
    scores.sort(key=lambda x: -x[0])
    if not scores or scores[0][0] < 0.6 or (len(scores) > 1 and scores[1][0] > scores[0][0] - 0.1):
        return None
    return scores[0][1]


def spoken_label(key):
    """How a session key is said: [gateway] labels, else the key with "-" and "_" as spaces and
    letters and digits apart ("web01" -> "web 01")."""
    labels = config.section("gateway").get("labels", {})
    if key in labels:
        return labels[key]
    return re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", key.replace("-", " ").replace("_", " "))


def spoken_when(at):
    """When a message was left: "today at 2:32 pm", "yesterday at ...", "on Monday the 26th
    at ..."."""
    t = time.localtime(at)
    clock = time.strftime("%I:%M %p", t).lstrip("0").replace("AM", "a m").replace("PM", "p m")
    days = (datetime.date.today() - datetime.date(t.tm_year, t.tm_mon, t.tm_mday)).days
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"yesterday at {clock}"
    day = t.tm_mday
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"on {time.strftime('%A', t)} the {day}{suffix} at {clock}"


class Phrases:
    """Split streamed text into phrases worth speaking as they complete: each line, and each
    sentence of a long line. Code blocks are left out (said as "code omitted")."""

    def __init__(self):
        self.buf, self.in_code = "", False

    def feed(self, text):
        self.buf += text
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.lstrip().startswith("```"):
                if self.in_code:
                    out.append("(code omitted.)")
                self.in_code = not self.in_code
            elif not self.in_code and line.strip():
                out.append(line)
        if not self.in_code and not self.buf.lstrip().startswith("`"):
            m = None
            for m in re.finditer(r"[.!?](?=\s)", self.buf):
                pass
            if m and m.end() >= 40:
                out.append(self.buf[:m.end()])
                self.buf = self.buf[m.end():].lstrip()
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        code, self.in_code = self.in_code, False
        return ["(code omitted.)"] if code else ([rest] if rest.strip() else [])
