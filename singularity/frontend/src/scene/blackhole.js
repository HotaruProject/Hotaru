export const BLACK_HOLE_VERTEX_SOURCE = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

export const BLACK_HOLE_FRAGMENT_SOURCE = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

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
const float B_CRIT = 2.598;
const float FLUX_NORM = 476.6;
const float FLOW = 12.0;
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
  if (r < 1.05 || r > DISK_OUT) return vec4(0.0);

  float omega = 2.2 * pow(max(r, 1.2), -1.5);
  float base = atan(hit.z, hit.x);
  float lr = log(max(r, 1.02));
  float c1 = fract(uTime / FLOW);
  float c2 = fract(uTime / FLOW + 0.5);
  float n1 = streaks(base + c1 * FLOW * omega + 1.4 * lr, lr, c1 * 3.0);
  float n2 = streaks(base + c2 * FLOW * omega + 1.4 * lr, lr, c2 * 3.0 + 11.0);
  float n = mix(n1, n2, abs(c1 * 2.0 - 1.0));
  float lanes = 0.12 + 1.25 * smoothstep(0.32, 0.8, n);
  lanes = mix(lanes, 0.85, 0.35 * step(0.5, order));

  float iscoFlux = (1.0 - sqrt(ISCO / max(r, ISCO))) / (r * r * r) * FLUX_NORM;
  float plungeMask = smoothstep(ISCO + 0.3, 1.2, r);
  float spiralStream = sin(base * 4.0 + 7.5 / r - uTime * 3.2);
  float plungeFlux = exp(-pow(r - 1.35, 2.0) * 2.8) * (0.35 + 0.65 * spiralStream * spiralStream) * 2.2;
  float flux = mix(iscoFlux, plungeFlux, plungeMask);

  float temp = pow(max(flux, 0.0), 0.25);

  float v = inversesqrt(max(2.0 * (r - 1.0), 0.15));
  v = min(v, 0.94);
  vec3 vel = normalize(vec3(-hit.z, 0.0, hit.x)) * v;
  float gamma = inversesqrt(max(1.0 - v * v, 0.04));
  float g = sqrt(max(1.0 - 1.5 / r, 0.06)) / (gamma * max(1.0 + dot(vel, dir), 0.12));

  vec3 col = glow(temp * g) * flux * g * g * g * g * lanes * 2.6;
  float inner = mix(0.35, 1.6, step(0.5, order));
  float edge = smoothstep(1.06, 1.4, r) * smoothstep(DISK_OUT, DISK_OUT - 6.0, r);
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
  bool escaped = false;
  float impact = 100.0;
  float minR = 100.0;
  vec3 minRPos = pos;

  bool startsInside = (c <= 0.0);
  bool hitsSphere = (disc > 0.0 && -b - sqrt(disc) > 0.0);

  if (startsInside || hitsSphere) {
    if (!startsInside) {
      pos += dir * (-b - sqrt(disc));
    }
    vec3 hv = cross(pos, dir);
    float h2 = dot(hv, hv);
    impact = sqrt(h2);

    int crossings = 0;
    for (int i = 0; i < MAX_STEPS; i++) {
      float r2 = dot(pos, pos);
      float r = sqrt(r2);
      if (r < minR) {
        minR = r;
        minRPos = pos;
      }
      if (r < 1.0) { captured = true; break; }
      if (r > R_OUT + 0.1 && dot(pos, dir) > 0.0) { escaped = true; break; }

      float r2safe = max(r2, 0.09);
      float rsafe = sqrt(r2safe);
      float dt = max(r < 9.0 ? 0.14 * rsafe * clamp(rsafe - 1.05, 0.25, 1.0) : 0.35 * rsafe, 0.02);
      vec3 prev = pos;
      vec3 vh = dir + (-0.75 * dt * h2 / (r2safe * r2safe * rsafe)) * pos;
      pos += dt * vh;
      float r2n = max(dot(pos, pos), 0.09);
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
  } else {
    escaped = true;
  }

  float rim = smoothstep(B_CRIT - 1.5 * pix, B_CRIT, impact);
  if (!captured || rim > 0.0) {
    col += rim * sky(normalize(dir), smoothstep(B_CRIT + 0.05, B_CRIT + 0.7, impact)) * (1.0 - alpha);
  }

  float ringW = 0.02 + 2.0 * pix;
  float ringX = max(impact - B_CRIT, 0.0) / ringW;
  col += vec3(0.36, 0.38, 0.42) * exp(-ringX * ringX) * (1.0 - 0.8 * alpha) * rim;

  if (camDist < 2.6) {
    float throatIntensity = smoothstep(2.6, 0.2, camDist);
    float angle = atan(uv.y, uv.x);
    float rad = length(uv);

    float stNoise = noise(vec2(angle * 7.0 + uTime * 0.35, rad * 9.0 - uTime * 6.5));
    float streaks = smoothstep(0.42, 0.88, stNoise) * exp(-abs(rad - 0.72) * 2.8);

    float rings = 0.5 + 0.5 * sin(rad * 36.0 - uTime * 14.0 - camDist * 10.0);
    float ringGlow = smoothstep(0.72, 0.99, rings) * exp(-rad * 1.5);

    float coreFlash = exp(-camDist * 4.2) * exp(-rad * rad * 14.0);

    vec3 tunnelCol = vec3(0.92, 0.96, 1.0) * streaks * 2.2
                   + vec3(1.15, 1.20, 1.25) * ringGlow * 1.6
                   + vec3(1.9, 1.95, 2.0) * coreFlash * 3.8;

    col += tunnelCol * throatIntensity;
  }

  col = col * (2.51 * col + 0.03) / (col * (2.43 * col + 0.59) + 0.14);
  gl_FragColor = vec4(pow(clamp(col, 0.0, 1.0), vec3(1.0 / 2.2)), 1.0);
}
`;

export const BRIGHT_SOURCE = `
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

export const BLUR_SOURCE = `
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

export const COMPOSITE_SOURCE = `
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
  vec3 c = texture2D(uScene, vUv).rgb + texture2D(uBloom, vUv).rgb * 0.95;
  c = 1.0 - (1.0 - clamp(c, 0.0, 1.0)) * (1.0 - clamp(c - 1.0, 0.0, 1.0));
  c += (hash12(gl_FragCoord.xy + fract(uTime) * 61.0) - 0.5) / 160.0;
  gl_FragColor = vec4(c, 1.0);
}
`;

export function makeNoiseTexture(gl) {
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

export function makeTarget(gl) {
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
