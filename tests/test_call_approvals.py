"""Commands that need the caller's OK: the call says what Hermes wants to do, and only the keypad answers.

Approvals go through Hermes's real approval queue: a thread waits for the decision like the agent does, while
the adapter gets the prompt the way the gateway hands it over (``send_exec_approval``)."""
import asyncio
import dataclasses
import sys
import threading

import pytest

from conftest import SHORT_PHRASES, FakeSTT, deliver, logged_in_phone, until
from test_outbound_calls import MESSAGE, heard_about, recording_handler

COMMAND = "rm -rf ~/Documents/old"


class Approval:
    """A command Hermes asks the caller's OK for. ``decision`` is what the waiting agent gets:
    ``{"resolved", "choice", ...}`` from ``tools.approval_gateway_wait``."""

    def __init__(self, adapter, chat_id: str, command: str = COMMAND, **flags):
        from tools.approval_gateway_wait import _await_gateway_decision

        loop = asyncio.get_running_loop()
        call = adapter._calls.get(chat_id)
        session_key = call.session_key if call else f"agent:main:phone:dm:{chat_id}"
        self.decision = loop.create_future()

        def notify(data):  # the gateway's notifier: runs in the agent thread, renders on the loop
            sent = asyncio.run_coroutine_threadsafe(adapter.send_exec_approval(
                chat_id, data["command"], session_key, description=data["description"],
                request_id=data["request_id"], **flags), loop)
            assert sent.result(timeout=15).success

        def wait():
            decision = _await_gateway_decision(session_key, notify, {
                "command": command, "description": "recursive delete", "pattern_key": "recursive delete",
                "pattern_keys": ["recursive delete"]})
            loop.call_soon_threadsafe(self.decision.set_result, decision)

        threading.Thread(target=wait, daemon=True).start()

    async def choice(self, timeout: float = 20):
        return (await asyncio.wait_for(asyncio.shield(self.decision), timeout))["choice"]


@pytest.fixture(autouse=True)
def hermes_waits_a_minute(monkeypatch):
    """Hermes's own approvals.timeout (300 s): shorter, so a test that fails leaves no thread waiting long."""
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 60)


async def calling(make_adapter, **settings):
    adapter = make_adapter(**settings)
    assert await adapter.connect()
    return adapter, await logged_in_phone(adapter)


async def asked(adapter, phone, **flags) -> Approval:
    """An approval whose prompt the caller has heard to the end."""
    approval = Approval(adapter, str(phone.call_id), **flags)
    await phone.wait_for_silence()
    phone.frames.clear()
    return approval


async def test_key_1_allows_the_command_once(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.press("1")

    assert await approval.choice() == "once"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["approved"])
    await phone.hang_up()


async def test_key_2_refuses_it(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.press("2")

    assert await approval.choice() == "deny"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["refused"])
    await phone.hang_up()


async def test_keys_pressed_before_the_prompt_was_heard_do_not_count(make_adapter):
    adapter, phone = await calling(make_adapter, approval_timeout=2)
    await phone.press("1")  # earlier in the call
    approval = Approval(adapter, str(phone.call_id))
    await phone.wait_for_speech(timeout=10, run=10)

    await phone.press("1")  # while the prompt is said

    assert await approval.choice() == "deny"
    await phone.hang_up()
    await adapter.disconnect()


async def test_no_key_in_time_refuses_it(make_adapter):
    adapter, phone = await calling(make_adapter, approval_timeout=2)
    approval = await asked(adapter, phone)

    assert await approval.choice() == "deny"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["refused"])
    await phone.hang_up()
    await adapter.disconnect()


