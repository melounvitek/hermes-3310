"""Outbound calls: a message Hermes delivers to the phone rings the user's configured number, and only that.
After the PIN the message is said and the call goes on like any other; a live call gets it between turns."""
import asyncio
import sys
import time
import uuid

import pytest

from conftest import (CALLER, HOME_NUMBER, PIN, SHORT_PHRASES, FakeSTT, Phone, Spool, deliver, logged_in_phone,
                      until)
from test_call_speech import reference_speech_frames

MESSAGE = "Za deset minut ti začíná porada."
LONG_MESSAGE = ("Připomínám, že za deset minut ti začíná porada ve velké zasedačce ve třetím patře. "
                "Nezapomeň si s sebou vzít podklady k rozpočtu na příští rok a nabíječku k notebooku.")
TONES = 40  # frames of ticks and beeps that may sound around speech (20 ms each)


def recording_handler(adapter):
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    return events


def heard_about(frames: int, text: str) -> bool:
    """``frames`` of speech are ``text`` said once."""
    expected = reference_speech_frames(text)
    return 0.8 * expected <= frames <= 1.25 * expected + TONES


async def hear(phone, text: str, since: float = 0) -> None:
    """Until ``text`` was said into the call (most of its speech arrived) and the line went quiet."""
    expected = reference_speech_frames(text)
    await until(lambda: phone.speech_frames(since) >= 0.8 * expected, timeout=30)
    await phone.wait_for_silence()


async def enter_pin(phone, *messages: str) -> None:
    """Type the PIN and wait until the messages after it were said."""
    phone.frames.clear()
    await phone.press(PIN + "#")
    await hear(phone, " ".join(messages))


async def answered_call(adapter, spool, *messages):
    """Deliver ``messages``, pick up the call Hermes places and wait until the PIN prompt was said."""
    for message in messages:
        await deliver(adapter, message)
    phone = await spool.answer(await spool.next_request(), adapter.port)
    await phone.wait_for_silence()
    return phone


async def test_delivery_rings_the_home_number(phone_adapter, spool):
    result = await deliver(phone_adapter, MESSAGE)
    request = await spool.next_request()

    assert result.success
    assert request.number == HOME_NUMBER
    assert uuid.UUID(request.vars["CALLUUID"])


@pytest.mark.parametrize("target", ["00420999888777", "+420999888777", str(uuid.uuid4())])
async def test_the_number_comes_from_config_never_from_the_delivery_target(phone_adapter, spool, target):
    await deliver(phone_adapter, MESSAGE, chat_id=target)

    assert (await spool.next_request()).number == HOME_NUMBER


@pytest.mark.parametrize("home", [None, "00420999888777", "+420777000111", "00420777000111;1",
                                  "００４２０７７７０００１１１"])
async def test_no_call_without_a_home_number_on_the_allow_list(make_adapter, monkeypatch, home):
    if home is None:
        monkeypatch.delenv("PHONE_HOME_NUMBER")
    else:
        monkeypatch.setenv("PHONE_HOME_NUMBER", home)
    adapter = make_adapter()
    assert await adapter.connect()

    result = await deliver(adapter, MESSAGE)

    assert not result.success and "PHONE_HOME_NUMBER" in result.error
    assert list(adapter.outgoing_dir.iterdir()) == []
    await adapter.disconnect()


@pytest.mark.parametrize("metadata", [None, {}, {"notify": True}, {"thread_id": "1"}])
async def test_gateway_notices_never_ring_the_phone(phone_adapter, metadata):
    result = await phone_adapter.send(str(uuid.uuid4()), "⚡ Stopped. You can continue this session.",
                                      metadata=metadata)
    await asyncio.sleep(0.3)

    assert result.success
    assert list(phone_adapter.outgoing_dir.iterdir()) == []


@pytest.mark.parametrize("caller_id, shown", [("00420212345678", "00420212345678"), (None, None),
                                               ("+420 212 345 678", None)])
async def test_call_shows_the_line_caller_id_from_the_settings(make_adapter, monkeypatch, caller_id, shown):
    if caller_id is not None:
        monkeypatch.setenv("SIP_CALLER_ID", caller_id)
    adapter = make_adapter()
    assert await adapter.connect()
    spool = Spool(adapter.outgoing_dir)

    await deliver(adapter, MESSAGE)

    assert (await spool.next_request()).vars.get("CALLER_ID") == shown  # not digits: the provider's default
    spool.stop()
    await adapter.disconnect()


async def test_only_one_call_rings_at_a_time(phone_adapter, spool):
    await deliver(phone_adapter, MESSAGE)
    await spool.next_request()

    assert (await deliver(phone_adapter, "A ještě jedna věc.")).success
    await asyncio.sleep(0.5)
    assert spool.requests.empty()


