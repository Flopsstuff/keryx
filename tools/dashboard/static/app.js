"use strict";

const HISTORY_SECONDS = 20;
const SCOPE_BLOCKS = 94;           // ~3 s of 512-sample blocks
const SPECTRO_COLUMNS = 400;       // ~12.8 s
const METER_FLOOR = -100;
const PEAK_HOLD_MS = 1200;
const PEAK_FALL_DB_PER_S = 25;
const SRP_ACTIVE_DB = [-96, -72];  // mic RMS range over which the SRP map fades in

const css = getComputedStyle(document.documentElement);
const color = name => css.getPropertyValue(name).trim();
const BEAM_COLORS = [0, 1, 2, 3].map(i => color(`--beam-${i}`));
const CHANNEL_COLORS = [color("--beam-3"), color("--beam-2"), "#8fb3ff", "#8fb3ff", "#8fb3ff", "#8fb3ff"];

const $ = id => document.getElementById(id);
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const deg = rad => ((rad * 180 / Math.PI) % 360 + 360) % 360;

// How the chip's frame sits on the dial, so the picture can be matched to how the board physically lies
const VIEW_KEY = "xvf-dashboard-view";
const view = loadView();

function loadView() {
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_KEY));
    if (saved && [0, 90, 180, 270].includes(saved.rotation)) return { flip: Boolean(saved.flip), rotation: saved.rotation };
  } catch {}
  return { flip: true, rotation: 0 };
}

function saveView() {
  try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch {}
}

// chip azimuth (radians, counter-clockwise from +X of the mic geometry) -> dial bearing in radians, unwrapped
const toBearing = rad => (view.flip ? -rad : rad) + view.rotation * Math.PI / 180;
const bearingDeg = rad => deg(toBearing(rad));

let info = null;
let lastFrame = null;
const control = { azimuth: [null, null, null, null], energy: [0, 0, 0, 0], selected: [null, null], doa: [0, 0] };
const controlHistory = [];
const srpHistory = [];
const meterState = Array.from({ length: 6 }, () => ({ rms: METER_FLOOR, peak: METER_FLOOR, peakAt: 0 }));
const rates = { frames: [], polls: [] };

// ---------- canvas plumbing ----------

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(rect.width * dpr));
  const h = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}

// perceptual dark-to-bright map (inferno-like stops)
const COLORMAP = (() => {
  const stops = [[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66], [245, 125, 21], [250, 193, 39], [252, 255, 164]];
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255 * (stops.length - 1);
    const k = Math.min(stops.length - 2, Math.floor(x));
    const f = x - k;
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f;
  }
  return lut;
})();
const colormap = t => {
  const i = clamp(Math.round(t * 255), 0, 255) * 3;
  return `rgb(${COLORMAP[i]},${COLORMAP[i + 1]},${COLORMAP[i + 2]})`;
};

// ---------- data intake ----------

const scope = {
  length: SCOPE_BLOCKS * 128,
  write: 0,
  min: Array.from({ length: 6 }, () => new Float32Array(SCOPE_BLOCKS * 128)),
  max: Array.from({ length: 6 }, () => new Float32Array(SCOPE_BLOCKS * 128)),
  gain: new Array(6).fill(1),
};

class Spectrogram {
  constructor(canvas) {
    this.canvas = canvas;
    this.buffer = document.createElement("canvas");
    this.buffer.width = SPECTRO_COLUMNS;
    this.buffer.height = 256;
    this.bctx = this.buffer.getContext("2d");
    this.bctx.fillStyle = "#000004";
    this.bctx.fillRect(0, 0, SPECTRO_COLUMNS, 256);
    this.column = this.bctx.createImageData(1, 256);
    this.top = -40;
  }

