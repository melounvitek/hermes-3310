"""Round 3: what the caller says reaches Hermes as text; unclear or slow recognition is handled."""
import asyncio
import time

import pytest

from conftest import PIN, FakeSTT, Phone, logged_in_phone, until


def recording_handler(adapter):
    events = []

    async def handle(event):
        events.append(event)

    adapter.set_message_handler(handle)
    return events


async def test_spoken_question_reaches_hermes_as_one_voice_message(phone_adapter, monkeypatch):
    stt = FakeSTT(monkeypatch)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("Dobrý den, jaké bude zítra v Praze počasí?")
    await until(lambda: events)

    assert [(e.text, e.message_type.value) for e in events] == [("Jaké bude zítra počasí?", "voice")]
    assert 2.0 < stt.seconds[0] < 6.0  # the whole sentence with its lead-in, not a fragment
    await phone.hang_up()


async def test_two_sentences_with_a_pause_become_two_messages(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("První otázka je tady.", then_silence=2.0)
    await phone.say("A tohle je druhá otázka.", then_silence=2.0)
    await until(lambda: len([e for e in events if e.text != "/stop"]) == 2)
    await phone.hang_up()


async def test_hanging_up_while_speech_is_recognised_sends_nothing(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, delay=1.5)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("Otázka, po které hned zavěsím.", then_silence=1.5)
    await asyncio.sleep(0.5)  # the sentence is being recognised now
    await phone.hang_up()
    await asyncio.sleep(2)

    assert events == []


async def test_nothing_is_transcribed_before_the_pin(phone_adapter, monkeypatch):
    stt = FakeSTT(monkeypatch)
    phone = await Phone().dial(phone_adapter.port, calls_dir=phone_adapter.calls_dir)
    await phone.say("Tohle říkám ještě před zadáním kódu.")
    await phone.press(PIN + "#")
    await asyncio.sleep(2)

    assert stt.seconds == []  # not before the PIN, and not queued up for after it either
    await phone.hang_up()


async def test_unclear_speech_is_not_sent_and_the_caller_is_asked_again(phone_adapter, monkeypatch):
    FakeSTT(monkeypatch, "")
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("Mumlání, kterému nikdo nerozumí.")
    await phone.wait_for_speech(timeout=10)  # "Nerozuměl jsem."

    assert events == []
    await phone.hang_up()


@pytest.mark.parametrize("transcript", ["no no no no", "..."])
async def test_repeated_words_and_noise_transcripts_are_not_sent(phone_adapter, monkeypatch, transcript):
    FakeSTT(monkeypatch, transcript)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)

    await phone.say("Šum na ulici.")
    await phone.wait_for_speech(timeout=10)

    assert events == []
    await phone.hang_up()


async def test_slow_recognition_is_cut_off_and_the_caller_is_asked_again(make_adapter, monkeypatch):
    stt = FakeSTT(monkeypatch, delay=3)
    adapter = make_adapter(stt_timeout=0.5)
    assert await adapter.connect()
    events = recording_handler(adapter)
    phone = await logged_in_phone(adapter)

    started = time.monotonic()
    await phone.say("Otázka, na kterou se čeká.", then_silence=1.5)
    await phone.wait_for_speech(timeout=10)

    assert time.monotonic() - started < 6 and events == [] and stt.local_calls == 0
    await phone.hang_up()
    await adapter.disconnect()


async def test_slow_recognition_falls_back_to_local_whisper_when_configured(make_adapter, monkeypatch):
    stt = FakeSTT(monkeypatch, delay=3)
    adapter = make_adapter(stt_timeout=0.5, stt_fallback="local")
    assert await adapter.connect()
    events = recording_handler(adapter)
    phone = await logged_in_phone(adapter)

    await phone.say("Otázka, na kterou se čeká.")
    await until(lambda: events)
    await asyncio.sleep(3)  # let the abandoned cloud attempt finish

    assert [e.text for e in events] == ["Lokální přepis."]
    assert len(set(stt.paths)) == 2 and not any(p.exists() for p in stt.paths)  # own WAV each, both removed
    await phone.hang_up()
    await adapter.disconnect()


async def test_an_internal_error_ends_the_call_instead_of_leaving_it_silent(phone_adapter, monkeypatch):
    from agent.tts_registry import get_provider
    FakeSTT(monkeypatch, "")  # unclear speech: the adapter answers with its "not understood" phrase
    phone = await logged_in_phone(phone_adapter)

    def broken(text, **kwargs):
        raise RuntimeError("voice failed")

    monkeypatch.setattr(get_provider("piper_stream"), "stream", broken)
    await phone.say("Něco, čemu nikdo nerozumí.")

    await asyncio.wait_for(phone.hung_up.wait(), timeout=10)
