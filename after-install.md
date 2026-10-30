## Hermes 3310 installed

A few more steps before you can call Hermes:

1. **Start Asterisk**, the Docker container that answers your SIP line, and give Hermes the phone's voice:
   ```
   ~/.hermes/plugins/hermes-3310/install-asterisk.sh
   docker exec hermes-3310-asterisk asterisk -rx 'pjsip show registrations'
   hermes config set tts.streaming.provider piper_stream
   ```
   The second command should say `Registered`.
2. **In the Hermes Desktop app** (reopen it first, or restart its Hermes backend, so it lists the new plugin),
   turn on Messaging → Hermes 3310, set the language and the phrases in Settings →
   Plugins → Hermes 3310, and a voice in Settings → Voice (see the README).
3. Restart the gateway: `hermes gateway restart`.

Then call your number, type the PIN and `#`, and talk.
