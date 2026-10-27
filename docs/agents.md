# token-ring for AI agents

This is what an AI agent needs to know to use the phone: ring the user, leave them voicemail,
handle what they say on a call, and hang up. It's written to be given to the agent as-is. Put it
wherever your agent reads instructions: a Claude Code skill (`~/.claude/skills/phone/SKILL.md`,
with a `name` and `description` front matter block), an `AGENTS.md`, a system prompt, or your own
tool's equivalent.

The commands talk to the token-ring gateway on the same machine. They work from any agent that
can run shell commands; which session the agent is in comes from the gateway's backend or from
`TOKEN_RING_SESSION`.

---

## Phone and voicemail (token-ring)

You can ring the user's phone, leave them voicemail, and talk to them by voice.

### Ringing them

```sh
timeout 90 token-ring call "The release build finished and all tests passed. Want me to publish it?"
```

Ring them when they asked to be called ("call me when the build finishes", "ring me if it
fails"), or when something needs them promptly and they aren't replying. Not for routine
progress updates.

When they answer, the message is read aloud and they're talking to you: what they say arrives as
messages starting `[Phone call. This message came through speech-to-text ...]`. The command
waits up to about a minute and prints the outcome:

- **Answered** (exit 0): carry on; their reply arrives as a normal message.
- **No answer** / **Call failed** (exit 1): leave the news as you normally would, or a
  voicemail. Don't ring again straight away.
- **Not calling: ...**: why (another call in progress, you rang in the last two minutes, or the
  phone system isn't connected).

### Leaving voicemail

```sh
timeout 30 token-ring voicemail "The nightly backup finished, but two files failed to copy. Nothing needs doing tonight."
```

It waits until they check their voicemail, then it's read out in your voice with when you left
it. Prefer voicemail to ringing for anything that can wait, and don't leave several where one
would do.

### Writing for the ear

Everything you send by phone or voicemail, and everything you write while they're on a call, is
read aloud by text-to-speech:

- A few short spoken sentences, the point first. No Markdown, lists, tables, code, URLs or long
  paths: say "the website project", not a path.
- Make calls and voicemail self-contained: they may be away from the screen, or hear it hours
  later. Say which project or task it's about, and what you need from them.
- No secrets: it's played aloud wherever they are.
- Speech-to-text mishears names and jargon: read their messages charitably and check anything
  ambiguous before acting on it.

### On a call

- Before anything that takes a while, say in a sentence what you're doing; they hear a soft pip
  while you work.
- They can interrupt you (keypad `*`, or saying "cancel that"). That stops your current turn
  only. If they ask you to stop or cancel something, use your judgement: never kill background
  work or throw away work in progress unless they confirm that's what they mean.
- Multiple-choice questions you ask are read out and answered with the keypad. Permission
  prompts can't be answered by phone: the call tells them to answer it elsewhere and hangs up.
- When the conversation's over (they say goodbye or ask you to hang up), say goodbye and run
  `token-ring hangup`: the call ends once your goodbye has been spoken.

### Voicemail from them

Their voicemails arrive as a message starting `[Voicemail from <name>, through speech-to-text
...]`. They aren't on the line: treat it as an ordinary request, and if they want an answer,
leave them a voicemail (or ring them if it's urgent).
