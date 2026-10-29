import asyncio
import json
import os
import threading
import urllib.request

import pytest

from token_ring import bridge, config, web


def test_seed(tmp_path):
    image, live = tmp_path / "image", tmp_path / "live"
    image.mkdir()
    live.mkdir()
    (image / "config.toml").write_text("a = 1\n")
    assert config.seed(image, live) == {"config.toml": "seeded"}
    (image / "config.toml").write_text("a = 2\n")             # a new image, not edited here
    assert config.seed(image, live) == {"config.toml": "updated"}
    assert (live / "config.toml").read_text() == "a = 2\n"
    (live / "config.toml").write_text("a = 99\n")             # edited here
    (image / "config.toml").write_text("a = 3\n")             # and a new image
    assert config.seed(image, live) == {"config.toml": "kept edit"}
    assert (live / "config.toml").read_text() == "a = 99\n"
    assert (live / "config.toml.image-new").read_text() == "a = 3\n"


def test_seed_leaves_mounted_files_alone(tmp_path):
    image, live = tmp_path / "image", tmp_path / "live"
    image.mkdir()
    live.mkdir()
    (image / "config.toml").write_text("a = 1\n")
    (live / "config.toml").write_text("mine = true\n")        # no record: mounted from outside
    assert config.seed(image, live) == {"config.toml": "unchanged"}
    assert (live / "config.toml").read_text() == "mine = true\n"


def test_ami_secret_by_section(conf, tmp_path):
    f = tmp_path / "manager_auth.conf"
    f.write_text("[token-ring]\nsecret = one\nread = user\n\n[token-ring-web]\nsecret = two\n")
    conf(f'[bridge]\nami_secret_file = "{f}"\n')
    assert bridge.ami_secret() == "one"
    assert bridge.ami_secret("token-ring-web") == "two"


def test_redact():
    assert web.redact("asterisk", "[100]\npassword = hunter2\nusername = 100\n") == \
        "[100]\npassword = ••••••\nusername = 100\n"
    assert web.redact("token-ring", 'gateway_token = "abc"\nami_secret = "x"\nname = "y"\n') == \
        'gateway_token = "••••••"\nami_secret = "••••••"\nname = "y"\n'


