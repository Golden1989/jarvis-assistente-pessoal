"""
"Hey Jarvis" wake word detection - fully local via openWakeWord.

No account, no API key, no audio ever leaves this machine for this part:
each ~80ms chunk of microphone audio is scored against the wake phrase in
memory and discarded immediately. Only after a detection does anything else
happen (see desktop.py, which then reuses the normal voice pipeline).

pause()/resume(): once "Hey Jarvis" fires, voice_capture.py needs the same
physical microphone to record the actual question. So desktop.py calls
pause() right before that and resume() once the conversation ends - this
fully releases the microphone in between, instead of just ignoring it.
"""

import threading
import time

import numpy as np
import openwakeword
import pyaudio
from openwakeword.model import Model

from audio_utils import pick_channels

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz - openWakeWord's recommended frame size
THRESHOLD = 0.35  # score above this wakes Jarvis (was 0.5 - real "Hey Jarvis" scores
# from the laptop mic mostly landed at 0.30-0.49 in mic_diagnostic.py)
LOG_THRESHOLD = 0.2  # every score above this is logged, even without triggering,
# so THRESHOLD can be calibrated by looking at the near-misses
COOLDOWN_SECONDS = 2.0  # ignore further detections briefly after one fires,
# so trailing audio from the same utterance can't trigger it again
RESUME_DISCARD_CHUNKS = 25  # ~2s of audio to throw away right after the mic
# reopens - covers driver startup noise AND lets Jarvis's own voice (still
# echoing in the room if no headphones are used) fade out before listening
# for the wake word again

_paused = threading.Event()


def pause():
    print("[wake_word] pause() called", flush=True)
    _paused.set()


def resume():
    print("[wake_word] resume() called", flush=True)
    _paused.clear()


def listen_forever(on_wake):
    """
    Blocks forever, calling on_wake() (no arguments) each time "Hey Jarvis"
    is heard. Meant to run in its own background thread.
    """
    openwakeword.utils.download_models(["hey_jarvis"])
    model = Model(wakeword_models=["hey_jarvis"])
    mic = pyaudio.PyAudio()

    def open_stream():
        channels = pick_channels(mic)
        stream = mic.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        return stream, channels

    stream, channels = open_stream()
    last_trigger = 0.0
    discard = RESUME_DISCARD_CHUNKS
    try:
        while True:
            if _paused.is_set():
                stream.stop_stream()
                stream.close()
                while _paused.is_set():
                    time.sleep(0.1)
                stream, channels = open_stream()  # device may have changed (headphones in/out)
                model.reset()  # clear the rolling audio buffer from before the pause
                discard = RESUME_DISCARD_CHUNKS
                continue

            raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            if discard > 0:
                discard -= 1
                continue

            if channels > 1:
                audio = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels).mean(axis=1).astype(np.int16)
            else:
                audio = np.frombuffer(raw, dtype=np.int16)
            prediction = model.predict(audio)
            score = prediction.get("hey_jarvis", 0.0)

            if score > LOG_THRESHOLD:
                print(f"[wake_word] score={score:.2f} (trigger threshold={THRESHOLD})", flush=True)
            if score > THRESHOLD:
                if (time.monotonic() - last_trigger) > COOLDOWN_SECONDS:
                    last_trigger = time.monotonic()
                    print("[wake_word] TRIGGERED - calling on_wake()", flush=True)
                    on_wake()
                else:
                    print("[wake_word] (within cooldown, ignored)", flush=True)
    finally:
        stream.stop_stream()
        stream.close()
        mic.terminate()
