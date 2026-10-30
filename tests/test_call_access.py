"""Round 2: only allowed callers who enter the PIN reach Hermes; nothing is sent to Hermes before that."""
import asyncio
import sys

import pytest

from conftest import CALLER, PIN, Phone, logged_in_phone


def recording_handler(adapter):
    events = []

    async def handle(event):
        events.append(event)
        return None

    adapter.set_message_handler(handle)
    return events


async def test_allowed_caller_with_the_right_pin_talks_to_hermes_as_that_caller(phone_adapter):
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone_adapter.submit_utterance(str(phone.call_id), "Jaké bude počasí?")
    await asyncio.sleep(0.2)

    assert [(e.text, e.source.user_id) for e in events] == [("Jaké bude počasí?", CALLER)]
    await phone.hang_up()


async def test_pin_prompt_and_greeting_are_spoken(phone_adapter):
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir)
    await phone.wait_for_silence()
    prompt = phone.speech_frames()
    await phone.press(PIN + "#")
    await phone.wait_for_silence()

    assert prompt > 0 and phone.speech_frames() > prompt
    await phone.hang_up()


async def test_wrong_pin_three_times_hangs_up_and_nothing_reaches_hermes(phone_adapter):
    events = recording_handler(phone_adapter)
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir)
    for attempt in range(3):
        await phone.press("9999#")
        await phone_adapter.submit_utterance(str(phone.call_id), "speech before the PIN")

    await asyncio.wait_for(phone.hung_up.wait(), timeout=20)
    assert events == []


async def test_wrong_pin_then_right_pin_is_accepted(phone_adapter):
    events = recording_handler(phone_adapter)
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir)
    await phone.press("1111#")
    await phone.press(PIN + "#")
    await asyncio.sleep(1.5)

    await phone_adapter.submit_utterance(str(phone.call_id), "Ahoj")
    await asyncio.sleep(0.2)
    assert len(events) == 1 and not phone.hung_up.is_set()
    await phone.hang_up()


@pytest.mark.parametrize("caller", ["", "420777999999", "anonymous", "421777000111", "77000111", "1"])
async def test_caller_not_on_the_allow_list_is_hung_up_without_a_pin_prompt(phone_adapter, caller):
    events = recording_handler(phone_adapter)
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir, caller=caller)
    await phone.press(PIN + "#")
    await phone_adapter.submit_utterance(str(phone.call_id), "Ahoj")

    await asyncio.wait_for(phone.hung_up.wait(), timeout=10)
    assert events == []


# Odorik shows Czech callers without the country code (777000111); the allow-list has it (420777000111).
@pytest.mark.parametrize("shown_as", ["+420777000111", "00420777000111", "420 777 000 111", "777000111"])
async def test_caller_number_formats_of_the_provider_match_the_allow_list(phone_adapter, shown_as):
    events = recording_handler(phone_adapter)
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir, caller=shown_as)
    await phone.press(PIN + "#")
    await asyncio.sleep(1.5)

    await phone_adapter.submit_utterance(str(phone.call_id), "Ahoj")
    await asyncio.sleep(0.2)
    assert [e.source.user_id for e in events] == [CALLER]
    await phone.hang_up()


async def test_caller_number_file_is_removed_after_the_call_starts(phone_adapter):
    phone = await logged_in_phone(phone_adapter)

    assert not (phone_adapter.calls_dir / str(phone.call_id)).exists()
    await phone.hang_up()


@pytest.mark.parametrize("missing", ["PHONE_PIN", "PHONE_ALLOWED_CALLERS"])
async def test_refuses_to_start_without_pin_or_allow_list(make_adapter, monkeypatch, missing):
    monkeypatch.delenv(missing)
    adapter = make_adapter()

    assert not await adapter.connect()
    with pytest.raises(OSError):  # nothing listens
        await asyncio.open_connection("127.0.0.1", adapter.port or 1)


async def test_stopping_the_adapter_ends_calls_still_at_the_pin(make_adapter):
    adapter = make_adapter()
    assert await adapter.connect()
    events = recording_handler(adapter)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)

    await adapter.disconnect()
    await asyncio.wait_for(phone.hung_up.wait(), timeout=5)
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    assert events == []


@pytest.mark.parametrize("pin", ["123", "12a4", "12 34", "１２３４", "1" * 33])  # fullwidth: cannot be typed
async def test_refuses_to_start_with_a_short_or_non_numeric_pin(make_adapter, monkeypatch, pin):
    monkeypatch.setenv("PHONE_PIN", pin)
    adapter = make_adapter()

    assert not await adapter.connect()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
async def test_refuses_to_listen_beyond_this_machine(make_adapter, host):
    adapter = make_adapter(platform={"host": host})  # AudioSocket has no authentication

    assert not await adapter.connect()


async def test_many_wrong_pins_lock_the_line_for_everyone(make_adapter, monkeypatch):
    adapter = make_adapter()
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "MAX_WRONG_PINS", 4)
    assert await adapter.connect()
    events = recording_handler(adapter)
    for _ in range(2):  # 6 wrong PINs over two calls
        phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
        for _ in range(3):
            await phone.press("9999#")
        await asyncio.wait_for(phone.hung_up.wait(), timeout=15)

    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await asyncio.wait_for(phone.hung_up.wait(), timeout=10)  # hung up before the PIN could be typed
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    assert events == []
    await adapter.disconnect()


async def test_the_wrong_pin_limit_also_ends_calls_already_at_the_pin(make_adapter, monkeypatch):
    adapter = make_adapter()
    monkeypatch.setattr(sys.modules[type(adapter).__module__], "MAX_WRONG_PINS", 2)
    assert await adapter.connect()
    events = recording_handler(adapter)
    guesser = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    waiting = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)  # at the PIN prompt meanwhile

    await guesser.press("9999#")
    await guesser.press("8888#")  # the limit is reached
    await waiting.press(PIN + "#")

    await asyncio.wait_for(waiting.hung_up.wait(), timeout=10)
    await adapter.submit_utterance(str(waiting.call_id), "Ahoj")
    assert events == []
    await adapter.disconnect()