@pytest.fixture
def site(conf, tmp_path, monkeypatch):
    etc = tmp_path / "asterisk"
    etc.mkdir()
    (etc / "pjsip.conf").write_text("[global]\ntype = global\n")
    (etc / ".image").mkdir()
    (etc / ".image" / "pjsip.conf").write_text("[global]\ntype = global\n")
    cfg = tmp_path / "config.toml"
    port = 18555
    cfg.write_text(f'[web]\nasterisk_dir = "{etc}"\nasterisk_log = "{tmp_path}/full.log"\nport = {port}\n'
                   f'listen = "127.0.0.1"\ncert_file = ""\n')
    conf(cfg.read_text())
    reloads = []
    monkeypatch.setattr(web, "reload_asterisk", lambda name: reloads.append(name) or {"reload": "ok", "warnings": []})
    server = web.Server(("127.0.0.1", port), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", etc, cfg, reloads
    server.shutdown()


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_config_edit_and_backup(site):
    base, etc, cfg, reloads = site
    code, files = call(base, "GET", "/api/config")
    names = {(f["kind"], f["name"]): f for f in files["files"]}
    assert ("asterisk", "pjsip.conf") in names and not names["asterisk", "pjsip.conf"]["edited"]
    code, r = call(base, "PUT", "/api/config/asterisk/pjsip.conf", {"content": "[global]\ntype = global\n; hi\n"})
    assert code == 200 and r["changed"] and reloads == ["pjsip.conf"]
    assert (etc / "pjsip.conf").read_text().endswith("; hi\n")
    code, f = call(base, "GET", "/api/config/asterisk/pjsip.conf?raw=1")
    assert len(f["backups"]) == 1 and f["image"] == "[global]\ntype = global\n"
    code, b = call(base, "GET", f"/api/config/asterisk/pjsip.conf/backup/{f['backups'][0]}")
    assert b["content"] == "[global]\ntype = global\n"
    code, files = call(base, "GET", "/api/config")
    assert {(x["kind"], x["name"]): x for x in files["files"]}["asterisk", "pjsip.conf"]["edited"]


def test_allowed_callers(site, monkeypatch):
    base, etc, cfg, reloads = site

    async def uk():
        return "44"
    monkeypatch.setattr(web, "country_code", uk)
    code, r = call(base, "GET", "/api/callers")
    assert r["exists"] is False and r["callers"] == [] and r["country_code"] == "44"
    code, r = call(base, "PUT", "/api/callers", {"callers": [
        {"name": "Someone", "number": "+44 7700 900123"}, {"name": "A; landline\n", "number": "01632 960000"},
        {"name": "again", "number": "07700900123"}, {"number": "0033 1 23 45 67 89"}]})
    assert code == 200 and r["changed"] and reloads == ["callers.conf"]
    assert r["callers"] == [{"number": "07700900123", "name": "Someone"}, {"number": "01632960000", "name": "A landline"},
                            {"number": "+33123456789", "name": ""}]
    text = (etc / "callers.conf").read_text()
    assert "[allowed-callers]\nexten => 07700900123,1,NoOp() ; Someone\n" in text
    code, r = call(base, "PUT", "/api/callers", {"callers": [{"number": "not a number"}]})
    assert code == 400 and "isn't a phone number" in r["error"]
    assert (etc / "callers.conf").read_text() == text


def test_config_refuses_bad_names_and_masked_text(site):
    base, etc, cfg, reloads = site
    assert call(base, "GET", "/api/config/asterisk/..%2Fpasswd")[0] == 404
    assert call(base, "PUT", "/api/config/asterisk/x.sh", {"content": "x"})[0] == 404
    code, r = call(base, "PUT", "/api/config/asterisk/pjsip.conf", {"content": "password = ••••••\n"})
    assert code == 400 and reloads == []


def test_token_ring_config_must_be_valid_toml(site):
    base, etc, cfg, reloads = site
    before = cfg.read_text()
    code, r = call(base, "PUT", "/api/config/token-ring/config.toml", {"content": "[web\n"})
    assert code == 400 and "TOML" in r["error"] and cfg.read_text() == before
    code, r = call(base, "PUT", "/api/config/token-ring/config.toml", {"content": before + "\n[user]\nname = \"X\"\n"})
    assert code == 200 and config.get()["user"]["name"] == "X"


def test_page_is_served(site):
    base = site[0]
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert b"token-ring PBX" in r.read()


def test_file_logs(site, tmp_path):
    base = site[0]
    (tmp_path / "full.log").write_text("".join(f"line {i}\n" for i in range(50)))
    code, r = call(base, "GET", "/api/logs?source=asterisk&lines=5")
    assert r["lines"] == [f"line {i}" for i in range(45, 50)]


def test_gateway_log_buffer():
    import logging
    from token_ring import gateway
    buf = gateway.LogBuffer(size=3)
    lg = logging.getLogger("test-buffer")
    lg.addHandler(buf)
    lg.propagate = False
    for i in range(5):
        lg.warning("line %d", i)
    lines, count = buf.since(0)
    assert count == 5 and [l.split(" ", 2)[2] for l in lines] == ["line 2", "line 3", "line 4"]
    lg.warning("line 5")
    lines, count = buf.since(5)
    assert count == 6 and lines[0].endswith("line 5")


def test_status_splits_trunks_from_phones(conf):
    """A trunk (outbound registration, identify by address) is listed apart from the phones."""
    conf("")

    class Conn:
        async def list(self, f):
            a = f["Action"]
            if a == "PJSIPShowEndpoints":
                return [{"ObjectName": "100", "DeviceState": "Not in use"}, {"ObjectName": "trunk", "DeviceState": "Not in use"}]
            if a == "PJSIPShowEndpoint" and f["Endpoint"] == "100":
                return [{"Event": "EndpointDetail", "Callerid": '"Phone" <100>', "Context": "internal"},
                        {"Event": "AuthDetail", "Password": "secret100"}]
            if a == "PJSIPShowEndpoint":
                return [{"Event": "EndpointDetail", "Context": "from-trunk", "Allow": "(alaw)"},
                        {"Event": "AuthDetail", "Password": "secrettrunk"},
                        {"Event": "ContactStatusDetail", "Status": "Reachable", "RoundtripUsec": "21000"},
                        {"Event": "IdentifyDetail", "Match": "192.0.2.1/255.255.255.255"}]
            if a == "PJSIPShowRegistrationsOutbound":
                return [{"Event": "OutboundRegistrationDetail", "ObjectName": "trunk",
                         "ClientUri": "sip:+441632960000@sip.example.net", "ServerUri": "sip:sip.example.net",
                         "Status": "Registered"},
                        {"Event": "AuthDetail", "ObjectName": "trunk", "Password": "secretreg"}]
            return []

        async def action(self, f):
            return {"Response": "Success"}

    st = asyncio.run(web.gather_status(Conn()))
    assert [e["name"] for e in st["endpoints"]] == ["100"]
    t, = st["trunks"]
    assert (t["number"], t["server"], t["registration"], t["status"], t["rtt_ms"], t["match"], t["context"]) == \
        ("+441632960000", "sip.example.net", "Registered", "Reachable", 21, "192.0.2.1", "from-trunk")
    assert "secret" not in json.dumps(st)


def test_call_log(site, monkeypatch, tmp_path):
    base, etc, cfg, reloads = site
    log_file = tmp_path / "calls.csv"
    cfg.write_text(cfg.read_text().replace("[web]\n", f'[web]\ncalls_log = "{log_file}"\ncalls_keep_days = 90\n'))
    config._cache.update(path=None, mtime=None, config=None)

    async def uk():
        return "44"
    monkeypatch.setattr(web, "country_code", uk)
    (etc / "callers.conf").write_text("[allowed-callers]\nexten => 07700900123,1,NoOp() ; Someone\n")
    now = 1_800_000_000
    def row(start, *rest):
        return ",".join('"' + str(x).replace('"', '""') + '"' for x in (start, *rest)) + "\n"   # as CSV_QUOTE does
    log_file.write_text(
        row(now - 100 * 86400, 0, now - 100 * 86400 + 5, 5, 0, "NO ANSWER", "07700900999", '"" <07700900999>', "aa-in",
            "from-trunk", "PJSIP/trunk-00000001", "", "Hangup", "1.1", "", "rejected")
        + row(now - 60, now - 58, now - 30, 30, 28, "ANSWERED", "+447700900123", '"Mobile" <+447700900123>', "aa-in",
              "from-trunk", "PJSIP/trunk-00000002", "", "Playback", "1.2", "", "allowed")
        + row(now - 10, now - 5, now, 10, 5, "ANSWERED", "600", '"Claude: app" <600>', "s", "token-ring-callback",
              "PJSIP/trunk-00000003", "", "Dial", "1.3", "PJSIP/07700900123@trunk", ""))
    calls = web.read_calls()
    assert [c["uniqueid"] for c in calls] == ["1.3", "1.2", "1.1"]          # newest first
    assert calls[1]["name"] == "Someone" and calls[1]["caller_name"] == "Mobile" and calls[1]["screen"] == "allowed"
    assert calls[0]["to_number"] == "07700900123" and calls[0]["to_name"] == "Someone"
    assert calls[2]["name"] == "" and calls[2]["answer"] is None
    code, r = call(base, "GET", "/api/calls")
    assert code == 200 and len(r["calls"]) == 3
    assert web.prune_calls(now=now) == 1                                    # the 100-day-old one goes
    assert [c["uniqueid"] for c in web.read_calls()] == ["1.3", "1.2"]
    assert web.prune_calls(now=now) == 0


def test_voices_saved(conf, tmp_path):
    conf('''# top
[tts]
default = "piper"

# Who speaks in which voice.
[voices]
operator = "piper/a"
pool = ["piper/a", "piper/b"]   # old style
rate = 0.9
[voices.names]
"piper/a" = "Ann"

# Trigger words
[keywords]
engine = "off"
''')
    v = web.read_voices()
    assert v["groups"] == {"pool": ["piper/a", "piper/b"]} and v["default"] == "@pool"
    v["groups"]["team"] = ["piper/b", "piper/c:x y"]
    v["sessions"]["my-project"] = "@team"
    v["names"]["piper/c:x y"] = "  Cee  "
    v["disabled"] = ["piper/b"]
    web.write_voices(v)
    text = (tmp_path / "config.toml").read_text()
    assert text.startswith("# top\n[tts]") and "# Who speaks in which voice.\n[voices]\n" in text
    assert "\n\n# Trigger words\n[keywords]\nengine = \"off\"\n" in text
    import tomllib
    assert "pool" not in tomllib.loads(text)["voices"]   # the old pool is a group now
    config._cache.update(mtime=None)
    v2 = web.read_voices()
    assert v2["sessions"] == {"my-project": "@team"} and v2["names"]["piper/c:x y"] == "Cee"
    assert v2["rate"] == 0.9 and v2["disabled"] == ["piper/b"] and v2["default"] == "@pool"
    assert config.section("keywords")["engine"] == "off"
    from token_ring.tts import voice_for
    assert voice_for("my-project") == "piper/c:x y"
    with pytest.raises(ValueError):
        web.write_voices({**v2, "operator": "@nope"})
    with pytest.raises(ValueError):
        web.write_voices({**v2, "rate": 9})


def test_splice_table():
    assert web.splice_table("a = 1\n", "voices", "[voices]\nx = 1\n") == "a = 1\n\n[voices]\nx = 1\n"
    with pytest.raises(ValueError):
        web.splice_table("[voices]\n[other]\n[voices.names]\n", "voices", "[voices]\n")
