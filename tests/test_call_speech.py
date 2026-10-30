"""Round 1: Hermes's reply is spoken into the call sentence by sentence, and exactly once."""
import asyncio

import numpy as np

from conftest import gateway_turn, logged_in_phone, speech_frames_of, streaming_tts

FIRST = "Dobrý den, tady je první věta odpovědi. "
SECOND = "A tohle je druhá věta, která přijde až po chvíli."


def reference_speech_frames(text: str) -> int:
    """Speech frames of ``text`` spoken once by the plugin's Piper provider, resampled to 8 kHz."""
    from agent.tts_registry import get_provider
    provider = get_provider("piper_stream")
    x = np.frombuffer(b"".join(provider.stream(text, format="pcm")), "<i2").astype(np.float32)
    n = int(len(x) * 8000 / provider.stream_sample_rate)
    return speech_frames_of(np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2").tobytes())


async def submit(adapter, phone, text="Jaké bude počasí?"):
    """What the call does once speech is recognised (the input side arrives in round 3)."""
    await adapter.submit_utterance(str(phone.call_id), text)


async def test_reply_is_spoken_sentence_by_sentence_while_it_is_generated(phone_adapter):
    phone = await logged_in_phone(phone_adapter)

    async def wait_until_first_sentence_is_heard():
        # The fake LLM holds back the second sentence until the caller hears the first one.
        await phone.wait_for_speech(timeout=15, run=20)

    phone_adapter.set_message_handler(gateway_turn(phone_adapter, FIRST, SECOND,
                                                   between=wait_until_first_sentence_is_heard))
    await submit(phone_adapter, phone)
    await phone.wait_for_silence()

    expected = reference_speech_frames(FIRST + SECOND)
    assert 0.8 * expected <= phone.speech_frames() <= 1.25 * expected  # whole reply, not repeated
    await phone.hang_up()


async def test_audio_is_paced_in_real_time_20_ms_frames(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    phone_adapter.set_message_handler(gateway_turn(phone_adapter, FIRST))
    await submit(phone_adapter, phone)
    await phone.wait_for_silence()

    assert {len(f) for _, f in phone.frames} == {320}
    first, last = phone.frames[0][0], phone.frames[-1][0]
    assert abs((last - first) - 0.02 * (len(phone.frames) - 1)) < 0.25
    await phone.hang_up()


async def test_idle_call_gets_silence_so_asterisk_keeps_the_socket(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    await asyncio.sleep(1)

    assert len(phone.frames) >= 40 and phone.speech_frames() == 0
    await phone.hang_up()


async def test_failed_stream_is_spoken_once_by_the_fallback_without_builtin_tts(hermes, phone_adapter, monkeypatch):
    builtin_calls = []
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", lambda **kw: builtin_calls.append(kw) or "{}")
    hermes.configure({"provider": "piper", "piper": streaming_tts()["piper"]})  # no streaming provider
    phone = await logged_in_phone(phone_adapter)
    phone_adapter.set_message_handler(gateway_turn(phone_adapter, FIRST, SECOND))
    await submit(phone_adapter, phone)
    await phone.wait_for_silence()

    expected = reference_speech_frames(FIRST + SECOND)
    assert 0.8 * expected <= phone.speech_frames() <= 1.25 * expected
    assert builtin_calls == []
    await phone.hang_up()


async def test_gateway_notices_sent_with_send_are_never_spoken(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    await asyncio.sleep(0.2)

    result = await phone_adapter.send(str(phone.call_id), "⚡ Stopped. You can continue this session.")
    await asyncio.sleep(1)

    assert result.success
    assert phone.speech_frames() == 0
    await phone.hang_up()


async def test_reply_after_hang_up_goes_nowhere(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    await asyncio.sleep(0.2)
    await phone.hang_up()
    await asyncio.sleep(0.2)

    assert not phone_adapter._should_auto_tts_for_chat(str(phone.call_id))  # no streaming turn starts
    assert (await phone_adapter.send(str(phone.call_id), "late reply")).success
