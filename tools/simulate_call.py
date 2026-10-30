#!/usr/bin/env python3
"""Fake Asterisk AudioSocket call for manual end-to-end checks of the phone adapter.

Runs the STEPs in order while sending 20 ms audio frames in real time (silence between steps, like a
real phone), waits for the reply, hangs up and prints a timeline of the sent steps and the received
speech, transcribed with faster-whisper.

Steps:
  KEY:<digits>     DTMF, 150 ms apart (KEY:1234#, KEY:*)
  WAIT:<seconds>   silence
  NOISE:<seconds>  random noise
  WAV:<file>       16-bit mono WAV of any rate
  anything else    Czech text spoken by Piper

After the last step it waits until a reply was heard and then --tail seconds of silence (at most
--max-wait seconds). Run it with ~/.hermes/hermes-agent/venv/bin/python, e.g. against the test gateway:
  HERMES_HOME=~/hermes-3310-test/home tools/simulate_call.py --caller 420777000111 \\
      KEY:1234# WAIT:2 "Kolik je hodin v Praze?"

With --answer it waits (up to --max-wait seconds) for the call Hermes places, takes its call file from
--outgoing-dir like Asterisk and answers it, then runs the steps:
  HERMES_HOME=~/hermes-3310-test/home tools/simulate_call.py --answer KEY:1234# WAIT:8 "Kde to bude?"
"""
import argparse
import asyncio
import contextlib
import os
import struct
import sys
import time
import uuid
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio import FRAME_BYTES, SR, to_call_rate  # noqa: E402

LIVE_PORT = 9092  # the default port of a running phone gateway: never call a real line by accident
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def is_speech(frame: bytes) -> bool:
    x = np.frombuffer(frame, "<i2").astype(np.float32)
    return bool(len(x)) and float(np.sqrt((x ** 2).mean())) > 300  # same threshold as tests/conftest.py


def prepare(steps, voice_path):
    """STEP strings -> (label, payload): DTMF digits (str), seconds to wait (float) or 8 kHz PCM (bytes)."""
    voice, prepared = None, []
    for step in steps:
        kind, _, arg = step.partition(":")
        try:
            if kind == "KEY":
                payload = arg
            elif kind == "WAIT":
                payload = float(arg)
            elif kind == "NOISE":
                noise = np.random.default_rng().normal(0, 3000, int(float(arg) * SR))
                payload = noise.clip(-32768, 32767).astype("<i2").tobytes()
            elif kind == "WAV":
                with wave.open(arg) as f:
                    if (f.getnchannels(), f.getsampwidth()) != (1, 2):
                        raise ValueError("not a 16-bit mono WAV")
                    payload = to_call_rate(f.readframes(f.getnframes()), f.getframerate())
            else:
                if voice is None:
                    from piper import PiperVoice
                    voice = PiperVoice.load(str(voice_path))
                chunks = list(voice.synthesize(step))
                payload = to_call_rate(b"".join(c.audio_int16_bytes for c in chunks), chunks[0].sample_rate)
                step = repr(step)
        except (OSError, EOFError, ValueError, wave.Error) as e:
            sys.exit(f"bad step {step!r}: {e}")
        prepared.append((step, payload))
    return prepared


async def take_call_request(outgoing_dir, max_wait):
    """Like Asterisk's spool: the first call file Hermes drops, marked as taken. Returns (path, call id)."""
    print(f"waiting for Hermes to call (call files in {outgoing_dir})", flush=True)
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        for path in outgoing_dir.glob("*.call"):
            fields = [line.split(": ", 1) for line in path.read_text().splitlines() if ": " in line]
            with path.open("a") as f:
                f.write(f"\nStartRetry: 1 1 ({int(time.time())})\n")
            print(f"Hermes calls {dict(fields)['Channel']}", flush=True)
            return path, uuid.UUID(next(v for k, v in fields if k == "Setvar" and v.startswith("CALLUUID="))[9:])
        await asyncio.sleep(0.1)
    sys.exit("Hermes did not call")


