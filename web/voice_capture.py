"""
Local speech-to-text for the desktop widget's "Hey Jarvis" conversations.

The browser's own speech recognition (used by the dashboard's manual mic
button, which works fine in a normal browser tab) needs a Google cloud
service that isn't reachable from inside an embedded WebView2 window - so
voice captured after the wake word is transcribed locally instead, with
faster-whisper. Multilingual (auto-detects English/Portuguese per
utterance), no account, nothing sent anywhere.
"""

import numpy as np
import pyaudio
from faster_whisper import WhisperModel

from audio_utils import pick_channels

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280
MAX_SECONDS = 15          # give up listening after this long regardless
GRACE_SECONDS = 3.0       # give up quickly if she never actually starts talking
SILENCE_SECONDS = 1.2     # how much quiet after speech means "she's done"
SILENCE_THRESHOLD = 25    # mean amplitude below this counts as silence (was 120) -
# measured on the laptop mic without a headset: noise floor ~0.2, normal speech
# averages ~68, so 25 sits between the two

_model = None


def _get_model():
    global _model
    if _model is None:
        # "small" multilingual, int8 quantized: light enough for a laptop
        # CPU, good enough for short voice commands (not long dictation).
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model


def warm_up():
    """Loads the model now, so the first real question isn't the one that
    waits for it. Call this once at startup, off the main thread."""
    _get_model()


def _record_utterance():
    """
    Records from the mic until ~1.2s of silence after speech, MAX_SECONDS
    total, or GRACE_SECONDS with no speech detected at all (she said
    nothing - no point waiting the full window). Returns (pcm_bytes, had_speech).
    """
    mic = pyaudio.PyAudio()
    channels = pick_channels(mic)
    stream = mic.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    frames = []  # mono int16 bytes, after downmixing each chunk if needed
    speaking = False
    silence_chunks = 0
    silence_limit = int(SILENCE_SECONDS * SAMPLE_RATE / CHUNK_SIZE)
    grace_chunks = int(GRACE_SECONDS * SAMPLE_RATE / CHUNK_SIZE)
    max_chunks = int(MAX_SECONDS * SAMPLE_RATE / CHUNK_SIZE)

    try:
        for i in range(max_chunks):
            raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            if channels > 1:
                mono = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels).mean(axis=1).astype(np.int16)
            else:
                mono = np.frombuffer(raw, dtype=np.int16)
            frames.append(mono.tobytes())
            # Widen to int32 before abs() - int16's range is -32768..32767,
            # so abs(-32768) overflows back to -32768 in int16, silently
            # undercounting the loudest (clipped) samples as "quiet".
            volume = np.abs(mono.astype(np.int32)).mean()
            if volume > SILENCE_THRESHOLD:
                speaking = True
                silence_chunks = 0
            elif speaking:
                silence_chunks += 1
                if silence_chunks > silence_limit:
                    break
            elif i > grace_chunks:
                break  # never actually started talking - stop waiting
    finally:
        stream.stop_stream()
        stream.close()
        mic.terminate()

    return b"".join(frames), speaking


def transcribe():
    """
    Records one utterance and transcribes it locally.
    Returns (text, language) - text is "" if she didn't actually say anything
    (silence is never sent to the model at all, so it can't hallucinate
    words out of background noise).
    """
    raw, had_speech = _record_utterance()
    if not had_speech:
        return "", None

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    model = _get_model()
    segments, info = model.transcribe(audio, language=None)  # auto-detect language
    text = " ".join(segment.text for segment in segments).strip()
    return text, info.language
