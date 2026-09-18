const radar = document.getElementById("radar");
const coreLabel = document.getElementById("core-label");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const clockEl = document.getElementById("clock");
const log = document.getElementById("log");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const button = form.querySelector('button[type="submit"]');
const micBtn = document.getElementById("mic-btn");
const langBtn = document.getElementById("lang-btn");
const muteBtn = document.getElementById("mute-btn");
const costEl = document.getElementById("cost");
const tokInEl = document.getElementById("tok-in");
const tokOutEl = document.getElementById("tok-out");
const barsEl = document.getElementById("bars");
const toolListEl = document.getElementById("tool-list");
const toolCountEl = document.getElementById("tool-count");
const calendarBody = document.getElementById("calendar-body");

let toolNames = [];
let barHistory = [];

function tick() {
  clockEl.textContent = new Date().toLocaleTimeString("en-GB", { hour12: false });
}
tick();
setInterval(tick, 1000);

function setThinking(on) {
  radar.classList.toggle("thinking", on);
  statusDot.classList.toggle("thinking", on);
  statusText.textContent = on ? "PROCESSING" : "OPTIMAL";
  coreLabel.textContent = on ? "CORE THINKING" : "CORE ACTIVE";
  if (window.Orb) Orb.setThinking(on);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function addLog(who, text) {
  const line = document.createElement("div");
  line.className = `log-line ${who}`;
  const ts = new Date().toLocaleTimeString("en-GB", { hour12: false });
  const label = who === "user" ? "YOU" : who === "jarvis" ? "JARVIS" : "SYS";
  line.innerHTML = `<span class="ts">[${ts}]</span><span class="who">${label}:</span>${escapeHtml(text)}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function renderTools() {
  toolListEl.innerHTML = "";
  toolNames.forEach((name) => {
    const li = document.createElement("li");
    li.dataset.tool = name;
    li.innerHTML = `<span class="ping"></span>${escapeHtml(name)}`;
    toolListEl.appendChild(li);
  });
  toolCountEl.textContent = `${toolNames.length} ONLINE`;
}

function flashToolsUsed(names) {
  names.forEach((name) => {
    const li = toolListEl.querySelector(`li[data-tool="${CSS.escape(name)}"]`);
    if (!li) return;
    li.classList.add("used");
    const ping = li.querySelector(".ping");
    ping.style.animation = "none";
    void ping.offsetWidth; // force reflow so the animation restarts
    ping.style.animation = "";
  });
}

function pushBar(tokens) {
  barHistory.push(tokens);
  if (barHistory.length > 24) barHistory.shift();
  const max = Math.max(...barHistory, 1);
  barsEl.innerHTML = "";
  barHistory.forEach((t) => {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.style.height = `${Math.max(3, (t / max) * 40)}px`;
    barsEl.appendChild(bar);
  });
}

function renderCalendar(nextEvent) {
  if (!nextEvent) {
    calendarBody.innerHTML = '<span class="dim">No upcoming events.</span>';
    return;
  }
  const when = new Date(nextEvent.start);
  const label = isNaN(when) ? nextEvent.start : when.toLocaleString();
  calendarBody.innerHTML = `
    <div class="event-title">${escapeHtml(nextEvent.summary)}</div>
    <div class="event-time">${escapeHtml(label)}</div>
  `;
}

// ---------- Voice: speech-to-text (microphone) ----------
// Web Speech API - built into Chrome/Edge, free, runs entirely in the
// browser. The browser needs to be told which language to listen for, so a
// small PT/EN toggle sits next to the mic button.

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let listening = false;
let sttLang = localStorage.getItem("jarvis-stt-lang") || "pt-BR";

function updateLangButton() {
  langBtn.textContent = sttLang.startsWith("pt") ? "PT" : "EN";
}
updateLangButton();

langBtn.addEventListener("click", () => {
  sttLang = sttLang.startsWith("pt") ? "en-US" : "pt-BR";
  localStorage.setItem("jarvis-stt-lang", sttLang);
  updateLangButton();
});

if (SpeechRecognitionImpl) {
  recognition = new SpeechRecognitionImpl();
  recognition.continuous = false;
  recognition.interimResults = true;

  recognition.addEventListener("result", (event) => {
    let transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      transcript += event.results[i][0].transcript;
    }
    input.value = transcript;
    if (event.results[event.results.length - 1].isFinal) {
      form.requestSubmit();
    }
  });

  recognition.addEventListener("end", () => {
    listening = false;
    micBtn.classList.remove("listening");
  });

  recognition.addEventListener("error", (event) => {
    listening = false;
    micBtn.classList.remove("listening");
    if (event.error !== "no-speech") {
      addLog("system", `mic error: ${event.error}`);
    }
  });

  micBtn.addEventListener("click", () => {
    if (listening) {
      recognition.stop();
      return;
    }
    recognition.lang = sttLang;
    recognition.start();
    listening = true;
    micBtn.classList.add("listening");
  });
} else {
  micBtn.disabled = true;
  micBtn.title = "Voice input isn't supported in this browser - try Chrome or Edge.";
}

// ---------- Voice: text-to-speech (Jarvis speaking) ----------

let voiceMuted = localStorage.getItem("jarvis-muted") === "true";

function updateMuteButton() {
  muteBtn.textContent = voiceMuted ? "\u{1F507}" : "\u{1F50A}";
  muteBtn.classList.toggle("muted", voiceMuted);
}
updateMuteButton();

muteBtn.addEventListener("click", () => {
  voiceMuted = !voiceMuted;
  localStorage.setItem("jarvis-muted", voiceMuted);
  updateMuteButton();
  if (voiceMuted && window.speechSynthesis) window.speechSynthesis.cancel();
});

function looksPortuguese(text) {
  return /[ãõçáéíóúâêô]/i.test(text) || /\b(você|não|está|para|isso|então)\b/i.test(text);
}

function setSpeaking(on) {
  statusText.textContent = on ? "SPEAKING" : "OPTIMAL";
  statusDot.classList.toggle("thinking", on);
}

function speak(text) {
  if (voiceMuted || !window.speechSynthesis) return;
  window.speechSynthesis.cancel(); // don't let replies overlap
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = looksPortuguese(text) ? "pt-BR" : "en-US";

  const match = window.speechSynthesis.getVoices().find((v) => v.lang === utterance.lang);
  if (match) utterance.voice = match;

  // No direct access to the synthesized audio to analyze its volume, so the
  // orb "reacts" to speech via word-boundary events instead of real amplitude.
  utterance.onboundary = () => {
    if (window.Orb) Orb.pulse();
  };
  utterance.onstart = () => setSpeaking(true);
  utterance.onend = () => setSpeaking(false);
  utterance.onerror = () => setSpeaking(false);

  window.speechSynthesis.speak(utterance);
}

async function refreshStatus() {
  try {
    const res = await fetch("/status");
    const data = await res.json();
    toolNames = data.tools || [];
    renderTools();
    tokInEl.textContent = data.session.input_tokens;
    tokOutEl.textContent = data.session.output_tokens;
    costEl.textContent = `$${data.session.cost.toFixed(4)}`;
    renderCalendar(data.next_event);
  } catch (error) {
    addLog("system", `status check failed: ${error}`);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;

  addLog("user", text);
  input.value = "";
  input.disabled = true;
  button.disabled = true;
  setThinking(true);

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await response.json();

    if (!response.ok) {
      addLog("system", `error: ${data.error}`);
    } else {
      if (data.tools_used && data.tools_used.length) {
        addLog("system", `tool(s) called: ${data.tools_used.join(", ")}`);
        flashToolsUsed(data.tools_used);
      }
      addLog("jarvis", data.reply);
      speak(data.reply);
      pushBar(data.turn_tokens || 0);
      costEl.textContent = `$${data.session_cost.toFixed(4)}`;
    }
  } catch (error) {
    addLog("system", `connection error: ${error}`);
  } finally {
    setThinking(false);
    input.disabled = false;
    button.disabled = false;
    input.focus();
    refreshStatus();
  }
});

const orbCanvas = document.getElementById("orb-canvas");
if (window.Orb && orbCanvas) {
  Orb.init(orbCanvas);
}

refreshStatus();
input.focus();