async def test_unanswered_call_ends_and_the_next_delivery_rings_again(phone_adapter, spool):
    await deliver(phone_adapter, MESSAGE)
    spool.ring_out(await spool.next_request())
    await asyncio.sleep(1)  # the plugin notices that Asterisk gave up

    await deliver(phone_adapter, "Druhá připomínka.")
    assert (await spool.next_request()).number == HOME_NUMBER


async def test_call_storm_is_capped(phone_adapter, spool):
    for _ in range(6):
        assert (await deliver(phone_adapter, MESSAGE)).success
        spool.ring_out(await spool.next_request())
        await asyncio.sleep(0.7)

    result = await deliver(phone_adapter, MESSAGE)

    assert not result.success and "calls" in result.error
    assert list(phone_adapter.outgoing_dir.iterdir()) == []


async def test_request_asterisk_does_not_take_is_withdrawn(phone_adapter, monkeypatch):
    monkeypatch.setattr(sys.modules[type(phone_adapter).__module__], "TAKE_TIMEOUT_S", 0.5)
    spool = Spool(phone_adapter.outgoing_dir)
    spool.running = False  # Asterisk is not running

    results = await asyncio.gather(deliver(phone_adapter, MESSAGE), deliver(phone_adapter, "Druhá zpráva."))
    spool.running = True  # a request left behind would ring when Asterisk starts
    await asyncio.sleep(0.3)

    assert [r.success for r in results] == [False, False]  # neither message reached anyone
    assert "Asterisk" in results[0].error
    assert spool.requests.empty() and list(phone_adapter.outgoing_dir.iterdir()) == []
    spool.stop()


async def test_answered_call_asks_for_the_pin_then_says_the_message(phone_adapter, spool):
    events = recording_handler(phone_adapter)
    phone = await answered_call(phone_adapter, spool, MESSAGE)
    before_pin = phone.speech_frames()

    await enter_pin(phone, MESSAGE)

    assert before_pin <= 1.25 * reference_speech_frames(SHORT_PHRASES["pin"])  # someone else may have answered
    assert heard_about(phone.speech_frames(), MESSAGE)
    assert events == []
    await phone.hang_up()


async def test_the_next_question_on_the_call_knows_the_message(phone_adapter, spool):
    from gateway.run_inbound import GatewayInboundMixin
    events = recording_handler(phone_adapter)
    phone = await answered_call(phone_adapter, spool, MESSAGE)
    await enter_pin(phone, MESSAGE)

    await phone_adapter.submit_utterance(str(phone.call_id), "Kde přesně?")
    await until(lambda: events and not phone_adapter._busy(phone_adapter._calls[str(phone.call_id)]))
    await phone_adapter.submit_utterance(str(phone.call_id), "A kdo tam bude?")
    await until(lambda: len(events) == 2)

    question, follow_up = events
    assert question.source.user_id == CALLER
    prompt = GatewayInboundMixin._prepend_inbound_reply_context(question, question.source, question.text)
    assert prompt == f'[Replying to your previous message: "{MESSAGE}"]\n\nKde přesně?'  # what the model reads
    assert follow_up.reply_to_text is None  # by then the message is in the call's history
    await phone.hang_up()


async def test_messages_delivered_while_it_rings_are_all_said_after_the_pin(phone_adapter, spool):
    second = "A pak zavolej mámě."
    events = recording_handler(phone_adapter)
    phone = await answered_call(phone_adapter, spool, MESSAGE, second)
    await deliver(phone_adapter, "Kup mléko.")  # still at the PIN: joins the call too

    await enter_pin(phone, MESSAGE, second, "Kup mléko.")
    await phone_adapter.submit_utterance(str(phone.call_id), "Dobře.")
    await until(lambda: events)

    assert spool.requests.empty()
    assert heard_about(phone.speech_frames(), f"{MESSAGE} {second} Kup mléko.")
    assert events[0].reply_to_text == f"{MESSAGE}\n\n{second}\n\nKup mléko."
    await phone.hang_up()


async def test_no_pin_typed_hangs_up_after_one_wait(phone_adapter, spool, monkeypatch):
    monkeypatch.setattr(sys.modules[type(phone_adapter).__module__], "PIN_TIMEOUT_S", 1.5)
    events = recording_handler(phone_adapter)
    phone = await answered_call(phone_adapter, spool, MESSAGE)  # e.g. voicemail

    await asyncio.wait_for(phone.hung_up.wait(), timeout=4)
    assert events == []
    await deliver(phone_adapter, MESSAGE)  # the call is over: the next delivery rings again
    assert (await spool.next_request()).number == HOME_NUMBER


