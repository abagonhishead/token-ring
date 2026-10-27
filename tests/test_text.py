from token_ring.text import Phrases, compact, match_session, pronounce, speakable, spoken_label


def test_speakable_strips_markdown():
    text = "# Done\n\n- **first** item\n- see [the docs](https://x.y)\n\n```sh\nrm -rf /\n```\n| a | b |\nhttps://example.com"
    assert speakable(text) == "Done. first item. see the docs. (code omitted.) a link."


def test_compact_numbers():
    assert compact("rass pie four double oh") == "rasspie400"


SESSIONS = [{"key": k, "label": spoken_label(k), "name": k} for k in ("ca-personal", "tv", "token-ring", "rea")]


def test_match_session(conf):
    assert match_session("the CA", SESSIONS)["key"] == "ca-personal"
    assert match_session("token ring", SESSIONS)["key"] == "token-ring"
    assert match_session("something else entirely", SESSIONS) is None


def test_labels_from_config(conf):
    conf('[gateway.labels]\nrc = "general use"\n')
    sessions = [{"key": "rc", "label": spoken_label("rc"), "name": "rc"}] + SESSIONS
    assert match_session("general use", sessions)["key"] == "rc"


def test_pronounce_longest_first(conf):
    conf('[pronunciations]\npi = "pie"\nraspi = "rass pie"\n')
    assert pronounce("raspi and pi, not pip") == "rass pie and pie, not pip"


def test_phrases_stream():
    p = Phrases()
    out = p.feed("Hello there.\nThis is a long sentence that goes on for a while. And")
    assert out == ["Hello there.", "This is a long sentence that goes on for a while."]
    assert p.feed(" more\n```\ncode\n") == ["And more"]
    assert p.feed("```\n") == ["(code omitted.)"]
    assert p.flush() == []
