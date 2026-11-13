import React, { memo, forwardRef, useRef, useImperativeHandle, useEffect } from 'react';

const VERT_SRC = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

const FRAG_SRC = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

// Schwarzschild black hole, rs = 1. Thin Novikov-Thorne disk from the ISCO (r = 3)
// out to DISK_OUT, its brightness set by the disk temperature, Doppler-beamed and gravitationally
// redshifted. Rays are integrated with an adaptive leapfrog step.

uniform vec2 uRes;
uniform vec2 uCenter;
uniform float uScale;
uniform float uTime;
uniform vec3 uCamPos;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
uniform vec3 uCamFwd;
uniform sampler2D uNoise;

const float ISCO = 3.0;
const float DISK_OUT = 12.0;
const float R_OUT = 17.0;
const float B_CRIT = 2.598;      // critical impact parameter, 3 sqrt(3) / 2
const float FLUX_NORM = 476.6;   // 1 / peak of (1 - sqrt(3/r)) / r^3, at r ~ 4.08
const float FLOW = 12.0;         // seconds per flow cycle, see shadeDisk
const int MAX_STEPS = 96;

float noise(vec2 p) {
  return texture2D(uNoise, p * (1.0 / 64.0)).r;
}

vec3 glow(float t) {
  float v = smoothstep(0.05, 1.35, t);
  return vec3(mix(0.1, 1.0, v * v));
}

float streaks(float a, float lr, float phase) {
  vec2 q = vec2(a * 10.186, lr * 22.0 + phase);
  float n = noise(q) * 0.45
    + noise(vec2(a * 20.372, lr * 54.0 - phase * 1.7)) * 0.33
    + noise(vec2(a * 40.744, lr * 130.0 + phase * 2.3)) * 0.22;
  return n;
}

vec4 shadeDisk(vec3 hit, vec3 dir, float order) {
  float r = length(hit.xz);
  if (r < ISCO * 0.98 || r > DISK_OUT) return vec4(0.0);

  float omega = 2.2 * pow(r, -1.5);
  float base = atan(hit.z, hit.x);
  float lr = log(r);
  float c1 = fract(uTime / FLOW);
  float c2 = fract(uTime / FLOW + 0.5);
  float n1 = streaks(base + c1 * FLOW * omega + 1.4 * lr, lr, c1 * 3.0);
  float n2 = streaks(base + c2 * FLOW * omega + 1.4 * lr, lr, c2 * 3.0 + 11.0);
  float n = mix(n1, n2, abs(c1 * 2.0 - 1.0));
  float lanes = 0.12 + 1.25 * smoothstep(0.32, 0.8, n);
  lanes = mix(lanes, 0.85, 0.35 * step(0.5, order));

  float flux = (1.0 - sqrt(ISCO / max(r, ISCO))) / (r * r * r) * FLUX_NORM;
  float temp = pow(max(flux, 0.0), 0.25);

  float v = inversesqrt(2.0 * (r - 1.0));
  vec3 vel = normalize(vec3(-hit.z, 0.0, hit.x)) * v;
  float gamma = inversesqrt(1.0 - v * v);
  float g = sqrt(1.0 - 1.5 / r) / (gamma * (1.0 + dot(vel, dir)));

  vec3 col = glow(temp * g) * flux * g * g * g * g * lanes * 2.6;
  float inner = mix(0.35, 1.6, step(0.5, order));
  float edge = smoothstep(ISCO, ISCO + inner, r) * smoothstep(DISK_OUT, DISK_OUT - 6.0, r);
  return vec4(col * edge, edge * mix(0.97, 0.75, step(0.5, order)));
}

float hash13(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.zyx + 31.32);
  return fract((p.x + p.y) * p.z);
}

vec3 sky(vec3 d, float starFade) {
  vec3 p = d * 120.0;
  vec3 cell = floor(p);
  vec3 f = fract(p) - 0.5;
  float h = hash13(cell);
  float s = 0.0;
  if (h > 0.982) {
    vec3 o = vec3(hash13(cell + 3.1), hash13(cell + 7.7), hash13(cell + 1.9)) - 0.5;
    float k = (h - 0.982) * 55.6;
    vec3 q = f - o * 0.55;
    s = exp(-dot(q, q) * 180.0) * (0.1 + 1.6 * k * k * k);
  }
  float lat = dot(d, vec3(0.28, 0.87, 0.41));
  float dust = noise(d.xz * 9.0 + d.y * 5.0) * 0.6 + noise(d.yx * 23.0) * 0.4;
  float band = exp(-lat * lat * 26.0) * (0.25 + 0.9 * dust * dust);
  return vec3(s * starFade + band * 0.09);
}

