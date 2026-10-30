"""The phone is set up in the Desktop app (and the web dashboard): the plugin's card on the Messaging page holds
the PIN, the callers and the SIP account, the plugin's page under Settings → Plugins what the phone says and
how it listens. Runs Hermes's own dashboard backend."""
import asyncio
import wave
from types import SimpleNamespace

from dotenv import dotenv_values

from conftest import CALLER, PIN, ROOT, Phone, call_audio, desktop, logged_in_phone, streaming_tts, until
from test_call_access import recording_handler
from test_outbound_calls import heard_about

CARD_FIELDS = ["PHONE_PIN", "PHONE_ALLOWED_CALLERS", "SIP_SERVER", "SIP_USER", "SIP_PASSWORD",
               "PHONE_HOME_NUMBER", "SIP_CALLER_ID", "SIP_IDENTIFY_EXTRA"]
PHRASES = ["phrase_pin", "phrase_wrong_pin", "phrase_hello", "phrase_bye", "phrase_not_understood",
           "phrase_stopped", "phrase_denied", "phrase_missed"]
CALLS = ["instructions", "language"]
LISTENING = ["stt_timeout", "stt_fallback", "end_silence", "start_vad", "barge_vad", "barge_windows"]


def phone_card(platforms: dict) -> dict:
    return next(platform for platform in platforms["platforms"] if platform["id"] == "phone")


async def gateway_phone(hermes, monkeypatch):
    """The phone adapter as the gateway starts it: .env into the environment, then the enabled platforms."""
    from gateway.config import Platform, load_gateway_config
    from gateway.platform_registry import platform_registry

    for key, value in dotenv_values(hermes.home / ".env").items():
        monkeypatch.setenv(key, value)
    config = load_gateway_config()
    assert Platform("phone") in config.get_connected_platforms()
    adapter = platform_registry.create_adapter("phone", config.platforms[Platform("phone")])
    adapter.calls_dir = hermes.home / "phone" / "calls"  # made by install-asterisk.sh
    adapter.calls_dir.mkdir(parents=True, exist_ok=True)
    assert await adapter.connect()
    return adapter


def test_phone_card_asks_for_the_pin_the_callers_and_the_sip_account(hermes):
    platforms, = desktop(hermes.home, ["GET", "/api/messaging/platforms"])
    card = phone_card(platforms)
    fields = {field["key"]: field for field in card["env_vars"]}

    assert list(fields) == CARD_FIELDS
    assert [key for key, field in fields.items() if field["required"]] == CARD_FIELDS[:5]
    assert all(field["prompt"] != key and field["description"] for key, field in fields.items())
    assert [key for key, field in fields.items() if field["is_password"]] == ["PHONE_PIN", "SIP_PASSWORD"]
    assert "install-asterisk.sh" in card["description"]


async def test_enabling_the_filled_in_card_turns_the_phone_on(hermes, monkeypatch):
    hermes.configure(streaming_tts(), platforms={"phone": {"port": 0}})  # 9092 may be taken on this machine
    hermes.load()
    before, saved, after = desktop(
        hermes.home, ["GET", "/api/messaging/platforms"],
        ["PUT", "/api/messaging/platforms/phone", {"enabled": True, "env": {
            "PHONE_PIN": PIN, "PHONE_ALLOWED_CALLERS": CALLER, "SIP_SERVER": "sip.example.com", "SIP_USER": "hermes",
            "SIP_PASSWORD": "secret"}}],
        ["GET", "/api/messaging/platforms"])
    assert [phone_card(before)[key] for key in ("enabled", "configured")] == [False, False]
    assert saved["ok"]
    assert [phone_card(after)[key] for key in ("enabled", "configured")] == [True, True]

    adapter = await gateway_phone(hermes, monkeypatch)
    events = recording_handler(adapter)
    phone = await logged_in_phone(adapter)
    await adapter.submit_utterance(str(phone.call_id), "Ahoj")
    await asyncio.sleep(0.2)

    assert [event.source.user_id for event in events] == [CALLER]
    await phone.hang_up()
    await adapter.disconnect()


async def test_settings_saved_on_the_plugin_page_are_used_on_the_next_call(hermes, monkeypatch):
    greeting = "Ahoj, tady Hermes. Co potřebuješ?"
    hermes.configure(streaming_tts(), platforms={"phone": {"enabled": True, "port": 0}})
    (hermes.home / ".env").write_text(f"PHONE_PIN={PIN}\nPHONE_ALLOWED_CALLERS={CALLER}\n")
    listed, saved = desktop(
        hermes.home, ["RPC", "plugins.manage", {"action": "list"}],
        ["RPC", "plugins.manage", {"action": "settings", "key": "hermes-3310", "values": {"phrase_hello": greeting}}])
    page = next(row for row in listed["result"]["plugins"] if row["key"] == "hermes-3310")
    form = {field["key"]: field for field in page["settings_schema"]}

    assert list(form) == CALLS + PHRASES + LISTENING
    assert all(field["label"] != key for key, field in form.items())
    assert form["phrase_hello"]["value"] == "Hello, this is Hermes."
    assert (form["stt_fallback"]["type"], form["stt_fallback"]["choices"]) == ("enum", ["none", "local"])
    assert saved["result"]["written"] == ["phrase_hello"]

    hermes.load()
    adapter = await gateway_phone(hermes, monkeypatch)
    phone = await Phone().dial(adapter.port, calls_dir=adapter.calls_dir)
    await phone.wait_for_silence()
    phone.frames.clear()
    await phone.press(PIN + "#")
    await phone.wait_for_silence()

    assert heard_about(phone.speech_frames(), greeting)
    await phone.hang_up()
    await adapter.disconnect()


def test_instructions_from_the_plugin_page_are_added_to_what_hermes_is_told_on_calls(hermes):
    from agent.system_prompt import platform_hint
    from hermes_cli.plugins_settings import save_plugin_settings

    save_plugin_settings("hermes-3310", ROOT, {"instructions": "Odpovídej česky, mluveným stylem."})
    hermes.load()
    hint = platform_hint(SimpleNamespace(platform="phone", _platform_hint_overrides={}))

    assert hint.startswith("You are on a phone call.") and hint.endswith("\n\nOdpovídej česky, mluveným stylem.")


async def test_calls_are_recognised_in_the_language_set_on_the_plugin_page(make_adapter, tmp_path, monkeypatch):
    from tools.transcription_tools import transcribe_audio

    languages = []

    def stt_provider(file_path, provider, stt_config, model, language, prompt):  # where Hermes's STT sends audio
        languages.append(language)
        return {"success": True, "transcript": "Jaké bude počasí?"}

    monkeypatch.setattr("tools.transcription_tools._route_stt_provider", stt_provider)
    adapter = make_adapter(language="cs")
    assert await adapter.connect()
    events = recording_handler(adapter)
    phone = await logged_in_phone(adapter)
    await phone.say("Jaké bude zítra počasí?")
    await until(lambda: events)
    with wave.open(str(tmp_path / "note.wav"), "wb") as note:  # e.g. a voice note on another platform
        note.setnchannels(1), note.setsampwidth(2), note.setframerate(8000), note.writeframes(call_audio("Ahoj."))
    transcribe_audio(str(tmp_path / "note.wav"), source="gateway")

    assert languages == ["cs", None]
    await phone.hang_up()
    await adapter.disconnect()
