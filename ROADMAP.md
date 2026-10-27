# Roadmap

Ideas and known gaps, roughly in order of how much they'd help. Nothing here is promised.

## Next

- **Web UI.** One page for everything that's a config file or a log today: the sessions and
  their status, voicemail (read, play, reply, delete), outbound calls and texts, recent calls with
  transcripts, and settings: voices (with samples), trigger words, pronunciations,
  speech-to-text and text-to-speech engines. Served by the gateway, behind its token.
- **Encrypted calls.** SIP over TLS (port 5061) and SRTP (SDES or DTLS) for the phones, so
  "encrypt everything" in a softphone works. Today calls are plain SIP and RTP: fine inside a
  VPN, not on an open network.
- **Texting sessions.** Text a session from the phone's chat (SIP MESSAGE) instead of calling:
  the replies come back as texts, multiple-choice questions as numbered lists. Sessions can
  text you with `token-ring text`. The plumbing for texts both ways already exists for voicemail
  notifications.

## Later

- **Read receipts for texts.** Linphone sends IMDN receipts (in CPIM), but Asterisk only accepts
  text/plain messages from phones. A small Asterisk patch would let a "read" receipt stop the
  reminders, rather than needing a reply.
- **Quiet hours** for reminders and calls from sessions.
- **Faster first words.** Piper behind its HTTP server adds about 0.2 s per phrase compared with
  running it in the bridge; streaming audio back as it's made, or a lighter server, would win
  that back.
- **Quicker call failures.** When a call from a session fails straight away (the phone isn't
  registered), say so at once rather than after the ring timeout.
- **More AI backends:** other agent CLIs (Codex CLI, Gemini CLI, aider, ...) and plain chat APIs,
  through the backend interface (`src/token_ring/backends/`).
- **Published images** for amd64 and arm64.
- **Message waiting indicator** on desk phones for unheard voicemail.