async def test_wrong_pin_three_times_hangs_up_without_the_message(phone_adapter, spool):
    events = recording_handler(phone_adapter)
    phone = await answered_call(phone_adapter, spool, LONG_MESSAGE)
    for _ in range(3):
        await phone.press("9999#")

    await asyncio.wait_for(phone.hung_up.wait(), timeout=10)
    assert phone.speech_frames() < 0.5 * reference_speech_frames(LONG_MESSAGE)  # prompts only
    assert events == []


async def test_delivery_during_a_live_call_is_said_once_hermes_is_done(phone_adapter, spool):
    phone = await logged_in_phone(phone_adapter)  # the user called in
    events, done = [], asyncio.Event()

    async def slow_turn(event):
        events.append(event)
        await done.wait()

    phone_adapter.set_message_handler(slow_turn)
    await phone_adapter.submit_utterance(str(phone.call_id), "Co mám dnes v plánu?")
    await until(lambda: events)
    assert (await deliver(phone_adapter, MESSAGE)).success
    await asyncio.sleep(1)
    assert phone.speech_frames() == 0  # not over Hermes's turn

    done.set()
    await hear(phone, MESSAGE)
    await phone_adapter.submit_utterance(str(phone.call_id), "Díky.")
    await until(lambda: len(events) == 2)

    assert spool.requests.empty()  # no second call
    assert heard_about(phone.speech_frames(), MESSAGE)
    assert events[1].reply_to_text == MESSAGE
    await phone.hang_up()


def slow_synthesis(monkeypatch, adapter, seconds: float):
    """Hermes takes ``seconds`` to prepare the spoken message (MESSAGE)."""
    module = sys.modules[type(adapter).__module__]
    voice = module._tts()

    class Slow:
        stream_sample_rate = voice.stream_sample_rate

        def stream(self, text, **kwargs):
            if "porada" in text:
                time.sleep(seconds)
            return voice.stream(text, **kwargs)

    monkeypatch.setattr(module, "_tts", Slow)


async def test_message_waits_while_the_caller_question_is_understood(phone_adapter, spool, monkeypatch):
    stt = FakeSTT(monkeypatch, "Co mám dnes v plánu?", "Díky.", delay=1.5)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("Co mám dnes v plánu?")
    await until(lambda: stt.paths)  # being recognised
    await deliver(phone_adapter, MESSAGE)
    await until(lambda: events)
    await hear(phone, MESSAGE)
    await phone.say("Díky.")
    await until(lambda: len(events) == 2)

    assert events[0].reply_to_text is None  # asked before the message was said
    assert events[1].reply_to_text == MESSAGE
    await phone.hang_up()


async def test_caller_who_starts_talking_first_hears_the_message_afterwards(phone_adapter, spool, monkeypatch):
    FakeSTT(monkeypatch, "Jaké bude počasí?")
    events = []

    async def handle(event):
        events.append((time.monotonic(), event))

    phone_adapter.set_message_handler(handle)
    phone = await logged_in_phone(phone_adapter)
    slow_synthesis(monkeypatch, phone_adapter, 1.0)

    await deliver(phone_adapter, MESSAGE)
    await asyncio.sleep(0.3)  # the message is still being prepared
    talking = time.monotonic()
    await phone.say("Jaké bude počasí?", realtime=True)
    await until(lambda: events)
    asked = events[0][0]
    await hear(phone, MESSAGE, since=asked)

    assert phone.speech_frames(talking, asked) <= TONES  # not said over the caller
    assert heard_about(phone.speech_frames(asked), MESSAGE)  # but afterwards
    await phone.hang_up()


async def test_talking_over_a_delivered_message_silences_it(phone_adapter, spool, monkeypatch):
    FakeSTT(monkeypatch, "Počkej, tohle už vím.")
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await deliver(phone_adapter, LONG_MESSAGE)
    await phone.wait_for_speech(timeout=10, run=20)
    talking = time.monotonic()
    await phone.say("Počkej, tohle už vím.")
    await until(lambda: events)

    assert phone.speech_frames(talking + 0.5) <= TONES
    assert events[0].reply_to_text == LONG_MESSAGE  # what was heard of it still counts
    await phone.hang_up()


async def test_star_key_drops_the_messages_still_waiting(phone_adapter, spool):
    phone = await logged_in_phone(phone_adapter)

    await deliver(phone_adapter, LONG_MESSAGE)
    await deliver(phone_adapter, LONG_MESSAGE)
    await phone.wait_for_speech(timeout=10, run=20)
    pressed = time.monotonic()
    await phone.press("*")
    await asyncio.sleep(4)

    assert phone.speech_frames(pressed + 0.3) < 100  # stop tone and "Stop." only
    await phone.hang_up()


async def test_call_request_is_private(phone_adapter, spool):
    await deliver(phone_adapter, MESSAGE)
    request = await spool.next_request()

    assert oct(request.path.stat().st_mode & 0o777) == "0o600"


