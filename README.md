# token-ring

Talk to your AI agents on the phone. token-ring connects an Asterisk PBX to AI coding agents such
as Claude Code: dial in, an AI operator answers, and it puts you through to any of your sessions.
You talk, it talks back in its own voice, and the sessions can ring you or leave you voicemail
when they need you.

## What it can do

- **Talk to sessions by voice.** Replies are read aloud as they're written. Interrupt by saying
  "stop" or pressing `*`, and answer multiple-choice questions on the keypad.
- **An operator** (dial 200) that lists sessions, connects you, or starts new ones. It's an agent
  too, so you can just talk to it.
- **Calls and voicemail from your agents.** A session can ring you when a job's done or it's stuck,
  or leave voicemail read in its own voice (dial 201). You get a text or the phone's voicemail
  indicator when there's a new one.
- **Texting.** Text the operator from the phone to ask a session to ring you back.
- **A SIP trunk** for a real phone number: your mobile is rung when the softphone can't be
  reached, and only the callers you allow get in.
- **A web page** for the PBX: what's connected, live logs, and the configuration.
- **Your own call apps** (an alarm clock, say) beside the operator and voicemail.

## What you need

| | What works |
|---|---|
| A machine for the PBX | Linux with Docker and Compose. A Raspberry Pi 4 or 5 is enough. Asterisk, the bridge and Piper run there in containers |
| A SIP phone | Any SIP phone or softphone. Linphone can be set up from a one-time link, with TLS and SRTP |
| Speech-to-text | Whisper is best. Use a server with OpenAI's transcription API: [speaches](https://github.com/speaches-ai/speaches) (a compose profile here; it wants a GPU or a fast CPU), OpenAI or Groq (cloud), or whisper.cpp. Or use any HTTP or command-line engine. Vosk runs offline inside the bridge, but it's much less accurate |
| Text-to-speech | [Piper](https://github.com/OHF-Voice/piper1-gpl) (included: fast enough on a Pi), anything with OpenAI's speech API (OpenAI, Kokoro, ...), or any command |
| An AI agent | [Claude Code](https://claude.com/claude-code), installed and logged in where the gateway runs. It can be headless, or interactive sessions in tmux, which can also be Remote Control sessions. Other AIs can be added as plug-ins, and an `echo` backend lets you try the phone side without one |
| Optional | A SIP trunk (a real number) and a VPN such as WireGuard for using the phone away from home |

## Getting started

Keep the PBX on your LAN or a VPN: don't expose SIP to the internet.

```sh
git clone https://github.com/abagonhishead/token-ring && cd token-ring
cp config.example.toml config.toml      # set [user], [stt], [voices] and [gateway]
mkdir -p secrets && openssl rand -hex 32 > secrets/token
# secrets/manager_auth.conf and secrets/pjsip_auth.conf: see docs/guide.md
docker compose --profile stt up -d      # Asterisk, the bridge, Piper and speaches
```

The first build compiles Asterisk, which takes about 20 minutes on a Pi 5. Register your phone as
extension 100, then dial 601 to check speech both ways. Set `[gateway] backend = "echo"` to try
the phone side without an AI. Then run the gateway beside your agents (`pipx install .` and the
systemd unit in `systemd/`), and give them `docs/agents.md`.

[docs/guide.md](docs/guide.md) has the full setup, configuration, security notes and plug-in
interfaces. [ROADMAP.md](ROADMAP.md) has what's next.

## Licence

[MPL-2.0](LICENSE). Piper, used through its own server, is GPL-3.0; Asterisk is GPL-2.0.
