"""The ``phone`` platform: Asterisk AudioSocket calls in, Hermes replies spoken back sentence by sentence.

AudioSocket frames are type (1 byte) + length (2 bytes, big endian) + payload. Each call is one chat
(``chat_id`` = AudioSocket call UUID), so every call gets a fresh Hermes session. A call reaches
Hermes only after its caller number passed the allow-list and the caller entered the PIN.

Messages Hermes delivers (cron jobs) go to the user: into their live call, else Hermes calls
``PHONE_HOME_NUMBER`` through an Asterisk call file. A message the user missed is kept for a day
and said on their next call.
"""
import asyncio
import contextlib
import contextvars
import hmac
import ipaddress
import json
import logging
import os
import re
import struct
import tempfile
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult, StreamingTTSHandle
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_context import get_session_env
from hermes_constants import get_hermes_home

from .audio import (FRAME_BYTES, HEARD_TICK, READY_BEEP, SR, STOP_TONE, THINK_BEEP, SpeechDetector, is_garbage,
                    to_call_rate)

logger = logging.getLogger(__name__)

HANGUP, CALL_ID, DTMF, AUDIO, ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF
PIN_ATTEMPTS, PIN_TIMEOUT_S = 3, 20
MAX_WRONG_PINS = 10  # in an hour, across calls; then the line answers nobody until the hour has passed
IDLE_TIMEOUT_S = 10  # longest wait for a stopped turn to end before the next question is sent
LOCAL_STT_TIMEOUT_S = 30  # local Whisper took 9-26 s per sentence under load on the author's machine
HOME_NUMBER_INVALID = ("PHONE_HOME_NUMBER must be your number in the provider's dial format (digits only) "
                       "and match an entry of PHONE_ALLOWED_CALLERS")
MAX_CALLS_PER_HOUR = 6  # a runaway cron job must not keep ringing the phone
# Cron's English header and footer around a job's output (cron.wrap_response): not read aloud.
CRON_WRAPPER = re.compile(r"Cronjob Response: [^\n]*\n(?:\(job_id: [^\n]*\)\n)?-+\n\n(.*?)"
                          r"(?:\n\nTo stop or manage this job, .*)?", re.S)
TAKE_TIMEOUT_S = 5  # Asterisk takes a call file at once; one it did not take is withdrawn
RING_TIMEOUT_S = 60  # the dialplan rings for 20 s; after this the call is given up in any case
MISSED_KEEP_S = 24 * 3600  # a missed message older than this is stale and dropped


def _digits(number: str) -> str:
    return "".join(c for c in number if c.isdigit())


def _international(number: str) -> str:
    """``+420 777…``, ``00420777…`` and ``420777…`` are the same number."""
    digits = _digits(number)
    return digits[2:] if digits.startswith("00") else digits


def _is_loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _redact(number: str) -> str:
    return f"…{_digits(number)[-3:]}" if _digits(number) else "unknown"


def _tts():
    from agent.tts_registry import get_provider
    return get_provider("piper_stream")


