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

Robustness: listen_forever() is a supervisor. Whatever goes wrong inside a listening
session (the microphone vanishes, the driver hiccups, an unplugged headset, a model
error) is logged and the session is started again from scratch - with a NEW PyAudio
instance, because a PyAudio that has lost its device is not always usable again. The
thread never dies, so "Hey Jarvis" cannot silently stop working until the next restart.
"""

import threading
import time
import traceback

import numpy as np
import openwakeword
import pyaudio
from openwakeword.model import Model

import wake_health
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

RESTART_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0)  # wait before restarting after the 1st, 2nd, 3rd, 4th+ failure in a row (10 s is the cap)
HEALTHY_SECONDS = 30.0  # a session that ran this long before failing counts as healthy: the wait starts over at 1 s

_paused = threading.Event()
_recovering = False  # set by a failure, cleared (with a log line) when the microphone is listening again


def pause():
    print("[wake_word] pause() called", flush=True)
    _paused.set()


def resume():
    print("[wake_word] resume() called", flush=True)
    _paused.clear()


def _close_quietly(stream):
    """stop + close a stream that may already be dead or None, without ever raising."""
    if stream is None:
        return
    for action in (stream.stop_stream, stream.close):
        try:
            action()
        except Exception:
            pass


def _terminate_quietly(mic):
    try:
        mic.terminate()
    except Exception:
        pass


def _device_name(mic):
    try:
        return mic.get_default_input_device_info().get("name", "unknown device")
    except Exception:
        return "default input device"


def _wait_while_paused():
    """desktop.py has the microphone (voice_capture) between pause() and resume(): never open it then."""
    while _paused.is_set():
        time.sleep(0.1)


def _listen_once(on_wake, model):
    """
    One listening session: a fresh PyAudio, the microphone, the detection loop. It only ends by
    raising (the loop is endless); the finally block releases the device whatever happened.
    """
    _wait_while_paused()
    mic = pyaudio.PyAudio()
    stream = None
    try:
        def open_stream():
            channels = pick_channels(mic)
            opened = mic.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )
            print(f"[wake_word] listening on {_device_name(mic)} ({channels} channel(s), {SAMPLE_RATE} Hz)", flush=True)
            global _recovering
            if _recovering:
                _recovering = False
                print("[wake_word] recovered", flush=True)
            return opened, channels

        stream, channels = open_stream()
        last_trigger = 0.0
        discard = RESUME_DISCARD_CHUNKS
        while True:
            if _paused.is_set():
                _close_quietly(stream)
                stream = None
                _wait_while_paused()
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
                    try:
                        on_wake()
                    except Exception as error:  # a broken callback is not a broken microphone: keep listening
                        print(f"[wake_word] on_wake() raised {error!r} - still listening", flush=True)
                else:
                    print("[wake_word] (within cooldown, ignored)", flush=True)
    finally:
        _close_quietly(stream)
        _terminate_quietly(mic)


def _report_failure(error):
    """Log the ORIGINAL error with its full traceback, and count the restart for /status."""
    global _recovering
    _recovering = True
    print(f"[wake_word] error: {error!r}\n{traceback.format_exc().rstrip()}", flush=True)
    wake_health.record_restart()
    state = wake_health.snapshot()
    if state["unstable"]:
        print(f"[wake_word] unstable: {state['restarts_last_minute']} restarts in the last minute", flush=True)


def listen_forever(on_wake):
    """
    Blocks forever, calling on_wake() (no arguments) each time "Hey Jarvis"
    is heard. Meant to run in its own background thread.

    Supervisor: any Exception in a session (including loading the model) is logged with its full
    traceback and the session restarts after a growing pause (1s, 2s, 5s, then 10s at most; back
    to 1s after a session that lasted HEALTHY_SECONDS). Only BaseExceptions (KeyboardInterrupt,
    SystemExit) end it. 3 restarts within a minute make /status report the wake word as unstable.
    """
    model = None  # loaded once, kept across restarts
    failures = 0  # consecutive failed sessions: picks the pause
    while True:
        started = time.monotonic()
        try:
            if model is None:
                openwakeword.utils.download_models(["hey_jarvis"])
                model = Model(wakeword_models=["hey_jarvis"])
            _listen_once(on_wake, model)
        except Exception as error:
            _report_failure(error)
        if time.monotonic() - started >= HEALTHY_SECONDS:
            failures = 0
        delay = RESTART_DELAYS_SECONDS[min(failures, len(RESTART_DELAYS_SECONDS) - 1)]
        failures += 1
        print(f"[wake_word] restarting in {delay:g}s", flush=True)
        time.sleep(delay)
