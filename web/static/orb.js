/*
 * A hand-rolled 3D wireframe sphere, rendered on <canvas>.
 * No 3D library - just basic trig to project 3D points onto the 2D canvas,
 * and a heartbeat-shaped pulse (two quick bumps, then a rest) instead of a
 * smooth "breathing" scale.
 */
(function () {
  const LAT_RINGS = 7;        // circles of latitude
  const LON_RINGS = 9;        // meridians (pole to pole)
  const POINTS_PER_RING = 48;
  const SPARK_COUNT = 46;

  const BLUE = [79, 195, 247];
  const RED = [255, 59, 78];

  let canvas, ctx, width, height, radius;
  let angleY = 0;
  const angleX = 0.35; // fixed tilt, like looking slightly down at a globe
  let thinking = false;
  let beatClock = 0;
  let lastTime = null;
  let speakKick = 0; // transient extra pulse, added on top of the heartbeat, decays fast

  const rings = [];   // each: array of unit-sphere {x,y,z} points
  const sparks = [];  // each: {theta, phi, size, mix}

  function spherePoint(lat, lon) {
    return {
      x: Math.cos(lat) * Math.cos(lon),
      y: Math.sin(lat),
      z: Math.cos(lat) * Math.sin(lon),
    };
  }

  function buildGeometry() {
    rings.length = 0;
    sparks.length = 0;

    for (let i = 1; i < LAT_RINGS; i++) {
      const lat = -Math.PI / 2 + (Math.PI * i) / LAT_RINGS;
      const ring = [];
      for (let j = 0; j <= POINTS_PER_RING; j++) {
        ring.push(spherePoint(lat, (2 * Math.PI * j) / POINTS_PER_RING));
      }
      rings.push(ring);
    }

    for (let i = 0; i < LON_RINGS; i++) {
      const lon0 = (Math.PI * i) / LON_RINGS;
      const ring = [];
      for (let j = 0; j <= POINTS_PER_RING; j++) {
        const t = (2 * Math.PI * j) / POINTS_PER_RING;
        ring.push({
          x: Math.sin(t) * Math.cos(lon0),
          y: Math.cos(t),
          z: Math.sin(t) * Math.sin(lon0),
        });
      }
      rings.push(ring);
    }

    for (let i = 0; i < SPARK_COUNT; i++) {
      sparks.push({
        theta: Math.random() * Math.PI * 2,
        phi: Math.acos(2 * Math.random() - 1),
        size: 0.6 + Math.random() * 1.6,
        mix: Math.random(),
      });
    }
  }

  function sparkPoint(s) {
    return {
      x: Math.sin(s.phi) * Math.cos(s.theta),
      y: Math.cos(s.phi),
      z: Math.sin(s.phi) * Math.sin(s.theta),
    };
  }

  function rotate(p, ay, ax) {
    let x = p.x * Math.cos(ay) - p.z * Math.sin(ay);
    let z = p.x * Math.sin(ay) + p.z * Math.cos(ay);
    const y = p.y * Math.cos(ax) - z * Math.sin(ax);
    z = p.y * Math.sin(ax) + z * Math.cos(ax);
    return { x, y, z };
  }

  function project(p, scale) {
    const focal = 3.2;
    const depth = focal + p.z;
    const f = (focal / depth) * scale;
    return { sx: width / 2 + p.x * f, sy: height / 2 + p.y * f, depth };
  }

  function mixColor(mix) {
    return [
      BLUE[0] + (RED[0] - BLUE[0]) * mix,
      BLUE[1] + (RED[1] - BLUE[1]) * mix,
      BLUE[2] + (RED[2] - BLUE[2]) * mix,
    ];
  }

  // Heartbeat: two quick bumps ("lub-dub"), then a rest - not a smooth sine.
  function heartbeatScale(t, period) {
    const phase = (t % period) / period;
    const bump = (center, width, amp) => {
      const d = (phase - center) / width;
      return amp * Math.exp(-d * d);
    };
    return 1 + bump(0.06, 0.05, 0.09) + bump(0.16, 0.06, 0.05);
  }

  function draw(time) {
    if (lastTime === null) lastTime = time;
    const dt = (time - lastTime) / 1000;
    lastTime = time;

    angleY += dt * (thinking ? 0.9 : 0.25);
    beatClock += dt;
    speakKick *= Math.pow(0.002, dt); // fast exponential decay

    const period = thinking ? 0.62 : 0.95;
    const scale = (heartbeatScale(beatClock, period) + speakKick) * radius;

    ctx.clearRect(0, 0, width, height);

    rings.forEach((ring, idx) => {
      const projected = ring.map((p) => project(rotate(p, angleY, angleX), scale));
      ctx.beginPath();
      projected.forEach((pt, i) => (i === 0 ? ctx.moveTo(pt.sx, pt.sy) : ctx.lineTo(pt.sx, pt.sy)));

      const avgDepth = projected.reduce((s, p) => s + p.depth, 0) / projected.length;
      const alpha = Math.max(0.08, Math.min(0.9, (avgDepth - 1.5) / 3));
      const [r, g, b] = mixColor(idx % 2 === 0 ? 0.15 : 0.55);

      ctx.strokeStyle = `rgba(${r | 0},${g | 0},${b | 0},${alpha})`;
      ctx.lineWidth = thinking ? 1.4 : 1;
      ctx.shadowColor = ctx.strokeStyle;
      ctx.shadowBlur = 6;
      ctx.stroke();
    });

    sparks.forEach((s) => {
      const p = project(rotate(sparkPoint(s), angleY, angleX), scale);
      const [r, g, b] = mixColor(s.mix);
      const alpha = Math.max(0.15, Math.min(1, (p.depth - 1.2) / 3.4));
      ctx.beginPath();
      ctx.fillStyle = `rgba(${r | 0},${g | 0},${b | 0},${alpha})`;
      ctx.shadowColor = ctx.fillStyle;
      ctx.shadowBlur = 8;
      ctx.arc(p.sx, p.sy, s.size, 0, Math.PI * 2);
      ctx.fill();
    });

    const glowRadius = scale * 0.35;
    const gradient = ctx.createRadialGradient(width / 2, height / 2, 0, width / 2, height / 2, glowRadius);
    gradient.addColorStop(0, "rgba(255,255,255,0.9)");
    gradient.addColorStop(0.4, "rgba(79,195,247,0.55)");
    gradient.addColorStop(1, "rgba(79,195,247,0)");
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(width / 2, height / 2, glowRadius, 0, Math.PI * 2);
    ctx.fill();

    requestAnimationFrame(draw);
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    width = rect.width;
    height = rect.height;
    radius = Math.min(width, height) * 0.34;
  }

  window.Orb = {
    init(canvasEl) {
      canvas = canvasEl;
      ctx = canvas.getContext("2d");
      buildGeometry();
      resize();
      window.addEventListener("resize", resize);
      requestAnimationFrame(draw);
    },
    setThinking(value) {
      thinking = value;
    },
    pulse() {
      speakKick = Math.min(speakKick + 0.15, 0.4);
    },
  };
})();