async def call(args, steps):
    """Dial (or answer Hermes's call), run the steps, wait for the reply, hang up. Returns the sent steps
    and the received frames."""
    if args.answer:
        caller_file, call_id = await take_call_request(args.outgoing_dir, args.max_wait)
    else:
        call_id = uuid.uuid4()
        caller_file = args.calls_dir / str(call_id)
        args.calls_dir.mkdir(parents=True, exist_ok=True)
        caller_file.write_text(args.caller + "\n")  # what the dialplan does before AudioSocket()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", args.port)
    except OSError as e:
        caller_file.unlink()
        sys.exit(f"cannot connect to 127.0.0.1:{args.port}: {e}")
    if args.answer:
        print(f"call {call_id} answered on 127.0.0.1:{args.port}", flush=True)
    else:
        print(f"call {call_id} from {args.caller or '(no number)'} to 127.0.0.1:{args.port}", flush=True)
    w.write(b"\x01\x00\x10" + call_id.bytes)
    t0 = time.monotonic()
    now = lambda: time.monotonic() - t0  # noqa: E731
    outgoing = bytearray()  # caller audio not sent yet; the pacer sends silence when it is empty
    sent = []  # [start, label, end of the caller's audio or keys]
    received = []  # (arrival time, 20 ms frame)
    last_speech = -1.0

    async def read():
        nonlocal last_speech
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                head = await r.readexactly(3)
                payload = await r.readexactly(struct.unpack(">H", head[1:])[0])
                if head[0] == 0x00:
                    return
                if head[0] == 0x10:
                    t = now()
                    for i in range(0, len(payload), FRAME_BYTES):
                        received.append((t + i / (2 * SR), payload[i:i + FRAME_BYTES]))
                        if is_speech(received[-1][1]):
                            last_speech = received[-1][0]

    async def pace():
        frames = 0
        with contextlib.suppress(ConnectionError):
            while True:
                frame = bytes(outgoing[:FRAME_BYTES]).ljust(FRAME_BYTES, b"\0")
                del outgoing[:FRAME_BYTES]
                w.write(b"\x10\x01\x40" + frame)
                await w.drain()
                frames += 1
                await asyncio.sleep(frames * 0.02 - now())

    async def script():
        for label, payload in steps:
            sent.append([now(), label, None])
            if isinstance(payload, float):
                await asyncio.sleep(payload)
                continue
            if isinstance(payload, str):
                for i, digit in enumerate(payload):
                    if i:
                        await asyncio.sleep(0.15)
                    w.write(b"\x03\x00\x01" + digit.encode())
            else:
                outgoing.extend(payload)
                while outgoing:
                    await asyncio.sleep(0.005)
            sent[-1][2] = now()
        steps_end = now()
        while now() < steps_end + args.max_wait:
            if last_speech > steps_end and now() - last_speech > args.tail:
                return
            await asyncio.sleep(0.05)

    tasks = [asyncio.create_task(c()) for c in (script, read, pace)]
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    we_hung_up = tasks[0] in done
    sent.append([now(), "hangup" if we_hung_up else "the other side hung up", None])
    with contextlib.suppress(ConnectionError):
        if we_hung_up:
            w.write(b"\x00\x00\x00")
            await w.drain()
        w.close()
        await w.wait_closed()
    if args.answer:
        caller_file.unlink()  # Asterisk removes a call file when the call is over
    return sent, received


def speech_segments(received):
    """[first, last] frame indexes of speech, merging gaps under 300 ms."""
    segments = []
    for i, (t, frame) in enumerate(received):
        if is_speech(frame):
            if segments and t - (received[segments[-1][1]][0] + 0.02) < 0.3:
                segments[-1][1] = i
            else:
                segments.append([i, i])
    return segments


def transcribe(pcms):
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    texts = []
    for pcm in pcms:
        x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        x = np.interp(np.arange(2 * len(x)) / 2, np.arange(len(x)), x).astype(np.float32)  # 8 -> 16 kHz
        parts, _ = model.transcribe(x, language="cs", condition_on_previous_text=False)
        texts.append(" ".join(p.text.strip() for p in parts) or "(no words)")
    return texts


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=9093)
    p.add_argument("--caller", default=os.environ.get("CALLER", ""),
                   help="caller number written for the adapter, may be empty (default: $CALLER)")
    p.add_argument("--calls-dir", type=Path, default=HERMES_HOME / "phone" / "calls",
                   help="where the dialplan writes caller numbers (default: $HERMES_HOME/phone/calls)")
    p.add_argument("--answer", action="store_true", help="answer the call Hermes places instead of calling")
    p.add_argument("--outgoing-dir", type=Path, default=HERMES_HOME / "phone" / "outgoing",
                   help="where Hermes drops call files (default: $HERMES_HOME/phone/outgoing)")
    p.add_argument("--voice", type=Path,
                   default=Path("~/.hermes/cache/piper-voices/cs_CZ-jirka-medium.onnx").expanduser())
    p.add_argument("--save", type=Path, metavar="OUT.wav", help="write the received audio to a WAV")
    p.add_argument("--no-transcribe", action="store_true")
    p.add_argument("--tail", type=float, default=4, help="seconds of silence that end the reply (default: 4)")
    p.add_argument("--max-wait", type=float, default=90, help="longest wait for the reply (default: 90)")
    p.add_argument("steps", nargs="*", metavar="STEP")
    args = p.parse_args()
    if args.port == LIVE_PORT:
        p.error(f"port {LIVE_PORT} is the live phone bridge; use the test gateway's port")

    sent, received = asyncio.run(call(args, prepare(args.steps, args.voice)))

    segments = speech_segments(received)
    pcms = [b"".join(frame for _, frame in received[first:last + 1]) for first, last in segments]
    texts = ["speech"] * len(pcms) if args.no_transcribe else transcribe(pcms)
    rows = [(start, end, ">", label) for start, label, end in sent]
    rows += [(received[first][0], received[last][0] + 0.02, "<", text)
             for (first, last), text in zip(segments, texts)]
    print("\ntimeline, seconds since connect (> sent, < received; +latency after the caller's last audio/key):")
    caller_end = None
    for start, end, who, text in sorted(rows, key=lambda row: row[0]):
        if who == "<" and caller_end is not None and start >= caller_end:
            text, caller_end = f"+{start - caller_end:.2f}s  {text}", None
        elif who == ">" and end is not None:
            caller_end = end
        span = f"{start:6.2f}" + ("" if end is None or end - start < 0.01 else f"-{end:.2f}")
        print(f"{span:14} {who} {text}")
    print(f"received {len(received) * 0.02:.1f}s of audio")

    if args.save:
        with wave.open(str(args.save), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SR)
            f.writeframes(b"".join(frame for _, frame in received))
    if not received:
        sys.exit("no audio received")


if __name__ == "__main__":
    main()
