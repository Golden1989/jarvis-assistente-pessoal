// The floating widget: just the orb. Click it to expand into the full
// dashboard - handled by desktop.py's Api.expand() through the pywebview
// JS bridge. Falls back to a normal navigation if opened as a plain
// browser tab (no pywebview bridge present).

const stage = document.getElementById("widget-stage");
const canvas = document.getElementById("orb-canvas");

if (window.Orb && canvas) {
  Orb.init(canvas);
}

function goToDashboard() {
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api.expand();
  } else {
    window.location.href = "/";
  }
}

stage.addEventListener("click", goToDashboard);

// ---------- Voice, driven from Python ----------
// "Hey Jarvis" detection AND transcribing the actual question both run
// locally in Python (wake_word.py / voice_capture.py) - the browser's own
// speech *recognition* doesn't work inside this embedded window, only
// speech *synthesis* does. So desktop.py runs the whole conversation and
// just calls in here to (a) speak text out loud and (b) show "thinking".

function speak(text, lang, onDone) {
  if (localStorage.getItem("jarvis-muted") === "true" || !window.speechSynthesis) {
    if (onDone) onDone();
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = lang;
  const match = window.speechSynthesis.getVoices().find((v) => v.lang === lang);
  if (match) utterance.voice = match;
  utterance.onboundary = () => window.Orb && Orb.pulse();
  utterance.onend = () => onDone && onDone();
  utterance.onerror = () => onDone && onDone();
  window.speechSynthesis.speak(utterance);
}

// Called from Python via evaluate_js, wrapped in a Promise it awaits -
// resolve is that Promise's resolve function, passed as onDone.
window.speakText = function (text, lang, onDone) {
  speak(text, lang, onDone);
};

window.setThinking = function (on) {
  stage.classList.toggle("thinking", on); // kept for compatibility: no CSS uses it now (the orb handles "thinking" itself)
  if (window.Orb) Orb.setThinking(on);
};

// Called from desktop.py (evaluate_js) and once on load.
window.setMode = function (mode) {
  stage.classList.toggle("serious", mode === "serious");
  document.documentElement.classList.toggle("serious", mode === "serious"); // page background too
  if (window.Orb) Orb.setMode(mode === "serious" ? "serious" : "normal");
};

// expand/shrink reload this page, so ask the server for the current mode once.
// /mode is tiny: no chat lock, no touch(), no Google Calendar.
fetch("/mode")
  .then((response) => response.json())
  .then((data) => window.setMode(data.mode))
  .catch(() => {});
