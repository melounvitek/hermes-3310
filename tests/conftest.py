"""Test harness: the plugin is loaded by Hermes's own plugin loader from a temporary HERMES_HOME.

Only the LLM is faked (``gateway_turn``); the TTS consumer, the gateway's streaming-TTS setup and
finalisation, the adapter base class and the Piper voice are the real ones.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")).expanduser()
sys.path.insert(0, str(HERMES_AGENT))
# Nothing a test runs may install packages into Hermes's venv (Hermes's own test harness sets this too).
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

VOICE = Path(os.environ.get("PHONE_TEST_VOICE", "~/.hermes/cache/piper-voices/cs_CZ-jirka-medium.onnx")).expanduser()
FRAME = 320  # 20 ms of 8 kHz int16 mono
PIN, CALLER = "4321", "420777000111"
HOME_NUMBER = "00420777000111"  # CALLER in the provider's dial format: the number Hermes calls
SHORT_PHRASES = {"pin": "Kód.", "wrong_pin": "Špatně.", "bye": "Nashle.", "hello": "Ahoj.",
                 "not_understood": "Nerozuměl jsem.", "stopped": "Stop.", "denied": "Zamítnuto.", "missed": "Volal jsem."}


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A Hermes home with this repo installed as an enabled plugin; ``configure(tts, **sections)`` writes
    config.yaml."""
    if not VOICE.exists():
        pytest.skip(f"Piper voice not found: {VOICE}")
    from hermes_cli.plugins import _reset_plugin_managers_for_tests

    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "hermes-3310").symlink_to(ROOT)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def configure(tts: dict, **sections) -> None:
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["hermes-3310"]}, "tts": tts,
                                                          **sections}))

    configure({"provider": "piper", "piper": {"voice": str(VOICE), "length_scale": 0.65}})
    _reset_plugin_managers_for_tests()
    yield SimpleNamespace(home=home, configure=configure, load=_load_plugin)
    _reset_plugin_managers_for_tests()


def _load_plugin():
    from hermes_cli.plugins import _ensure_plugins_discovered
    _ensure_plugins_discovered(force=True)


def desktop(home: Path, *requests) -> list:
    """The responses of Hermes's dashboard backend for ``home`` to ``requests`` (see desktop_backend.py)."""
    output = home / "desktop-responses.json"
    backend = ROOT / "tests" / "desktop_backend.py"
    run = subprocess.run([sys.executable, str(backend), json.dumps(requests), str(output)],
                         env={**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(HERMES_AGENT)},
                         capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(output.read_text())


def streaming_tts(voice=VOICE, **piper):
    # Without Piper's noise the same text always sounds the same, so speech can be compared by length.
    return {"provider": "piper", "streaming": {"provider": "piper_stream"},
            "piper": {"voice": str(voice), "length_scale": 0.65, "noise_scale": 0, "noise_w_scale": 0, **piper}}


@pytest.fixture
def make_adapter(hermes, tmp_path, monkeypatch):
    """Build the registered ``phone`` adapter (listening on a free port) with PIN and allow-list env;
    ``make(platform=None, **settings)`` first saves the plugin settings like the plugin's page in the Desktop
    does, then starts like the gateway: the plugin loads, then the adapter is made with ``platforms.phone``
    (``platform`` adds keys to it)."""
    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins_settings import save_plugin_settings

    monkeypatch.setenv("PHONE_PIN", PIN)
    monkeypatch.setenv("PHONE_ALLOWED_CALLERS", f"{CALLER}, 420777000222")
    monkeypatch.setenv("PHONE_HOME_NUMBER", HOME_NUMBER)
    hermes.configure(streaming_tts())
    calls_dir, outgoing_dir = tmp_path / "calls", tmp_path / "phone" / "outgoing"
    calls_dir.mkdir()
    outgoing_dir.mkdir(parents=True)

    def make(platform=None, **settings):
        save_plugin_settings("hermes-3310", ROOT, {
            **{f"phrase_{name}": text for name, text in SHORT_PHRASES.items()}, **settings})
        hermes.load()
        entry = platform_registry.get("phone")
        assert entry is not None, "plugin did not register the phone platform"
        adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={
            "port": 0, "calls_dir": str(calls_dir), "outgoing_dir": str(outgoing_dir), **(platform or {})}))
        adapter.calls_dir, adapter.outgoing_dir = calls_dir, outgoing_dir
        return adapter

    return make