def _transcribe_wav(pcm: bytes, local: bool) -> dict:
    """One recognition attempt in an STT worker, on its own WAV that it removes afterwards."""
    from tools.transcription_tools import transcribe_audio, transcribe_audio_local_fallback
    fd, path = tempfile.mkstemp(prefix="phone-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(pcm)
        return transcribe_audio_local_fallback(path) if local else transcribe_audio(path, source="phone")
    finally:
        os.unlink(path)


@dataclass
class Outbound:
    """A call Hermes placed that rings or waits for the PIN; its messages are said after the PIN."""
    call_id: str
    messages: list[str]
    answered: bool = False


class Call:
    """One AudioSocket connection. Audio queued with ``play`` goes out as paced 20 ms frames;
    silence is sent while idle because Asterisk drops a socket that stays quiet for 2 s."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.id = ""
        self.user_id: Optional[str] = None  # the matched allow-list entry, once the PIN was entered
        self.session_key = ""
        self.playout = bytearray()
        self.voice = False  # the queued audio is speech (not just beeps): the caller can talk over it
        # Sounds the caller made over Hermes that are not understood yet: Hermes's audio waits until none is left.
        self.holds = 0
        self.last_sound = 0.0
        # Reply audio plays only for the latest utterance, and not once the caller talked over it.
        self.latest: Optional[str] = None
        self.muted: set[str] = set()
        self.spoken: set[str] = set()  # utterances whose reply audio reached the call
        self.generation = 0  # bumped by *: speech still being recognised is dropped
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()
        self.detector: Optional[SpeechDetector] = None  # set once the PIN was entered
        self.on_speech_start: Callable[[], bool] = lambda: False  # returns whether it talks over Hermes
        self.talks_over = False
        self.on_star: Callable[[], None] = lambda: None
        self.utterances: asyncio.Queue[Optional[tuple[bytes, bool]]] = asyncio.Queue()
        self.recognising = False  # an utterance is on its way to Hermes
        self.closed = asyncio.Event()
        self.deliveries: list[str] = []  # messages Hermes delivered that wait to be said
        self.saying = False  # a task says them
        self.context: list[str] = []  # messages said since the caller's last question

    def play(self, pcm: bytes, voice: bool = False) -> None:
        self.playout += pcm
        self.voice = self.voice or voice

    def owns_output(self, utterance_id: Optional[str]) -> bool:
        return utterance_id == self.latest and utterance_id not in self.muted

    def silence(self) -> None:
        """Stop what is playing and everything the current turn would still say."""
        self.playout.clear()
        self.voice = False
        if self.latest:
            self.muted.add(self.latest)

    async def hang_up(self) -> None:
        """Let queued speech finish, then end the call."""
        while self.playout and not self.closed.is_set():
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        with contextlib.suppress(ConnectionError, RuntimeError):
            self.writer.write(bytes([HANGUP, 0, 0]))
            await self.writer.drain()

    async def read_frame(self) -> tuple[int, bytes]:
        head = await self.reader.readexactly(3)
        length = struct.unpack(">H", head[1:])[0]
        return head[0], await self.reader.readexactly(length) if length else b""

    async def read_frames(self) -> None:
        try:
            with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
                while True:
                    kind, payload = await self.read_frame()
                    if kind == HANGUP:
                        return
                    if kind == AUDIO and self.detector is not None:
                        was_speaking = self.detector.in_speech
                        utterance = self.detector.feed(payload, strict=self.voice)
                        if self.detector.in_speech and not was_speaking:
                            self.talks_over = self.on_speech_start()
                        if utterance:
                            self.utterances.put_nowait((utterance, self.talks_over))
                        elif was_speaking and not self.detector.in_speech and self.talks_over:
                            self.holds -= 1  # too short to be anything
                    elif kind == DTMF and payload == b"*" and self.detector is not None:
                        self.on_star()
                    elif kind == DTMF:
                        self.dtmf.put_nowait(payload.decode(errors="ignore"))
                    elif kind == ERROR:
                        logger.warning("phone: Asterisk reported an error on call %s: %r", self.id[:8], payload)
                        return
        finally:
            self.closed.set()
            self.dtmf.put_nowait("")  # wake whoever waits for keys or speech
            self.utterances.put_nowait(None)

    async def write_paced(self) -> None:
        next_at = time.monotonic()
        with contextlib.suppress(ConnectionError, RuntimeError):
            while True:
                frame = b"" if self.holds else bytes(self.playout[:FRAME_BYTES])
                if frame:
                    self.last_sound = time.monotonic()
                    del self.playout[:FRAME_BYTES]
                    self.voice = self.voice and bool(self.playout)
                frame = frame.ljust(FRAME_BYTES, b"\x00")
                self.writer.write(bytes([AUDIO]) + struct.pack(">H", FRAME_BYTES) + frame)
                await self.writer.drain()
                next_at = max(next_at + 0.02, time.monotonic() - 0.06)  # catch up after a stall, not burst
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))


class PhoneAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config, settings: dict, **kwargs):
        """``config.extra`` is ``platforms.phone`` (where Asterisk connects, which must match
        install-asterisk.sh); ``settings`` are the plugin settings (see ``register``)."""
        super().__init__(config=config, platform=Platform("phone"))
        extra = config.extra or {}
        self._host, self._port = str(extra.get("host", "127.0.0.1")), int(extra.get("port", 9092))
        self._calls_dir = Path(extra.get("calls_dir") or get_hermes_home() / "phone" / "calls").expanduser()
        # What the adapter says itself, e.g. self._phrases["pin"].
        self._phrases = {key.removeprefix("phrase_"): text for key, text in settings.items()
                         if key.startswith("phrase_")}
        self._pin = str(get_scoped_secret("PHONE_PIN", "") or "").strip()
        self._allowed = [c.strip() for c in str(get_scoped_secret("PHONE_ALLOWED_CALLERS", "") or "").split(",")
                         if _digits(c)]
        # The only number Hermes ever calls, dialled as written (the provider's dial format).
        self._home_number = str(get_scoped_secret("PHONE_HOME_NUMBER", "") or "").strip()
        self._home = (self._allowed_entry(self._home_number) if re.fullmatch(r"[0-9]+", self._home_number)
                      else None)  # its allow-list entry: the user's identity on calls
        # Shown on Hermes's calls; set per call, so a change needs no Asterisk re-install.
        self._caller_id = str(get_scoped_secret("SIP_CALLER_ID", "") or "").strip()
        self._outgoing_dir = Path(extra.get("outgoing_dir") or get_hermes_home() / "phone" / "outgoing").expanduser()
        self._outbound: Optional[Outbound] = None
        self._missed_file = get_hermes_home() / "phone" / "missed.json"
        self._placing = asyncio.Lock()
        self._placed: list[float] = []  # when recent outbound calls were placed
        self._vad = {"start_threshold": float(settings["start_vad"]), "end_silence_s": float(settings["end_silence"]),
                     "strict_threshold": float(settings["barge_vad"]), "strict_windows": int(settings["barge_windows"])}
        self._stt_timeout = float(settings["stt_timeout"])
        self._stt_fallback = settings["stt_fallback"]
        self._stt_pool: Optional[ThreadPoolExecutor] = None
        self._wrong_pins: list[float] = []  # when a wrong PIN was typed, on any call
        self._calls: dict[str, Call] = {}  # calls past the PIN, by chat id
        self._server: Optional[asyncio.base_events.Server] = None
        self._warm_task: Optional[asyncio.Task] = None
        self._connections: set[asyncio.Task] = set()  # one per AudioSocket connection, PIN or not
        self._tasks: set[asyncio.Task] = set()  # * handling started from the call's read loop
        self.port: Optional[int] = None

    # --- lifecycle

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not re.fullmatch(r"[0-9]{4,32}", self._pin) or not self._allowed:
            message = "PHONE_PIN (4 to 32 digits) and PHONE_ALLOWED_CALLERS must both be set"
            logger.error("phone: %s, not answering calls", message)
            self._set_fatal_error("config_missing", message, retryable=False)
            return False
        if not _is_loopback(self._host):  # AudioSocket has no authentication
            message = f"platforms.phone.host must be this machine (127.0.0.1), not {self._host}"
            logger.error("phone: %s, not answering calls", message)
            self._set_fatal_error("config_invalid", message, retryable=False)
            return False
        try:
            self._server = await asyncio.start_server(self._on_call, self._host, self._port)
        except OSError as e:
            logger.error("phone: cannot listen on %s:%s: %s", self._host, self._port, e)
            self._set_fatal_error("bind_failed", str(e), retryable=True)
            return False
        self.port = self._server.sockets[0].getsockname()[1]
        # Its own workers: a recognition left running after the cutoff must not hold up speech synthesis.
        self._stt_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="phone-stt")
        self._warm_task = asyncio.create_task(self._warm_voice())
        self._mark_connected()
        logger.info("phone: waiting for Asterisk on AudioSocket %s:%s (Asterisk runs in Docker, set it up with %s)",
                    self._host, self.port, Path(__file__).with_name("install-asterisk.sh"))
        if self._home_number and self._home is None:
            logger.error("phone: %s, Hermes cannot call you", HOME_NUMBER_INVALID)
        if self._caller_id and not self._caller_id.isdigit():
            logger.error("phone: SIP_CALLER_ID must be digits in the provider's dial format; calls from Hermes "
                         "show the provider's default number")
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._server:
            self._server.close()
        running = [*self._connections, *self._tasks, *([self._warm_task] if self._warm_task else [])]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()
            self._server = None
        if self._stt_pool:
            self._stt_pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    async def _warm_voice() -> None:
        """Download/load the Piper voice at startup instead of during the first call."""
        try:
            if (tts := _tts()) is not None:
                await asyncio.to_thread(tts.warm)
        except Exception:
            logger.warning("phone: could not load the Piper voice", exc_info=True)

    async def _on_call(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        call, tasks, outbound = Call(reader, writer), [], None
        self._connections.add(asyncio.current_task())
        try:
            kind, payload = await asyncio.wait_for(call.read_frame(), timeout=5)
            if kind != CALL_ID or len(payload) != 16:
                return
            call.id = str(uuid.UUID(bytes=payload))
            if self._outbound and self._outbound.call_id == call.id:  # the call Hermes placed
                outbound = self._outbound
                outbound.answered = True
                call.deliveries = outbound.messages  # deliveries until the PIN join it
            tasks = [asyncio.create_task(call.write_paced()), asyncio.create_task(call.read_frames())]
            call.user_id = await self._admit(call, outbound is not None)
            if call.user_id is None:
                await self._speak(call, self._phrases["bye"])
                await call.hang_up()
                return
            call.session_key = self._source_session_key(self._call_source(call))
            call.on_speech_start = lambda: self._talks_over(call)
            call.on_star = lambda: self._spawn(self._hard_stop(call))
            call.detector = await asyncio.to_thread(SpeechDetector, **self._vad)
            self._calls[call.id] = call
            logger.info("phone: call %s %s %s connected", call.id[:8], "to" if outbound else "from",
                        _redact(call.user_id))
            if call.user_id == self._home and (missed := self._take_missed()):
                call.deliveries[:0] = [self._phrases["missed"], *missed]
            if outbound:
                self._outbound = None  # from now on deliveries find this live call
            else:
                await self._speak(call, self._phrases["hello"])
            if call.deliveries:
                self._say_deliveries(call)
            listener = asyncio.create_task(self._listen(call))
            tasks.append(listener)
            # Until the caller hangs up (which also drops speech still being recognised) or the
            # listener fails, which ends the call rather than leaving the caller in silence.
            await asyncio.wait({tasks[1], listener}, return_when=asyncio.FIRST_COMPLETED)
            if listener.done() and listener.exception():
                logger.error("phone: call %s failed", call.id[:8], exc_info=listener.exception())
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if outbound and self._outbound is outbound:
                self._outbound = None  # not past the PIN
            self._connections.discard(asyncio.current_task())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Messages not said: nobody typed the PIN, or the user hung up before hearing them.
            self._keep_missed([text for text in call.deliveries if text != self._phrases["missed"]])
            self._calls.pop(call.id, None)
            if call.session_key and self._busy(call):
                with contextlib.suppress(Exception):
                    await self._stop_turn(call)  # hung up while Hermes works: stop it
            writer.close()
            logger.info("phone: call %s ended", call.id[:8] or "?")

    async def _listen(self, call: Call) -> None:
        """Transcribe each utterance of the call and hand it to Hermes, until the caller hangs up.

        Speech over a running turn stops that turn first and becomes the next question
        (stop-then-ask), so a question never reaches Hermes while a turn of this call runs.
        """
        while True:
            call.recognising = False
            if (item := await call.utterances.get()) is None:
                return
            call.recognising = True  # until Hermes has it or it is dropped
            pcm, talks_over = item
            generation, started = call.generation, time.monotonic()
            if not talks_over:
                call.play(HEARD_TICK + THINK_BEEP)  # "heard you, working on it"
            text = await self._transcribe(pcm)
            logger.info("phone: call %s heard %.1f s of speech, recognised in %.1f s", call.id[:8],
                        len(pcm) / SR / 2, time.monotonic() - started)
            if generation != call.generation:
                continue  # * was pressed meanwhile (it dropped this speech and its hold)
            if talks_over:
                call.holds -= 1
                if len(pcm) < SR * 2 or len(text.split()) < 2 or is_garbage(text):
                    continue  # a cough or street noise over Hermes: it goes on where it stopped
            elif not text or is_garbage(text):
                await self._speak(call, self._phrases["not_understood"])
                continue
            busy = self._busy(call)
            if talks_over or busy:
                call.silence()  # real speech: what Hermes was saying ends here
            if talks_over:
                call.play(HEARD_TICK + THINK_BEEP)
            if busy:
                await self._stop_turn(call)
                if not await self._until_idle(call) or generation != call.generation:
                    continue
            await self.submit_utterance(call.id, text)

    def _talks_over(self, call: Call) -> bool:
        """The caller started speaking over Hermes (its reply, a message, or a turn still working): hold
        Hermes's audio until the speech is understood. Noise then lets it go on where it stopped; real
        speech ends it and becomes the next question (``_listen``)."""
        talks_over = bool(call.holds) or call.voice or self._busy(call)
        call.holds += talks_over
        return talks_over

    async def _hard_stop(self, call: Call) -> None:
        """Keypad *: silence everything, forget what the caller said so far, stop the running turn."""
        call.generation += 1
        call.detector.drop()
        call.holds = 0  # everything the caller said so far is dropped
        while not call.utterances.empty():
            if call.utterances.get_nowait() is None:  # keep the end-of-call marker
                call.utterances.put_nowait(None)
                break
        call.silence()
        call.deliveries.clear()
        call.play(STOP_TONE)
        if self._busy(call):
            await self._stop_turn(call)
        await self._speak(call, self._phrases["stopped"])

    def _busy(self, call: Call) -> bool:
        return call.session_key in self._active_sessions  # a turn of this call is running

    async def _stop_turn(self, call: Call) -> None:
        """Hermes's own /stop for the call's session; its text reply goes to send() and is not spoken."""
        await self.handle_message(MessageEvent(text="/stop", message_type=MessageType.TEXT,
                                               source=self._call_source(call)))

    async def _until_idle(self, call: Call) -> bool:
        deadline = time.monotonic() + IDLE_TIMEOUT_S
        while self._busy(call):
            if time.monotonic() > deadline:
                logger.warning("phone: turn on call %s did not stop, dropping the new question", call.id[:8])
                await self._speak(call, self._phrases["not_understood"])
                return False
            await asyncio.sleep(0.05)
        return True

    def _call_source(self, call: Call, message_id: Optional[str] = None):
        return self.build_source(chat_id=call.id, chat_type="dm", user_id=call.user_id, message_id=message_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _transcribe(self, pcm: bytes) -> str:
        """Hermes STT (stt.* config) with a cutoff; on failure the local fallback if configured."""
        loop = asyncio.get_running_loop()

        def attempt(local: bool):  # the worker keeps this profile's context (STT config, keys)
            return loop.run_in_executor(self._stt_pool, contextvars.copy_context().run, _transcribe_wav, pcm, local)

        try:
            result = await asyncio.wait_for(attempt(False), self._stt_timeout)
        except Exception as e:  # timeout or any STT error: the attempt is abandoned, never awaited again
            result = {"success": False, "error": str(e) or type(e).__name__}
        if not result.get("success") and self._stt_fallback == "local":
            logger.warning("phone: speech recognition failed (%s), using local fallback", result.get("error"))
            with contextlib.suppress(Exception):
                result = await asyncio.wait_for(attempt(True), LOCAL_STT_TIMEOUT_S)
        return (result.get("transcript") or "").strip() if result.get("success") else ""

    # --- access: allow-list, then PIN, before anything reaches Hermes

    async def _admit(self, call: Call, outbound: bool) -> Optional[str]:
        """The caller's allow-list entry if the number is allowed and the PIN was entered, else None.
        A call Hermes placed went to PHONE_HOME_NUMBER, but whoever answered still needs the PIN."""
        if outbound:
            entry = self._home
        else:
            caller = await self._caller_number(call.id)
            entry = self._allowed_entry(caller)
            if entry is None:
                logger.warning("phone: call %s from %s is not on the allow-list", call.id[:8], _redact(caller))
                return None
        if self._pin_locked(call):
            return None
        await self._speak(call, self._phrases["pin"])
        for attempt in range(PIN_ATTEMPTS):
            entered = await self._read_pin(call)
            if call.closed.is_set() or self._pin_locked(call):  # also guesses on calls that waited meanwhile
                return None
            if hmac.compare_digest(entered.encode(), self._pin.encode()):
                return entry
            logger.warning("phone: wrong PIN on call %s", call.id[:8])
            if entered:
                self._wrong_pins.append(time.monotonic())
            if outbound and not entered:
                return None  # nobody typed anything, e.g. voicemail answered
            if attempt < PIN_ATTEMPTS - 1:
                await self._speak(call, self._phrases["wrong_pin"])
        return None

    def _pin_locked(self, call: Call) -> bool:
        """Someone is guessing the PIN: too many wrong ones within the last hour, on any calls."""
        now = time.monotonic()
        self._wrong_pins = [t for t in self._wrong_pins if now - t < 3600]
        if len(self._wrong_pins) < MAX_WRONG_PINS:
            return False
        logger.warning("phone: %d wrong PINs in the last hour, not answering call %s", len(self._wrong_pins),
                       call.id[:8])
        return True

    async def _caller_number(self, call_id: str) -> str:
        """The number the dialplan wrote to ``<calls_dir>/<call id>`` just before connecting."""
        path = self._calls_dir / call_id
        for _ in range(25):
            if path.exists():
                break
            await asyncio.sleep(0.02)
        try:
            number = path.read_text().strip()
            path.unlink()
        except OSError:
            return ""
        return number

    def _allowed_entry(self, caller: str) -> Optional[str]:
        """The allow-list entry (written with the country code) that ``caller`` is. Providers show domestic
        callers without the country code (Odorik: 777000111 for 420777000111), so a shorter number of 9+
        digits matches the end of an entry; a full number must match exactly (421777000111 does not)."""
        number = _international(caller)
        return next((entry for entry in self._allowed if number and (
            (allowed := _international(entry)) == number
            or (9 <= len(number) < len(allowed) and allowed.endswith(number)))), None)

    @staticmethod
    async def _read_pin(call: Call) -> str:
        """Keys up to ``#``; whatever was typed when the time is up or the caller hung up."""
        entered, deadline = "", time.monotonic() + PIN_TIMEOUT_S
        while (left := deadline - time.monotonic()) > 0:
            try:
                key = await asyncio.wait_for(call.dtmf.get(), timeout=left)
            except asyncio.TimeoutError:
                break
            if key in ("#", ""):
                break
            entered = (entered + key)[:32]
        return entered

    # --- inbound

    async def submit_utterance(self, chat_id: str, text: str) -> None:
        """Hand one recognised utterance of a call past the PIN to Hermes as a voice message."""
        call = self._calls.get(chat_id)
        if call is None or call.closed.is_set():
            return
        utterance_id = uuid.uuid4().hex
        call.latest = utterance_id  # from now on only this turn's reply is played
        heard, call.context = call.context, []
        # Both ids are needed: Hermes serialises them separately and binds the source one
        # as HERMES_SESSION_MESSAGE_ID for the turn, which is how reply audio finds its utterance.
        # Delivered messages the caller heard are quoted as Hermes's own, so they stay in the call's history.
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.VOICE,
                                               source=self._call_source(call, utterance_id),
                                               message_id=utterance_id,
                                               reply_to_message_id=f"delivered-{utterance_id}" if heard else None,
                                               reply_to_text="\n\n".join(heard) or None,
                                               reply_to_is_own_message=bool(heard)))

    async def on_processing_complete(self, event, outcome) -> None:
        call = self._calls.get(event.source.chat_id)
        if call is not None and event.message_id and call.owns_output(event.message_id):
            call.play(READY_BEEP)  # after the reply: the caller's turn

    # --- streaming speech (Hermes's StreamingTTSConsumer drives these)

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        return chat_id in self._calls  # enables the streaming consumer for voice turns of live calls

    def _wants_auto_tts(self, *args, **kwargs) -> bool:
        return False  # no built-in whole-file TTS: the final-text fallback below speaks instead

    def supports_streaming_tts(self, chat_id, audio_format) -> bool:
        return chat_id in self._calls and audio_format.channels == 1 and audio_format.sample_width == 2

    async def begin_streaming_tts(self, chat_id, audio_format, metadata=None):
        return StreamingTTSHandle(chat_id=chat_id, audio_format=audio_format) if chat_id in self._calls else None

    async def write_streaming_tts(self, handle, chunk: bytes) -> None:
        # The consumer runs in its turn's context, so this names the utterance the audio answers;
        # a stopped turn that still streams (e.g. under the session authority) is never heard.
        utterance_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
        call = self._calls.get(handle.chat_id)
        if call is None or handle.aborted or not call.owns_output(utterance_id):
            return
        call.play(to_call_rate(chunk, handle.audio_format.sample_rate), voice=True)
        call.spoken.add(utterance_id)

    async def finish_streaming_tts(self, handle, *, interrupted: bool = False) -> None:
        """Nothing to wait for: queued audio keeps playing after the stream ends."""

    async def abort_streaming_tts(self, handle, error: Optional[str] = None) -> None:
        handle.aborted = True  # late chunks are dropped; audio already queued still plays

    # --- outbound text

    async def _send_final_text(self, event, session_key, text_content, metadata, is_ephemeral_response,
                               ephemeral_ttl, record_delivery) -> None:
        """Speak the final reply only if its turn streamed no audio (e.g. the stream failed)."""
        call = self._calls.get(event.source.chat_id)
        if call is not None and call.owns_output(event.message_id) and event.message_id not in call.spoken:
            await self._speak(call, text_content, answering=event.message_id)
        await super()._send_final_text(event, session_key, text_content, metadata, is_ephemeral_response,
                                       ephemeral_ttl, record_delivery)

    async def _speak(self, call: Call, text: str, answering: Optional[str] = None) -> None:
        """Say ``text``; a reply to utterance ``answering`` is dropped if the caller stopped it meanwhile."""
        pcm = await self._synthesize(text)
        if pcm and (answering is None or call.owns_output(answering)):
            call.play(pcm, voice=True)

    @staticmethod
    async def _synthesize(text: str) -> bytes:
        """``text`` spoken as call audio."""
        from tools.tts_text_normalize import prepare_spoken_text
        tts = _tts()
        text = prepare_spoken_text(text, max_chars=None)
        if tts is None or not text:
            return b""
        pcm = await asyncio.to_thread(lambda: b"".join(tts.stream(text, format="pcm")))
        return to_call_rate(pcm, tts.stream_sample_rate)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Hermes is working (called every 2 s during a turn): beep if the line has been quiet. Quiet means
        1.5 s, not 2: a beep ends 0.3 s after it starts, and the next call must still beep."""
        call = self._calls.get(chat_id)
        if call is not None and not call.playout and time.monotonic() - call.last_sound > 1.5:
            call.play(THINK_BEEP)

    async def _send_exec_approval_prompt(self, prompt) -> SendResult:
        """Command approvals cannot be given safely by voice: deny at once and tell the caller."""
        from tools.approval import resolve_gateway_approval
        denied = resolve_gateway_approval(prompt.session_key, "deny")
        logger.warning("phone: denied %d command approval(s) on call %s", denied, prompt.chat_id[:8])
        self.resume_typing_for_chat(prompt.chat_id)  # the runner paused the beeps for the prompt
        if (call := self._calls.get(prompt.chat_id)) is not None:
            await self._speak(call, self._phrases["denied"])
        return SendResult(success=True)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Cron deliveries (marked with their job id) reach the user. Anything else is never spoken:
        notices from the gateway are English text and replies arrive as audio."""
        if (metadata or {}).get("job_id"):
            return await self._deliver(content)
        logger.debug("phone: not speaking text sent to %s: %.80s", chat_id[:8], content)
        return SendResult(success=True)

    # --- outbound: messages Hermes delivers

    async def _deliver(self, text: str) -> SendResult:
        """Say ``text`` in the user's live call, else call the user. The delivery target is ignored:
        Hermes only ever calls PHONE_HOME_NUMBER."""
        if self._home is None:
            return SendResult(success=False, error=HOME_NUMBER_INVALID)
        async with self._placing:  # a delivery arriving while a call is placed learns how it went
            call = next((c for c in self._calls.values() if c.user_id == self._home and not c.closed.is_set()), None)
            if call is not None:
                call.deliveries.append(text)
                self._say_deliveries(call)
                return SendResult(success=True)
            if self._outbound is not None:  # one call at a time: say it on the call that already rings
                self._outbound.messages.append(text)
                return SendResult(success=True)
            now = time.monotonic()
            self._placed = [t for t in self._placed if now - t < 3600]
            if len(self._placed) >= MAX_CALLS_PER_HOUR:
                logger.warning("phone: not calling, %d calls in the last hour already", len(self._placed))
                return SendResult(success=False, error=f"phone: at most {MAX_CALLS_PER_HOUR} calls an hour")
            self._placed.append(now)
            outbound = self._outbound = Outbound(str(uuid.uuid4()), [text])
            request = self._outgoing_dir / f"{outbound.call_id}.call"
            try:
                self._write_call_file(request, outbound.call_id)
            except OSError as e:
                self._outbound = None
                return SendResult(success=False, error=f"phone: cannot write the call request: {e}")
            if not await self._taken(request):
                with contextlib.suppress(OSError):
                    request.unlink()
                self._outbound = None
                return SendResult(success=False, error="phone: Asterisk did not take the call request; is its "
                                                         f"container running with {self._outgoing_dir} mounted?")
            logger.info("phone: calling %s (call %s)", _redact(self._home_number), outbound.call_id[:8])
            self._spawn(self._until_answered(outbound, request))
            return SendResult(success=True)

    def _say_deliveries(self, call: Call) -> None:
        if not call.saying:
            call.saying = True
            self._spawn(self._say_queued(call))

    async def _say_queued(self, call: Call) -> None:
        """Say delivered messages when the line is quiet: no turn running or on its way, nothing playing,
        the caller not talking. A message is said only if the line is still quiet once it is ready."""
        def quiet() -> bool:
            return not (self._busy(call) or call.recognising or call.playout or call.detector.in_speech
                        or call.holds)

        try:
            while call.deliveries and not call.closed.is_set():
                if not quiet():
                    await asyncio.sleep(0.1)
                    continue
                text = call.deliveries[0]
                pcm = await self._synthesize(match[1] if (match := CRON_WRAPPER.fullmatch(text)) else text)
                # Not dropped by * or overtaken meanwhile, and not hung up: then it is kept as missed.
                if call.deliveries[:1] == [text] and quiet() and not call.closed.is_set():
                    call.deliveries.pop(0)
                    call.latest = uuid.uuid4().hex  # it takes the place of a reply: talking over it silences it
                    call.play(pcm, voice=True)
                    call.context.append(text)  # the caller hears it: the next question may be about it
        finally:
            call.saying = False

    def _write_call_file(self, request: Path, call_id: str) -> None:
        """Asterisk dials as soon as a call file appears, so it is written next to the dir and moved in."""
        fd, tmp = tempfile.mkstemp(dir=request.parent.parent, prefix=".phone-call-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"Channel: Local/{self._home_number}@hermes-callout/n\n"
                        "MaxRetries: 0\nWaitTime: 30\n"
                        "Context: hermes-answered\nExtension: s\nPriority: 1\n"
                        f"Setvar: CALLUUID={call_id}\n"
                        # The dialplan drops a request that sat unclaimed (e.g. Asterisk was down).
                        f"Setvar: __CALL_EXPIRES={int(time.time()) + 60}\n"
                        + (f"Setvar: __CALLER_ID={self._caller_id}\n" if self._caller_id.isdigit() else ""))
            os.rename(tmp, request)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @staticmethod
    async def _taken(request: Path) -> bool:
        """Asterisk appends a StartRetry line when it starts dialling, and removes the file when done."""
        deadline = time.monotonic() + TAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if "StartRetry:" in request.read_text():
                    return True
            except FileNotFoundError:
                return True
            await asyncio.sleep(0.05)
        return False

    async def _until_answered(self, outbound: Outbound, request: Path) -> None:
        """Unanswered, Asterisk removes the request after ringing; then the next delivery calls again."""
        deadline = time.monotonic() + RING_TIMEOUT_S
        try:
            while not outbound.answered and request.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
        finally:  # also when the gateway stops while it rings
            if not outbound.answered:
                logger.info("phone: call %s was not answered", outbound.call_id[:8])
                with contextlib.suppress(OSError):
                    request.unlink()
                if self._outbound is outbound:
                    self._outbound = None
                self._keep_missed(outbound.messages)

    # --- missed messages: kept in a private file, so a gateway restart does not lose them

    def _keep_missed(self, messages: list[str]) -> None:
        if not messages:
            return
        now = time.time()
        try:
            self._write_missed(self._read_missed() + [{"at": now, "text": text} for text in messages])
        except OSError:
            logger.exception("phone: could not keep %d missed message(s)", len(messages))
            return
        logger.info("phone: kept %d missed message(s) for the next call", len(messages))

    def _take_missed(self) -> list[str]:
        missed = self._read_missed()
        if missed:
            try:
                self._missed_file.unlink()
            except OSError:  # said now, it would be said again on every call
                logger.exception("phone: could not remove the missed messages, not saying them")
                return []
        return [entry["text"] for entry in missed]

    def _read_missed(self) -> list[dict]:
        """Missed messages younger than a day."""
        try:
            missed = json.loads(self._missed_file.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            logger.exception("phone: could not read the missed messages")
            return []
        return [entry for entry in missed if time.time() - entry["at"] < MISSED_KEEP_S]

    def _write_missed(self, missed: list[dict]) -> None:
        self._missed_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._missed_file.parent, prefix=".missed-")  # mode 600
        with os.fdopen(fd, "w") as f:
            json.dump(missed, f)
        os.replace(tmp, self._missed_file)

    async def send_voice(self, chat_id: str, audio_path: str, *args, **kwargs) -> SendResult:
        return SendResult(success=True)  # also covers play_tts: replies are only ever spoken by this adapter

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"Phone call {chat_id[:8]}", "type": "dm", "chat_id": chat_id}
