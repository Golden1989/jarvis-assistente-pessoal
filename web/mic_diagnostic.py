"""
Standalone microphone/wake-word diagnostic - NOT part of the running app.

Run this by hand, interactively, in a terminal:

    python mic_diagnostic.py

It records controlled samples and prints numbers - it does not change any
of voice_capture.py's or wake_word.py's actual settings. Use the numbers to
decide what to adjust (that's a separate step).

What it measures:
  1. 5s of silence -> mean/peak amplitude, per channel and mixed to mono.
  2. 5s of you speaking normally -> same stats, for comparison.
  3. 15s of listening -> the highest "hey_jarvis" wake-word score seen in
     each second (printed even when it's below the trigger threshold), using
     the same mono downmix the app uses, AND (if the device has more than
     one channel) each raw channel on its own - to see whether averaging
     channels together is helping or hurting the signal.
"""

import numpy as np
import openwakeword
import pyaudio
from openwakeword.model import Model

from audio_utils import pick_channels

SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz


def _record_chunks(stream, seconds):
    n_chunks = int(seconds * SAMPLE_RATE / CHUNK_SIZE)
    return [stream.read(CHUNK_SIZE, exception_on_overflow=False) for _ in range(n_chunks)]


def _to_matrix(raw_chunks, channels):
    """Raw int16 PCM chunks -> a (frames, channels) int16 array."""
    flat = np.frombuffer(b"".join(raw_chunks), dtype=np.int16)
    return flat.reshape(-1, channels)


def _volume_stats(matrix):
    """Per-channel and mono-downmix mean/peak amplitude. Widened to int32
    before abs() so a clipped sample (-32768) doesn't wrap back negative."""
    widened = matrix.astype(np.int32)
    stats = {}
    for ch in range(matrix.shape[1]):
        col = np.abs(widened[:, ch])
        stats[f"channel {ch}"] = (float(col.mean()), int(col.max()))
    mono = np.abs(widened.mean(axis=1))
    stats["mono mix (avg of channels)"] = (float(mono.mean()), float(mono.max()))
    return stats


def _print_stats(label, stats):
    print(f"\n--- {label} ---")
    for name, (mean, peak) in stats.items():
        print(f"  {name:28s} mean={mean:8.1f}   peak={peak:8.1f}")


def main():
    mic = pyaudio.PyAudio()
    channels = pick_channels(mic)
    info = mic.get_default_input_device_info()
    print(f"Default input device: {info['name']!r}")
    print(f"Opening the stream with {channels} channel(s) (same as the real app).")

    stream = mic.open(
        format=pyaudio.paInt16,
        channels=channels,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    try:
        input("\n[1/3] Press ENTER, then stay SILENT for 5 seconds...")
        silence_matrix = _to_matrix(_record_chunks(stream, 5), channels)
        _print_stats("SILENCE (5s)", _volume_stats(silence_matrix))

        input("\n[2/3] Press ENTER, then speak NORMALLY (your usual distance/volume) for 5 seconds...")
        speech_matrix = _to_matrix(_record_chunks(stream, 5), channels)
        _print_stats("SPEECH (5s)", _volume_stats(speech_matrix))

        print("\n[3/3] Listening for 15s - say \"Hey Jarvis\" once or twice, normally, whenever you like.")
        openwakeword.utils.download_models(["hey_jarvis"])
        model_mix = Model(wakeword_models=["hey_jarvis"])
        channel_models = [Model(wakeword_models=["hey_jarvis"]) for _ in range(channels)] if channels > 1 else []

        chunks_per_second = int(SAMPLE_RATE / CHUNK_SIZE)
        overall_max_mix = 0.0
        overall_max_per_channel = [0.0] * channels

        for second in range(15):
            max_mix = 0.0
            max_per_channel = [0.0] * channels
            for _ in range(chunks_per_second):
                raw = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                flat = np.frombuffer(raw, dtype=np.int16)
                if channels > 1:
                    matrix = flat.reshape(-1, channels)
                    mono = matrix.mean(axis=1).astype(np.int16)
                else:
                    mono = flat

                max_mix = max(max_mix, model_mix.predict(mono).get("hey_jarvis", 0.0))

                for ch, ch_model in enumerate(channel_models):
                    ch_audio = matrix[:, ch].astype(np.int16) if channels > 1 else mono
                    score = ch_model.predict(ch_audio).get("hey_jarvis", 0.0)
                    max_per_channel[ch] = max(max_per_channel[ch], score)

            overall_max_mix = max(overall_max_mix, max_mix)
            line = f"  t={second + 1:2d}s   mono_mix_max={max_mix:.2f}"
            for ch in range(channels):
                overall_max_per_channel[ch] = max(overall_max_per_channel[ch], max_per_channel[ch])
                if channel_models:
                    line += f"   ch{ch}_max={max_per_channel[ch]:.2f}"
            print(line)

        print(f"\nOverall max score, mono mix: {overall_max_mix:.2f}")
        for ch in range(channels):
            if channel_models:
                print(f"Overall max score, channel {ch} alone: {overall_max_per_channel[ch]:.2f}")

        print("\nCurrent production settings for reference:")
        print("  voice_capture.SILENCE_THRESHOLD = 120")
        print("  wake_word.THRESHOLD = 0.5")

    finally:
        stream.stop_stream()
        stream.close()
        mic.terminate()


if __name__ == "__main__":
    main()
