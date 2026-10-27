import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from token_ring import provision

AUTH = """[100](auth-template)
username = 100
password = old-one

[101](auth-template)
username = 101
password = keep-me
"""

CONF = {"name": "pbx.example.net", "sip_port": 5061, "transport": "tls", "media_encryption": "srtp",
        "media_encryption_mandatory": True}


def test_set_password_changes_only_that_extension(tmp_path):
    f = tmp_path / "pjsip_auth.conf"
    f.write_text(AUTH)
    provision.set_password(f, "100", "new-one")
    text = f.read_text()
    assert "password = new-one" in text and "old-one" not in text
    assert "password = keep-me" in text


def test_set_password_unknown_extension(tmp_path):
    f = tmp_path / "pjsip_auth.conf"
    f.write_text(AUTH)
    with pytest.raises(SystemExit):
        provision.set_password(f, "102", "x")
    assert f.read_text() == AUTH


def entries(xml):
    ns = {"c": "http://www.linphone.org/xsds/lpconfig.xsd"}
    return {(s.get("name"), e.get("name")): e.text
            for s in ET.fromstring(xml).findall("c:section", ns) for e in s.findall("c:entry", ns)}


def test_linphone_xml():
    e = entries(provision.linphone_xml("100", "p&ss<word>", CONF))
    assert e["proxy_0", "reg_proxy"] == "<sip:pbx.example.net:5061;transport=tls>"
    assert e["proxy_0", "reg_route"] == e["proxy_0", "reg_proxy"]
    assert e["proxy_0", "reg_identity"] == "sip:100@pbx.example.net"
    assert e["auth_info_0", "passwd"] == "p&ss<word>"
    assert e["sip", "media_encryption"] == "srtp" and e["sip", "media_encryption_mandatory"] == "1"
    assert e["misc", "transient_provisioning"] == "1"


def test_linphone_xml_leaves_encryption_alone():
    e = entries(provision.linphone_xml("100", "pw", {**CONF, "media_encryption": ""}))
    assert ("sip", "media_encryption") not in e


def test_serve_once():
    result = {}
    port = 19443
    base = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=lambda: result.update(ok=provision.serve(
        "100", "<config/>", "tok", base, "127.0.0.1", port, None, wait_s=10)))
    t.start()
    for _ in range(50):
        try:
            page = urllib.request.urlopen(f"{base}/tok", timeout=2).read().decode()
            break
        except OSError:
            threading.Event().wait(0.1)
    assert f"linphone-config:{base}/tok/linphone.xml" in page
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/wrong", timeout=2)
    assert urllib.request.urlopen(f"{base}/tok/linphone.xml", timeout=2).read() == b"<config/>"
    t.join(5)
    assert result["ok"] is True
