"""Contacts against a real Radicale (skipped without it), with docker/contacts' rights file."""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from token_ring import contacts

pytest.importorskip("radicale")
ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture
def server(conf, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    users = tmp_path / "users"
    users.write_text("token-ring:ownerpw\nphones:readpw\n")
    cfg = tmp_path / "radicale.conf"
    cfg.write_text(open(os.path.join(ROOT, "docker/contacts/config")).read()
                   .replace("/run/secrets/contacts_users", str(users))
                   .replace("/etc/radicale/rights", os.path.join(ROOT, "docker/contacts/rights"))
                   .replace("/var/lib/radicale/collections", str(tmp_path / "collections"))
                   .replace("0.0.0.0:5232, [::]:5232", f"127.0.0.1:{port}"))
    proc = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(cfg)])
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except urllib.error.HTTPError:
            break
        except OSError:
            time.sleep(0.1)
    conf(f'[contacts]\nurl = "{url}"\nlocal_url = "{url}"\nusers_file = "{users}"\n'
         f'[provision]\nname = "pbx.example.com"\n')
    yield url
    proc.terminate()
    proc.wait()


def test_vcard_round_trip():
    text = contacts.card("Jo, Smith; Jr", ["07700 900123", "+447700900124"], ["sip:jo@example.com"], "line one\nline two", "u1")
    assert contacts.parse(text) == {"uid": "u1", "name": "Jo, Smith; Jr", "tels": ["07700900123", "+447700900124"],
                                    "sips": ["sip:jo@example.com"], "note": "line one\nline two"}


def test_contacts_and_directory(server):
    contacts.ensure_book()
    uid = contacts.save({"name": "Ann Example", "tels": ["07700 900123"]})
    contacts.save({"name": "Bob", "sips": ["bob@example.com"]})
    assert [p["name"] for p in contacts.people()] == ["Ann Example", "Bob"]
    contacts.save({"uid": uid, "name": "Ann E", "tels": ["07700900999"]})
    assert contacts.people()[0]["tels"] == ["07700900999"]
    with pytest.raises(ValueError):
        contacts.save({"name": "Nobody"})
    contacts.delete(uid)
    assert [p["name"] for p in contacts.people()] == ["Bob"]

    sessions = [{"key": "rc", "label": "general use", "line": "301"}, {"key": "x", "label": "x", "line": ""}]
    assert contacts.sync_directory([("100", "Phone")], sessions, "pbx.example.com") == 4
    assert contacts.sync_directory([("100", "Phone")], sessions, "pbx.example.com") == 0   # nothing changed
    names = [c["name"] for c in contacts.split()[1]]
    assert names == ["Phone", "Operator", "Voicemail", "General use session"]
    assert [p["name"] for p in contacts.people()] == ["Bob"]   # people and the directory kept apart
    assert contacts.sync_directory([("100", "Phone")], [], "pbx.example.com") == 1      # the line's gone

    # the phones' login reads, but can't write
    import base64
    def as_phones(method, path, body=None):
        req = urllib.request.Request(server + path, data=body, method=method,
                                     headers={"Authorization": "Basic " + base64.b64encode(b"phones:readpw").decode()})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    assert as_phones("GET", "/phones/pbx/") == 200
    # Linphone searches as you type (an addressbook-query on the name)
    query = b"""<?xml version="1.0" encoding="utf-8"?>
<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
<D:prop><C:address-data/></D:prop>
<C:filter><C:prop-filter name="FN"><C:text-match match-type="contains">bo</C:text-match></C:prop-filter></C:filter>
</C:addressbook-query>"""
    req = urllib.request.Request(server + "/phones/pbx/", data=query, method="REPORT",
                                 headers={"Authorization": "Basic " + base64.b64encode(b"phones:readpw").decode(),
                                          "Depth": "1", "Content-Type": "application/xml"})
    found = urllib.request.urlopen(req, timeout=5).read().decode()
    assert "FN:Bob" in found
    assert as_phones("PUT", "/phones/pbx/evil.vcf", contacts.card("Evil", ["1"]).encode()) == 403
    assert as_phones("DELETE", "/phones/pbx/") == 403 and as_phones("MKCOL", "/phones/other/") == 403

    # Linphone's sync: the login's principal, its address book home, the first address book there
    def propfind(path, prop, depth="0"):
        body = (f'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                f'<d:prop>{prop}</d:prop></d:propfind>').encode()
        req = urllib.request.Request(server + path, data=body, method="PROPFIND",
                                     headers={"Authorization": "Basic " + base64.b64encode(b"phones:readpw").decode(),
                                              "Depth": depth, "Content-Type": "application/xml"})
        return urllib.request.urlopen(req, timeout=5).read().decode()
    import re
    principal = re.search(r"current-user-principal>\s*<[^>]*href>([^<]+)<", propfind("/phones/pbx/", "<d:current-user-principal/>")).group(1)
    home = re.search(r"addressbook-home-set>\s*<[^>]*href>([^<]+)<", propfind(principal, "<card:addressbook-home-set/>")).group(1)
    listing = propfind(home, "<d:resourcetype/><d:displayname/>", depth="1")
    assert (principal, home) == ("/phones/", "/phones/") and "/phones/pbx/" in listing and "addressbook" in listing


def test_provisioning_has_the_address_books(server):
    from token_ring import provision
    xml = provision.linphone_xml("100", "pw", {"name": "pbx.example.com", "transport": "tls", "sip_port": 5061})
    assert '<section name="carddav_0">' in xml and f'"server_url" overwrite="true">{server}/phones/pbx/' in xml
    assert '<entry name="passwd" overwrite="true">readpw</entry>' in xml
