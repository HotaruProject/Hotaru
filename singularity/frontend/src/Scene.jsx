import React, { useEffect, useRef } from "react";
import {
  buildProjectorBeamVertices,
  buildSatelliteMesh,
  getTerminalPlaneQuad,
} from "./satelliteMesh";

const BH_VERT_SRC = `
attribute vec2 aPos;
void main() {
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

const BH_FRAG_SRC = `
precision highp float;

uniform vec2 uRes;
uniform float uTime;
uniform vec3 uCamPos;
uniform vec3 uCamFwd;
uniform vec3 uCamRight;
uniform vec3 uCamUp;

#define PI 3.14159265359
#define MAX_STEPS 128

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

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash12(i + vec2(0.0, 0.0)), hash12(i + vec2(1.0, 0.0)), f.x),
    mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), f.x),
    f.y
  );
}

float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.5;
  mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 5; i++) {
    v += a * noise(p);
    p = rot * p * 2.04 + vec2(17.1, 31.7);
    a *= 0.5;
  }
  return v;
}

vec3 backgroundStars(vec3 dir) {
  vec3 col = vec3(0.004, 0.005, 0.008);

  float band = exp(-abs(dir.y) * 5.0);
  float dust = fbm(dir.xz * 4.5 + vec2(dir.y * 2.0, 0.0));
  col += vec3(0.022, 0.025, 0.032) * band * dust;

  vec3 p = dir * 170.0;
  vec3 id = floor(p);
  vec3 f = fract(p) - 0.5;
  vec3 rnd = hash33(id);
  vec3 starOffset = (rnd - 0.5) * 0.72;
  float d = length(f - starOffset);

  float starProb = step(0.972, rnd.x);
  float brightness = smoothstep(0.16, 0.0, d) * starProb;
  float twinkle = 0.85 + 0.15 * sin(uTime * 2.0 + rnd.y * 40.0);

  vec3 starTint = mix(vec3(0.75, 0.80, 0.90), vec3(1.0, 0.96, 0.90), rnd.z);
  col += starTint * brightness * twinkle * 1.35;

  vec3 p2 = dir * 75.0;
  vec3 id2 = floor(p2);
  vec3 f2 = fract(p2) - 0.5;
  vec3 rnd2 = hash33(id2 + 19.3);
  float d2 = length(f2 - (rnd2 - 0.5) * 0.6);
  float brightStar = smoothstep(0.11, 0.0, d2) * step(0.994, rnd2.x);
  col += vec3(0.92, 0.95, 1.0) * brightStar * 2.2;

  return col;
}

vec4 sampleDisk(vec3 hitPos, vec3 rayDir) {
  float r = length(hitPos.xz);
  float innerR = 2.05;
  float outerR = 7.6;

  if (r < innerR || r > outerR) {
    return vec4(0.0);
  }

  float radial = (r - innerR) / (outerR - innerR);
  float fadeInner = smoothstep(0.0, 0.09, radial);
  float fadeOuter = pow(1.0 - smoothstep(0.22, 1.0, radial), 1.75);
  float radialMask = fadeInner * fadeOuter;

  float angle = atan(hitPos.z, hitPos.x);
  float angularSpeed = 1.65 / pow(r, 1.15);
  float spiralAngle = angle + uTime * angularSpeed - log(r) * 3.8;

  vec2 polarUv = vec2(r * 2.3, spiralAngle * 1.5);
  float streaks = fbm(vec2(r * 3.8, spiralAngle * 2.0));
  float fineRings = 0.55 + 0.45 * sin(r * 16.0 - streaks * 4.5);
  float clouds = fbm(polarUv * vec2(1.4, 0.8));

  float density = radialMask * mix(0.45, 1.15, streaks * 0.65 + clouds * 0.35) * fineRings;

  vec3 tangent = normalize(vec3(-hitPos.z, 0.0, hitPos.x));
  float doppler = dot(tangent, -rayDir);
  float beaming = clamp(1.0 + doppler * 0.68, 0.28, 2.15);
  beaming = pow(beaming, 1.65);

  float temp = pow(1.0 - radial, 0.75);
  vec3 innerHot = vec3(1.0, 0.99, 0.97);
  vec3 midSilver = vec3(0.80, 0.84, 0.90);
  vec3 outerAsh = vec3(0.32, 0.35, 0.40);

  vec3 baseCol = mix(outerAsh, midSilver, smoothstep(0.0, 0.55, temp));
  baseCol = mix(baseCol, innerHot, smoothstep(0.55, 0.95, temp));

  vec3 shiftTint = mix(vec3(0.82, 0.78, 0.74), vec3(0.94, 0.97, 1.04), clamp(doppler * 0.5 + 0.5, 0.0, 1.0));
  vec3 emission = baseCol * shiftTint * density * beaming * 2.35;
  float alpha = clamp(density * 0.92, 0.0, 0.96);

  return vec4(emission, alpha);
}

