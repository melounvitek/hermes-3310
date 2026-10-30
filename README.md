# Hermes 3310

Use Hermes from a Nokia 3310. Any other phone works too, but it's less fun.

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin for calls over a normal phone line. You call your
number, type a PIN and talk to your Hermes, with the same agent, tools and memory as in your other chats.
Replies are spoken sentence by sentence while Hermes is still writing them, and Hermes can call you too ("call
me in ten minutes and remind me…").

```
phone ─ SIP provider ◄─ Asterisk (Docker, registers outbound) ─ AudioSocket 127.0.0.1 ─ Hermes gateway + this plugin
```

Asterisk logs in to your SIP provider like a desk phone, so you need no public IP, port forward or tunnel.

**Status: experimental.** Its author uses it daily on a Czech line from Odorik, on Linux, for calls in both
directions. Tested with Hermes Agent v0.22.5 and newer. Other providers, languages and set-ups have not been
tried yet.

## What you need

- Linux with Hermes Agent and its gateway, Docker that allows host networking, and `envsubst` (GNU gettext)
  for the install script.
- A SIP account with a phone number that lets your own device register with a username and password, and G.711
  audio. Providers that require a fixed public IP for a PBX won't work. Used with
  [Odorik](https://www.odorik.cz); Axfone, VOIPEX, WMS and Savvy document plain registration too.
- Speech recognition: an OpenAI API key (`gpt-transcribe`) or any other Hermes STT provider. Recognition and the
  language Hermes answers in come from your Hermes settings, not from the plugin.
- [Piper](https://github.com/OHF-Voice/piper1-gpl) for the voice: the plugin installs it as a Python dependency,
  and Hermes downloads the voice you pick (any language Piper has) on first use. See [Voice](#voice).

## Voice

The phone speaks with Piper, in the voice set in Hermes's Piper settings (`tts.piper.voice`).

- **Hermes's replies** use Hermes's streaming voice, `tts.streaming.provider`. Install step 2 sets it to
  `piper_stream`, the Piper provider this plugin adds. Other streaming voices should work too, but are untested.
- **What the plugin says itself** (the PIN prompt, the greeting, a message from a cron job, a reply whose
  streaming failed) is always spoken by Piper, whatever Hermes's voice is.

## Install

1. Install the plugin; it asks for the PIN, the allowed caller numbers and the SIP account (server such as
   `sip.odorik.cz`, user name, password) and stores them in `~/.hermes/.env`:
   ```
   hermes plugins install https://github.com/melounvitek/hermes-3310 --enable
   ```
2. Start Asterisk and give Hermes the phone's voice, the one setting the Desktop cannot make (from the plugin
   directory, usually `~/.hermes/plugins/hermes-3310/`):
   ```
   ./install-asterisk.sh
   docker exec hermes-3310-asterisk asterisk -rx 'pjsip show registrations'   # should say Registered
   hermes config set tts.streaming.provider piper_stream
   ```
3. In the Hermes Desktop app (reopen it first so it lists the new plugin; a backend on another machine needs its
   `hermes dashboard` / `hermes serve` restarted):
   - **Messaging → Hermes 3310**: turn the card on. For calls from Hermes (optional), also add your number and
     the line's caller ID, both as your provider dials them (Odorik: `00420…`).
   - **Settings → Plugins → Hermes 3310**: the language you speak, instructions for Hermes and what the phone
     says (all of it English until set; Czech example below).
   - **Settings → Voice**: text to speech Piper with a voice for your language (`cs_CZ-jirka-medium`), speech to
     text OpenAI with the model `gpt-transcribe` (or another provider).
4. Restart the gateway: `hermes gateway restart`. Later changes in the Desktop also apply after a restart.

The Czech set-up as it ends up in `~/.hermes/config.yaml` (without the Desktop, write it there yourself):
```yaml
plugins:
  entries:
    hermes-3310:
      settings:
        language: cs
        instructions: "Odpovídej česky, mluveným stylem."
        phrase_pin: "Zadej kód a mřížku."
        phrase_wrong_pin: "Špatný kód."
        phrase_hello: "Ahoj, tady Hermes. Co potřebuješ?"
        phrase_bye: "Na shledanou."
        phrase_not_understood: "Nerozuměl jsem, zopakuj to prosím."
        phrase_stopped: "Zastaveno."
        phrase_denied: "Tohle potřebuje schválení, to po telefonu nejde."
        phrase_missed: "Zkoušel jsem ti volat."
platforms:
  phone:
    enabled: true
tts:
  provider: piper
  streaming: {provider: piper_stream}
  piper: {voice: cs_CZ-jirka-medium}
stt:
  provider: openai
  openai: {model: gpt-transcribe}
```

Call your number, type the PIN and `#`, and talk.

Optional: give the gateway CPU priority, so calls stay snappy while the machine is busy. It applies at once and
survives reboots (`CPUWeight=` undoes it):
```
systemctl --user set-property hermes-gateway.service CPUWeight=1000
```

## On the call

- A short tick: your sentence was caught. A double beep every few seconds: Hermes is working. A single beep:
  Hermes finished, your turn.
- Talk over Hermes to interrupt it: Hermes pauses as soon as you start, and what you said becomes the next
  question. If it was only a cough or noise, Hermes goes on where it stopped (after a pause of a second or two).
- Press `*` to stop Hermes immediately; whatever you were saying is dropped too.
- Commands that need an approval in Hermes are always denied on the phone (a spoken "yes" is too easy to give by
  accident); Hermes says so.

## Calls from Hermes

Hermes calls `PHONE_HOME_NUMBER` from cron jobs that deliver to `phone`: ask on a call "call me in ten minutes
and remind me to…" (a job made on a call reports back by phone), or from another chat "every morning at 7, call
me with the weather" (`deliver=phone`).

- **Not answered** within 20 seconds, Hermes hangs up: a missed call, free with most providers. It keeps the
  message for a day (in `~/.hermes/phone/missed.json`, so a restart doesn't lose it) and says it when you call
  from that number, or on its next call to you.
- **Answered**, you hear only the PIN prompt, since it may be someone else or your voicemail (which makes it a
  paid call). After the PIN, Hermes reads the message and the call goes on like any other. No PIN within 20
  seconds, or three wrong ones, end the call and the message is kept as missed.
- **Already on a call:** the message is read there once Hermes finishes its answer. Messages arriving while
  Hermes is calling you are read on that call.
- **At most 6 calls an hour**, so a runaway job can't keep ringing your phone; further deliveries fail and show
  in `hermes cron list`.
- `deliver=all` and the failure notices of a phone job call you too. `hermes send --to phone` doesn't work: only
  cron jobs run by the gateway can call.

## Settings

The Hermes 3310 card on the Desktop's **Messaging** page, stored in `~/.hermes/.env`. `install-asterisk.sh` reads the
SIP account (`SIP_SERVER`, `SIP_USER`, `SIP_PASSWORD`, `SIP_IDENTIFY_EXTRA`), so run it again after changing one;
everything else applies after a gateway restart.

| Variable | Meaning |
|---|---|
| `PHONE_PIN` | keypad PIN, required |
| `PHONE_ALLOWED_CALLERS` | allowed caller numbers, comma-separated, required |
| `SIP_SERVER`, `SIP_USER`, `SIP_PASSWORD` | the SIP account, required |
| `PHONE_HOME_NUMBER` | the number Hermes calls you on, digits as your provider dials them; optional |
| `SIP_CALLER_ID` | the line's own number shown on calls from Hermes, digits as your provider dials them; optional |
| `SIP_IDENTIFY_EXTRA` | an Asterisk identify line for a provider's media servers (set for Odorik); optional |

The plugin's page under the Desktop's **Settings → Plugins**, stored in `plugins.entries.hermes-3310.settings` of
`config.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `language` | – | the language you speak (`cs`), for speech recognition on calls; else Hermes's STT settings decide |
| `instructions` | – | added to what Hermes is told on calls, e.g. the language to answer in |
| `phrase_pin` … `phrase_missed` | English | the texts the plugin speaks itself (see the example above) |
| `stt_timeout` | `5` | seconds before recognition is given up |
| `stt_fallback` | `none` | `local`: use local Whisper when the cloud fails or is slow (needs faster-whisper, ~1 GB RAM); it hears in Hermes's `stt.local.language` or `stt.language`, not `language` |
| `end_silence`, `start_vad` | `1.2`, `0.5` | seconds of silence that end a sentence, speech start threshold |
| `barge_vad`, `barge_windows` | `0.5`, `5` | how clear and long (× 32 ms) speech over Hermes must be to interrupt it |

Only in `config.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `platforms.phone.port`, `host` | `9092`, `127.0.0.1` | where the AudioSocket listener binds (keep it local) |
| `platforms.phone.calls_dir` | `~/.hermes/phone/calls` | where Asterisk hands over the caller number |
| `platforms.phone.outgoing_dir` | `~/.hermes/phone/outgoing` | where the plugin leaves call files for Asterisk |
| `tts.streaming.provider` | – | `piper_stream` (see [Voice](#voice)); without a streaming voice Hermes speaks a reply only once it is complete |
| `tts.piper_stream.length_scale` | `tts.piper.length_scale` | speaking speed on the phone only (lower is faster); applies from the next reply |

The `platforms.phone` port and paths must match Asterisk: if you change one, pass the same to
`install-asterisk.sh` (`--audiosocket-port`, `--calls-dir`, `--outgoing-dir`; `--help` lists all options). A
faster model for voice turns can be set as `auxiliary.voice_chat` (Desktop: Settings → Model).

## Security

- Every call needs the PIN, checked by the plugin before any speech is recognised or anything reaches Hermes.
  Caller ID can be spoofed, so the allow-list alone is not enough. The gateway re-checks the caller against the
  same list.
- The PIN must be 4 to 32 digits; use 6 or more. A call gets 3 tries. After 10 wrong PINs within an hour, on
  any calls, the line answers nobody (you included) until that hour has passed; a gateway restart clears it.
- The plugin refuses to start without a PIN and an allow-list. AudioSocket has no authentication, so the plugin
  only listens on this machine (`127.0.0.1`) and refuses any other `host`.
- Asterisk takes calls only from your provider's addresses (`SIP_SERVER`, plus `SIP_IDENTIFY_EXTRA`). It still
  listens on SIP UDP 5070 and RTP UDP 16000–16200 on all interfaces: if the machine has a public address, let
  only your provider reach those ports.
- The SIP password stays in `~/.hermes/.env` and the rendered `~/.hermes/phone/asterisk/pjsip.conf` (mode 600).
- Hermes only ever calls `PHONE_HOME_NUMBER`, never a number from a model, a job or a tool. The plugin starts a
  call by leaving an Asterisk call file in `~/.hermes/phone/outgoing` (mode 700): no network port, no extra
  password and no Docker access for the gateway. Whoever can write there can make calls, which is the same
  user who can read the SIP password in `.env`. Incoming calls cannot reach the dialplan that dials out.

## Privacy

- SIP and the call audio are not encrypted, like most phone lines.
- What you say is sent to your Hermes speech-to-text provider (OpenAI with the settings above), and the
  conversation is stored in Hermes like any other chat.
- The plugin logs only the last three digits of a number; Asterisk's own log in its container has full caller
  numbers.

## Limits

- Restarting the gateway (updates, `/restart`) drops calls in progress.
- The 6 calls an hour from Hermes are counted in memory (a restart resets them), and they are not a spending
  limit: set one with your provider. A call answered by your voicemail is a paid call.
- Hermes's text cleaner writes units and symbols as English words; for Czech voices the plugin puts them back
  into Czech (`20 %` → "20 procent", `°C` → "stupňů Celsia").

## Development

```
~/.hermes/hermes-agent/venv/bin/python -m pytest -q          # about 7 minutes
```

- The tests use Hermes's real plugin loader and streaming-speech pipeline; only the LLM and speech recognition
  are faked.
- They run in the Python environment of a Hermes Agent checkout (`HERMES_AGENT_DIR`, default
  `~/.hermes/hermes-agent`) with its development extras (pytest, pytest-asyncio, pytest-timeout).
- Tests that speak need the Piper voice in `PHONE_TEST_VOICE` (default
  `~/.hermes/cache/piper-voices/cs_CZ-jirka-medium.onnx`) and are skipped without it. Get it with
  `python -m piper.download_voices cs_CZ-jirka-medium --download-dir ~/.hermes/cache/piper-voices`.

For a full check without a phone, run a separate test gateway (its own `HERMES_HOME` outside `~/.hermes`,
`hermes gateway run --force`, `platforms.phone.port: 9093`) and call it with the simulator:

```
HERMES_HOME=~/hermes-3310-test/home ~/.hermes/hermes-agent/venv/bin/python tools/simulate_call.py \
    --port 9093 --caller 420777000111 KEY:1234# WAIT:3 "Kolik je hodin v Praze?"
```

With `--answer` the simulator waits for a call from Hermes instead, takes the call file like Asterisk and
answers it: start it before a job delivers to `phone`. It transcribes what it heard with faster-whisper
(`--no-transcribe` skips that).

## Licence

MIT, see `LICENSE`. The bundled [Silero VAD](https://github.com/snakers4/silero-vad) model is MIT as well. Piper,
which speaks, is GPL-3.0-or-later and installed separately; see `THIRD_PARTY_NOTICES.md`.
