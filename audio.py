"""Call audio: Asterisk AudioSocket carries 8 kHz 16-bit mono PCM in 20 ms frames."""
import re
from pathlib import Path
from typing import Optional

import numpy as np

SR = 8000
FRAME_BYTES = 320  # 20 ms
VAD_MODEL = Path(__file__).with_name("assets") / "silero_vad.onnx"  # Silero VAD v6, MIT licence


def _lowpass_kernel(cutoff: float, taps: int = 101) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff * n) * np.hamming(taps)
    return h / h.sum()


def to_call_rate(pcm: bytes, rate: int) -> bytes:
    """int16 mono PCM at ``rate`` -> 8 kHz, with a windowed-sinc anti-alias filter."""
    x = np.frombuffer(pcm[:len(pcm) // 2 * 2], "<i2").astype(np.float32) / 32768
    if rate != SR and len(x):
        if rate > SR:
            x = np.convolve(x, _lowpass_kernel(0.45 * SR / rate), mode="same")
        t = np.linspace(0, len(x) - 1, int(round(len(x) * SR / rate)))
        x = np.interp(t, np.arange(len(x)), x)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def _tone(freq: float, ms: int, volume: float = 0.12) -> bytes:
    t = np.arange(int(SR * ms / 1000)) / SR
    envelope = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)  # 10 ms fades, no clicks
    return (volume * envelope * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2").tobytes()


def _pause(ms: int) -> bytes:
    return b"\x00" * int(SR * ms / 1000) * 2


HEARD_TICK = _tone(1200, 35, 0.10)  # the caller's speech was caught
THINK_BEEP = _tone(660, 90) + _pause(80) + _tone(880, 90)  # Hermes is working
READY_BEEP = _tone(880, 120)  # Hermes finished; the caller's turn
STOP_TONE = _tone(660, 110) + _pause(30) + _tone(440, 160)  # * pressed


class SpeechDetector:
    """Silero VAD over call audio; cuts the caller's speech into utterances.

    Silero works on 32 ms windows at 16 kHz, so the 8 kHz call audio is upsampled by linear
    interpolation (it scored quiet car-call speech above the start threshold where sample
    doubling did not). An utterance keeps ``preroll_s`` of audio from before speech was
    detected, because quiet sentence starts trigger the VAD late. While Hermes is speaking
    (``strict``), speech must be clearer and longer to count, so line echo and noise do not cut
    the reply off.
    """

    END_THRESHOLD, START_WINDOWS = 0.35, 3
    MIN_S, MAX_S = 0.5, 45

    def __init__(self, start_threshold: float = 0.5, end_silence_s: float = 1.2, preroll_s: float = 0.8,
                 strict_threshold: float = 0.5, strict_windows: int = 5):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._session = ort.InferenceSession(str(VAD_MODEL), providers=["CPUExecutionProvider"], sess_options=opts)
        self._state = np.zeros((1, 1, 128), np.float32), np.zeros((1, 1, 128), np.float32)
        self._context, self._pending, self._last = np.zeros(64, np.float32), np.zeros(0, np.float32), 0.0
        self.start_threshold, self.strict_threshold, self.strict_windows = start_threshold, strict_threshold, strict_windows
        self._end_windows = int(end_silence_s / 0.032)
        self._preroll = int(SR * 2 * preroll_s)
        self._lead_in, self._speech = bytearray(), bytearray()
        self.in_speech, self._loud, self._quiet = False, 0, 0

    def _probabilities(self, pcm: bytes) -> list[float]:
        x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        x16 = np.empty(len(x) * 2, np.float32)
        x16[1::2] = x
        x16[0::2] = (np.concatenate([[self._last], x[:-1]]) + x) / 2
        self._last = float(x[-1]) if len(x) else self._last
        self._pending = np.concatenate([self._pending, x16])
        probabilities = []
        while len(self._pending) >= 512:
            window, self._pending = self._pending[:512], self._pending[512:]
            out, h, c = self._session.run(None, {"input": np.concatenate([self._context, window])[None, :],
                                                 "h": self._state[0], "c": self._state[1]})
            self._state, self._context = (h, c), window[-64:]
            probabilities.append(float(np.ravel(out)[0]))
        return probabilities

    def drop(self) -> None:
        """Forget speech in progress."""
        self.in_speech, self._loud, self._speech = False, 0, bytearray()

    def feed(self, pcm: bytes, strict: bool = False) -> Optional[bytes]:
        """Feed caller audio; returns a finished utterance when the caller stopped speaking."""
        threshold, windows = ((self.strict_threshold, self.strict_windows) if strict
                              else (self.start_threshold, self.START_WINDOWS))
        for p in self._probabilities(pcm):
            if not self.in_speech:
                self._loud = self._loud + 1 if p > threshold else 0
                if self._loud >= windows:
                    self.in_speech, self._quiet, self._speech = True, 0, bytearray(self._lead_in)
            else:
                self._quiet = self._quiet + 1 if p < self.END_THRESHOLD else 0
        if not self.in_speech:
            self._lead_in = (self._lead_in + pcm)[-self._preroll:]
            return None
        self._speech += pcm
        ended = self._quiet >= self._end_windows
        if not ended and len(self._speech) < SR * 2 * self.MAX_S:
            return None
        utterance, self._speech = bytes(self._speech), bytearray()
        self.in_speech, self._loud = False, 0
        return utterance if len(utterance) >= SR * 2 * self.MIN_S else None


def is_garbage(text: str) -> bool:
    """No words at all, or the same word over and over (recognisers loop on noise)."""
    words = re.findall(r"\w+", text.lower())
    if not words:
        return True
    return len(words) >= 3 and max(words.count(w) for w in set(words)) / len(words) > 0.5
