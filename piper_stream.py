"""``piper_stream``: Piper with raw PCM output, so Hermes speaks replies sentence by sentence.

Uses the ``tts.piper`` settings and Hermes's own voice loader and cache. A ``tts.piper_stream``
section overrides any ``tts.piper`` key for streamed speech only (e.g. a faster phone voice).
Config is read on every call, so changes apply from the next sentence.
"""
import json
import re
import threading
import wave
from pathlib import Path

from agent.tts_provider import TTSProvider

# Hermes's text cleaner (tools/tts_text_normalize.py) writes units, currencies and symbols as
# English words before any provider sees the text; a Czech voice gets them back in Czech.
_CZECH_WORDS = tuple((re.compile(pattern), czech) for pattern, czech in (
    (r"(\d) to (-?\d)", r"\1 až \2"),  # temperature ranges: 11-17 °C
    (r"\bdegrees Celsius\b", "stupňů Celsia"), (r"\bdegrees Fahrenheit\b", "stupňů Fahrenheita"),
    (r"\bdegrees\b", "stupňů"), (r"\bkilometres per hour\b", "kilometrů za hodinu"),
    (r"\bmillimetres\b", "milimetrů"), (r"\bcentimetres\b", "centimetrů"), (r"\bmetres\b", "metrů"),
    (r"\bpercent\b", "procent"), (r"\bNew Zealand dollars\b", "novozélandských dolarů"),
    (r"\bAustralian dollars\b", "australských dolarů"), (r"\bUS dollars\b", "amerických dolarů"),
    (r"\bdollars\b", "dolarů"), (r"\beuros\b", "eur"), (r"\bpounds\b", "liber"),
    (r"\bper\b", "za"), (r"\band\b", "a"), (r"\babout\b", "asi"),
))


def czech_spoken(text: str) -> str:
    for pattern, czech in _CZECH_WORDS:
        text = pattern.sub(czech, text)
    return text


def _synthesis_config(piper: dict):
    from piper import SynthesisConfig
    speaker_id = piper.get("speaker_id", 0)
    return SynthesisConfig(
        length_scale=float(piper.get("length_scale", 1.0)), noise_scale=float(piper.get("noise_scale", 0.667)),
        noise_w_scale=float(piper.get("noise_w_scale", 0.8)), volume=float(piper.get("volume", 1.0)),
        normalize_audio=bool(piper.get("normalize_audio", True)),
        speaker_id=speaker_id if type(speaker_id) is int else 0)


class PiperStream(TTSProvider):
    streams_pcm = True

    def __init__(self):
        self._lock = threading.Lock()  # espeak phonemization is not safe to run concurrently

    @property
    def name(self) -> str:
        return "piper_stream"

    def is_available(self) -> bool:
        return self._voice_metadata() is not None

    @property
    def stream_sample_rate(self):
        meta = self._voice_metadata()
        return meta["audio"]["sample_rate"] if meta else None

    def stream(self, text, *, voice=None, model=None, format="pcm", speed=None, **extra):
        if format != "pcm":
            raise NotImplementedError("piper_stream streams raw PCM only")
        piper_voice, piper = self._load()
        with self._lock:
            chunks = list(piper_voice.synthesize(self._localized(text), syn_config=_synthesis_config(piper)))
        for chunk in chunks:
            yield chunk.audio_int16_bytes

    def synthesize(self, text, output_path, *, voice=None, model=None, speed=None, format="wav", **extra):
        piper_voice, piper = self._load()
        wav_path = str(Path(output_path).with_suffix(".wav"))
        with self._lock, wave.open(wav_path, "wb") as wav_file:
            piper_voice.synthesize_wav(self._localized(text), wav_file, syn_config=_synthesis_config(piper))
        return wav_path

    def _localized(self, text: str) -> str:
        meta = self._voice_metadata() or {}
        return czech_spoken(text) if (meta.get("language") or {}).get("family") == "cs" else text

    def warm(self) -> None:
        """Load the voice, downloading it on first use, so the first reply is not delayed."""
        self._load()

    @staticmethod
    def _config() -> dict:
        from tools.tts_tool import _load_tts_config
        tts = _load_tts_config()
        return {**tts, "piper": {**(tts.get("piper") or {}), **(tts.get("piper_stream") or {})}}

    def _load(self):
        from tools.tts_tool_local import _load_piper_voice_for_config
        return _load_piper_voice_for_config(self._config())

    def _voice_metadata(self):
        """The voice's parsed ``.onnx.json`` if the voice is on disk (never downloads)."""
        from tools.tts_tool_local import DEFAULT_PIPER_VOICE, _get_piper_voices_dir
        piper = self._config()["piper"]
        name = piper.get("voice") or DEFAULT_PIPER_VOICE
        onnx = Path(name).expanduser()
        if onnx.suffix.lower() != ".onnx":
            onnx = Path(piper.get("voices_dir") or _get_piper_voices_dir()).expanduser() / f"{name}.onnx"
        meta = Path(f"{onnx}.json")
        return json.loads(meta.read_text()) if meta.exists() else None