vec3 traceScene(vec3 ro, vec3 rd) {
  vec3 pos = ro;
  vec3 dir = rd;

  vec3 accumCol = vec3(0.0);
  float transmittance = 1.0;
  float minR = 1e5;
  bool captured = false;

  const float rs = 1.0;

  for (int i = 0; i < MAX_STEPS; i++) {
    float r = length(pos);
    minR = min(minR, r);

    if (r < rs * 1.02) {
      captured = true;
      break;
    }

    if (r > 36.0 && dot(pos, dir) > 0.0) {
      break;
    }

    float dt = clamp(0.045 * (r - 0.82), 0.028, 0.62);
    float invR = 1.0 / max(r, 0.001);
    vec3 accel = -1.5 * rs * pos * (invR * invR * invR * invR);

    vec3 nextDir = normalize(dir + accel * dt * 2.15);
    vec3 nextPos = pos + 0.5 * (dir + nextDir) * dt;

    if (pos.y * nextPos.y <= 0.0) {
      float denom = pos.y - nextPos.y;
      float frac = abs(denom) > 1e-5 ? clamp(pos.y / denom, 0.0, 1.0) : 0.5;
      vec3 crossPos = mix(pos, nextPos, frac);

      vec4 disk = sampleDisk(crossPos, nextDir);
      accumCol += transmittance * disk.rgb;
      transmittance *= (1.0 - disk.a);

      if (transmittance < 0.015) {
        break;
      }
    }

    pos = nextPos;
    dir = nextDir;
  }

  float photonRing = exp(-pow((minR - 1.52) / 0.085, 2.0));
  accumCol += transmittance * vec3(0.92, 0.95, 1.0) * photonRing * 0.85;

  float halo = exp(-max(minR - 1.45, 0.0) * 1.15);
  accumCol += vec3(0.18, 0.20, 0.25) * halo * 0.22;

  if (!captured) {
    accumCol += transmittance * backgroundStars(dir);
  }

  return accumCol;
}

void main() {
  float minDim = min(uRes.x, uRes.y);
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / minDim;

  float fov = 0.85;
  vec3 dir = normalize(uCamFwd + uv.x * fov * uCamRight + uv.y * fov * uCamUp);

  vec3 col = traceScene(uCamPos, dir);

  col = (col * (2.51 * col + 0.03)) / (col * (2.43 * col + 0.59) + 0.14);
  col = pow(clamp(col, 0.0, 1.0), vec3(0.92));

  vec2 q = gl_FragCoord.xy / uRes;
  float vig = pow(16.0 * q.x * q.y * (1.0 - q.x) * (1.0 - q.y), 0.16);
  col *= mix(0.55, 1.0, vig);

  gl_FragColor = vec4(col, 1.0);
}
`;

const MESH_VERT_SRC = `
precision highp float;

attribute vec3 aPosition;
attribute vec3 aNormal;
attribute vec3 aColor;
attribute float aEmissive;

uniform vec2 uRes;
uniform vec3 uCamPos;
uniform vec3 uCamFwd;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
uniform vec3 uProbeOffset;

varying vec3 vWorldPos;
varying vec3 vNormal;
varying vec3 vColor;
varying float vEmissive;

