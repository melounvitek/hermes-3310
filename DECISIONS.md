# Decisions

Why the phone line works the way it does. Each entry: the decision and the reason. The plugin replaced an
earlier standalone bridge that talked to Hermes over its HTTP API; some decisions date from that bridge.

## Own bridge instead of a hosted voice platform (2026-10-12)
Vapi, Retell, Patter, ElevenLabs Agents and similar platforms must reach Hermes from the internet. The host has
no public inbound access. Our own bridge also reuses a Czech speech pipeline (Whisper, Piper) and the "working"
beeps.

## Not the hermes-voip community plugin (2026-10-12)
It needed Python 3.13+ while Hermes ran on 3.11, required encrypted SIP that not every Czech provider offers,
and its offline voice has no Czech.

## Asterisk in Docker, registering outbound to the SIP provider (2026-10-12)
Asterisk logs in to the provider like a desk phone and keeps the registration alive, so incoming calls arrive
over that connection. No public IP, port forward or tunnel is needed. Asterisk handles SIP, NAT and codecs; the
plugin only gets raw audio over AudioSocket. Asterisk cannot live inside a Hermes plugin, so it stays a separate
container set up by an install script.

## Odorik.cz as the SIP provider (2026-10-12)
Chosen because it was the one provider checked properly: Czech, its docs cover Asterisk registering to
sip.odorik.cz without a public IP, a regional landline number for 20 CZK once, free incoming calls, about 30 CZK a
month minimum spend. Nothing Odorik-specific is required.
Any provider works if it allows your own SIP device or software (username and password registration), gives a
public number for incoming calls and supports G.711. Avoid providers that require a fixed public IP for a PBX.
Axfone, VOIPEX, WMS and Savvy document plain registration (checked 2026-10-27, not tested).

## PIN on every incoming call, plus a caller allow-list
Caller ID can be spoofed and the line gives full access to Hermes. The PIN is entered on the keypad (DTMF, ending
with #) and checked inside the adapter before any speech is transcribed, so an unauthenticated call never
reaches Hermes. Calls from Hermes go only to the user's own number, which is fixed in the config; the remaining
risk there is who picks up (an unlocked phone, voicemail, forwarding), so those calls ask for the PIN too.

## Czech is a hard requirement
Speech recognition language, a Czech Piper voice (cs_CZ-jirka) and a spoken-style Czech prompt. All of it is
configuration, not code, so other languages work the same way.

## OpenAI gpt-transcribe for speech recognition (October 2026)
On 14 real phone sentences local Whisper turbo got about half right in 9 to 26 s; gpt-transcribe got about 12
right in under 1.5 s. A local fallback never ran in 577 utterances, so it is optional (saves 0.6 to 1.5 GB RAM).

## Keypad * stops the turn; talking over the reply interrupts it
Spoken stop words were too short for voice detection and waited seconds for transcription. DTMF bypasses both
and a cough cannot trigger it. Talking over a running turn stops it and becomes the next question.

## A Hermes plugin instead of the standalone bridge (2026-10-27)
To share the phone line with other people with a simple install. A platform adapter runs inside the gateway
instead of talking to Hermes over its HTTP API, so session and auth workarounds go away, settings live in normal
Hermes config and secrets, and installing is one step plus the Asterisk container.

## Speak sentence by sentence while the reply is generated
A plugin that only got the whole reply would leave the caller in silence for seconds on longer answers. Hermes
has a streaming speech contract for voice adapters; Piper is not a built-in streaming provider there, so the
plugin adds it as a TTS provider with raw PCM output (`piper_stream`).

## The adapter stops the turn itself before asking the next question
Hermes's own handling of a message that arrives while a turn runs depends on a global setting, and it queues
instead of interrupting while subagents run. So talking over a reply first only pauses Hermes; once the speech is
understood, noise lets it go on, and real speech makes the adapter stop the turn (`/stop`) and only then send
the new question, and reply audio is tied to the utterance it answers, so a stopped turn that
keeps producing audio is never heard.

## Calls from Hermes through Asterisk call files
To start a call, the plugin drops a call file into a private folder that Asterisk watches. That needs no network
port, no extra password and no Docker access for the gateway, unlike Asterisk's manager interface or
`docker exec`. The caller ID goes into each call file, so changing it needs no Asterisk reinstall. A message the
user did not hear is kept for a day and said on their next call.

## Settings in the Hermes Desktop
Secrets are fields of the plugin's card on the Messaging page, the rest is the plugin's settings page, so a user
does not have to edit files. Only what must match the Asterisk container (ports and folders) stays in
`config.yaml`.
