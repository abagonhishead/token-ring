"""Contacts: address books for the phones, on a CardDAV server (docker/contacts: Radicale).

The web page keeps two, as [contacts] owner: "contacts" (people, edited on its Contacts tab or
with any CardDAV app) and "directory" (the PBX's own numbers: the phones, the operator and
voicemail, and each session's direct line, rewritten as they change). Phones read both with one
read-only login, [contacts] reader, which provisioning sets up in Linphone. The passwords are in
[contacts] users_file, one "user:password" line each (Radicale's htpasswd, plain).

[contacts]
url = "https://pbx.example.com:5232"   # the server, as the phones reach it ("": no contacts)
local_url = "https://127.0.0.1:5232"   # as the web page reaches it (its certificate isn't checked)
users_file = "/run/secrets/contacts_users"
owner = "token-ring"
reader = "phones"
[contacts.directory]                   # numbers for the directory, beside the phones and sessions
"200" = "Operator"
"201" = "Voicemail"
"""
import base64
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config

log = logging.getLogger("token-ring")

BOOKS = {"contacts": "Contacts", "directory": "PBX directory"}


def conf():
    return config.section("contacts")


def enabled():
    return bool(conf().get("url"))


def password(user):
    try:
        with open(config.expand(conf()["users_file"])) as f:
            for line in f:
                name, sep, secret = line.strip().partition(":")
                if sep and name == user:
                    return secret
    except OSError:
        pass
    return ""


def book_url(book, base=None):
    return f"{(base or conf()['url']).rstrip('/')}/{conf()['owner']}/{book}/"


# CardDAV, as the owner

def request(method, path, body=None, headers=None):
    """(status, body) for a request to the server (path under its root), as the owner."""
    c = conf()
    url = c["local_url"].rstrip("/") + path
    auth = base64.b64encode(f"{c['owner']}:{password(c['owner'])}".encode()).decode()
    req = urllib.request.Request(url, data=body.encode() if isinstance(body, str) else body, method=method,
                                 headers={"Authorization": f"Basic {auth}", **(headers or {})})
    context = None
    if url.startswith("https:"):
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE   # (itself, by address)
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


MKCOL = """<?xml version="1.0" encoding="utf-8"?>
<mkcol xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav"><set><prop>
<resourcetype><collection/><C:addressbook/></resourcetype><displayname>{name}</displayname>
</prop></set></mkcol>"""


def ensure_books():
    """Make the address books if they aren't there yet."""
    owner = conf()["owner"]
    request("PROPFIND", f"/{owner}/", headers={"Depth": "0"})   # (makes the owner's principal)
    for book, name in BOOKS.items():
        code, _ = request("PROPFIND", f"/{owner}/{book}/", headers={"Depth": "0"})
        if code == 404:
            code, body = request("MKCOL", f"/{owner}/{book}/", MKCOL.format(name=name),
                                 {"Content-Type": "application/xml"})
            if code not in (200, 201):
                raise OSError(f"can't make the {book} address book: {code} {body[:200]!r}")


def cards(book):
    """{uid: vCard text} in an address book."""
    code, body = request("GET", f"/{conf()['owner']}/{book}/")
    if code != 200:
        raise OSError(f"can't read the {book} address book: {code}")
    out = {}
    for text in re.findall(r"BEGIN:VCARD\r?\n.*?END:VCARD", body.decode("utf-8", "replace"), re.S):
        uid = parse(text).get("uid")
        if uid:
            out[uid] = text.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n"
    return out


def put(book, uid, text):
    code, body = request("PUT", f"/{conf()['owner']}/{book}/{urllib.parse.quote(uid)}.vcf", text,
                         {"Content-Type": "text/vcard; charset=utf-8"})
    if code not in (200, 201, 204):
        raise OSError(f"can't save the contact: {code} {body[:200]!r}")


def delete(book, uid):
    code, _ = request("DELETE", f"/{conf()['owner']}/{book}/{urllib.parse.quote(uid)}.vcf")
    if code not in (200, 204, 404):
        raise OSError(f"can't delete the contact: {code}")


# vCards (4.0): a name, phone numbers (TEL) and SIP addresses (IMPP), which Linphone reads

def esc(value):
    return re.sub(r"([\\,;])", r"\\\1", str(value)).replace("\n", "\\n")