void main() {
  vec3 worldPos = aPosition + uProbeOffset;
  vWorldPos = worldPos;
  vNormal = aNormal;
  vColor = aColor;
  vEmissive = aEmissive;

  vec3 rel = worldPos - uCamPos;
  float xCam = dot(rel, uCamRight);
  float yCam = dot(rel, uCamUp);
  float zCam = dot(rel, uCamFwd);

  float minDim = min(uRes.x, uRes.y);
  float fov = 0.85;
  float clipX = (xCam / fov) * (2.0 * minDim / uRes.x);
  float clipY = (yCam / fov) * (2.0 * minDim / uRes.y);

  float near = 0.12;
  float far = 80.0;
  float clipZ = ((far + near) / (far - near)) * zCam - (2.0 * far * near) / (far - near);

  gl_Position = vec4(clipX, clipY, clipZ, zCam);
}
`;

const MESH_FRAG_SRC = `
precision highp float;

uniform vec3 uCamPos;
uniform float uTime;

varying vec3 vWorldPos;
varying vec3 vNormal;
varying vec3 vColor;
varying float vEmissive;

void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(uCamPos - vWorldPos);

  vec3 toSingularity = normalize(-vWorldPos);
  float diffDisk = max(dot(N, toSingularity), 0.0);

  vec3 fillDir = normalize(vec3(0.35, 0.78, -0.52));
  float diffFill = max(dot(N, fillDir), 0.0);

  float fresnel = pow(1.0 - max(dot(N, V), 0.0), 2.6);
  vec3 H = normalize(toSingularity + V);
  float spec = pow(max(dot(N, H), 0.0), 28.0);

  vec3 lit = vColor * (0.20 + diffDisk * 0.92 + diffFill * 0.36);
  lit += vec3(0.88, 0.92, 0.98) * spec * 0.42;
  lit += vec3(0.55, 0.78, 0.95) * fresnel * 0.30;

  float pulse = 0.90 + 0.10 * sin(uTime * 4.2);
  vec3 emissiveGlow = vColor * vEmissive * 1.45 * pulse;

  vec3 col = clamp(lit + emissiveGlow, 0.0, 1.0);
  gl_FragColor = vec4(col, 1.0);
}
`;

const BEAM_VERT_SRC = `
precision highp float;

attribute vec3 aPosition;
attribute float aAlpha;

uniform vec2 uRes;
uniform vec3 uCamPos;
uniform vec3 uCamFwd;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
uniform vec3 uProbeOffset;

varying float vAlpha;

void main() {
  vec3 worldPos = aPosition + uProbeOffset;
  vAlpha = aAlpha;

  vec3 rel = worldPos - uCamPos;
  float xCam = dot(rel, uCamRight);
  float yCam = dot(rel, uCamUp);
  float zCam = dot(rel, uCamFwd);

  float minDim = min(uRes.x, uRes.y);
  float fov = 0.85;
  float clipX = (xCam / fov) * (2.0 * minDim / uRes.x);
  float clipY = (yCam / fov) * (2.0 * minDim / uRes.y);

  float near = 0.12;
  float far = 80.0;
  float clipZ = ((far + near) / (far - near)) * zCam - (2.0 * far * near) / (far - near);

  gl_Position = vec4(clipX, clipY, clipZ, zCam);
}
`;

const BEAM_FRAG_SRC = `
precision highp float;

uniform float uTime;
varying float vAlpha;

void main() {
  float shimmer = 0.88 + 0.12 * sin(uTime * 6.0);
  vec3 beamCol = vec3(0.46, 0.88, 1.0) * shimmer;
  gl_FragColor = vec4(beamCol, vAlpha);
}
`;

const PLANE_VERT_SRC = `
precision highp float;

attribute vec3 aPosition;
attribute vec2 aUv;

uniform vec2 uRes;
uniform vec3 uCamPos;
uniform vec3 uCamFwd;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
uniform vec3 uProbeOffset;

varying vec2 vUv;

