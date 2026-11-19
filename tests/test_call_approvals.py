"""Commands that need the caller's OK: the call says what Hermes wants to do, and only the keypad answers.

Approvals go through Hermes's real approval queue: a thread waits for the decision like the agent does, while
the adapter gets the prompt the way the gateway hands it over (``send_exec_approval``)."""
import asyncio
import threading

import pytest

from conftest import SHORT_PHRASES, logged_in_phone
from test_outbound_calls import heard_about

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