def unesc(value):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def card(name, tels=(), sips=(), note="", uid=None):
    lines = ["BEGIN:VCARD", "VERSION:4.0", f"UID:{uid or uuid.uuid4()}", f"FN:{esc(name)}", f"N:{esc(name)};;;;"]
    lines += [f"TEL;VALUE=uri:tel:{re.sub(r'[^0-9+*#]', '', t)}" for t in tels if t.strip()]
    lines += [f"IMPP:{s if s.startswith('sip:') else 'sip:' + s}" for s in sips if s.strip()]
    if note:
        lines.append(f"NOTE:{esc(note)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def parse(text):
    """A vCard's uid, name, phone numbers, SIP addresses and note."""
    text = re.sub(r"\r?\n[ \t]", "", text)   # unfold
    out = {"uid": "", "name": "", "tels": [], "sips": [], "note": ""}
    for line in text.splitlines():
        head, sep, value = line.partition(":")
        if not sep:
            continue
        prop = head.split(";")[0].upper().split(".")[-1]
        if prop == "UID":
            out["uid"] = value.strip()
        elif prop == "FN":
            out["name"] = unesc(value).strip()
        elif prop == "TEL":
            out["tels"].append(re.sub(r"^tel:", "", value.strip()))
        elif prop == "IMPP" or (prop == "X-SIP"):
            out["sips"].append(value.strip())
        elif prop == "NOTE":
            out["note"] = unesc(value)
    return out


def people():
    """The contacts book, as dicts, by name."""
    return sorted((parse(t) for t in cards("contacts").values()), key=lambda c: c["name"].lower())


def save(contact):
    """Add or change a contact ({uid?, name, tels, sips, note}); returns its uid."""
    name = " ".join(str(contact.get("name", "")).split())
    if not name:
        raise ValueError("a contact needs a name")
    tels = [t for t in (contact.get("tels") or []) if isinstance(t, str) and re.sub(r"[^0-9]", "", t)]
    sips = [s for s in (contact.get("sips") or []) if isinstance(s, str) and s.strip()]
    if not tels and not sips:
        raise ValueError("a contact needs a number or a SIP address")
    uid = contact.get("uid") or str(uuid.uuid4())
    if not re.fullmatch(r"[\w.@-]{1,100}", uid):
        raise ValueError("that isn't a contact's id")
    put("contacts", uid, card(name, tels, sips, str(contact.get("note", "")), uid))
    return uid


# The directory: what's on the PBX itself

def directory(endpoints, sessions, host):
    """{uid: vCard} for the phones (endpoints: [(extension, name)]), [contacts.directory] and the
    sessions' direct lines (gateway sessions: key, label, line)."""
    entries = [(ext, name or f"Phone {ext}", "phone") for ext, name in endpoints]
    entries += [(ext, name, "service") for ext, name in conf().get("directory", {}).items()]
    entries += [(s["line"], f"{s['label'][:1].upper()}{s['label'][1:]} session", f"session {s['key']}")
                for s in sessions if s.get("line")]
    return {f"pbx-{ext}": card(name, sips=[f"sip:{ext}@{host}"], note=f"Extension {ext} ({what})", uid=f"pbx-{ext}")
            for ext, name, what in entries}


def sync_directory(endpoints, sessions, host):
    """Rewrite the directory book to match; returns how many cards changed."""
    want = directory(endpoints, sessions, host)
    have = cards("directory")
    changed = 0
    for uid, text in want.items():
        if uid not in have or parse(have[uid]) != parse(text):
            put("directory", uid, text)
            changed += 1
    for uid in set(have) - set(want):
        delete("directory", uid)
        changed += 1
    return changed


def directory_loop(endpoints_and_sessions, host, every_s=60):
    """Keep the directory up to date (for the web page, in a thread). endpoints_and_sessions()
    gives (endpoints, sessions) as directory() takes them."""
    ready, warned = False, False
    while True:
        try:
            if not ready:
                ensure_books()
                ready = True
            endpoints, sessions = endpoints_and_sessions()
            if endpoints or sessions:
                n = sync_directory(endpoints, sessions, host)
                if n:
                    log.info("contacts: %d directory entries updated", n)
            warned = False
        except Exception as e:
            if not warned:
                log.warning("contacts: %s", e)
                warned = True
        time.sleep(every_s)