async def test_hanging_up_refuses_it(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.hang_up()

    assert await approval.choice(timeout=5) == "deny"


async def test_an_approval_after_the_call_ended_is_refused(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    chat_id = str(phone.call_id)
    await phone.hang_up()

    assert await Approval(phone_adapter, chat_id).choice(timeout=5) == "deny"


async def test_with_approvals_off_commands_are_refused_and_the_caller_is_told(make_adapter):
    adapter, phone = await calling(make_adapter, approvals="deny")

    approval = Approval(adapter, str(phone.call_id))

    assert await approval.choice(timeout=5) == "deny"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["denied"])
    await phone.hang_up()
    await adapter.disconnect()


async def test_approvals_are_asked_one_after_the_other_and_each_key_answers_its_own(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    first = await asked(phone_adapter, phone)
    second = Approval(phone_adapter, str(phone.call_id), command="rm -rf ~/Downloads/old")
    await asyncio.sleep(0.5)
    assert not second.decision.done()

    await phone.press("2")
    assert await first.choice() == "deny"
    await phone.wait_for_silence()  # refused, then the second prompt
    await phone.press("1")

    assert await second.choice() == "once"
    await phone.hang_up()


async def test_speech_never_answers_an_approval_and_the_keys_are_said_again(make_adapter, monkeypatch):
    FakeSTT(monkeypatch, "Ano, schvaluji to.")
    adapter, phone = await calling(make_adapter, approval_timeout=8)
    events = recording_handler(adapter)
    approval = await asked(adapter, phone)

    await phone.say("Ano, schvaluji to.")
    await phone.wait_for_silence()

    assert heard_about(phone.speech_frames(), SHORT_PHRASES["approval_keys"])
    assert not events  # nothing reached Hermes
    assert await approval.choice() == "deny"  # no key: refused
    await phone.hang_up()
    await adapter.disconnect()


async def test_what_the_caller_says_cannot_answer_an_approval_in_hermes(phone_adapter, monkeypatch):
    """Hermes takes a bare "yes" while an approval waits as its answer; a transcript must never be one."""
    from gateway.run_busy import GatewayBusySessionMixin

    FakeSTT(monkeypatch, "yes")
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)
    await phone.say("Yes.")
    await until(lambda: events)
    approval = await asked(phone_adapter, phone)
    answers = []

    class Gateway(GatewayBusySessionMixin):
        async def _handle_approve_command(self, event):
            answers.append(event.text)

        _handle_deny_command = _handle_approve_command

        def _delivery_adapter_for(self, source):
            return None

    def route(event):
        session_key = phone_adapter._calls[str(phone.call_id)].session_key
        return Gateway._route_plaintext_approval_while_busy(Gateway.__new__(Gateway), event, session_key)

    assert not await route(events[0]) and not answers
    assert await route(dataclasses.replace(events[0], allow_gateway_control=True))  # what it would do
    await phone.press("2")
    assert await approval.choice() == "deny"
    await phone.hang_up()


async def test_star_refuses_every_waiting_approval(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    first = await asked(phone_adapter, phone)
    second = Approval(phone_adapter, str(phone.call_id), command="rm -rf ~/Downloads/old")
    await asyncio.sleep(0.5)

    await phone.press("*")

    assert [await first.choice(timeout=5), await second.choice(timeout=5)] == ["deny", "deny"]
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["stopped"])
    await phone.hang_up()


async def test_the_keys_are_said_again_on_any_other_key_and_while_it_waits(phone_adapter, monkeypatch):
    monkeypatch.setattr(sys.modules[type(phone_adapter).__module__], "KEYS_AGAIN_S", 3)
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.wait_for_silence()  # said again after 3 s without a key
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["approval_keys"])
    phone.frames.clear()
    await phone.press("5")
    await phone.wait_for_silence(seconds=0.5)
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["approval_keys"])

    await phone.press("1")
    assert await approval.choice() == "once"
    await phone.hang_up()


async def test_an_approval_hermes_gave_up_on_is_left_for_the_next_one(phone_adapter, monkeypatch):
    hermes_waits = [3]
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: hermes_waits[0])
    phone = await logged_in_phone(phone_adapter)
    expired = await asked(phone_adapter, phone)

    assert await expired.choice(timeout=10) is None  # Hermes's timeout: not answered
    hermes_waits[0] = 60
    approval = await asked(phone_adapter, phone)
    await phone.press("1")

    assert await approval.choice() == "once"
    await phone.hang_up()


async def test_an_approval_the_caller_cannot_hear_is_refused(phone_adapter, monkeypatch):
    phone = await logged_in_phone(phone_adapter)

    async def no_voice(text):
        return b""

    monkeypatch.setattr(phone_adapter, "_synthesize", no_voice)

    assert await Approval(phone_adapter, str(phone.call_id)).choice(timeout=5) == "deny"
    await phone.hang_up()


async def test_a_message_waits_until_the_approval_is_answered(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await deliver(phone_adapter, MESSAGE)
    await asyncio.sleep(2)
    assert not phone.speech_frames()
    await phone.press("1")

    assert await approval.choice() == "once"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), f"{SHORT_PHRASES['approved']} {MESSAGE}")
    await phone.hang_up()
