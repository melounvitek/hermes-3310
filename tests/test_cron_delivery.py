"""Hermes's own cron delivery reaches the phone: a job delivering to ``phone``, or one created on a call,
calls the user. Runs the real scheduler delivery against the live adapter."""
import asyncio
import sys
import uuid
from types import SimpleNamespace

import yaml

from conftest import HOME_NUMBER, Spool, until
from test_outbound_calls import MESSAGE, enter_pin, heard_about, recording_handler


async def cron_delivers(hermes, adapter, job: dict, output: str):
    """What the gateway's cron ticker does with a finished job's output; returns its delivery error."""
    from cron.scheduler_delivery import _deliver_result
    from gateway.config import Platform
    config = yaml.safe_load((hermes.home / "config.yaml").read_text())
    (hermes.home / "config.yaml").write_text(yaml.safe_dump({**config, "platforms": {"phone": {"enabled": True}}}))
    return await asyncio.to_thread(_deliver_result, {"id": "a1b2c3", "name": "porada", **job}, output,
                                   adapters={Platform("phone"): adapter}, loop=asyncio.get_running_loop())


async def test_job_delivering_to_phone_calls_the_user_and_says_only_its_output(hermes, phone_adapter, spool):
    events = recording_handler(phone_adapter)

    assert await cron_delivers(hermes, phone_adapter, {"deliver": "phone"}, MESSAGE) is None
    request = await spool.next_request()
    phone = await spool.answer(request, phone_adapter.port)
    await phone.wait_for_silence()
    await enter_pin(phone, MESSAGE)
    await phone_adapter.submit_utterance(str(phone.call_id), "Kde?")
    await until(lambda: events)

    assert request.number == HOME_NUMBER
    assert heard_about(phone.speech_frames(), MESSAGE)  # not cron's English header and footer
    assert MESSAGE in events[0].reply_to_text and "a1b2c3" in events[0].reply_to_text  # Hermes can manage the job
    await phone.hang_up()


async def test_job_created_on_a_call_calls_back_after_the_hangup(hermes, phone_adapter, spool):
    origin = {"platform": "phone", "chat_id": str(uuid.uuid4()), "chat_type": "dm"}  # that call has ended

    assert await cron_delivers(hermes, phone_adapter, {"deliver": "origin", "origin": origin}, MESSAGE) is None
    assert (await spool.next_request()).number == HOME_NUMBER


async def test_call_that_could_not_be_placed_is_reported_to_cron(hermes, phone_adapter, monkeypatch):
    monkeypatch.setattr(sys.modules[type(phone_adapter).__module__], "TAKE_TIMEOUT_S", 0.3)
    # Cron runs inside the gateway, so its fallback finds the live adapter (importing gateway.run here
    # would make Hermes build a runtime for the test's home).
    gateway = SimpleNamespace(_gateway_loop=asyncio.get_running_loop())
    monkeypatch.setattr("tools.send_message_tool._live_adapter", lambda platform, **kw: (gateway, phone_adapter))
    spool = Spool(phone_adapter.outgoing_dir)
    spool.running = False  # Asterisk is down

    error = await cron_delivers(hermes, phone_adapter, {"deliver": "phone"}, MESSAGE)

    # Cron retries a failed live delivery through its fallback, which must not report a silent success.
    assert error and "Asterisk did not take the call request" in error
    spool.stop()