void main() {
  float minDim = min(uRes.x, uRes.y);
  vec2 uv = (gl_FragCoord.xy - uCenter * uRes) / minDim * uScale;
  float camDist = length(uCamPos);
  float pix = uScale / minDim * 0.9 * camDist;

  vec3 dir = normalize(uCamFwd + uv.x * 0.9 * uCamRight + uv.y * 0.9 * uCamUp);
  vec3 pos = uCamPos;

  float b = dot(pos, dir);
  float c = dot(pos, pos) - R_OUT * R_OUT;
  float disc = b * b - c;
  vec3 col = vec3(0.0);
  float alpha = 0.0;
  bool captured = false;
  bool escaped = disc <= 0.0;
  float impact = 100.0;

  if (disc > 0.0 && -b - sqrt(disc) > 0.0) {
    pos += dir * (-b - sqrt(disc));
    vec3 hv = cross(pos, dir);
    float h2 = dot(hv, hv);
    impact = sqrt(h2);

    int crossings = 0;
    for (int i = 0; i < MAX_STEPS; i++) {
      float r2 = dot(pos, pos);
      float r = sqrt(r2);
      if (r < 1.0) { captured = true; break; }
      if (r > R_OUT + 0.1 && dot(pos, dir) > 0.0) { escaped = true; break; }

      float dt = max(r < 9.0 ? 0.14 * r * clamp(r - 1.1, 0.35, 1.0) : 0.35 * r, 0.02);
      vec3 prev = pos;
      vec3 vh = dir + (-0.75 * dt * h2 / (r2 * r2 * r)) * pos;
      pos += dt * vh;
      float r2n = dot(pos, pos);
      dir = vh + (-0.75 * dt * h2 / (r2n * r2n * sqrt(r2n))) * pos;

      if (prev.y * pos.y < 0.0 && crossings < 3) {
        float t = prev.y / (prev.y - pos.y);
        vec4 d = shadeDisk(mix(prev, pos, t), normalize(dir), float(crossings));
        float fadeW = 0.02 + 3.0 * pix;
        if (crossings > 0) d *= smoothstep(B_CRIT + 0.03, B_CRIT + 0.03 + 2.0 * fadeW, impact);
        crossings++;
        col += d.rgb * (1.0 - alpha);
        alpha += d.a * (1.0 - alpha);
        if (alpha > 0.985) { escaped = true; break; }
      }
    }
    if (!escaped || impact < B_CRIT) captured = true;
  }

  float rim = smoothstep(B_CRIT - 1.5 * pix, B_CRIT, impact);
  if (!captured || rim > 0.0) col += rim * sky(normalize(dir), smoothstep(B_CRIT + 0.05, B_CRIT + 0.7, impact)) * (1.0 - alpha);

  float ringW = 0.02 + 2.0 * pix;
  float ringX = max(impact - B_CRIT, 0.0) / ringW;
  col += vec3(0.32) * exp(-ringX * ringX) * (1.0 - 0.8 * alpha) * rim;

  col = col * (2.51 * col + 0.03) / (col * (2.43 * col + 0.59) + 0.14);
  gl_FragColor = vec4(pow(clamp(col, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}
`;

const BRIGHT_SRC = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uTexel;
void main() {
  vec3 c = texture2D(uTex, vUv + uTexel * vec2(-1.0, -1.0)).rgb
    + texture2D(uTex, vUv + uTexel * vec2(1.0, -1.0)).rgb
    + texture2D(uTex, vUv + uTexel * vec2(-1.0, 1.0)).rgb
    + texture2D(uTex, vUv + uTexel * vec2(1.0, 1.0)).rgb;
  c *= 0.25;
  float l = max(c.r, max(c.g, c.b));
  gl_FragColor = vec4(c * smoothstep(0.4, 1.0, l), 1.0);
}
`;

const BLUR_SRC = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uDir;
void main() {
  vec3 c = texture2D(uTex, vUv).rgb * 0.227;
  c += (texture2D(uTex, vUv + uDir * 1.385).rgb + texture2D(uTex, vUv - uDir * 1.385).rgb) * 0.316;
  c += (texture2D(uTex, vUv + uDir * 3.231).rgb + texture2D(uTex, vUv - uDir * 3.231).rgb) * 0.07;
  gl_FragColor = vec4(c, 1.0);
}
`;

const COMPOSITE_SRC = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform float uTime;
float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
void main() {
  vec3 c = texture2D(uScene, vUv).rgb + texture2D(uBloom, vUv).rgb * 0.9;
  c = 1.0 - (1.0 - clamp(c, 0.0, 1.0)) * (1.0 - clamp(c - 1.0, 0.0, 1.0));
  c += (hash12(gl_FragCoord.xy + fract(uTime) * 61.0) - 0.5) / 160.0;
  gl_FragColor = vec4(c, 1.0);
}
`;

function compileShader(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const err = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error(err || 'Shader compile failed');
  }
  return sh;
}

function createProgram(gl, vsSrc, fsSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.bindAttribLocation(prog, 0, 'aPos');
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const err = gl.getProgramInfoLog(prog);
    gl.deleteProgram(prog);
    throw new Error(err || 'Program link failed');
  }
  return prog;
}

function makeNoiseTexture(gl) {
  const N = 64;
  const grid = 8;
  const lattice = new Float32Array(grid * grid);
  let seed = 1337;
  for (let i = 0; i < lattice.length; i++) {
    seed = (seed * 16807) % 2147483647;
    lattice[i] = seed / 2147483647;
  }
  const at = (x, y) => lattice[((y % grid) + grid) % grid * grid + (((x % grid) + grid) % grid)];
  const fade = (t) => t * t * (3 - 2 * t);
  const data = new Uint8Array(N * N);
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      let v = 0;
      let amp = 0.5;
      let freq = 1;
      for (let o = 0; o < 3; o++) {
        const fx = (x / N) * grid * freq;
        const fy = (y / N) * grid * freq;
        const ix = Math.floor(fx);
        const iy = Math.floor(fy);
        const ux = fade(fx - ix);
        const uy = fade(fy - iy);
        const a = at(ix, iy) * (1 - ux) + at(ix + 1, iy) * ux;
        const b = at(ix, iy + 1) * (1 - ux) + at(ix + 1, iy + 1) * ux;
        v += amp * (a * (1 - uy) + b * uy);
        amp *= 0.5;
        freq *= 2;
      }
      data[y * N + x] = Math.round((v / 0.875) * 255);
    }
  }
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, N, N, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, data);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
  return tex;
}