@pytest.fixture
async def phone_adapter(make_adapter):
    adapter = make_adapter()
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


async def logged_in_phone(adapter) -> "Phone":
    """An allowed caller past the PIN, with the prompts already played and forgotten."""
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()
    phone.frames.clear()
    await phone.press(PIN + "#")
    await phone.wait_for_silence()  # greeting
    phone.frames.clear()
    return phone


class Phone:
    """The Asterisk side of one AudioSocket call: sends the call id, records what it hears."""

    def __init__(self, call_id: uuid.UUID = None):
        self.call_id = call_id or uuid.uuid4()
        self.frames: list[tuple[float, bytes]] = []
        self.hung_up = asyncio.Event()
        self._reader_task = None

    async def dial(self, port: int, calls_dir: Path, caller: str = CALLER) -> "Phone":
        """Like the dialplan: write the caller number for the call id, then connect."""
        (calls_dir / str(self.call_id)).write_text(caller + "\n")
        return await self.connect(port)

    async def connect(self, port: int) -> "Phone":
        self.r, self.w = await asyncio.open_connection("127.0.0.1", port)
        self.w.write(b"\x01\x00\x10" + self.call_id.bytes)
        await self.w.drain()
        self._reader_task = asyncio.create_task(self._read())
        deadline = time.monotonic() + 5
        while not self.frames:  # the first (silent) frame means the adapter has the call
            assert time.monotonic() < deadline, "the adapter never answered the call"
            await asyncio.sleep(0.01)
        return self

    async def _read(self):
        try:
            while True:
                head = await self.r.readexactly(3)
                payload = await self.r.readexactly(struct.unpack(">H", head[1:])[0])
                if head[0] == 0x10:
                    self.frames.append((time.monotonic(), payload))
                elif head[0] == 0x00:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            self.hung_up.set()

    async def say(self, text: str, then_silence: float = 2.0, realtime: bool = False) -> None:
        """Speak Czech ``text`` (Piper voice, 8 kHz), then keep sending silence like a real phone."""
        await self.send_audio(call_audio(text) + b"\x00" * int(16000 * then_silence), realtime)

    async def send_audio(self, pcm: bytes, realtime: bool = False) -> None:
        """All at once by default (faster tests); ``realtime`` paces it like a phone."""
        started = time.monotonic()
        for n, i in enumerate(range(0, len(pcm), FRAME)):
            self.w.write(b"\x10\x01\x40" + pcm[i:i + FRAME].ljust(FRAME, b"\x00"))
            if realtime:
                await self.w.drain()
                await asyncio.sleep(max(0.0, started + 0.02 * (n + 1) - time.monotonic()))
        await self.w.drain()

    async def press(self, keys: str) -> None:
        for key in keys:
            self.w.write(b"\x03\x00\x01" + key.encode())
            await self.w.drain()
            await asyncio.sleep(0.15)

    def speech_frames(self, start: float = 0, end: float = float("inf")) -> int:
        return sum(1 for t, f in self.frames if start <= t < end and is_speech(f))

    async def wait_for_speech(self, timeout: float, run: int = 1) -> None:
        """Wait for ``run`` consecutive speech frames (20 or more is a voice, not a beep)."""
        deadline = time.monotonic() + timeout
        while not self._longest_speech_run() >= run:
            assert time.monotonic() < deadline, "no speech heard on the call"
            await asyncio.sleep(0.02)

    def _longest_speech_run(self) -> int:
        longest = current = 0
        for _, frame in self.frames:
            current = current + 1 if is_speech(frame) else 0
            longest = max(longest, current)
        return longest

    async def wait_for_silence(self, seconds: float = 1.0, timeout: float = 60) -> None:
        """Wait until speech was heard and then nothing but silence for ``seconds``."""
        deadline = time.monotonic() + timeout
        while True:
            assert time.monotonic() < deadline, "the call never went quiet"
            speech = [t for t, f in self.frames if is_speech(f)]
            if speech and time.monotonic() - speech[-1] > seconds:
                return
            await asyncio.sleep(0.05)

    async def hang_up(self):
        with contextlib.suppress(ConnectionError):
            self.w.write(b"\x00\x00\x00")
            await self.w.drain()
        self.w.close()
        if self._reader_task:
            await self._reader_task