void main() {
  vec3 worldPos = aPosition + uProbeOffset;
  vUv = aUv;

  vec3 rel = worldPos - uCamPos;
  float xCam = dot(rel, uCamRight);
  float yCam = dot(rel, uCamUp);
  float zCam = dot(rel, uCamFwd);

  float minDim = min(uRes.x, uRes.y);
  float fov = 0.85;
  float clipX = (xCam / fov) * (2.0 * minDim / uRes.x);
  float clipY = (yCam / fov) * (2.0 * minDim / uRes.y);

  float near = 0.12;
  float far = 80.0;
  float clipZ = ((far + near) / (far - near)) * zCam - (2.0 * far * near) / (far - near);

  gl_Position = vec4(clipX, clipY, clipZ, zCam);
}
`;

const PLANE_FRAG_SRC = `
precision highp float;

uniform sampler2D uTex;
uniform float uTime;

varying vec2 vUv;

void main() {
  vec4 texel = texture2D(uTex, vUv);
  gl_FragColor = vec4(texel.rgb, texel.a * 0.97);
}
`;

const BLIT_FRAG_SRC = `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uRes;
void main() {
  vec2 uv = gl_FragCoord.xy / uRes;
  gl_FragColor = texture2D(uTex, uv);
}
`;

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`WebGL shader compile error: ${info}`);
  }
  return shader;
}

function linkProgram(gl, vertSrc, fragSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vertSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fragSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(prog);
    gl.deleteProgram(prog);
    throw new Error(`WebGL program link error: ${info}`);
  }
  return prog;
}

export default function Scene({
  director,
  projector,
  onBlackHoleClick,
  onBackgroundClickInSatellite,
  onProjectionBoundsChange,
}) {
  const canvasRef = useRef(null);
  const callbacksRef = useRef({
    onBlackHoleClick,
    onBackgroundClickInSatellite,
    onProjectionBoundsChange,
  });
  callbacksRef.current = {
    onBlackHoleClick,
    onBackgroundClickInSatellite,
    onProjectionBoundsChange,
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !director || !projector) return undefined;

    const gl =
      canvas.getContext("webgl", {
        antialias: false,
        alpha: false,
        depth: true,
        powerPreference: "high-performance",
      }) || canvas.getContext("experimental-webgl");

    if (!gl) {
      return undefined;
    }

    let isSoftwareGl = false;
    try {
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      const renderer = dbg
        ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || "")
        : String(gl.getParameter(gl.RENDERER) || "");
      isSoftwareGl = /swiftshader|llvmpipe|software|softpipe/i.test(renderer);
    } catch {
      isSoftwareGl = false;
    }

    const bhFragSource = isSoftwareGl
      ? BH_FRAG_SRC.replace("#define MAX_STEPS 128", "#define MAX_STEPS 48")
          .replace(
            "float dt = clamp(0.045 * (r - 0.82), 0.028, 0.62);",
            "float dt = clamp(0.088 * (r - 0.78), 0.052, 1.05);"
          )
          .replace("for (int i = 0; i < 5; i++)", "for (int i = 0; i < 3; i++)")
      : BH_FRAG_SRC;
    const bhProg = linkProgram(gl, BH_VERT_SRC, bhFragSource);
    const blitProg = isSoftwareGl ? linkProgram(gl, BH_VERT_SRC, BLIT_FRAG_SRC) : null;
    const meshProg = linkProgram(gl, MESH_VERT_SRC, MESH_FRAG_SRC);
    const beamProg = linkProgram(gl, BEAM_VERT_SRC, BEAM_FRAG_SRC);
    const planeProg = linkProgram(gl, PLANE_VERT_SRC, PLANE_FRAG_SRC);

    let bhFbo = null;
    let bhFboTex = null;
    let bhFboW = 0;
    let bhFboH = 0;
    if (isSoftwareGl) {
      bhFbo = gl.createFramebuffer();
      bhFboTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, bhFboTex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    const quadBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW
    );

    const satMesh = buildSatelliteMesh();
    const satBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, satBuf);
    gl.bufferData(gl.ARRAY_BUFFER, satMesh.vertexData, gl.STATIC_DRAW);

    const beamBuf = gl.createBuffer();
    const planeBuf = gl.createBuffer();

    const terminalTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, terminalTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    projector.renderToCanvas(Date.now());
    if (projector.canvas) {
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        projector.canvas
      );
    }

    const bhLoc = {
      aPos: gl.getAttribLocation(bhProg, "aPos"),
      uRes: gl.getUniformLocation(bhProg, "uRes"),
      uTime: gl.getUniformLocation(bhProg, "uTime"),
      uCamPos: gl.getUniformLocation(bhProg, "uCamPos"),
      uCamFwd: gl.getUniformLocation(bhProg, "uCamFwd"),
      uCamRight: gl.getUniformLocation(bhProg, "uCamRight"),
      uCamUp: gl.getUniformLocation(bhProg, "uCamUp"),
    };

    const meshLoc = {
      aPosition: gl.getAttribLocation(meshProg, "aPosition"),
      aNormal: gl.getAttribLocation(meshProg, "aNormal"),
      aColor: gl.getAttribLocation(meshProg, "aColor"),
      aEmissive: gl.getAttribLocation(meshProg, "aEmissive"),
      uRes: gl.getUniformLocation(meshProg, "uRes"),
      uCamPos: gl.getUniformLocation(meshProg, "uCamPos"),
      uCamFwd: gl.getUniformLocation(meshProg, "uCamFwd"),
      uCamRight: gl.getUniformLocation(meshProg, "uCamRight"),
      uCamUp: gl.getUniformLocation(meshProg, "uCamUp"),
      uProbeOffset: gl.getUniformLocation(meshProg, "uProbeOffset"),
      uTime: gl.getUniformLocation(meshProg, "uTime"),
    };

    const beamLoc = {
      aPosition: gl.getAttribLocation(beamProg, "aPosition"),
      aAlpha: gl.getAttribLocation(beamProg, "aAlpha"),
      uRes: gl.getUniformLocation(beamProg, "uRes"),
      uCamPos: gl.getUniformLocation(beamProg, "uCamPos"),
      uCamFwd: gl.getUniformLocation(beamProg, "uCamFwd"),
      uCamRight: gl.getUniformLocation(beamProg, "uCamRight"),
      uCamUp: gl.getUniformLocation(beamProg, "uCamUp"),
      uProbeOffset: gl.getUniformLocation(beamProg, "uProbeOffset"),
      uTime: gl.getUniformLocation(beamProg, "uTime"),
    };

    const planeLoc = {
      aPosition: gl.getAttribLocation(planeProg, "aPosition"),
      aUv: gl.getAttribLocation(planeProg, "aUv"),
      uRes: gl.getUniformLocation(planeProg, "uRes"),
      uCamPos: gl.getUniformLocation(planeProg, "uCamPos"),
      uCamFwd: gl.getUniformLocation(planeProg, "uCamFwd"),
      uCamRight: gl.getUniformLocation(planeProg, "uCamRight"),
      uCamUp: gl.getUniformLocation(planeProg, "uCamUp"),
      uProbeOffset: gl.getUniformLocation(planeProg, "uProbeOffset"),
      uTex: gl.getUniformLocation(planeProg, "uTex"),
      uTime: gl.getUniformLocation(planeProg, "uTime"),
    };

    const blitLoc = blitProg
      ? {
          aPos: gl.getAttribLocation(blitProg, "aPos"),
          uTex: gl.getUniformLocation(blitProg, "uTex"),
          uRes: gl.getUniformLocation(blitProg, "uRes"),
        }
      : null;

    const resize = () => {
      const rawDpr = window.devicePixelRatio || 1;
      const dpr = isSoftwareGl
        ? window.innerWidth <= 600
          ? Math.min(rawDpr, 1.5)
          : 1.0
        : Math.min(rawDpr, 2.0);
      const w = Math.max(1, Math.floor(window.innerWidth * dpr));
      const h = Math.max(1, Math.floor(window.innerHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
        gl.viewport(0, 0, w, h);
        if (isSoftwareGl && bhFbo && bhFboTex) {
          bhFboW = Math.max(64, Math.floor(w * 0.18));
          bhFboH = Math.max(64, Math.floor(h * 0.18));
          gl.bindTexture(gl.TEXTURE_2D, bhFboTex);
          gl.texImage2D(
            gl.TEXTURE_2D,
            0,
            gl.RGBA,
            bhFboW,
            bhFboH,
            0,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            null
          );
          gl.bindFramebuffer(gl.FRAMEBUFFER, bhFbo);
          gl.framebufferTexture2D(
            gl.FRAMEBUFFER,
            gl.COLOR_ATTACHMENT0,
            gl.TEXTURE_2D,
            bhFboTex,
            0
          );
          gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        }
      }
    };

    resize();
    window.addEventListener("resize", resize);

    const drag = {
      down: false,
      moved: false,
      startX: 0,
      startY: 0,
      lastX: 0,
      lastY: 0,
    };

    const onPointerDown = (e) => {
      if (director.isInputLocked) return;
      drag.down = true;
      drag.moved = false;
      drag.startX = e.clientX;
      drag.startY = e.clientY;
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
    };

    const onPointerMove = (e) => {
      if (!drag.down) return;
      if (director.isInputLocked) {
        drag.down = false;
        return;
      }
      const dx = e.clientX - drag.lastX;
      const dy = e.clientY - drag.lastY;
      if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) > 6) {
        drag.moved = true;
      }
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      if (director.currentShot === "overview" && drag.moved) {
        director.applyOverviewOrbit(-dx * 0.0045, dy * 0.0035, 0);
      }
    };

    const onPointerUp = (e) => {
      if (!drag.down) return;
      const wasClick = !drag.moved;
      drag.down = false;
      if (!wasClick || director.isInputLocked) return;

      const vw = window.innerWidth;
      const vh = window.innerHeight;
      if (director.currentShot === "overview") {
        if (director.isClickNearBlackHole(e.clientX, e.clientY, vw, vh)) {
          callbacksRef.current.onBlackHoleClick?.();
        }
      } else if (director.currentShot === "satellite") {
        callbacksRef.current.onBackgroundClickInSatellite?.();
      }
    };

    const onWheel = (e) => {
      e.preventDefault();
      if (director.isInputLocked || director.currentShot !== "overview") return;
      director.applyOverviewOrbit(0, 0, e.deltaY * 0.012);
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    let rafId = 0;
    let softTimerId = 0;
    const startPerf = performance.now();
    let lastBoundsKey = "";

    const renderFrame = (nowPerf) => {
      const elapsedSec = (nowPerf - startPerf) * 0.001;
      const basis = director.step(nowPerf);

      const probeOffset = [
        Math.sin(elapsedSec * 0.9) * 0.014,
        Math.cos(elapsedSec * 1.15) * 0.022,
        0.0,
      ];

      if (projector.renderToCanvas(Date.now()) && projector.canvas) {
        gl.bindTexture(gl.TEXTURE_2D, terminalTex);
        gl.texImage2D(
          gl.TEXTURE_2D,
          0,
          gl.RGBA,
          gl.RGBA,
          gl.UNSIGNED_BYTE,
          projector.canvas
        );
      }

      const w = canvas.width;
      const h = canvas.height;

      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.BLEND);
      gl.depthMask(false);

      if (isSoftwareGl && bhFbo && bhFboTex && blitProg && blitLoc) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, bhFbo);
        gl.viewport(0, 0, bhFboW, bhFboH);

        gl.useProgram(bhProg);
        gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
        gl.enableVertexAttribArray(bhLoc.aPos);
        gl.vertexAttribPointer(bhLoc.aPos, 2, gl.FLOAT, false, 0, 0);

        gl.uniform2f(bhLoc.uRes, bhFboW, bhFboH);
        gl.uniform1f(bhLoc.uTime, elapsedSec);
        gl.uniform3fv(bhLoc.uCamPos, basis.pos);
        gl.uniform3fv(bhLoc.uCamFwd, basis.fwd);
        gl.uniform3fv(bhLoc.uCamRight, basis.right);
        gl.uniform3fv(bhLoc.uCamUp, basis.up);

        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.disableVertexAttribArray(bhLoc.aPos);

        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, w, h);

        gl.useProgram(blitProg);
        gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
        gl.enableVertexAttribArray(blitLoc.aPos);
        gl.vertexAttribPointer(blitLoc.aPos, 2, gl.FLOAT, false, 0, 0);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, bhFboTex);
        gl.uniform1i(blitLoc.uTex, 0);
        gl.uniform2f(blitLoc.uRes, w, h);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.disableVertexAttribArray(blitLoc.aPos);
      } else {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, w, h);

        gl.useProgram(bhProg);
        gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
        gl.enableVertexAttribArray(bhLoc.aPos);
        gl.vertexAttribPointer(bhLoc.aPos, 2, gl.FLOAT, false, 0, 0);

        gl.uniform2f(bhLoc.uRes, w, h);
        gl.uniform1f(bhLoc.uTime, elapsedSec);
        gl.uniform3fv(bhLoc.uCamPos, basis.pos);
        gl.uniform3fv(bhLoc.uCamFwd, basis.fwd);
        gl.uniform3fv(bhLoc.uCamRight, basis.right);
        gl.uniform3fv(bhLoc.uCamUp, basis.up);

        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        gl.disableVertexAttribArray(bhLoc.aPos);
      }

      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.depthMask(true);
      gl.clear(gl.DEPTH_BUFFER_BIT);

      gl.useProgram(meshProg);
      gl.bindBuffer(gl.ARRAY_BUFFER, satBuf);
      const stride = satMesh.strideBytes;
      gl.enableVertexAttribArray(meshLoc.aPosition);
      gl.vertexAttribPointer(meshLoc.aPosition, 3, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(meshLoc.aNormal);
      gl.vertexAttribPointer(meshLoc.aNormal, 3, gl.FLOAT, false, stride, 12);
      gl.enableVertexAttribArray(meshLoc.aColor);
      gl.vertexAttribPointer(meshLoc.aColor, 3, gl.FLOAT, false, stride, 24);
      gl.enableVertexAttribArray(meshLoc.aEmissive);
      gl.vertexAttribPointer(meshLoc.aEmissive, 1, gl.FLOAT, false, stride, 36);

      gl.uniform2f(meshLoc.uRes, w, h);
      gl.uniform3fv(meshLoc.uCamPos, basis.pos);
      gl.uniform3fv(meshLoc.uCamFwd, basis.fwd);
      gl.uniform3fv(meshLoc.uCamRight, basis.right);
      gl.uniform3fv(meshLoc.uCamUp, basis.up);
      gl.uniform3fv(meshLoc.uProbeOffset, probeOffset);
      gl.uniform1f(meshLoc.uTime, elapsedSec);

      gl.drawArrays(gl.TRIANGLES, 0, satMesh.vertexCount);

      gl.disableVertexAttribArray(meshLoc.aPosition);
      gl.disableVertexAttribArray(meshLoc.aNormal);
      gl.disableVertexAttribArray(meshLoc.aColor);
      gl.disableVertexAttribArray(meshLoc.aEmissive);

      const planeQuad = getTerminalPlaneQuad(basis);
      const [tl, tr, br, bl] = planeQuad.corners;

      const beam = buildProjectorBeamVertices(planeQuad.corners);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.depthMask(false);

      gl.useProgram(beamProg);
      gl.bindBuffer(gl.ARRAY_BUFFER, beamBuf);
      gl.bufferData(gl.ARRAY_BUFFER, beam.data, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(beamLoc.aPosition);
      gl.vertexAttribPointer(beamLoc.aPosition, 3, gl.FLOAT, false, 16, 0);
      gl.enableVertexAttribArray(beamLoc.aAlpha);
      gl.vertexAttribPointer(beamLoc.aAlpha, 1, gl.FLOAT, false, 16, 12);

      gl.uniform2f(beamLoc.uRes, w, h);
      gl.uniform3fv(beamLoc.uCamPos, basis.pos);
      gl.uniform3fv(beamLoc.uCamFwd, basis.fwd);
      gl.uniform3fv(beamLoc.uCamRight, basis.right);
      gl.uniform3fv(beamLoc.uCamUp, basis.up);
      gl.uniform3fv(beamLoc.uProbeOffset, probeOffset);
      gl.uniform1f(beamLoc.uTime, elapsedSec);

      gl.drawArrays(gl.TRIANGLES, 0, beam.vertexCount);
      gl.disableVertexAttribArray(beamLoc.aPosition);
      gl.disableVertexAttribArray(beamLoc.aAlpha);

      const planeVerts = new Float32Array([
        tl[0], tl[1], tl[2], 0, 0,
        bl[0], bl[1], bl[2], 0, 1,
        tr[0], tr[1], tr[2], 1, 0,
        tr[0], tr[1], tr[2], 1, 0,
        bl[0], bl[1], bl[2], 0, 1,
        br[0], br[1], br[2], 1, 1,
      ]);

      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.useProgram(planeProg);
      gl.bindBuffer(gl.ARRAY_BUFFER, planeBuf);
      gl.bufferData(gl.ARRAY_BUFFER, planeVerts, gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(planeLoc.aPosition);
      gl.vertexAttribPointer(planeLoc.aPosition, 3, gl.FLOAT, false, 20, 0);
      gl.enableVertexAttribArray(planeLoc.aUv);
      gl.vertexAttribPointer(planeLoc.aUv, 2, gl.FLOAT, false, 20, 12);

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, terminalTex);
      gl.uniform1i(planeLoc.uTex, 0);
      gl.uniform2f(planeLoc.uRes, w, h);
      gl.uniform3fv(planeLoc.uCamPos, basis.pos);
      gl.uniform3fv(planeLoc.uCamFwd, basis.fwd);
      gl.uniform3fv(planeLoc.uCamRight, basis.right);
      gl.uniform3fv(planeLoc.uCamUp, basis.up);
      gl.uniform3fv(planeLoc.uProbeOffset, probeOffset);
      gl.uniform1f(planeLoc.uTime, elapsedSec);

      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.disableVertexAttribArray(planeLoc.aPosition);
      gl.disableVertexAttribArray(planeLoc.aUv);

      if (director.currentShot === "satellite" && !director.isTransitioning) {
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const screenPts = planeQuad.corners.map((pt) =>
          director.projectWorldToScreen(pt, vw, vh)
        );
        if (screenPts.every((p) => p.visible)) {
          const xs = screenPts.map((p) => p.x);
          const ys = screenPts.map((p) => p.y);
          const left = Math.round(Math.min(...xs));
          const right = Math.round(Math.max(...xs));
          const top = Math.round(Math.min(...ys));
          const bottom = Math.round(Math.max(...ys));
          const key = `${left},${top},${right - left},${bottom - top}`;
          if (key !== lastBoundsKey) {
            lastBoundsKey = key;
            callbacksRef.current.onProjectionBoundsChange?.({
              visible: true,
              left,
              top,
              width: Math.max(80, right - left),
              height: Math.max(60, bottom - top),
            });
          }
        }
      } else if (lastBoundsKey !== "hidden") {
        lastBoundsKey = "hidden";
        callbacksRef.current.onProjectionBoundsChange?.({
          visible: false,
          left: 0,
          top: 0,
          width: 0,
          height: 0,
        });
      }

      if (isSoftwareGl) {
        softTimerId = window.setTimeout(() => {
          rafId = requestAnimationFrame(renderFrame);
        }, 2);
      } else {
        rafId = requestAnimationFrame(renderFrame);
      }
    };

    rafId = requestAnimationFrame(renderFrame);

    return () => {
      cancelAnimationFrame(rafId);
      clearTimeout(softTimerId);
      window.removeEventListener("resize", resize);
      canvas.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      gl.deleteTexture(terminalTex);
      if (bhFboTex) gl.deleteTexture(bhFboTex);
      if (bhFbo) gl.deleteFramebuffer(bhFbo);
      gl.deleteBuffer(quadBuf);
      gl.deleteBuffer(satBuf);
      gl.deleteBuffer(beamBuf);
      gl.deleteBuffer(planeBuf);
      gl.deleteProgram(bhProg);
      if (blitProg) gl.deleteProgram(blitProg);
      gl.deleteProgram(meshProg);
      gl.deleteProgram(beamProg);
      gl.deleteProgram(planeProg);
    };
  }, [director, projector]);

  return (
    <canvas
      ref={canvasRef}
      className="scene-canvas"
      data-testid="singularity-canvas"
    />
  );
}
