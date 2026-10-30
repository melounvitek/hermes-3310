"""Round 4: talking over a reply and the * key stop the turn; hangup stops it; approvals are denied;
beeps while Hermes works."""
import asyncio
import time

import pytest

from conftest import FakeGateway, FakeSTT, is_speech, logged_in_phone, until
from test_call_speech import reference_speech_frames

QUESTION = "Řekni mi něco o historii Prahy, ale pořádně."
FOLLOW_UP = "Ne, radši mi řekni, kolik je teď hodin."
LONG_REPLY = ["Praha je jedno z nejstarších měst ve střední Evropě a má velmi bohatou historii. ",
              "Podle pověsti ji založila kněžna Libuše, když z Vyšehradu předpověděla slávu města. ",
              "Ve čtrnáctém století za Karla Čtvrtého se Praha stala sídlem císaře a vznikla univerzita. ",
              "V roce sedmnáct set čtyřicet osm začala stavba Karlova mostu, který stojí dodnes. ",
              "Po roce devatenáct set osmnáct se Praha stala hlavním městem samostatného Československa."]
SHORT_REPLY = ["Je přesně poledne."]
TONES = 40  # frames of beeps and ticks that may sound between two replies (20 ms each)


def voice_messages(gateway):
    return [e.text for e in gateway.events]


async def talk_over_the_reply(adapter, phone, gateway):
    """Ask, wait until the reply is heard, then talk over it. Returns when the caller started talking."""
    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20, run=20)  # the reply, not the beep
    talking = time.monotonic()
    await phone.say(FOLLOW_UP)
    return talking


@pytest.mark.parametrize("old_turn_keeps_streaming", [False, True])
async def test_talking_over_the_reply_silences_it_and_asks_the_new_question(
        phone_adapter, monkeypatch, old_turn_keeps_streaming):
    FakeSTT(monkeypatch, QUESTION, FOLLOW_UP, delay=1)  # the "heard you" tick comes after recognition
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, LONG_REPLY, ["", *SHORT_REPLY],  # the new reply starts a second later
                          between=lambda: asyncio.sleep(1), keeps_running=old_turn_keeps_streaming)
    phone_adapter.set_message_handler(gateway)

    talking = await talk_over_the_reply(phone_adapter, phone, gateway)
    await until(lambda: len(gateway.started) == 2, timeout=20)
    await phone.wait_for_silence(seconds=1.5)

    assert voice_messages(gateway) == [QUESTION, "/stop", FOLLOW_UP]
    # The old reply stopped at once; only the tick and the working beep sound before the new reply.
    assert 5 <= phone.speech_frames(talking + 0.5, gateway.started[1] + 0.8) <= TONES
    assert phone.speech_frames(gateway.started[1]) > 30  # the new reply is heard...
    assert phone.speech_frames(gateway.started[1]) < 200  # ...and only it, not the rest of the old one
    await phone.hang_up()


async def test_talking_in_a_pause_between_sentences_silences_the_rest_at_once(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, FOLLOW_UP)
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, ["Dobře, hned to zjistím. ", *LONG_REPLY[1:]], SHORT_REPLY,
                          between=lambda: asyncio.sleep(4))
    phone_adapter.set_message_handler(gateway)

    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20, run=20)
    await phone.wait_for_silence(seconds=0.3)  # the first sentence is over, the next one is coming
    talking = time.monotonic()
    await phone.say(FOLLOW_UP, realtime=True)  # still talking when the next sentence arrives
    await until(lambda: len(gateway.started) == 2, timeout=20)

    assert phone.speech_frames(talking + 0.5, gateway.started[1]) <= TONES
    await phone.hang_up()


async def test_a_cough_over_the_reply_only_pauses_it(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, "Ehm.", delay=1.5)  # the sound turns out to be noise, after a moment
    reply = LONG_REPLY[:2]
    whole_reply = reference_speech_frames("".join(reply))
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, reply)
    phone_adapter.set_message_handler(gateway)

    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20, run=20)
    coughed = time.monotonic()
    await phone.say("Ehm, ehm.")
    await asyncio.sleep(1.2)
    paused = phone.speech_frames(coughed + 0.3, coughed + 1.2)
    await until(lambda: phone.speech_frames() >= 0.9 * whole_reply, timeout=30)  # then the rest of it

    assert paused <= TONES  # held while it was unclear what the sound was
    assert voice_messages(gateway) == [QUESTION]
    await phone.hang_up()