# --- a message the user missed is said on their next call

HELLO, MISSED = SHORT_PHRASES["hello"], SHORT_PHRASES["missed"]


async def call_in(adapter, caller: str = CALLER, expect: str = HELLO):
    """The user calls Hermes and types the PIN; returns once ``expect`` was said after the PIN."""
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir, caller=caller)
    await phone.wait_for_silence()
    await enter_pin(phone, expect)
    return phone


async def miss_call(adapter, spool, monkeypatch, how: str, message: str = MESSAGE) -> None:
    await deliver(adapter, message)
    request = await spool.next_request()
    if how == "not answered":
        spool.ring_out(request)
        await asyncio.sleep(1)  # the plugin notices that Asterisk gave up
    else:  # answered by voicemail or someone else: nobody types the PIN
        module = sys.modules[type(adapter).__module__]
        pin_timeout = module.PIN_TIMEOUT_S
        monkeypatch.setattr(module, "PIN_TIMEOUT_S", 1.5)
        phone = await spool.answer(request, adapter.port)
        await asyncio.wait_for(phone.hung_up.wait(), timeout=5)
        monkeypatch.setattr(module, "PIN_TIMEOUT_S", pin_timeout)  # for the user's own call back


@pytest.mark.parametrize("how", ["not answered", "no PIN"])
async def test_missed_message_is_said_once_when_the_user_calls(phone_adapter, spool, monkeypatch, how):
    events = recording_handler(phone_adapter)
    await miss_call(phone_adapter, spool, monkeypatch, how)

    phone = await call_in(phone_adapter, expect=f"{HELLO} {MISSED} {MESSAGE}")
    assert heard_about(phone.speech_frames(), f"{HELLO} {MISSED} {MESSAGE}")
    await phone_adapter.submit_utterance(str(phone.call_id), "Kde přesně?")
    await until(lambda: events)
    assert MESSAGE in events[0].reply_to_text  # the caller can ask about it
    await phone.hang_up()

    again = await call_in(phone_adapter)
    assert heard_about(again.speech_frames(), HELLO)  # said once, not on every call
    await again.hang_up()


@pytest.mark.parametrize("when", ["after the missed call", "while it rings"])
async def test_missed_message_survives_a_gateway_restart(make_adapter, monkeypatch, when):
    adapter = make_adapter()
    assert await adapter.connect()
    spool = Spool(adapter.outgoing_dir)
    if when == "while it rings":
        await deliver(adapter, MESSAGE)
        await spool.next_request()
    else:
        await miss_call(adapter, spool, monkeypatch, "not answered")
    await adapter.disconnect()
    spool.stop()

    restarted = make_adapter()
    assert await restarted.connect()
    phone = await call_in(restarted, expect=f"{HELLO} {MISSED} {MESSAGE}")
    assert heard_about(phone.speech_frames(), f"{HELLO} {MISSED} {MESSAGE}")
    await phone.hang_up()
    await restarted.disconnect()


async def test_missed_message_is_only_for_the_home_number(phone_adapter, spool, monkeypatch):
    await miss_call(phone_adapter, spool, monkeypatch, "not answered")

    other = await call_in(phone_adapter, caller="420777000222")
    assert heard_about(other.speech_frames(), HELLO)
    await other.hang_up()
    phone = await call_in(phone_adapter, expect=f"{HELLO} {MISSED} {MESSAGE}")
    await phone.hang_up()


async def test_missed_message_is_said_on_the_next_call_hermes_places(phone_adapter, spool, monkeypatch):
    await miss_call(phone_adapter, spool, monkeypatch, "not answered")
    second = "A pak zavolej mámě."

    phone = await answered_call(phone_adapter, spool, second)
    await enter_pin(phone, MISSED, MESSAGE, second)

    assert heard_about(phone.speech_frames(), f"{MISSED} {MESSAGE} {second}")
    await phone.hang_up()


async def test_missed_message_older_than_a_day_is_dropped(phone_adapter, spool, monkeypatch):
    monkeypatch.setattr(sys.modules[type(phone_adapter).__module__], "MISSED_KEEP_S", 0.5)
    await miss_call(phone_adapter, spool, monkeypatch, "not answered")
    await asyncio.sleep(1)

    phone = await call_in(phone_adapter)
    assert heard_about(phone.speech_frames(), HELLO)
    await phone.hang_up()


async def test_missed_messages_are_private(phone_adapter, spool, monkeypatch):
    from hermes_constants import get_hermes_home
    await miss_call(phone_adapter, spool, monkeypatch, "not answered")

    stored = [p for p in (get_hermes_home() / "phone").iterdir() if p.is_file()]
    assert stored and all(oct(p.stat().st_mode & 0o777) == "0o600" for p in stored)
