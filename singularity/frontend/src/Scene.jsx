import React from 'react';

const VERT_SRC = `
attribute vec2 aPos;
void main() {
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

const FRAG_SRC = `
#ifdef GL_FRAGMENT_PRECISION_HIGH
precision highp float;
#else
precision mediump float;
#endif

uniform vec2 uRes;
uniform float uTime;
uniform float uYaw;
uniform float uPitch;
uniform float uDist;

float hash13(vec3 p3) {
  p3 = fract(p3 * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float hash12(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

vec3 hash33(vec3 p3) {
  p3 = fract(p3 * vec3(0.1031, 0.1030, 0.0973));
  p3 += dot(p3, p3.yxz + 33.33);
  return fract((p3.xxy + p3.yxx) * p3.zyx);
}

float noise2(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  float a = hash12(i);
  float b = hash12(i + vec2(1.0, 0.0));
  float c = hash12(i + vec2(0.0, 1.0));
  float d = hash12(i + vec2(1.0, 1.0));
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm2(vec2 p) {
  float v = 0.0;
  float amp = 0.52;
  for (int i = 0; i < 4; i++) {
    v += amp * noise2(p);
    p = p * 2.15 + 7.1;
    amp *= 0.48;
  }
  return v;
}

vec3 backgroundStars(vec3 dir) {
  vec3 col = vec3(0.0003, 0.0004, 0.0008);

  vec3 pStar = dir * 200.0;
  vec3 iStar = floor(pStar);
  vec3 fStar = fract(pStar) - 0.5;
  float rnd = hash13(iStar);
  if (rnd > 0.985) {
    vec3 offset = (hash33(iStar) - 0.5) * 0.65;
    float dist = length(fStar - offset);
    float size = 0.045 + 0.055 * fract(rnd * 137.1);
    float glow = exp(-dist * dist / (size * size * 0.20));
    float twinkle = 0.75 + 0.25 * sin(uTime * 2.5 + rnd * 60.0);
    vec3 starCol = mix(vec3(0.95, 0.98, 1.0), vec3(0.75, 0.90, 1.0), fract(rnd * 43.1));
    if (fract(rnd * 89.2) > 0.85) starCol = vec3(1.0, 0.92, 0.82);
    col += starCol * glow * twinkle * 2.2;
  }

  return col;
}

vec4 sampleDisk(vec3 hit, vec3 rayDir) {
  float rad = length(hit.xz);
  float innerR = 1.88;
  float outerR = 7.6;
  if (rad < innerR || rad > outerR) return vec4(0.0);

  float speed = 1.55 / (sqrt(rad) + 0.25);
  float angle = -uTime * speed;
  float ca = cos(angle), sa = sin(angle);
  vec2 rotP = vec2(ca * hit.x - sa * hit.z, sa * hit.x + ca * hit.z);

  float turb = fbm2(rotP * 0.95);
  float bands = 0.35 * sin(rad * 22.0 + turb * 2.8 - uTime * 0.2)
              + 0.25 * sin(rad * 46.0)
              + 0.20 * sin(rad * 8.5)
              + 0.45;
  float edgeIn = smoothstep(innerR, innerR + 0.35, rad);
  float edgeOut = smoothstep(outerR, outerR - 1.5, rad);
  float density = edgeIn * edgeOut * (0.35 + 0.65 * turb) * (0.5 + 0.5 * bands);

  vec3 diskVelocity = normalize(vec3(-hit.z, 0.0, hit.x));
  float doppler = dot(diskVelocity, -rayDir);
  float dopplerFactor = pow(clamp(1.0 + 0.62 * doppler, 0.15, 2.7), 3.5);

  vec3 colHot = vec3(1.35, 1.40, 1.48);
  vec3 colMid = vec3(0.96, 0.98, 1.05);
  vec3 colWarm = vec3(0.85, 0.82, 0.78);
  vec3 colOuter = vec3(0.55, 0.35, 0.18);

  vec3 col = mix(colOuter, colWarm, smoothstep(0.0, 0.35, density));
  col = mix(col, colMid, smoothstep(0.3, 0.7, density));
  col = mix(col, colHot, smoothstep(0.65, 0.98, density));

  float innerGlow = smoothstep(innerR + 0.95, innerR, rad);
  col += colHot * innerGlow * innerGlow * 2.4;

  col *= dopplerFactor * 1.95;
  float alpha = smoothstep(0.015, 0.32, density);
  return vec4(col, alpha);
}

vec3 traceScene(vec2 uv) {
  vec3 lookAt = vec3(0.0, 0.0, 0.0);
  vec3 camPos = lookAt + uDist * vec3(
    cos(uPitch) * sin(uYaw),
    sin(uPitch),
    -cos(uPitch) * cos(uYaw)
  );
  vec3 fwd = normalize(lookAt - camPos);
  vec3 right = normalize(cross(fwd, vec3(0.0, 1.0, 0.0)));
  vec3 up = cross(right, fwd);
  float fov = 0.85;
  vec3 dir = normalize(fwd + uv.x * fov * right + uv.y * fov * up);

  vec3 pos = camPos;
  vec3 hvec = cross(pos, dir);
  float h2 = dot(hvec, hvec);
  float eventR = 1.0;
  float photonR = 1.52;

  vec4 diskAccum = vec4(0.0);
  bool captured = false;
  float minR = 1000.0;
  int diskCrossings = 0;

  for (int i = 0; i < 360; i++) {
    vec3 prevPos = pos;
    float r = length(pos);
    minR = min(minR, r);

    float dt = clamp(r * 0.042, 0.005, 0.85);
    float r2safe = max(dot(pos, pos), 0.55);
    vec3 acc = -1.65 * h2 * pos / pow(r2safe, 2.5);
    dir = normalize(dir + acc * dt);
    pos += dir * dt;

    if (r < eventR) {
      captured = true;
      break;
    }

    if (prevPos.y * pos.y < 0.0 && diskAccum.a < 0.98) {
      float t = prevPos.y / (prevPos.y - pos.y);
      vec3 hit = mix(prevPos, pos, t);
      vec4 dc = sampleDisk(hit, dir);
      if (diskCrossings >= 2) {
        dc.rgb *= 0.35;
        dc.a *= 0.35;
      }
      diskAccum.rgb += dc.rgb * dc.a * (1.0 - diskAccum.a);
      diskAccum.a += dc.a * (1.0 - diskAccum.a);
      diskCrossings++;
    }

    if (r > 40.0) {
      break;
    }
  }

  vec3 col = diskAccum.rgb;
  if (!captured) {
    col += backgroundStars(normalize(dir)) * (1.0 - diskAccum.a);
    float ringGlow = exp(-abs(minR - photonR) * 22.0);
    col += vec3(1.35, 1.40, 1.48) * ringGlow * 2.9;
    float ringHalo = exp(-abs(minR - photonR) * 4.2);
    col += vec3(0.96, 0.98, 1.05) * ringHalo * 0.45;
  }

  return col;
}

void main() {
  float minDim = min(uRes.x, uRes.y);
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / minDim;
  vec3 col = traceScene(uv);
  col = col / (1.0 + col);
  col = pow(col, vec3(0.86));
  float vig = 1.0 - 0.20 * smoothstep(0.4, 1.4, length(uv));
  col *= vig;
  gl_FragColor = vec4(col, 1.0);
}
`;

function compileShader(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error(log);
  }
  return sh;
}

function createProgram(gl, vsSrc, fsSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(prog);
    gl.deleteProgram(prog);
    throw new Error(log);
  }
  return prog;
}

export const Scene = React.memo(React.forwardRef(function Scene({ reducedMotion }, ref) {
  const canvasRef = React.useRef(null);
  const directorRef = React.useRef({
    yaw: 0.0,
    pitch: 0.18,
    dist: 26.0,
    autoRotate: !reducedMotion,
    yawVel: 0,
    pitchVel: 0,
    animating: false,
    from: { yaw: 0, pitch: 0.18, dist: 26.0 },
    to: { yaw: 0, pitch: 0.18, dist: 26.0 },
    t0: 0,
    duration: 1200,
  });

  React.useImperativeHandle(ref, () => ({
    flyTo: (preset, duration = 1200) => {
      const d = directorRef.current;
      d.from = { yaw: d.yaw, pitch: d.pitch, dist: d.dist };
      d.to = { yaw: preset.yaw, pitch: preset.pitch, dist: preset.dist };
      d.t0 = performance.now();
      d.duration = duration;
      d.animating = true;
      d.autoRotate = false;
      d.yawVel = 0;
      d.pitchVel = 0;
    },
    recenter: () => {
      const d = directorRef.current;
      d.from = { yaw: d.yaw, pitch: d.pitch, dist: d.dist };
      d.to = { yaw: 0.0, pitch: 0.18, dist: 26.0 };
      d.t0 = performance.now();
      d.duration = 1000;
      d.animating = true;
      d.autoRotate = !reducedMotion;
      d.yawVel = 0;
      d.pitchVel = 0;
    }
  }));

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) return;

    let program;
    try {
      program = createProgram(gl, VERT_SRC, FRAG_SRC);
    } catch (e) {
      console.error('Shader compile error:', e);
      return;
    }
    gl.useProgram(program);

    const quad = new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);

    const aPos = gl.getAttribLocation(program, 'aPos');
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(program, 'uRes');
    const uTime = gl.getUniformLocation(program, 'uTime');
    const uYaw = gl.getUniformLocation(program, 'uYaw');
    const uPitch = gl.getUniformLocation(program, 'uPitch');
    const uDist = gl.getUniformLocation(program, 'uDist');

    const RENDER_SCALE = 1.0;
    let width = 0, height = 0, dpr = 1;
    let rafId = null;
    const startTime = performance.now();

    function resize() {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(2.0, Math.max(1, window.devicePixelRatio || 1));
      width = Math.max(1, Math.floor((rect.width || window.innerWidth) * dpr * RENDER_SCALE));
      height = Math.max(1, Math.floor((rect.height || window.innerHeight) * dpr * RENDER_SCALE));
      canvas.width = width;
      canvas.height = height;
      gl.viewport(0, 0, width, height);
    }

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();

    let dragging = false;
    let lastX = 0, lastY = 0;
    let pinchStart = null;

    function onPointerDown(e) {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      dragging = true;
      const d = directorRef.current;
      d.animating = false;
      d.autoRotate = false;
      lastX = e.clientX;
      lastY = e.clientY;
      canvas.style.cursor = 'grabbing';
    }

    function onPointerMove(e) {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;

      const d = directorRef.current;
      const sens = 0.0050;
      d.yawVel = -dx * sens;
      d.pitchVel = dy * sens;
      d.yaw += d.yawVel;
      d.pitch = Math.max(-1.42, Math.min(1.42, d.pitch + d.pitchVel));
    }

    function onPointerUp() {
      dragging = false;
      canvas.style.cursor = 'grab';
    }

    function onWheel(e) {
      e.preventDefault();
      const d = directorRef.current;
      d.animating = false;
      const delta = Math.sign(e.deltaY) * 1.5;
      d.dist = Math.max(12.0, Math.min(48.0, d.dist + delta));
    }

    function onTouchStart(e) {
      if (e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        pinchStart = Math.hypot(dx, dy);
      }
    }

    function onTouchMove(e) {
      if (e.touches.length === 2 && pinchStart) {
        const dx = e.touches[0].clientX - e.touches[1].clientX;
        const dy = e.touches[0].clientY - e.touches[1].clientY;
        const currentDist = Math.hypot(dx, dy);
        const factor = (pinchStart - currentDist) * 0.055;
        const d = directorRef.current;
        d.dist = Math.max(12.0, Math.min(48.0, d.dist + factor));
        pinchStart = currentDist;
      }
    }

    function onTouchEnd(e) {
      if (e.touches.length < 2) pinchStart = null;
    }

    canvas.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('touchstart', onTouchStart, { passive: true });
    canvas.addEventListener('touchmove', onTouchMove, { passive: false });
    canvas.addEventListener('touchend', onTouchEnd, { passive: true });
    canvas.style.cursor = 'grab';

    function draw(now) {
      const d = directorRef.current;
      if (d.animating) {
        const elapsed = now - d.t0;
        const p = Math.min(1.0, elapsed / d.duration);
        const k = 1 - Math.pow(1 - p, 4);
        d.yaw = d.from.yaw + (d.to.yaw - d.from.yaw) * k;
        d.pitch = d.from.pitch + (d.to.pitch - d.from.pitch) * k;
        d.dist = d.from.dist + (d.to.dist - d.from.dist) * k;
        if (p >= 1.0) {
          d.animating = false;
          d.yaw = d.to.yaw;
          d.pitch = d.to.pitch;
          d.dist = d.to.dist;
        }
      } else if (!dragging) {
        d.yawVel *= 0.92;
        d.pitchVel *= 0.92;
        d.yaw += d.yawVel;
        d.pitch = Math.max(-1.42, Math.min(1.42, d.pitch + d.pitchVel));
        if (d.autoRotate) d.yaw += 0.0009;
      }

      const time = reducedMotion ? 4.2 : (now - startTime) / 1000;
      gl.uniform2f(uRes, width, height);
      gl.uniform1f(uTime, time);
      gl.uniform1f(uYaw, d.yaw);
      gl.uniform1f(uPitch, d.pitch);
      gl.uniform1f(uDist, d.dist);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      if (!reducedMotion) {
        rafId = requestAnimationFrame(draw);
      }
    }

    rafId = requestAnimationFrame(draw);

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
      canvas.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('wheel', onWheel);
      canvas.removeEventListener('touchstart', onTouchStart);
      canvas.removeEventListener('touchmove', onTouchMove);
      canvas.removeEventListener('touchend', onTouchEnd);
      gl.deleteProgram(program);
      gl.deleteBuffer(buf);
    };
  }, [reducedMotion]);

  return <canvas ref={canvasRef} className="observatory-canvas" role="img" aria-label="Black hole viewport" />;
}));