def is_speech(frame: bytes) -> bool:
    x = np.frombuffer(frame, "<i2").astype(np.float32)
    return bool(len(x)) and float(np.sqrt((x ** 2).mean())) > 300


_caller_voice = None


def call_audio(text: str) -> bytes:
    """``text`` spoken by the Czech Piper voice, as 8 kHz call audio."""
    global _caller_voice
    from piper import PiperVoice
    _caller_voice = _caller_voice or PiperVoice.load(str(VOICE))
    chunks = list(_caller_voice.synthesize(text))
    x = np.concatenate([np.frombuffer(c.audio_int16_bytes, "<i2") for c in chunks]).astype(np.float32)
    n = int(len(x) * 8000 / chunks[0].sample_rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2").tobytes()


def speech_frames_of(pcm8k: bytes) -> int:
    return sum(1 for i in range(0, len(pcm8k), FRAME) if is_speech(pcm8k[i:i + FRAME]))


class FakeGateway:
    """Message handler that plays Hermes's runner with a scripted fake LLM.

    Each voice message gets the next reply from ``replies`` (a list of text parts; the last reply
    repeats), streamed through the runner's own streaming-TTS start and finalisation, with the
    session context variables bound like the runner does. ``between()`` is awaited after each part
    but the last. ``/stop`` is answered like Hermes; the base adapter then cancels the running turn.
    On cancel the runner's cleanup aborts the turn's speech; with ``keeps_running`` the old turn
    instead goes on streaming its remaining parts, as an execution owned by the session authority does.
    """

    def __init__(self, adapter, *replies, between=None, keeps_running=False):
        self.adapter, self.replies, self.between, self.keeps_running = adapter, replies, between, keeps_running
        self.events, self.started = [], []

    async def __call__(self, event):
        from gateway.run_turn import GatewayTurnMixin
        from gateway.session_context import clear_session_vars, set_session_vars

        self.events.append(event)
        if event.text == "/stop":
            return "⚡ Stopped. You can continue this session."
        self.started.append(time.monotonic())
        parts = self.replies[min(len(self.started), len(self.replies)) - 1]
        tokens = set_session_vars(platform="phone", chat_id=event.source.chat_id,
                                  message_id=event.source.message_id or "")
        holder, sent = [None], 0
        try:
            runner = SimpleNamespace(_delivery_adapter_for=lambda source: self.adapter,
                                     _gateway_loop=asyncio.get_running_loop())
            GatewayTurnMixin._run_agent_start_streaming_tts(runner, event.source, event.message_type, None, holder)
            for part in parts:
                if holder[0] is not None:
                    holder[0].on_delta(part)
                sent += 1
                if self.between and sent < len(parts):
                    await self.between()
            turn = SimpleNamespace(streaming_tts_consumer_holder=holder, session_key="phone-test",
                                   run_generation=1)
            await GatewayTurnMixin._run_agent_finalize_streaming_tts(runner, turn, self.adapter)
            return "".join(parts)
        except asyncio.CancelledError:
            if holder[0] is not None:
                if self.keeps_running:
                    asyncio.create_task(self._go_on(holder[0], parts[sent:]))
                else:
                    holder[0].abort("cleanup")
            raise
        finally:
            clear_session_vars(tokens)

    async def _go_on(self, consumer, parts):
        for part in parts:
            await asyncio.sleep(0.3)
            consumer.on_delta(part)
        consumer.finish()


def gateway_turn(adapter, *parts: str, between=None):
    """One scripted reply streamed in ``parts`` (see FakeGateway)."""
    return FakeGateway(adapter, list(parts), between=between)


class FakeSTT:
    """Stands in for Hermes's speech-to-text: answers with the next of ``transcripts`` (the last
    repeats) and records each WAV it was given."""

    def __init__(self, monkeypatch, *transcripts, delay=0.0, local="Lokální přepis."):
        self.transcripts = list(transcripts) or ["Jaké bude zítra počasí?"]
        self.delay, self.local = delay, local
        self.seconds, self.paths, self.local_calls = [], [], 0
        monkeypatch.setattr("tools.transcription_tools.transcribe_audio", self.cloud)
        monkeypatch.setattr("tools.transcription_tools.transcribe_audio_local_fallback", self.fallback)

    def _record(self, path):
        import wave
        self.paths.append(Path(path))
        with wave.open(path) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
            self.seconds.append(w.getnframes() / 8000)

    def cloud(self, file_path, model=None, source=None):
        self._record(file_path)
        time.sleep(self.delay)
        transcript = self.transcripts[min(len(self.seconds), len(self.transcripts)) - 1]
        return {"success": True, "transcript": transcript, "provider": "fake"}

    def fallback(self, file_path, model=None):
        self._record(file_path)
        self.local_calls += 1
        return {"success": True, "transcript": self.local, "provider": "local"}


class Spool:
    """Asterisk's side of call files: like pbx_spool it takes each call request the plugin drops into
    the outgoing dir (appending a StartRetry line) and queues it for the test to answer or ring out.
    With ``running`` False, Asterisk is down and requests are left alone."""

    def __init__(self, outgoing_dir: Path):
        self.dir, self.running = outgoing_dir, True
        self.requests: asyncio.Queue[SimpleNamespace] = asyncio.Queue()
        self._taken: set[Path] = set()
        self._task = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            for path in sorted(self.dir.iterdir()) if self.running else ():
                if path not in self._taken:
                    self._taken.add(path)
                    lines = [line.split(": ", 1) for line in path.read_text().splitlines() if ": " in line]
                    with path.open("a") as f:
                        f.write(f"\nStartRetry: 1 1 ({int(time.time())})\n")
                    channel = dict(lines)["Channel"]
                    self.requests.put_nowait(SimpleNamespace(
                        path=path, channel=channel, number=channel.removeprefix("Local/").split("@")[0],
                        vars=dict(v.lstrip("_").split("=", 1) for k, v in lines if k == "Setvar")))
            await asyncio.sleep(0.02)

    async def next_request(self, timeout: float = 5) -> SimpleNamespace:
        return await asyncio.wait_for(self.requests.get(), timeout)

    async def answer(self, request, port: int) -> "Phone":
        """The callee picked up: hermes-answered connects AudioSocket with the plugin's call id."""
        return await Phone(uuid.UUID(request.vars["CALLUUID"])).connect(port)

    @staticmethod
    def ring_out(request) -> None:
        """Nobody answered: Asterisk removes the request."""
        request.path.unlink()

    def stop(self):
        self._task.cancel()


@pytest.fixture
async def spool(phone_adapter):
    spool = Spool(phone_adapter.outgoing_dir)
    yield spool
    spool.stop()


async def deliver(adapter, text: str, chat_id: str = HOME_NUMBER):
    """What cron's live delivery to ``phone`` does: a send marked with the job id."""
    return await adapter.send(chat_id, text, metadata={"job_id": "job-1", "notify": True})


async def until(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)
