"""The piper_stream TTS provider: raw PCM for Hermes's streaming path, speed from config."""
from conftest import streaming_tts


def provider():
    from agent.tts_registry import get_provider
    return get_provider("piper_stream")


def samples(text: str) -> int:
    return len(b"".join(provider().stream(text, format="pcm"))) // 2


def test_registered_as_a_pcm_streaming_provider_at_the_voice_rate(hermes):
    hermes.configure(streaming_tts())
    hermes.load()

    assert provider().streams_pcm and provider().is_available()
    assert provider().stream_sample_rate == 22050  # from the voice's .onnx.json


def test_hermes_streaming_path_resolves_it(hermes):
    from tools.tts_streaming import resolve_streaming_provider
    hermes.configure(streaming_tts())
    hermes.load()

    streamer = resolve_streaming_provider(streaming_tts())
    assert streamer is not None and streamer.sample_rate == 22050
    assert len(b"".join(streamer.stream("Dobrý den."))) > 22050 // 2


def test_phone_speed_follows_config_from_the_next_sentence_without_restart(hermes):
    hermes.configure(streaming_tts())
    hermes.load()
    text = "Zítra bude v Praze polojasno, kolem patnácti stupňů."
    fast = samples(text)

    hermes.configure({**streaming_tts(), "piper_stream": {"length_scale": 1.0}})

    assert samples(text) > 1.3 * fast