  push(bins) {
    const loudest = Math.max(...bins);
    // follow loud material quickly, relax slowly so quiet passages still show structure
    this.top = loudest > this.top ? loudest : this.top - 0.03;
    const hi = this.top, lo = hi - 80;
    for (let i = 0; i < 256; i++) {
      const t = clamp((bins[i] - lo) / (hi - lo), 0, 1);
      const k = Math.round(t * 255) * 3;
      const p = (255 - i) * 4;
      this.column.data[p] = COLORMAP[k];
      this.column.data[p + 1] = COLORMAP[k + 1];
      this.column.data[p + 2] = COLORMAP[k + 2];
      this.column.data[p + 3] = 255;
    }
    this.bctx.drawImage(this.buffer, 1, 0, SPECTRO_COLUMNS - 1, 256, 0, 0, SPECTRO_COLUMNS - 1, 256);
    this.bctx.putImageData(this.column, SPECTRO_COLUMNS - 1, 0);
  }

  draw() {
    const { ctx, w, h } = setupCanvas(this.canvas);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(this.buffer, 0, 0, w, h);
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.font = "10px " + color("--mono");
    for (const khz of [1, 2, 4, 6]) {
      const y = h - khz / 8 * h;
      ctx.fillRect(0, y, 6, 1);
      ctx.fillText(`${khz}k`, 8, y + 3);
    }
  }
}
const spectrograms = [0, 1, 2].map(i => new Spectrogram($(`spec${i}`)));

function onInfo(message) {
  info = message;
  $("product").textContent = info.product;
  $("firmware").textContent = `${info.version} · ${info.build}`;
  $("mic-gain").textContent = info.mic_gain.toFixed(1);
  $("sys-delay").textContent = `${info.sys_delay} smp`;
  const select = $("mic-category");
  select.innerHTML = "";
  for (const [category, name] of Object.entries(info.mic_categories)) {
    const option = new Option(`${name} (cat ${category})`, category);
    select.add(option);
  }
  select.value = String(info.routing[2][0]);
  // spectrogram panels follow SPECTRUM_CHANNELS on the server: mic 0 channel, processed, ASR
  [2, 0, 1].forEach((ch, i) => { $(`spec${i}-label`).textContent = `ch${ch} · ${info.labels[ch]}`; });
  $("beam-legend").innerHTML = info.beam_names
    .map((name, i) => `<li style="--swatch:${BEAM_COLORS[i]}">${name}</li>`)
    .concat([`<li style="--swatch:${color("--srp")}">host SRP-PHAT (raw mics)</li>`,
             `<li style="--swatch:${color("--selected")}">processed DoA</li>`])
    .join("");
}

function onFrame(frame) {
  const now = performance.now();
  lastFrame = frame;
  rates.frames.push(now);

  for (let ch = 0; ch < 6; ch++) {
    const wave = frame.wave[ch];
    const lo = scope.min[ch], hi = scope.max[ch];
    for (let b = 0; b < 128; b++) {
      lo[(scope.write + b) % scope.length] = wave[2 * b] / 32768;
      hi[(scope.write + b) % scope.length] = wave[2 * b + 1] / 32768;
    }
    const m = meterState[ch];
    m.rms = frame.rms[ch];
    if (frame.peak[ch] >= m.peak) { m.peak = frame.peak[ch]; m.peakAt = now; }
  }
  scope.write = (scope.write + 128) % scope.length;

  frame.spectra.forEach((bins, i) => spectrograms[i].push(bins));

  const srp = frame.srp;
  let best = 0;
  for (let i = 1; i < srp.length; i++) if (srp[i] > srp[best]) best = i;
  const activity = clamp((frame.mic_rms - SRP_ACTIVE_DB[0]) / (SRP_ACTIVE_DB[1] - SRP_ACTIVE_DB[0]), 0, 1);
  srpHistory.push({ t: now, angle: best * 2 * Math.PI / srp.length, activity });

  if (frame.control) {
    Object.assign(control, frame.control);
    rates.polls.push(now);
    controlHistory.push({
      t: now,
      azimuth: control.azimuth.slice(),
      energy: control.energy.slice(),
      selected: control.selected[0],
    });
  }

  const horizon = now - HISTORY_SECONDS * 1000;
  while (controlHistory.length && controlHistory[0].t < horizon) controlHistory.shift();
  while (srpHistory.length && srpHistory[0].t < horizon) srpHistory.shift();
}

// ---------- panels ----------

const energyLevel = e => Math.log10(1 + Math.max(0, e));

