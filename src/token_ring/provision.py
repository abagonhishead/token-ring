"""Set up a phone by remote provisioning (Linphone, and other apps that read its format).

  token-ring-provision 100    give extension 100 a new password, then serve its settings once
                              over HTTPS at a random address, and print the link
  token-ring-provision --keep-password 100
                              the same with its current password (setting the phone up again)

Opening the link on the phone shows a button that hands the settings to Linphone, which applies
them and forgets the address (or paste the address into Linphone's remote provisioning setting).
The settings are the account (registrar [provision] name, its new password, a display name
from [provision.names], the voicemail button dialling [bridge] voicemail_extension, the
voicemail light, push notifications off and the app kept running so it stays registered), the
address books ([contacts], with the phones' read-only login) and, with TLS, mandatory SRTP. The
address works once, and for [provision] wait_minutes at most; the old password stops working
straight away, so run it again if the phone didn't fetch it.

It writes the password into [provision] auth_file (the pjsip.conf include with the extensions'
passwords), so it needs write access to that, the certificate for [provision] name, and the
bridge's login to Asterisk's manager interface for the reload. In containers:
  docker compose run --rm provision 100
"""
import asyncio
import http.server
import logging
import re
import secrets
import ssl
import sys
import threading
import urllib.parse
from xml.sax.saxutils import escape

from . import bridge, config

log = logging.getLogger("token-ring")

PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Phone settings</title>
<body style="font: 18px system-ui, sans-serif; margin: 2em auto; max-width: 30em; padding: 0 1em">
<h1 style="font-size: 1.4em">Settings for extension {ext}</h1>
<p><a href="linphone-config:{url}" style="display: inline-block; padding: .8em 1.4em;
 background: #ff5e00; color: #fff; border-radius: .5em; text-decoration: none">Open in Linphone</a></p>