function makeTarget(gl) {
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  const fb = gl.createFramebuffer();
  const t = { tex, fb, w: 0, h: 0 };
  t.size = (w, h) => {
    if (t.w === w && t.h === h) return;
    t.w = w;
    t.h = h;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
  };
  t.free = () => {
    gl.deleteTexture(tex);
    gl.deleteFramebuffer(fb);
  };
  return t;
}

const norm = (v) => {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
};
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function rotate(v, k, ang) {
  const c = Math.cos(ang);
  const s = Math.sin(ang);
  const kv = cross(k, v);
  const d = dot(k, v) * (1 - c);
  return [v[0] * c + kv[0] * s + k[0] * d, v[1] * c + kv[1] * s + k[1] * d, v[2] * c + kv[2] * s + k[2] * d];
}

function frameFromAngles(yaw, pitch) {
  const cp = Math.cos(pitch);
  const back = [cp * Math.sin(yaw), Math.sin(pitch), -cp * Math.cos(yaw)];
  const right = norm(cross([0, 1, 0], back));
  const up = cross(back, right);
  return { right, up, back };
}

const QUALITY = [0.32, 0.42, 0.54, 0.68, 0.82, 0.95];

export const Scene = memo(forwardRef(function Scene({ reducedMotion }, ref) {
  const canvasRef = useRef(null);
  const renderRef = useRef(null);
  const stateRef = useRef({
    ...frameFromAngles(0.0, 0.18),
    yaw: 0.0,
    pitch: 0.18,
    dist: 26.0,
    isDragging: false,
    lastX: 0,
    lastY: 0,
    velX: 0,
    velY: 0,
    animating: false,
    from: { yaw: 0.0, pitch: 0.18, dist: 26.0 },
    to: { yaw: 0.0, pitch: 0.18, dist: 26.0 },
    t0: 0,
    duration: 1200,
  });

  useImperativeHandle(ref, () => ({
    flyTo: (preset, duration = 1200) => {
      const st = stateRef.current;
      st.from = { yaw: st.yaw, pitch: st.pitch, dist: st.dist };
      st.to = { yaw: preset.yaw, pitch: preset.pitch, dist: preset.dist };
      st.t0 = performance.now();
      st.duration = duration;
      st.animating = true;
      st.velX = 0;
      st.velY = 0;
      renderRef.current?.wake();
    },
    recenter: () => {
      const st = stateRef.current;
      st.from = { yaw: st.yaw, pitch: st.pitch, dist: st.dist };
      st.to = { yaw: 0.0, pitch: 0.18, dist: 26.0 };
      st.t0 = performance.now();
      st.duration = 1000;
      st.animating = true;
      st.velX = 0;
      st.velY = 0;
      renderRef.current?.wake();
    }
  }));

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    let gl;
    try {
      gl = canvas.getContext('webgl', {
        powerPreference: 'high-performance',
        alpha: false,
        depth: false,
        stencil: false,
        antialias: false,
        preserveDrawingBuffer: false
      });
    } catch {
      return;
    }
    if (!gl) return;

    let progs;
    try {
      progs = {
        scene: createProgram(gl, VERT_SRC, FRAG_SRC),
        bright: createProgram(gl, VERT_SRC, BRIGHT_SRC),
        blur: createProgram(gl, VERT_SRC, BLUR_SRC),
        comp: createProgram(gl, VERT_SRC, COMPOSITE_SRC),
      };
    } catch (err) {
      console.error('Shader program compilation failed:', err);
      return;
    }

    const posBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    const loc = {};
    Object.entries(progs).forEach(([key, p]) => {
      loc[key] = {};
      ['uRes', 'uCenter', 'uScale', 'uTime', 'uCamPos', 'uCamRight', 'uCamUp', 'uCamFwd', 'uNoise', 'uTex', 'uTexel', 'uDir', 'uScene', 'uBloom'].forEach((n) => {
        loc[key][n] = gl.getUniformLocation(p, n);
      });
    });

    const noiseTex = makeNoiseTexture(gl);
    const sceneT = makeTarget(gl);
    const bloomA = makeTarget(gl);
    const bloomB = makeTarget(gl);

    const coarse = window.matchMedia('(pointer: coarse)').matches;
    let quality = coarse ? 1 : 2;
    let frameSum = 0;
    let frameCount = 0;
    let lastDraw = 0;
    const FRAME_MIN = 1000 / 61;

    let visible = true;
    let running = false;
    let animId = null;
    let t0 = performance.now();

    const pass = (prog, target, w, h) => {
      gl.useProgram(prog);
      gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fb : null);
      gl.viewport(0, 0, w, h);
    };
    const bindTex = (unit, tex, uniform) => {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.uniform1i(uniform, unit);
    };

    const turn = (a, b) => {
      const st = stateRef.current;
      let { right, up, back } = st;
      if (a) {
        back = rotate(back, up, a);
        right = rotate(right, up, a);
      }
      if (b) {
        back = rotate(back, right, b);
        up = rotate(up, right, b);
      }
      back = norm(back);
      right = norm(cross(up, back));
      up = cross(back, right);
      st.right = right;
      st.up = up;
      st.back = back;
      st.pitch = Math.asin(Math.max(-1.0, Math.min(1.0, back[1])));
      st.yaw = Math.atan2(back[0], -back[2]);
    };

    const render = (now) => {
      if (now - lastDraw < FRAME_MIN) {
        animId = requestAnimationFrame(render);
        return;
      }
      const dt = Math.min((now - t0) * 0.001, 0.1);
      t0 = now;
      if (lastDraw) {
        frameSum += now - lastDraw;
        frameCount++;
        if (frameCount === 20) {
          const avg = frameSum / frameCount;
          if (avg > 34 && quality > 1) quality -= 2;
          else if (avg > 21 && quality > 0) quality--;
          else if (avg < 17.5 && quality < QUALITY.length - 1) quality++;
          frameSum = 0;
          frameCount = 0;
        }
      }
      lastDraw = now;

      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const outW = Math.max(1, Math.round(rect.width * Math.min(dpr, 1.5)));
      const outH = Math.max(1, Math.round(rect.height * Math.min(dpr, 1.5)));
      const sw = Math.max(1, Math.round(rect.width * dpr * QUALITY[quality]));
      const sh = Math.max(1, Math.round(rect.height * dpr * QUALITY[quality]));
      const bw = Math.max(1, Math.round(sw / 4));
      const bh = Math.max(1, Math.round(sh / 4));

      if (canvas.width !== outW || canvas.height !== outH) {
        canvas.width = outW;
        canvas.height = outH;
      }
      sceneT.size(sw, sh);
      bloomA.size(bw, bh);
      bloomB.size(bw, bh);

      const st = stateRef.current;
      if (st.animating) {
        const elapsed = now - st.t0;
        const p = reducedMotion ? 1.0 : Math.min(1.0, elapsed / st.duration);
        const k = 1 - Math.pow(1 - p, 4);
        st.yaw = st.from.yaw + (st.to.yaw - st.from.yaw) * k;
        st.pitch = st.from.pitch + (st.to.pitch - st.from.pitch) * k;
        st.dist = st.from.dist + (st.to.dist - st.from.dist) * k;
        const frame = frameFromAngles(st.yaw, st.pitch);
        st.right = frame.right;
        st.up = frame.up;
        st.back = frame.back;
        if (p >= 1.0) {
          st.animating = false;
          st.yaw = st.to.yaw;
          st.pitch = st.to.pitch;
          st.dist = st.to.dist;
        }
      } else if (!st.isDragging) {
        const damp = Math.pow(0.93, dt * 60);
        st.velX *= damp;
        st.velY *= damp;
        let yawRate = st.velX;
        if (!reducedMotion) yawRate += 0.05;
        turn(yawRate * dt, st.velY * dt);
      }

      const pos = st.back.map((v) => v * st.dist);
      const fwd = st.back.map((v) => -v);
      const center = [0.5, 0.5];
      const scale = 1.0 * (st.dist / 26.0);

      const S = loc.scene;
      pass(progs.scene, sceneT, sw, sh);
      gl.uniform2f(S.uRes, sw, sh);
      gl.uniform1f(S.uTime, (now * 0.001) % 3600);
      gl.uniform3fv(S.uCamPos, pos);
      gl.uniform3fv(S.uCamRight, st.right);
      gl.uniform3fv(S.uCamUp, st.up);
      gl.uniform3fv(S.uCamFwd, fwd);
      gl.uniform2f(S.uCenter, center[0], center[1]);
      gl.uniform1f(S.uScale, scale);
      bindTex(0, noiseTex, S.uNoise);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      pass(progs.bright, bloomA, bw, bh);
      gl.uniform2f(loc.bright.uTexel, 1 / sw, 1 / sh);
      bindTex(0, sceneT.tex, loc.bright.uTex);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      for (let i = 0; i < 2; i++) {
        pass(progs.blur, bloomB, bw, bh);
        gl.uniform2f(loc.blur.uDir, (1 + i) / bw, 0);
        bindTex(0, bloomA.tex, loc.blur.uTex);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
        pass(progs.blur, bloomA, bw, bh);
        gl.uniform2f(loc.blur.uDir, 0, (1 + i) / bh);
        bindTex(0, bloomB.tex, loc.blur.uTex);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }

      pass(progs.comp, null, outW, outH);
      gl.uniform1f(loc.comp.uTime, (now * 0.001) % 3600);
      bindTex(0, sceneT.tex, loc.comp.uScene);
      bindTex(1, bloomA.tex, loc.comp.uBloom);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      const moving = st.animating || st.isDragging || Math.abs(st.velX) > 0.002 || Math.abs(st.velY) > 0.002;
      if (visible && (!reducedMotion || moving)) {
        animId = requestAnimationFrame(render);
      } else {
        running = false;
      }
    };

    const start = () => {
      if (running || !visible) return;
      running = true;
      t0 = performance.now();
      lastDraw = 0;
      animId = requestAnimationFrame(render);
    };
    renderRef.current = { wake: start };

    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) start();
    });
    observer.observe(canvas);

    const onVisibility = () => {
      visible = !document.hidden;
      if (visible) start();
    };
    document.addEventListener('visibilitychange', onVisibility);

    const onContextLost = (e) => {
      e.preventDefault();
      if (animId) cancelAnimationFrame(animId);
      running = false;
      visible = false;
    };
    canvas.addEventListener('webglcontextlost', onContextLost);
    start();

    const SENS = 0.0055;
    const dragTo = (x, y) => {
      const st = stateRef.current;
      const dx = x - st.lastX;
      const dy = y - st.lastY;
      st.lastX = x;
      st.lastY = y;
      turn(-dx * SENS, dy * SENS);
      st.velX = -dx * SENS * 35;
      st.velY = dy * SENS * 35;
    };
    const beginDrag = (x, y) => {
      const st = stateRef.current;
      st.animating = false;
      st.isDragging = true;
      st.lastX = x;
      st.lastY = y;
      st.velX = 0;
      st.velY = 0;
      canvas.style.cursor = 'grabbing';
      start();
    };
    const zoomBy = (k) => {
      const st = stateRef.current;
      st.animating = false;
      st.dist = Math.max(12.0, Math.min(48.0, st.dist * k));
      start();
    };

    const onPointerDown = (e) => {
      if (e.pointerType === 'touch' || (e.button !== 0 && e.pointerType === 'mouse')) return;
      beginDrag(e.clientX, e.clientY);
    };
    const onPointerMove = (e) => {
      if (e.pointerType === 'touch' || !stateRef.current.isDragging) return;
      dragTo(e.clientX, e.clientY);
    };
    const onPointerUp = (e) => {
      if (e.pointerType === 'touch') return;
      stateRef.current.isDragging = false;
      canvas.style.cursor = 'grab';
    };

    let pinch = 0;
    const touchDist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onTouchStart = (e) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        pinch = touchDist(e.touches);
        stateRef.current.isDragging = false;
        return;
      }
      const t = e.touches[0];
      e.preventDefault();
      beginDrag(t.clientX, t.clientY);
    };
    const onTouchMove = (e) => {
      if (e.touches.length === 2 && pinch) {
        e.preventDefault();
        const d = touchDist(e.touches);
        if (d > 0) zoomBy(pinch / d);
        pinch = d;
        return;
      }
      if (!stateRef.current.isDragging) return;
      e.preventDefault();
      dragTo(e.touches[0].clientX, e.touches[0].clientY);
    };
    const onTouchEnd = (e) => {
      if (e.touches.length < 2) pinch = 0;
      if (e.touches.length === 0) {
        stateRef.current.isDragging = false;
        canvas.style.cursor = 'grab';
      }
    };

    const onWheel = (e) => {
      e.preventDefault();
      if (e.deltaY) zoomBy(Math.exp(e.deltaY * 0.0015));
    };

    canvas.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('touchstart', onTouchStart, { passive: false });
    canvas.addEventListener('touchmove', onTouchMove, { passive: false });
    canvas.addEventListener('touchend', onTouchEnd);
    canvas.addEventListener('touchcancel', onTouchEnd);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.style.cursor = 'grab';

    return () => {
      renderRef.current = null;
      if (animId) cancelAnimationFrame(animId);
      observer.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      canvas.removeEventListener('webglcontextlost', onContextLost);
      canvas.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('touchstart', onTouchStart);
      canvas.removeEventListener('touchmove', onTouchMove);
      canvas.removeEventListener('touchend', onTouchEnd);
      canvas.removeEventListener('touchcancel', onTouchEnd);
      canvas.removeEventListener('wheel', onWheel);
      gl.deleteBuffer(posBuf);
      gl.deleteTexture(noiseTex);
      [sceneT, bloomA, bloomB].forEach((t) => t.free());
      Object.values(progs).forEach((p) => gl.deleteProgram(p));
    };
  }, [reducedMotion]);

  return <canvas ref={canvasRef} className="observatory-canvas" role="img" aria-label="Black hole viewport" />;
}));
