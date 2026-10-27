from token_ring.backends.claude_code import parse_dialog, parse_draft

QUESTION = """
● Here are three things I changed:
  1. The parser
  2. The tests
  3. The docs

────────────────────────────────────────────────────────
 ←  ☐ Colour  ☐ Size  ✔ Submit  →

Which colour do you prefer?

❯ 1. Red
     Warm and bright
  2. Blue
     Cool
  3. Type something.
  4. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

MULTI = """
────────────────────────────────────────────────────────
 ←  ☒ Colour  ☐ Toppings  ✔ Submit  →

Which toppings?

❯ 1. [✔] Cheese
  2. [ ] Ham
  3. [✔] Olives
  4. [ ] Type something

Enter to select · Tab to switch questions · Esc to cancel
"""

REVIEW = """
 ←  ☒ Colour  ☒ Toppings  ✔ Submit  →

Review your answers

 ● Which colour do you prefer?
   → Red
 ● Which toppings?
   → Cheese, Olives

Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel
"""

PERMISSION = """
────────────────────────────────────────────────────────
 Bash command

   rm -rf build
   Remove the build directory

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /home/me/app
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to amend
"""

NOTICE = """
────────────────────────────────────────────────────────
 New MCP server found in .mcp.json: example

 ❯ 1. Use this and all future MCP servers in this project
   2. Use this MCP server
   3. Continue without using this MCP server

 Enter to confirm · Esc to cancel
"""


def test_question_ignores_numbered_reply_and_extras():
    d = parse_dialog(QUESTION)
    assert d["kind"] == "question"
    assert d["question"] == "Which colour do you prefer?"
    assert d["options"] == [["1", "Red"], ["2", "Blue"]]
    assert (d["count"], d["index"]) == (2, 1)


def test_multi_select():
    d = parse_dialog(MULTI)
    assert d["kind"] == "multi"
    assert d["checked"] == ["1", "3"]
    assert [o[1] for o in d["options"]] == ["Cheese", "Ham", "Olives"]
    assert (d["count"], d["index"]) == (2, 2)


def test_review():
    d = parse_dialog(REVIEW)
    assert d["kind"] == "review"
    assert "Cheese, Olives" in d["summary"]


def test_permission_is_never_a_question():
    assert parse_dialog(PERMISSION)["kind"] == "permission"


def test_notice_with_permission_words_is_permission():
    assert parse_dialog(NOTICE)["kind"] == "notice"
    assert parse_dialog(NOTICE.replace("New MCP server", "Allow this MCP server"))["kind"] == "permission"


def test_no_dialog():
    assert parse_dialog("● All done.\n  1. one\n  2. two\n\n❯ \n") is None


def test_draft_ignores_dim_placeholder():
    box = "───────\n❯ \x1b[2mTry \"fix lint errors\"\x1b[0m\n───────\n"
    assert parse_draft(box) == ""
    assert parse_draft("───────\n❯ half a thought\n───────\n") == "half a thought"