function drawPolar() {
  const { ctx, w, h } = setupCanvas($("polar"));
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) / 2 - 44;
  // the dial: bearing 0° points down and grows clockwise; chip angles reach it through toBearing
  const place = (bearing, r) => [cx - r * Math.sin(bearing), cy + r * Math.cos(bearing)];
  const at = (azimuth, r) => place(toBearing(azimuth), r);
  ctx.clearRect(0, 0, w, h);

  // rings and bearings
  ctx.strokeStyle = color("--grid");
  ctx.lineWidth = 1;
  for (const f of [1 / 3, 2 / 3, 1]) {
    ctx.beginPath();
    ctx.arc(cx, cy, R * f, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.fillStyle = color("--muted");
  ctx.font = "11px " + color("--mono");
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (let d = 0; d < 360; d += 30) {
    const a = d * Math.PI / 180;
    ctx.beginPath();
    ctx.moveTo(...place(a, R * 0.97));
    ctx.lineTo(...place(a, R));
    ctx.stroke();
    ctx.fillText(`${d}°`, ...place(a, R + 24));
  }

  // host SRP-PHAT: heat ring on the rim and a lobe inside
  if (lastFrame) {
    const srp = lastFrame.srp;
    const lo = Math.min(...srp), hi = Math.max(...srp);
    const activity = clamp((lastFrame.mic_rms - SRP_ACTIVE_DB[0]) / (SRP_ACTIVE_DB[1] - SRP_ACTIVE_DB[0]), 0, 1);
    const n = srp.length;
    const step = 2 * Math.PI / n;
    for (let i = 0; i < n; i++) {
      const t = hi > lo ? (srp[i] - lo) / (hi - lo) : 0;
      ctx.strokeStyle = colormap(t);
      ctx.globalAlpha = 0.25 + 0.75 * activity;
      ctx.lineWidth = 10;
      ctx.beginPath();
      // canvas angles run clockwise from +X, so a bearing b sits at b + 90°
      const start = toBearing((i - 0.5) * step) + Math.PI / 2;
      const end = toBearing((i + 0.5) * step) + Math.PI / 2;
      ctx.arc(cx, cy, R + 7, start, end, view.flip);
      ctx.stroke();
    }
    ctx.globalAlpha = 0.08 + 0.3 * activity;
    ctx.fillStyle = color("--srp");
    ctx.beginPath();
    for (let i = 0; i <= n; i++) {
      const t = hi > lo ? (srp[i % n] - lo) / (hi - lo) : 0;
      const [x, y] = at(i * step, R * (0.12 + 0.85 * t * t));
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  // board and microphones, scaled up so the 44 mm square is visible
  if (info) {
    const reach = Math.max(...info.geometry.map(([x, y]) => Math.hypot(x, y)));
    const scale = R * 0.2 / reach;
    ctx.strokeStyle = color("--border");
    ctx.lineWidth = 2;
    ctx.strokeRect(cx - reach * scale * 1.15, cy - reach * scale * 1.15, reach * scale * 2.3, reach * scale * 2.3);
    info.geometry.forEach(([x, y], i) => {
      const level = meterState[i + 2].rms;
      const glow = clamp((level - SRP_ACTIVE_DB[0]) / (SRP_ACTIVE_DB[1] - SRP_ACTIVE_DB[0]), 0, 1);
      const [px, py] = at(Math.atan2(y, x), Math.hypot(x, y) * scale);
      ctx.fillStyle = `rgba(143,179,255,${0.35 + 0.65 * glow})`;
      ctx.beginPath();
      ctx.arc(px, py, 5 + 5 * glow, 0, 2 * Math.PI);
      ctx.fill();
      ctx.fillStyle = color("--text");
      ctx.font = "10px " + color("--mono");
      ctx.fillText(`M${i}`, px + Math.sign(px - cx) * 16, py + Math.sign(py - cy) * 10);
    });
  }

  // the chip's four beams
  control.azimuth.forEach((azimuth, i) => {
    if (azimuth === null) return;
    const e = energyLevel(control.energy[i]);
    const speaking = control.energy[i] > 0;
    const length = R * (speaking ? clamp(0.3 + e / 7, 0.3, 1) : 0.28);
    ctx.strokeStyle = BEAM_COLORS[i];
    ctx.globalAlpha = speaking ? 1 : 0.35;
    ctx.lineWidth = i === 3 ? 5 : 2.5;
    ctx.setLineDash(speaking ? [] : [4, 4]);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(...at(azimuth, length));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = BEAM_COLORS[i];
    ctx.beginPath();
    ctx.arc(...at(azimuth, length), i === 3 ? 6 : 4, 0, 2 * Math.PI);
    ctx.fill();
  });
  ctx.globalAlpha = 1;

  // processed DoA (speech-gated) as a pointer on the rim
  const selected = control.selected[0];
  if (selected !== null) {
    const [x, y] = at(selected, R - 2);
    const [lx, ly] = at(selected + 0.09, R - 22);
    const [rx, ry] = at(selected - 0.09, R - 22);
    ctx.fillStyle = color("--selected");
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(lx, ly);
    ctx.lineTo(rx, ry);
    ctx.fill();
  }

  // host SRP peak marker
  const lastSrp = srpHistory[srpHistory.length - 1];
  if (lastSrp && lastSrp.activity > 0.2) {
    const a = lastSrp.angle;
    ctx.strokeStyle = color("--srp");
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(...at(a, R + 1));
    ctx.lineTo(...at(a, R + 14));
    ctx.stroke();
  }

  const [doa, speech] = control.doa;
  const srpText = lastSrp && lastSrp.activity > 0.2 ? `${Math.round(bearingDeg(lastSrp.angle))}°` : "–";
  $("doa-readout").innerHTML =
    `<b style="color:${speech ? color("--ok") : color("--muted")}">${Math.round(bearingDeg(doa * Math.PI / 180))}°</b>\n` +
    `${speech ? "speech" : "no speech"} · chip ${doa}°\n` +
    `processed ${selected === null ? "–" : Math.round(bearingDeg(selected)) + "°"}\n` +
    `host SRP ${srpText}`;
}

function drawTimeAxes(ctx, w, h, pad, yTicks, yLabel) {
  ctx.strokeStyle = color("--grid");
  ctx.fillStyle = color("--muted");
  ctx.font = "10px " + color("--mono");
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const [value, y] of yTicks) {
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.fillText(yLabel(value), pad.l - 6, y);
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let s = 0; s <= HISTORY_SECONDS; s += 5) {
    const x = w - pad.r - s / HISTORY_SECONDS * (w - pad.l - pad.r);
    ctx.fillText(s ? `-${s}s` : "now", x, h - pad.b + 4);
  }
}

function drawAzimuth() {
  const { ctx, w, h } = setupCanvas($("azimuth"));
  const pad = { l: 40, r: 8, t: 6, b: 18 };
  const now = performance.now();
  const X = t => w - pad.r - (now - t) / (HISTORY_SECONDS * 1000) * (w - pad.l - pad.r);
  const Y = d => pad.t + (1 - d / 360) * (h - pad.t - pad.b);
  ctx.clearRect(0, 0, w, h);
  drawTimeAxes(ctx, w, h, pad, [0, 90, 180, 270, 360].map(d => [d, Y(d)]), d => `${d}°`);

  for (const s of srpHistory) {
    if (s.activity < 0.05) continue;
    ctx.fillStyle = color("--srp");
    ctx.globalAlpha = 0.15 + 0.6 * s.activity;
    ctx.fillRect(X(s.t) - 1, Y(bearingDeg(s.angle)) - 1, 2, 2);
  }
  for (const c of controlHistory) {
    c.azimuth.forEach((a, i) => {
      if (a === null) return;
      const speaking = c.energy[i] > 0;
      ctx.globalAlpha = speaking ? clamp(0.3 + energyLevel(c.energy[i]) / 7, 0.3, 1) : 0.12;
      ctx.fillStyle = BEAM_COLORS[i];
      const r = i === 3 ? 2.5 : 1.8;
      ctx.beginPath();
      ctx.arc(X(c.t), Y(bearingDeg(a)), r, 0, 2 * Math.PI);
      ctx.fill();
    });
    if (c.selected !== null) {
      ctx.globalAlpha = 1;
      ctx.fillStyle = color("--selected");
      ctx.fillRect(X(c.t) - 1.5, Y(bearingDeg(c.selected)) - 1.5, 3, 3);
    }
  }
  ctx.globalAlpha = 1;
}

function drawEnergy() {
  const { ctx, w, h } = setupCanvas($("energy"));
  const pad = { l: 40, r: 8, t: 6, b: 18 };
  const now = performance.now();
  const top = 7;
  const X = t => w - pad.r - (now - t) / (HISTORY_SECONDS * 1000) * (w - pad.l - pad.r);
  const Y = v => pad.t + (1 - clamp(v / top, 0, 1)) * (h - pad.t - pad.b);
  ctx.clearRect(0, 0, w, h);
  drawTimeAxes(ctx, w, h, pad, [0, 2, 4, 6].map(v => [v, Y(v)]), v => `${v}`);
  for (let i = 0; i < 4; i++) {
    ctx.strokeStyle = BEAM_COLORS[i];
    ctx.lineWidth = i === 3 ? 2.5 : 1.5;
    ctx.beginPath();
    controlHistory.forEach((c, k) => {
      const x = X(c.t), y = Y(energyLevel(c.energy[i]));
      k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }
}

function drawMeters() {
  const { ctx, w, h } = setupCanvas($("meters"));
  const now = performance.now();
  const pad = { l: 34, r: 4, t: 8, b: 180 };
  const n = 6;
  const slot = (w - pad.l - pad.r) / n;
  const barW = Math.min(28, slot * 0.55);
  const Y = db => pad.t + (1 - (clamp(db, METER_FLOOR, 0) - METER_FLOOR) / -METER_FLOOR) * (h - pad.t - pad.b);
  ctx.clearRect(0, 0, w, h);

  ctx.font = "10px " + color("--mono");
  ctx.fillStyle = color("--muted");
  ctx.strokeStyle = color("--grid");
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let db = 0; db >= METER_FLOOR; db -= 20) {
    ctx.beginPath();
    ctx.moveTo(pad.l, Y(db));
    ctx.lineTo(w - pad.r, Y(db));
    ctx.stroke();
    ctx.fillText(db, pad.l - 6, Y(db));
  }

  const gradient = ctx.createLinearGradient(0, Y(0), 0, Y(METER_FLOOR));
  gradient.addColorStop(0, color("--bad"));
  gradient.addColorStop(0.12, color("--warn"));
  gradient.addColorStop(0.3, color("--ok"));
  gradient.addColorStop(1, "#1b5e3a");

  for (let ch = 0; ch < n; ch++) {
    const m = meterState[ch];
    if (now - m.peakAt > PEAK_HOLD_MS) {
      m.peak = Math.max(m.rms, m.peak - PEAK_FALL_DB_PER_S / 60);
    }
    const x = pad.l + slot * ch + (slot - barW) / 2;
    ctx.fillStyle = "#0a0d12";
    ctx.fillRect(x, Y(0), barW, Y(METER_FLOOR) - Y(0));
    ctx.fillStyle = gradient;
    ctx.fillRect(x, Y(m.rms), barW, Y(METER_FLOOR) - Y(m.rms));
    ctx.fillStyle = color("--text");
    ctx.fillRect(x - 2, Y(m.peak) - 1, barW + 4, 2);

    ctx.save();
    ctx.translate(x + barW / 2, Y(METER_FLOOR) + 8);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = color("--text");
    ctx.fillText(`ch${ch}`, 0, 0);
    ctx.fillStyle = color("--muted");
    ctx.fillText(m.rms.toFixed(0), 0, 13);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillStyle = CHANNEL_COLORS[ch];
    ctx.fillText(info ? info.labels[ch] : "", -30, 0);
    ctx.restore();
  }
}

function drawScope() {
  const { ctx, w, h } = setupCanvas($("scope"));
  const pad = { l: 240, r: 8 };
  const rowH = h / 6;
  const plotW = w - pad.l - pad.r;
  ctx.clearRect(0, 0, w, h);

  for (let ch = 0; ch < 6; ch++) {
    const lo = scope.min[ch], hi = scope.max[ch];
    let peak = 0;
    for (let i = 0; i < scope.length; i++) peak = Math.max(peak, -lo[i], hi[i]);
    // settle the auto-gain smoothly so the trace does not pump every frame
    const target = clamp(0.9 / Math.max(peak, 1 / 32768), 1, 4096);
    scope.gain[ch] += (target - scope.gain[ch]) * 0.08;
    const gain = scope.gain[ch];

    const mid = rowH * ch + rowH / 2;
    ctx.strokeStyle = color("--grid");
    ctx.beginPath();
    ctx.moveTo(pad.l, mid);
    ctx.lineTo(w - pad.r, mid);
    ctx.stroke();
    if (ch) {
      ctx.strokeStyle = color("--border");
      ctx.beginPath();
      ctx.moveTo(0, rowH * ch);
      ctx.lineTo(w, rowH * ch);
      ctx.stroke();
    }

    ctx.fillStyle = CHANNEL_COLORS[ch];
    ctx.font = "12px " + color("--mono");
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(`ch${ch} ${info ? info.labels[ch] : ""}`, 0, mid - 8);
    ctx.fillStyle = color("--muted");
    ctx.font = "10px " + color("--mono");
    const gainDb = 20 * Math.log10(gain);
    ctx.fillText(`auto-gain +${gainDb.toFixed(0)} dB · peak ${(20 * Math.log10(peak + 1e-9)).toFixed(0)} dBFS`, 0, mid + 8);

    ctx.fillStyle = CHANNEL_COLORS[ch];
    const perPixel = scope.length / plotW;
    for (let px = 0; px < plotW; px++) {
      const start = Math.floor(px * perPixel), end = Math.max(start + 1, Math.floor((px + 1) * perPixel));
      let mn = 0, mx = 0;
      for (let k = start; k < end; k++) {
        const idx = (scope.write + k) % scope.length;
        if (lo[idx] < mn) mn = lo[idx];
        if (hi[idx] > mx) mx = hi[idx];
      }
      const top = mid - clamp(mx * gain, -1, 1) * rowH * 0.45;
      const bottom = mid - clamp(mn * gain, -1, 1) * rowH * 0.45;
      ctx.fillRect(pad.l + px, top, 1, Math.max(1, bottom - top));
    }
  }
}

function updateStats() {
  const now = performance.now();
  for (const list of [rates.frames, rates.polls]) while (list.length && list[0] < now - 2000) list.shift();
  $("audio-rate").textContent = `${(rates.frames.length / 2).toFixed(0)} fps`;
  $("poll-rate").textContent = `${(rates.polls.length / 2).toFixed(0)} Hz`;
  if (control.agc_gain !== undefined) $("agc-gain").textContent = `${control.agc_gain.toFixed(1)}×`;
  if (control.aec_converged !== undefined) {
    $("aec").textContent = control.aec_converged ? "converged" : "adapting";
  }
  $("status").classList.toggle("live", rates.frames.length > 0);
}

function render() {
  drawPolar();
  drawAzimuth();
  drawEnergy();
  drawMeters();
  drawScope();
  spectrograms.forEach(s => s.draw());
  updateStats();
  requestAnimationFrame(render);
}

// ---------- connection ----------

let socket = null;

function connect() {
  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "info") onInfo(message);
    else if (message.type === "frame") onFrame(message);
  };
  socket.onclose = () => setTimeout(connect, 1000);
}

function syncViewControls() {
  $("flip").checked = view.flip;
  for (const button of $("rotation").querySelectorAll("button")) {
    button.classList.toggle("active", Number(button.dataset.rotation) === view.rotation);
  }
}

$("flip").addEventListener("change", event => {
  view.flip = event.target.checked;
  saveView();
  syncViewControls();
});

$("rotation").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  view.rotation = Number(button.dataset.rotation);
  saveView();
  syncViewControls();
});

$("mic-category").addEventListener("change", event => {
  socket.send(JSON.stringify({ type: "mic_category", category: Number(event.target.value) }));
});

syncViewControls();
connect();
requestAnimationFrame(render);
