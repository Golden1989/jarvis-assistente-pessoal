"""Shared microphone helpers for wake_word.py and voice_capture.py."""


def pick_channels(mic):
    """
    Ask whatever the CURRENT default input device is how many channels it
    actually supports (1 for a plain headset mic, more for a multi-mic
    laptop array) instead of assuming - the default device can change any
    time headphones are plugged in or out.
    """
    try:
        info = mic.get_default_input_device_info()
        return 2 if int(info.get("maxInputChannels", 1)) >= 2 else 1
    except Exception:
        return 1