<p>Or paste this into the app's remote provisioning setting:</p>
<p style="word-break: break-all; font-family: monospace">{url}</p>
<p>It works once. Ask for a new link if this one fails.</p>
"""


def provision_conf():
    return config.section("provision")


def get_password(path, ext):
    """The password in [ext] of an Asterisk auth include."""
    section = None
    with open(path) as f:
        for line in f:
            m = re.match(r"\s*\[([^\]]+)\]", line)
            if m:
                section = m.group(1).strip()
            elif section == ext:
                m = re.match(r"\s*password\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    raise SystemExit(f"{path}: no password for [{ext}]")


def set_password(path, ext, password):
    """Replace the password in [ext] of an Asterisk auth include, in place (it's often a bind
    mount, which can't be replaced by renaming)."""
    with open(path, "r+") as f:
        lines = f.read().splitlines(keepends=True)
        section, done = None, False
        for i, line in enumerate(lines):
            m = re.match(r"\s*\[([^\]]+)\]", line)
            if m:
                section = m.group(1).strip()
            elif section == ext and re.match(r"\s*password\s*=", line):
                lines[i] = f"password = {password}\n"
                done = True
        if not done:
            raise SystemExit(f"{path}: no password for [{ext}]")
        f.seek(0)
        f.write("".join(lines))
        f.truncate()


async def reload_and_check(ext, password):
    """Reload PJSIP and confirm Asterisk has the new password."""
    await bridge.ami_action({"Action": "Command", "Command": "module reload res_pjsip.so"})
    out = (await bridge.ami_action({"Action": "Command", "Command": f"pjsip show auth {ext}"}))["Output"]
    return any(re.match(rf"\s*password\s*:\s*{re.escape(password)}\s*$", l) for l in out)


def linphone_xml(ext, password, c, voicemail=""):
    host, transport = c["name"], c["transport"]
    proxy = f"<sip:{host}:{c['sip_port']};transport={transport}>"
    name = c.get("names", {}).get(ext, "").replace('"', "")
    identity = f'"{name}" <sip:{ext}@{host}>' if name else f"sip:{ext}@{host}"
    sections = {
        "proxy_0": {"reg_proxy": proxy, "reg_route": proxy, "reg_identity": identity,
                    "reg_expires": 3600, "reg_sendregister": 1,
                    # the voicemail light: subscribe to this extension's mailbox (pjsip.conf)
                    "mwi_server_uri": f"sip:{ext}@{host}",
                    # and the app's voicemail button dials this
                    **({"voicemail_uri": f"sip:{voicemail}@{host}"} if voicemail else {}),
                    # Asterisk can't wake the app with a push notification: stay registered instead
                    "push_notification_allowed": 0, "remote_push_notification_allowed": 0},
        "app": {"keep_service_alive": 1, "auto_start": 1},
        "auth_info_0": {"username": ext, "userid": ext, "passwd": password, "domain": host},
        "misc": {"transient_provisioning": 1},  # apply once, then forget the address
    }
    sections.update(contact_directories())
    if c.get("media_encryption"):
        sections["sip"] = {"media_encryption": c["media_encryption"],
                           "media_encryption_mandatory": int(bool(c.get("media_encryption_mandatory", True)))}
    body = "".join(
        f'  <section name="{name}">\n' + "".join(
            f'    <entry name="{k}" overwrite="true">{escape(str(v))}</entry>\n' for k, v in entries.items())
        + "  </section>\n" for name, entries in sections.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<config xmlns="http://www.linphone.org/xsds/lpconfig.xsd">\n' + body + "</config>\n")


def contact_directories():
    """Linphone's settings for the address books ([contacts]: contacts.py): each is a CardDAV
    directory it searches as you type, read with the phones' shared read-only login."""
    from . import contacts
    c = contacts.conf()
    secret = contacts.password(c["reader"]) if contacts.enabled() else ""
    if not secret:
        return {}
    out = {f"remote_contact_directory_{i}": {"enabled": 1, "type": "carddav", "uri": contacts.book_url(book),
                                             "min_characters": 1, "results_limit": 0}
           for i, book in enumerate(contacts.BOOKS)}
    host = urllib.parse.urlsplit(c["url"]).hostname
    out["auth_info_1"] = {"username": c["reader"], "userid": c["reader"], "passwd": secret,
                          "realm": "token-ring contacts", "domain": host}
    return out


def serve(ext, xml, token, base, host, port, context=None, wait_s=600):
    """Serve the landing page at /<token> and the settings once at /<token>/linphone.xml.
    True once the settings have been fetched."""
    fetched = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == f"/{token}" and not fetched.is_set():
                self.reply(200, "text/html; charset=utf-8",
                           PAGE.format(ext=ext, url=escape(f"{base}/{token}/linphone.xml")))
            elif self.path == f"/{token}/linphone.xml" and not fetched.is_set():
                log.info("settings fetched by %s (%s)", self.client_address[0], self.headers.get("User-Agent", "?"))
                log.debug("request headers: %s", dict(self.headers))
                self.reply(200, "application/xml", xml)
                fetched.set()
            else:
                self.reply(404, "text/plain", "Not found\n")

        def reply(self, code, ctype, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    if context:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return fetched.wait(wait_s)
    finally:
        server.shutdown()
        server.server_close()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    keep = "--keep-password" in args
    args = [a for a in args if a != "--keep-password"]
    if len(args) != 1 or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0 if args else 2)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    ext, c = args[0], provision_conf()
    if not c.get("name"):
        sys.exit("[provision] name isn't set: the host name the phones register to (in the certificate)")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(config.expand(c["cert_file"]), config.expand(c["key_file"]))

    if keep:
        password = get_password(config.expand(c["auth_file"]), ext)
    else:
        password = secrets.token_urlsafe(24)
        set_password(config.expand(c["auth_file"]), ext, password)
        if not asyncio.run(reload_and_check(ext, password)):
            sys.exit("Asterisk didn't pick up the new password (module reload res_pjsip.so); nothing served")
        log.info("extension %s has a new password", ext)

    token = secrets.token_urlsafe(18)
    base = f"https://{c['name']}:{c['port']}"
    print(f"\nOpen on the phone:  {base}/{token}\n"
          f"or in Linphone:     linphone-config:{base}/{token}/linphone.xml\n", flush=True)
    voicemail = c.get("voicemail") or config.section("bridge").get("voicemail_extension", "")
    if serve(ext, linphone_xml(ext, password, c, voicemail), token, base, c["listen"], c["port"], context,
             c["wait_minutes"] * 60):
        log.info("done: the phone has its settings")
    else:
        sys.exit(f"not fetched within {c['wait_minutes']} minutes; run it again for a new link")


if __name__ == "__main__":
    main()