async def test_speech_right_after_a_cough_keeps_hermes_paused(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, "Ehm.", FOLLOW_UP, delay=1.5)
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, LONG_REPLY[:2], SHORT_REPLY)
    phone_adapter.set_message_handler(gateway)

    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20, run=20)
    coughed = time.monotonic()
    await phone.say("Ehm, ehm.")
    await phone.say(FOLLOW_UP)  # finished before the cough is understood
    await until(lambda: len(gateway.started) == 2, timeout=20)

    assert phone.speech_frames(coughed + 0.3, gateway.started[1]) <= TONES  # not resumed in between
    await phone.hang_up()


async def test_noise_while_hermes_answers_does_not_stop_the_turn(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, "ehm")
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, LONG_REPLY, between=lambda: asyncio.sleep(0.5))
    phone_adapter.set_message_handler(gateway)

    await talk_over_the_reply(phone_adapter, phone, gateway)
    await asyncio.sleep(4)

    assert voice_messages(gateway) == [QUESTION]
    await phone.hang_up()


async def test_star_key_silences_the_reply_and_stops_the_turn(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, FOLLOW_UP)
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, LONG_REPLY, between=lambda: asyncio.sleep(1))
    phone_adapter.set_message_handler(gateway)

    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20)
    await phone.say(FOLLOW_UP, then_silence=0)  # still being spoken when * is pressed: dropped too
    pressed = time.monotonic()
    await phone.press("*")
    await phone.send_audio(b"\x00" * 16000 * 3)
    await asyncio.sleep(4)

    assert voice_messages(gateway) == [QUESTION, "/stop"]
    assert 10 <= phone.speech_frames(pressed + 0.3) < 100  # stop tone and "Stop." only, not the rest of the reply
    await phone.hang_up()


async def test_hanging_up_stops_the_running_turn(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION)
    phone = await logged_in_phone(phone_adapter)
    gateway = FakeGateway(phone_adapter, LONG_REPLY, between=lambda: asyncio.sleep(2))
    phone_adapter.set_message_handler(gateway)

    await phone.say(QUESTION)
    await phone.wait_for_speech(timeout=20)
    await phone.hang_up()

    await until(lambda: voice_messages(gateway) == [QUESTION, "/stop"], timeout=5)


async def test_command_approvals_are_denied_and_the_caller_is_told(phone_adapter, monkeypatch):
    resolved = []
    monkeypatch.setattr("tools.approval.resolve_gateway_approval",
                        lambda session_key, choice, **kw: resolved.append((session_key, choice)) or 1)
    phone = await logged_in_phone(phone_adapter)

    result = await phone_adapter.send_exec_approval(str(phone.call_id), "rm -rf ~/Documents", "session-key")
    await phone.wait_for_speech(timeout=10)

    assert result.success and resolved == [("session-key", "deny")]
    await phone.hang_up()


def sound_onsets(phone, start: float, end: float) -> list[float]:
    """When separate sounds start (a gap over 300 ms separates them)."""
    onsets, last = [], None
    for t, frame in phone.frames:
        if start <= t < end and is_speech(frame):
            if last is None or t - last > 0.3:
                onsets.append(t)
            last = t
    return onsets


async def test_beeps_at_once_when_the_caller_stops_then_every_two_seconds(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, QUESTION, delay=1.5)  # recognition takes a moment, as it does on a real call
    phone = await logged_in_phone(phone_adapter)
    working = asyncio.Event()

    async def slow_turn(event):
        working.set()
        await asyncio.sleep(8)

    phone_adapter.set_message_handler(slow_turn)
    await phone.say(QUESTION)
    stopped = time.monotonic()  # the adapter takes the sentence as soon as its trailing silence arrives
    await asyncio.wait_for(working.wait(), timeout=10)
    started = time.monotonic()
    await asyncio.sleep(6.8)

    assert phone.speech_frames(stopped, started) >= 8  # the tick and the working beep, before Hermes starts
    assert len(sound_onsets(phone, started + 0.5, started + 6.5)) >= 3  # a beep every ~2 s while it works
    assert phone.speech_frames(started + 0.5, started + 6.5) <= 4 * 15  # short beeps, not continuous sound
    await phone.hang_up()
