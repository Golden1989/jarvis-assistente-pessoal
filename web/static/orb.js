/*
 * HUD orb (SVG + CSS) - drop-in replacement for the canvas orb.
 *
 *   Orb.init(el)            el is the old <canvas> (replaced, keeping its id/classes) or any container
 *   Orb.setThinking(bool)   active + faster spins + red/wine accent, until turned off
 *   Orb.pulse()             one core pulse, marks activity (called on each spoken word)
 *   Orb.setMode(mode)       "serious" | anything else (palette comes from CSS variables)
 *   Orb.create(el, opts)    an independent instance (prototype gallery)
 *
 * NO requestAnimationFrame loop and no setInterval: the rings are Web Animations that the
 * compositor runs; they are PAUSED whenever the orb is idle, hidden or the user prefers
 * reduced motion. One setTimeout brings the orb back to rest ~1.5 s after the last activity.
 */
(function () {
  "use strict";

  const IDLE_MS = 1500;          // rest again this long after the last activity
  const STEPS_PER_SECOND = 30;   // rotation updates per second: slow spins look identical at 30/s and
                                 // the display (240 Hz here) is not asked to repaint them 240 times
  const THINK_RATE = 3;          // "thinking" spins this much faster
  const reduceMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  let uid = 0;

  const TAU = 2 * Math.PI;
  const circ = (r) => TAU * r;
  // dash pattern with `count` marks of length `mark` evenly spaced around a circle of radius r
  const marks = (r, count, mark) => `${mark} ${(circ(r) / count - mark).toFixed(3)}`;

  function template(n, simple) {
    return `
<svg class="layer l0" viewBox="-210 -210 420 420" fill="none" aria-hidden="true">
  <defs>
    <linearGradient id="rim${n}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" style="stop-color:var(--c1);stop-opacity:.75"/>
      <stop offset="1" style="stop-color:var(--c2);stop-opacity:.35"/>
    </linearGradient>
    <radialGradient id="core${n}" cx="50%" cy="50%" r="50%">
      <stop offset="0" style="stop-color:var(--hi);stop-opacity:.55"/>
      <stop offset=".45" style="stop-color:var(--c1);stop-opacity:.22"/>
      <stop offset="1" style="stop-color:var(--c1);stop-opacity:0"/>
    </radialGradient>
  </defs>
  <!-- outer thin dark ring, with a small notch -->
  <circle r="200" stroke="url(#rim${n})" stroke-width="9" opacity=".10"/>
  <circle r="200" stroke="url(#rim${n})" stroke-width="2" stroke-dasharray="${(circ(200) - 22).toFixed(2)} 22" transform="rotate(-96)"/>
  <circle r="188" class="s-c2" stroke-width="1" opacity=".35"/>
  <!-- inner ring with evenly spaced nodes / gear teeth -->
  <circle r="104" class="s-c1" stroke-width="1" opacity=".35"/>
  <circle r="104" class="s-c1" stroke-width="9" stroke-dasharray="${marks(104, 24, 4)}" opacity=".85"/>
  <g class="detail">
    <circle r="104" class="s-c2" stroke-width="15" stroke-dasharray="${marks(104, 8, 8)}" opacity=".55"/>
    <circle r="72" class="s-c2" stroke-width="1" stroke-dasharray="2 5" opacity=".5"/>
  </g>
  <!-- core: concentric circles (a wide faint stroke under each fine one) and a bright centre -->
  <circle r="66" fill="url(#core${n})"/>
  <circle r="46" class="s-c1" stroke-width="10" opacity=".10"/>
  <circle r="46" class="s-c1" stroke-width="1.6" opacity=".85"/>
  <circle r="32" class="s-hi" stroke-width="1" stroke-dasharray="3 5" opacity=".6"/>
  <circle r="20" class="s-c1" stroke-width="2" opacity=".9"/>
  <circle r="12" class="f-hi" opacity=".22"/>
  <circle r="5" class="f-hi"/>
</svg>
<div class="layer spin l1w">
  <div class="layer l1g"></div>
  <div class="layer l1"></div>
</div>
<svg class="layer spin l2" viewBox="-210 -210 420 420" fill="none" aria-hidden="true">
  <circle r="140" class="s-c1" stroke-width="1" opacity=".55"/>
  <circle r="140" class="s-c1" stroke-width="7" stroke-dasharray="${marks(140, 90, 1.2)}" opacity=".75"/>
  <circle r="140" class="s-c1" stroke-width="13" stroke-dasharray="${marks(140, 18, 2)}" opacity=".9"/>
  <circle r="126" class="s-hi" stroke-width="3.5" stroke-linecap="round" stroke-dasharray="${marks(126, 36, 0.1)}" opacity=".75"/>
</svg>
<svg class="layer spin l3" viewBox="-210 -210 420 420" fill="none" aria-hidden="true">
  <defs>
    <linearGradient id="arc${n}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" style="stop-color:var(--c1)"/>
      <stop offset="1" style="stop-color:var(--c2)"/>
    </linearGradient>
  </defs>
  <circle r="84" class="s-c1" stroke-width="1" opacity=".35"/>
  <circle r="84" stroke="url(#arc${n})" stroke-width="7" stroke-dasharray="110 30 70 40 40 ${(circ(84) - 290).toFixed(2)}"/>
  <circle r="93" class="s-c1" stroke-width="2.5" stroke-dasharray="60 40 30 ${(circ(93) - 130).toFixed(2)}" stroke-dashoffset="-150" opacity=".85"/>
  <g class="accent">
    <circle r="84" class="s-acc" stroke-width="8" stroke-dasharray="52 ${(circ(84) - 52).toFixed(2)}" stroke-dashoffset="-20"/>
    <circle r="93" class="s-acc" stroke-width="3" stroke-dasharray="34 ${(circ(93) - 34).toFixed(2)}" stroke-dashoffset="-250"/>
    <circle r="84" class="s-acc" stroke-width="8" stroke-dasharray="26 ${(circ(84) - 26).toFixed(2)}" stroke-dashoffset="-330"/>
  </g>
</svg>
<div class="pulse-accent"></div>
<div class="pulse"></div>`;
  }

  function create(container, opts) {
    opts = opts || {};
    const n = ++uid;
    const replace = container.tagName === "CANVAS";
    const size = container.getBoundingClientRect ? container.getBoundingClientRect().width : 0;
    const simple = opts.simple !== undefined ? opts.simple : (size > 0 && size <= 200);   // widget: 3 rings + core

    const root = document.createElement("div");
    root.className = "orb" + (simple ? " orb-simple" : "");
    root.innerHTML = template(n, simple);
    if (replace) {                                   // keep the old canvas' id and classes (layout CSS, app.js lookups)
      root.id = container.id;
      container.classList.forEach((c) => root.classList.add(c));
      container.replaceWith(root);
    } else {
      container.appendChild(root);
    }

    const q = (sel) => root.querySelector(sel);
    const core = q(".pulse");
    const spins = [];
    function addSpin(el, seconds, dir) {
      if (!el || el.offsetParent === null && getComputedStyle(el).display === "none") return;   // hidden in the simple variant
      const a = el.animate(
        [{ transform: "rotate(0deg)" }, { transform: `rotate(${360 * dir}deg)` }],
        { duration: seconds * 1000, iterations: Infinity, easing: `steps(${Math.round(seconds * STEPS_PER_SECOND)})` });
      a.pause();                                     // born paused: an idle orb costs nothing
      spins.push(a);
    }
    addSpin(q(".l1w"), 30, 1);      // thick segmented ring, clockwise
    addSpin(q(".l2"), 18, -1);      // ring with hash marks, counter-clockwise
    addSpin(q(".l3"), 45, 1);       // broken arcs, clockwise

    let active = false, thinking = false, timer = 0;
    const canRun = () => !reduceMotion.matches && !document.hidden;
    const play = () => { if (canRun()) spins.forEach((a) => a.play()); };
    const pause = () => spins.forEach((a) => a.pause());

    function rest() { active = false; timer = 0; root.classList.remove("active"); pause(); }
    function touch() {                                // any activity: run, and schedule the return to rest
      if (!active) { active = true; root.classList.add("active"); play(); }
      clearTimeout(timer);
      timer = thinking ? 0 : setTimeout(rest, IDLE_MS);
    }

    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); else if (active) play(); });

    return {
      root,
      setThinking(on) {
        on = !!on;
        thinking = on;
        root.classList.toggle("thinking", on);
        spins.forEach((a) => a.updatePlaybackRate(on ? THINK_RATE : 1));
        if (on) touch();
        else if (active) { clearTimeout(timer); timer = setTimeout(rest, IDLE_MS); }
      },
      pulse() {
        if (reduceMotion.matches) return;
        touch();
        core.animate([{ transform: "scale(1)", opacity: 1 }, { transform: "scale(1.22)", opacity: 1 }, { transform: "scale(1)", opacity: 1 }],
                     { duration: 320, easing: "steps(10)" });   // stepped like the rings: ~30 updates/s, not one per vsync (240 Hz)
      },
      setMode(mode) { root.classList.toggle("is-serious", mode === "serious"); },
      // test hook: freeze the rings at a fixed pose (for screenshots)
      _pose(ms) { spins.forEach((a) => { a.pause(); a.currentTime = ms; }); },
      _spins: spins,
    };
  }

  // ---- drop-in singleton API used by app.js / widget.js ----
  let current = null;
  const pending = { mode: null, thinking: false };
  window.Orb = {
    create,
    init(el) {
      current = create(el);
      if (pending.mode) current.setMode(pending.mode);
      if (pending.thinking) current.setThinking(true);
    },
    setThinking(v) { pending.thinking = !!v; if (current) current.setThinking(v); },
    setMode(m) { pending.mode = m; if (current) current.setMode(m); },
    pulse() { if (current) current.pulse(); },
  };
})();
