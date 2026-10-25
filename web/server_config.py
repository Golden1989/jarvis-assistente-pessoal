"""
Shared HOST/PORT for the Flask app in server.py.

Its own tiny, side-effect-free module - no Flask, no webview/tkinter/audio - so server.py and
desktop.py can both import it, and so tests can read these constants (and confirm desktop.py uses
them, by reading its source text) without ever importing desktop.py itself, which has real GUI/
microphone side effects on import.
"""

HOST = "127.0.0.1"
PORT = 5000
