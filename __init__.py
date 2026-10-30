"""Hermes phone plugin: the ``phone`` platform and the ``piper_stream`` TTS provider."""
import importlib.util
from pathlib import Path

PLATFORM_HINT = (
    "You are on a phone call. The user's words come from speech recognition and may contain errors; "
    "infer the meaning. Your reply is read aloud: no markdown, lists, tables, emoji, URLs, code or "
    "symbols such as %, °C or &; write numbers and units so they read well. Before using a tool, say "
    "one short sentence about what you are going to do. Keep the answer to two or three short "
    "sentences and offer more if needed. The user can press * to stop you, and talking over you "
    "interrupts you. A job you schedule from this call reports back by calling the user and reading its "
    "answer aloud, so ask in the job's prompt for a short answer that reads well.")


def check_requirements() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "piper", "onnxruntime"))


def refuse_send(args, chat_id, platform_name, pconfig) -> dict:
    """Sends outside the gateway's live adapter cannot call: ``hermes send``, and cron's fallback after
    a failed delivery, which would otherwise reach the adapter unmarked and report a silent success."""
    return {"error": "phone: only cron jobs run by the gateway can call you"}


def register(ctx):
    # Imported here, not at module level, so this file also imports standalone (pytest, tools).
    from gateway.platforms._shared import env_is_connected

    from .adapter import PhoneAdapter
    from .piper_stream import PiperStream

    # The plugin's settings page in the Desktop (plugins.entries.hermes-3310.settings) with the defaults of
    # plugin.yaml, read when the gateway starts, like platforms.phone.
    settings = {key: ctx.get_config(key, spec.get("default")) for key, spec in ctx.manifest.config_schema.items()}
    ctx.register_platform(
        name="phone", label="Hermes 3310", adapter_factory=lambda config: PhoneAdapter(config, settings),
        check_fn=check_requirements, install_hint="pip install numpy piper-tts onnxruntime",
        platform_hint=f"{PLATFORM_HINT}\n\n{settings['instructions']}".strip(),
        required_env=["PHONE_PIN", "PHONE_ALLOWED_CALLERS"],
        # Without both the gateway leaves the phone off, and the Messaging page shows it is not set up.
        is_connected=env_is_connected("PHONE_PIN", "PHONE_ALLOWED_CALLERS"),
        # The adapter checks callers itself; the gateway re-checks the same list (defence in depth).
        allowed_users_env="PHONE_ALLOWED_CALLERS",
        # deliver=phone calls this number (it is never seeded as a home channel the gateway notifies).
        cron_deliver_env_var="PHONE_HOME_NUMBER", send_message_handler=refuse_send,
        display_tier="minimal", pii_safe=True, allow_update_command=False, emoji="📞",
        # The allow-list is required: a caller off it is never offered a pairing code.
        allowlist_suppresses_pairing=True,
        # The Hermes 3310 card on the Desktop's Messaging page (labels and help come from plugin.yaml).
        dashboard_description=(
            "Call Hermes on a normal phone line: type the PIN, then talk. Asterisk answers the line in Docker: "
            f"set it up with {Path(__file__).with_name('install-asterisk.sh')}, and run it again after changing "
            "the SIP server, user or password."),
        dashboard_env_vars=("PHONE_PIN", "PHONE_ALLOWED_CALLERS", "SIP_SERVER", "SIP_USER", "SIP_PASSWORD",
                            "PHONE_HOME_NUMBER", "SIP_CALLER_ID", "SIP_IDENTIFY_EXTRA"),
        dashboard_required_env=("PHONE_PIN", "PHONE_ALLOWED_CALLERS", "SIP_SERVER", "SIP_USER", "SIP_PASSWORD"))
    ctx.register_tts_provider(PiperStream())
    if language := settings["language"].strip():
        # Only for calls: the adapter transcribes with source="phone".
        ctx.register_hook("pre_transcription",
                          lambda source=None, **_: {"language": language} if source == "phone" else None)
