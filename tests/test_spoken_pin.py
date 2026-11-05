"""Spoken PIN (off by default): the caller may say the PIN instead of typing it. It is recognised on this machine
with the local Whisper model and never reaches Hermes, its speech recognition or the log."""
import asyncio
import logging
import sys

import pytest

from conftest import FRAME, SHORT_PHRASES, FakeSTT, Phone, call_audio, until
from test_call_access import recording_handler
from test_outbound_calls import heard_about

# One the local model hears reliably in the test voice (in 4321 it often hears the first čtyři as tři).
PIN, SAID = "2580", "dva, pět, osm, nula"
WRONG = "jedna, pět, devět, nula"


@pytest.fixture
def start(make_adapter, monkeypatch):
    """The adapter with the PIN above, started; with the spoken PIN on, once its model loaded (in the background)."""
    monkeypatch.setenv("PHONE_PIN", PIN)

    async def start(**settings):
        adapter = make_adapter(**settings)
        assert await adapter.connect()
        if settings.get("spoken_pin"):
            await until(lambda: adapter._pin_model is not None, timeout=30)
        return adapter

    return start


async def test_saying_the_pin_lets_the_caller_in_and_none_of_it_reaches_hermes(start, monkeypatch, caplog):
    stt = FakeSTT(monkeypatch, "Jaké bude počasí?")
    adapter = await start(spoken_pin=True)
    caplog.set_level(logging.DEBUG, logger=type(adapter).__module__)
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["pin_spoken"])  # the prompt offers saying it
    phone.frames.clear()

    await phone.say(SAID)
    await phone.say(", ".join([SAID] * 3), realtime=True)  # said again, and still being said when it is recognised
    await phone.wait_for_silence()  # the greeting
    assert stt.seconds == []  # Hermes's speech recognition heard none of it
    await phone.say("Jaké bude počasí?")
    await until(lambda: events, timeout=30)

    assert [event.text for event in events] == ["Jaké bude počasí?"] and len(stt.seconds) == 1
    logged = " ".join(record.getMessage() for record in caplog.records if record.name == type(adapter).__module__)
    assert "connected" in logged and PIN not in logged and "osm" not in logged
    await phone.hang_up()
    await adapter.disconnect()


async def test_typing_the_pin_while_saying_it_keeps_the_speech_out_of_hermes(start, monkeypatch):
    stt = FakeSTT(monkeypatch, "Jaké bude počasí?")
    adapter = await start(spoken_pin=True)
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()
    phone.frames.clear()

    saying = asyncio.create_task(phone.say(", ".join([SAID] * 2), realtime=True))
    await asyncio.sleep(1)
    await phone.press(PIN + "#")
    await saying
    await phone.wait_for_silence()  # the greeting
    assert stt.seconds == []
    await phone.say("Jaké bude počasí?")
    await until(lambda: events, timeout=30)

    assert [event.text for event in events] == ["Jaké bude počasí?"] and len(stt.seconds) == 1
    await phone.hang_up()
    await adapter.disconnect()


def test_the_call_goes_on_only_after_a_pause_not_while_the_pin_is_said_again(make_adapter):
    """Speech that starts as the PIN is recognised counts at once, before the detector confirms it as speech,
    and long speech is cut into utterances without a pause: neither is silence."""
    detector = sys.modules[type(make_adapter()).__module__].SpeechDetector()
    detector.MAX_S = 2
    for _ in range(75):  # 1.5 s of silence
        detector.feed(bytes(FRAME))
    assert detector.silent

    speech = call_audio(", ".join([SAID] * 3))
    heard = [(detector.feed(speech[i:i + FRAME]), detector.in_speech, detector.silent)
             for i in range(0, len(speech), FRAME)]
    onset = next(i for i, (_, _, silent) in enumerate(heard) if not silent)

    assert not heard[onset][1]  # not confirmed as speech yet
    assert not any(silent for _, _, silent in heard[onset:]) and any(cut for cut, _, _ in heard)


async def test_a_wrong_spoken_pin_counts_toward_the_lockout(start, monkeypatch):
    adapter = await start(spoken_pin=True)
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "MAX_WRONG_PINS", 1)
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()

    await phone.say(WRONG)

    await asyncio.wait_for(phone.hung_up.wait(), timeout=30)  # the line locked at once
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    assert events == []
    await adapter.disconnect()


async def test_speech_without_a_pin_uses_a_try_but_not_the_lockout_and_keys_typed_meanwhile_count(start,
                                                                                                    monkeypatch):
    adapter = await start(spoken_pin=True)
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "MAX_WRONG_PINS", 1)
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()

    await phone.say("Dobrý den, tady Vítek.")
    await asyncio.sleep(0.3)
    await phone.press(PIN + "#")  # typed while that speech is recognised: the next try
    await until(lambda: str(phone.call_id) in adapter._calls, timeout=30)
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    await until(lambda: events)

    assert [event.text for event in events] == ["Ahoj"] and not phone.hung_up.is_set()
    await phone.hang_up()
    await adapter.disconnect()


async def test_a_spoken_pin_is_ignored_unless_turned_on(start, monkeypatch):
    stt = FakeSTT(monkeypatch)
    adapter = await start()
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()

    await phone.say(SAID)
    await asyncio.sleep(3)
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    await asyncio.sleep(0.2)

    assert events == [] and stt.seconds == []
    await phone.hang_up()
    await adapter.disconnect()
