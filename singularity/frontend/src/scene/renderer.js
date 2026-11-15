import {
  BLACK_HOLE_VERTEX_SOURCE,
  BLACK_HOLE_FRAGMENT_SOURCE,
  BRIGHT_SOURCE,
  BLUR_SOURCE,
  COMPOSITE_SOURCE,
  makeNoiseTexture,
  makeTarget,
} from "./blackhole.js";
import { buildSatelliteMesh, satellitePose } from "./satellite.js";

const MESH_VERTEX_SOURCE = `
precision highp float;

attribute vec3 aPosition;
attribute vec3 aNormal;
attribute vec3 aColor;
attribute float aEmissive;
attribute float aPhase;

uniform vec2 uRes;
uniform vec3 uCamPos;
uniform vec3 uCamFwd;
uniform vec3 uCamRight;
uniform vec3 uCamUp;
uniform float uSatelliteYaw;
uniform vec3 uSatelliteOffset;

varying vec3 vWorldPos;
varying vec3 vNormal;
varying vec3 vColor;
varying float vEmissive;
varying float vPhase;

void main() {
  float c = cos(uSatelliteYaw);
  float s = sin(uSatelliteYaw);
  mat3 rot = mat3(c, 0.0, -s, 0.0, 1.0, 0.0, s, 0.0, c);

  vec3 worldPos = rot * aPosition + uSatelliteOffset;
  vWorldPos = worldPos;
  vNormal = normalize(rot * aNormal);
  vColor = aColor;
  vEmissive = aEmissive;
  vPhase = aPhase;

  vec3 rel = worldPos - uCamPos;
  float xCam = dot(rel, uCamRight);
  float yCam = dot(rel, uCamUp);
  float zCam = dot(rel, uCamFwd);

  float minDim = min(uRes.x, uRes.y);
  float fov = 0.85;
  float clipX = (xCam / fov) * (2.0 * minDim / uRes.x);
  float clipY = (yCam / fov) * (2.0 * minDim / uRes.y);

  float nearPlane = 0.05;
  float farPlane = 160.0;
  float clipZ = ((farPlane + nearPlane) / (farPlane - nearPlane)) * zCam
    - (2.0 * farPlane * nearPlane) / (farPlane - nearPlane);

  gl_Position = vec4(clipX, clipY, clipZ, zCam);
}
`;

const MESH_FRAGMENT_SOURCE = `
precision highp float;

uniform vec3 uCamPos;
uniform float uTime;
uniform vec3 uDiskPos;

varying vec3 vWorldPos;
varying vec3 vNormal;
varying vec3 vColor;
varying float vEmissive;
varying float vPhase;

void main() {
  vec3 camToFrag = vWorldPos - uCamPos;
  float totalDist = length(camToFrag);
  vec3 rayDir = camToFrag / totalDist;
  
  float transmission = 1.0;

  float tOrigin = -dot(uCamPos, rayDir);
  if (tOrigin > 0.0 && tOrigin < totalDist) {
    vec3 pClosest = uCamPos + rayDir * tOrigin;
    float distToOrigin = length(pClosest);
    if (distToOrigin < 2.65) {
      transmission = 0.0;
    } else if (distToOrigin < 4.2) {
      transmission *= smoothstep(2.65, 4.2, distToOrigin);
    }
  }

  if (abs(rayDir.y) > 1e-4) {
    float tDisk = -uCamPos.y / rayDir.y;
    if (tDisk > 0.0 && tDisk < totalDist) {
      vec3 pDisk = uCamPos + rayDir * tDisk;
      float rDisk = length(pDisk.xz);
      if (rDisk >= 2.9 && rDisk <= 12.2) {
        float diskOpacity = smoothstep(12.2, 6.2, rDisk) * 0.96;
        transmission *= (1.0 - diskOpacity);
      }
    }
  }

  if (transmission <= 0.01) {
    discard;
  }

  vec3 n = normalize(vNormal);
  vec3 view = normalize(uCamPos - vWorldPos);
  
  vec3 toDisk = normalize(uDiskPos - vWorldPos);
  float distToCenter = length(uDiskPos - vWorldPos);

  float diskNdotL = max(dot(n, toDisk), 0.0);
  vec3 diskTangent = normalize(vec3(-vWorldPos.z, 0.0, vWorldPos.x));
  float dopplerAlign = dot(diskTangent, toDisk);
  vec3 diskLightColor = mix(vec3(0.92, 0.96, 1.05), vec3(1.35, 1.42, 1.55), clamp(0.5 + 0.5 * dopplerAlign, 0.0, 1.0));
  float diskIntensity = (42.0 / (distToCenter * distToCenter + 10.0)) * 2.4;
  vec3 diskDiffuse = vColor * diskLightColor * (diskNdotL * diskIntensity);

  float headLight = max(dot(n, view), 0.0);
  vec3 fillDir = normalize(vec3(0.35, 0.75, -0.55));
  float fillLight = max(dot(n, fillDir), 0.0);
  vec3 ambient = vec3(0.045, 0.05, 0.065);

  vec3 halfVector = normalize(toDisk + view);
  float specDisk = pow(max(dot(n, halfVector), 0.0), 38.0);
  vec3 halfHead = normalize(view + view);
  float specHead = pow(max(dot(n, halfHead), 0.0), 18.0);
  vec3 specular = vec3(1.1, 1.2, 1.3) * specDisk * 0.75 + vec3(0.85, 0.9, 1.0) * specHead * 0.22;

  float fresnel = pow(1.0 - max(dot(n, view), 0.0), 3.2);
  vec3 rimGlow = vec3(0.45, 0.75, 1.0) * fresnel * 0.55;

  float scanline = 0.94 + 0.06 * sin(vWorldPos.y * 70.0 - uTime * 5.0);

  float blink = 1.0;
  if (vEmissive > 0.5) {
    float bCycle = max(sin(uTime * 3.5 + vPhase), 0.0);
    blink = 0.2 + 0.8 * pow(bCycle, 10.0);
  }
  float pulse = 0.94 + 0.06 * sin(uTime * 2.4 + vPhase);
  vec3 emissive = vColor * vEmissive * 2.2 * blink * pulse;

  vec3 lit = (vColor * (ambient + headLight * 0.45 + fillLight * 0.18) + diskDiffuse + specular + rimGlow) * scanline + emissive;

  lit = lit * (2.51 * lit + 0.03) / (lit * (2.43 * lit + 0.59) + 0.14);
  gl_FragColor = vec4(clamp(lit, 0.0, 1.0), transmission);
}
`;

const QUALITY_STEPS = [0.65, 0.78, 0.88, 1.0];

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

function linkProgram(gl, vertexSource, fragmentSource) {
  const vertexShader = compileShader(gl, gl.VERTEX_SHADER, vertexSource);
  const fragmentShader = compileShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
  const program = gl.createProgram();
  gl.attachShader(program, vertexShader);
  gl.attachShader(program, fragmentShader);
  gl.linkProgram(program);
  gl.deleteShader(vertexShader);
  gl.deleteShader(fragmentShader);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(program);
    gl.deleteProgram(program);
    throw new Error(`WebGL program link error: ${info}`);
  }
  return program;
}

export class SceneRenderer {
  constructor(canvas, director, options = {}) {
    this.canvas = canvas;
    this.director = director;
    this.onSatelliteTap = options.onSatelliteTap || null;
    this.onBackgroundTap = options.onBackgroundTap || null;
    this.onTelemetryPointer = options.onTelemetryPointer || null;
    this._running = false;
    this._paused = false;
    this._frameId = 0;
    this._startTime = 0;
    this._lastFrameTime = 0;
    this._qualityIndex = 2;
    this._frameSum = 0;
    this._frameCount = 0;
    this._velX = 0;
    this._velY = 0;
    this._lastMoveTime = 0;
    this._lastInteractionTime = 0;
    this._lastTapTime = 0;
    this._pointer = { active: false, moved: false, x: 0, y: 0, startX: 0, startY: 0 };
    this._gl = null;
    this._programs = null;
    this._buffers = null;
    this._targets = null;
    this._noiseTex = null;
    this._onResize = () => this._resize();
  }

  get quality() {
    return { scale: QUALITY_STEPS[this._qualityIndex] };
  }

  start() {
    const canvas = this.canvas;
    if (!canvas || this._running) return false;
    const gl =
      canvas.getContext("webgl", {
        antialias: false,
        alpha: false,
        depth: true,
        powerPreference: "high-performance",
        preserveDrawingBuffer: false,
      }) || canvas.getContext("experimental-webgl");
    if (!gl) return false;

    this._gl = gl;
    this._running = true;
    this._startTime = performance.now();
    this._lastFrameTime = this._startTime;

    try {
      this._programs = {
        blackHole: linkProgram(gl, BLACK_HOLE_VERTEX_SOURCE, BLACK_HOLE_FRAGMENT_SOURCE),
        bright: linkProgram(gl, BLACK_HOLE_VERTEX_SOURCE, BRIGHT_SOURCE),
        blur: linkProgram(gl, BLACK_HOLE_VERTEX_SOURCE, BLUR_SOURCE),
        comp: linkProgram(gl, BLACK_HOLE_VERTEX_SOURCE, COMPOSITE_SOURCE),
        mesh: linkProgram(gl, MESH_VERTEX_SOURCE, MESH_FRAGMENT_SOURCE),
      };
    } catch (error) {
      console.error("Program link failed:", error);
      this._running = false;
      return false;
    }

    this._loc = {};
    for (const [key, prog] of Object.entries(this._programs)) {
      this._loc[key] = {};
      const uniforms = [
        "uRes", "uCenter", "uScale", "uTime", "uCamPos", "uCamRight", "uCamUp", "uCamFwd",
        "uNoise", "uTex", "uTexel", "uDir", "uScene", "uBloom",
        "uSatelliteYaw", "uSatelliteOffset", "uDiskPos"
      ];
      for (const name of uniforms) {
        this._loc[key][name] = gl.getUniformLocation(prog, name);
      }
    }

    const satellite = buildSatelliteMesh();
    this._satellite = satellite;

    this._buffers = {
      quad: this._createBuffer(gl, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1])),
      satellite: this._createBuffer(gl, satellite.vertexData),
    };

    this._noiseTex = makeNoiseTexture(gl);
    this._targets = {
      scene: makeTarget(gl),
      bloomA: makeTarget(gl),
      bloomB: makeTarget(gl),
    };

    this._attachPointerHandlers();
    window.addEventListener("resize", this._onResize);
    this._resize();
    this._frameId = requestAnimationFrame((time) => this._render(time));
    return true;
  }

  _createBuffer(gl, data) {
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return buffer;
  }

  _attachPointerHandlers() {
    const canvas = this.canvas;

    const handleTap = (clientX, clientY) => {
      const now = performance.now();
      const isDoubleTap = now - this._lastTapTime < 320;
      this._lastTapTime = now;
      if (isDoubleTap && this.director.distance < 0) {
        if (this.director.toggleTurn()) return;
      }
      const width = window.innerWidth;
      const height = window.innerHeight;
      if (this.director.currentShot === "overview") {
        if (
          this.director.isPointerOnSatellite(
            clientX,
            clientY,
            width,
            height
          )
        ) {
          this.onSatelliteTap?.();
        } else {
          this.director.goTo("singularity");
        }
      } else if (this.director.currentShot === "singularity") {
        this.director.goTo("overview");
      } else if (this.director.currentShot === "satellite") {
        this.director.goTo("overview");
      } else {
        this.onBackgroundTap?.();
      }
    };

    const onPointerDown = (event) => {
      if (event.pointerType === "touch") return;
      this._pointer.active = true;
      this._pointer.moved = false;
      this._pointer.x = event.clientX;
      this._pointer.y = event.clientY;
      this._pointer.startX = event.clientX;
      this._pointer.startY = event.clientY;
      this._lastMoveTime = performance.now();
      this._lastInteractionTime = performance.now();
      this._velX = 0;
      this._velY = 0;
    };

    const onPointerMove = (event) => {
      if (event.pointerType === "touch") return;
      if (!this._pointer.active) return;
      const now = performance.now();
      const deltaX = event.clientX - this._pointer.x;
      const deltaY = event.clientY - this._pointer.y;
      this._pointer.x = event.clientX;
      this._pointer.y = event.clientY;
      if (
        Math.hypot(
          event.clientX - this._pointer.startX,
          event.clientY - this._pointer.startY
        ) > 5
      ) {
        this._pointer.moved = true;
      }
      const isOrbitable =
        this.director.currentShot === "overview" ||
        this.director.currentShot === "singularity";
      if (
        this._pointer.moved &&
        isOrbitable &&
        !this.director.isInputLocked
      ) {
        this.director.applyOrbit(-deltaX * 0.0055, deltaY * 0.0045, 0);
        const dtMove = Math.max(0.008, (now - this._lastMoveTime) * 0.001);
        this._velX = Math.max(-3.5, Math.min(3.5, (-deltaX * 0.0055) / dtMove * 0.024));
        this._velY = Math.max(-2.5, Math.min(2.5, (deltaY * 0.0045) / dtMove * 0.024));
      }
      this._lastMoveTime = now;
      this._lastInteractionTime = now;
    };

    const onPointerUp = (event) => {
      if (event.pointerType === "touch") return;
      if (!this._pointer.active) return;
      const wasTap = !this._pointer.moved;
      this._pointer.active = false;
      if (!wasTap || this.director.isInputLocked) return;
      handleTap(event.clientX, event.clientY);
    };

    const onWheel = (event) => {
      event.preventDefault();
      const isOrbitable =
        this.director.currentShot === "overview" ||
        this.director.currentShot === "singularity";
      if (this.director.isInputLocked || !isOrbitable) {
        return;
      }
      const delta = Math.sign(event.deltaY) * Math.min(Math.abs(event.deltaY) * 0.015, 1.2);
      this.director.zoomDelta(delta);
      this._lastInteractionTime = performance.now();
    };

    let pinch = 0;
    const touchDist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);

    const onTouchStart = (event) => {
      if (event.touches.length === 2) {
        event.preventDefault();
        pinch = touchDist(event.touches);
        this._pointer.active = false;
        return;
      }
      if (event.touches.length === 1) {
        const t = event.touches[0];
        this._pointer.active = true;
        this._pointer.moved = false;
        this._pointer.x = t.clientX;
        this._pointer.y = t.clientY;
        this._pointer.startX = t.clientX;
        this._pointer.startY = t.clientY;
        this._lastMoveTime = performance.now();
        this._lastInteractionTime = performance.now();
        this._velX = 0;
        this._velY = 0;
      }
    };

    const onTouchMove = (event) => {
      if (event.touches.length === 2 && pinch > 0) {
        event.preventDefault();
        const d = touchDist(event.touches);
        if (d > 2) {
          const delta = d - pinch;
          pinch = d;
          this.director.zoomDelta(-delta * 0.045);
        }
        this._lastInteractionTime = performance.now();
        return;
      }
      if (event.touches.length === 1 && this._pointer.active) {
        event.preventDefault();
        const now = performance.now();
        const t = event.touches[0];
        const deltaX = t.clientX - this._pointer.x;
        const deltaY = t.clientY - this._pointer.y;
        this._pointer.x = t.clientX;
        this._pointer.y = t.clientY;
        if (
          Math.hypot(
            t.clientX - this._pointer.startX,
            t.clientY - this._pointer.startY
          ) > 5
        ) {
          this._pointer.moved = true;
        }
        const isOrbitable =
          this.director.currentShot === "overview" ||
          this.director.currentShot === "singularity";
        if (
          this._pointer.moved &&
          isOrbitable &&
          !this.director.isInputLocked
        ) {
          this.director.applyOrbit(-deltaX * 0.0055, deltaY * 0.0045, 0);
          const dtMove = Math.max(0.008, (now - this._lastMoveTime) * 0.001);
          this._velX = Math.max(-3.5, Math.min(3.5, (-deltaX * 0.0055) / dtMove * 0.024));
          this._velY = Math.max(-2.5, Math.min(2.5, (deltaY * 0.0045) / dtMove * 0.024));
        }
        this._lastMoveTime = now;
        this._lastInteractionTime = now;
      }
    };

    const onTouchEnd = (event) => {
      if (event.touches.length < 2) pinch = 0;
      if (event.touches.length === 0 && this._pointer.active) {
        const wasTap = !this._pointer.moved;
        this._pointer.active = false;
        if (!wasTap || this.director.isInputLocked) return;
        const changed = event.changedTouches?.[0];
        const cx = changed ? changed.clientX : this._pointer.x;
        const cy = changed ? changed.clientY : this._pointer.y;
        handleTap(cx, cy);
      }
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("touchstart", onTouchStart, { passive: false });
    canvas.addEventListener("touchmove", onTouchMove, { passive: false });
    canvas.addEventListener("touchend", onTouchEnd);
    canvas.addEventListener("touchcancel", onTouchEnd);

    this._pointerHandlers = {
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onWheel,
      onTouchStart,
      onTouchMove,
      onTouchEnd,
    };
  }

  _detachPointerHandlers() {
    const canvas = this.canvas;
    const handlers = this._pointerHandlers;
    if (!handlers) return;
    canvas.removeEventListener("pointerdown", handlers.onPointerDown);
    window.removeEventListener("pointermove", handlers.onPointerMove);
    window.removeEventListener("pointerup", handlers.onPointerUp);
    canvas.removeEventListener("wheel", handlers.onWheel);
    canvas.removeEventListener("touchstart", handlers.onTouchStart);
    canvas.removeEventListener("touchmove", handlers.onTouchMove);
    canvas.removeEventListener("touchend", handlers.onTouchEnd);
    canvas.removeEventListener("touchcancel", handlers.onTouchEnd);
    this._pointerHandlers = null;
  }

  _resize() {
    const gl = this._gl;
    if (!gl) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const outW = Math.max(1, Math.round(window.innerWidth * Math.min(dpr, 1.5)));
    const outH = Math.max(1, Math.round(window.innerHeight * Math.min(dpr, 1.5)));
    if (this.canvas.width !== outW || this.canvas.height !== outH) {
      this.canvas.width = outW;
      this.canvas.height = outH;
      gl.viewport(0, 0, outW, outH);
    }
  }

  _render(now) {
    if (!this._running) return;
    if (this._paused) {
      this._frameId = requestAnimationFrame((time) => this._render(time));
      return;
    }
    const gl = this._gl;
    const canvas = this.canvas;
    const dt = Math.min(0.1, Math.max(0.001, (now - this._lastFrameTime) * 0.001));
    this._lastFrameTime = now;

    if (!this._pointer.active && !this.director.isTransitioning) {
      const isOrbitable =
        this.director.currentShot === "overview" ||
        this.director.currentShot === "singularity";
      if (isOrbitable) {
        const damp = Math.pow(0.92, dt * 60);
        this._velX *= damp;
        this._velY *= damp;
        let yawRate = this._velX;
        if (
          now - this._lastInteractionTime > 3200 &&
          this.director.distance > 0 &&
          Math.abs(this._velX) < 0.01
        ) {
          yawRate += 0.016;
        }
        if (Math.abs(yawRate) > 0.0001 || Math.abs(this._velY) > 0.0001) {
          this.director.applyOrbit(yawRate * dt, this._velY * dt, 0);
        }
      }
    }

    const basis = this.director.step(now);
    const elapsed = (now - this._startTime) * 0.001;
    const delta = dt * 1000;

    if (delta > 0) {
      this._frameSum += delta;
      this._frameCount++;
      if (this._frameCount >= 25) {
        const avg = this._frameSum / this._frameCount;
        if (avg > 35 && this._qualityIndex > 0) {
          this._qualityIndex--;
        } else if (avg < 18 && this._qualityIndex < QUALITY_STEPS.length - 1) {
          this._qualityIndex++;
        }
        this._frameSum = 0;
        this._frameCount = 0;
      }
    }

    const pose = satellitePose(elapsed);
    this.director.setSatellitePosition(pose.offset);

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const scale = QUALITY_STEPS[this._qualityIndex];
    const sw = Math.max(32, Math.round(window.innerWidth * dpr * scale));
    const sh = Math.max(32, Math.round(window.innerHeight * dpr * scale));
    const bw = Math.max(16, Math.round(sw / 4));
    const bh = Math.max(16, Math.round(sh / 4));

    this._targets.scene.size(sw, sh);
    this._targets.bloomA.size(bw, bh);
    this._targets.bloomB.size(bw, bh);

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

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.depthMask(false);

    pass(this._programs.blackHole, this._targets.scene, sw, sh);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._buffers.quad);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    const bhLoc = this._loc.blackHole;
    gl.uniform2f(bhLoc.uRes, sw, sh);
    gl.uniform1f(bhLoc.uTime, elapsed % 3600);
    gl.uniform3fv(bhLoc.uCamPos, basis.position);
    gl.uniform3fv(bhLoc.uCamRight, basis.right);
    gl.uniform3fv(bhLoc.uCamUp, basis.up);
    gl.uniform3fv(bhLoc.uCamFwd, basis.forward);
    gl.uniform2f(bhLoc.uCenter, 0.5, 0.5);
    gl.uniform1f(bhLoc.uScale, 1.0 * (Math.hypot(basis.position[0], basis.position[1], basis.position[2]) / 26.0));
    bindTex(0, this._noiseTex, bhLoc.uNoise);
    gl.drawArrays(gl.TRIANGLES, 0, 6);

    pass(this._programs.bright, this._targets.bloomA, bw, bh);
    gl.uniform2f(this._loc.bright.uTexel, 1 / sw, 1 / sh);
    bindTex(0, this._targets.scene.tex, this._loc.bright.uTex);
    gl.drawArrays(gl.TRIANGLES, 0, 6);

    for (let i = 0; i < 2; i++) {
      pass(this._programs.blur, this._targets.bloomB, bw, bh);
      gl.uniform2f(this._loc.blur.uDir, (1 + i) / bw, 0);
      bindTex(0, this._targets.bloomA.tex, this._loc.blur.uTex);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      pass(this._programs.blur, this._targets.bloomA, bw, bh);
      gl.uniform2f(this._loc.blur.uDir, 0, (1 + i) / bh);
      bindTex(0, this._targets.bloomB.tex, this._loc.blur.uTex);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    pass(this._programs.comp, null, canvas.width, canvas.height);
    gl.uniform1f(this._loc.comp.uTime, elapsed % 3600);
    bindTex(0, this._targets.scene.tex, this._loc.comp.uScene);
    bindTex(1, this._targets.bloomA.tex, this._loc.comp.uBloom);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.disableVertexAttribArray(0);

    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.depthMask(true);
    gl.clear(gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    gl.useProgram(this._programs.mesh);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._buffers.satellite);
    const stride = this._satellite.strideBytes;
    const meshLoc = {
      pos: gl.getAttribLocation(this._programs.mesh, "aPosition"),
      norm: gl.getAttribLocation(this._programs.mesh, "aNormal"),
      col: gl.getAttribLocation(this._programs.mesh, "aColor"),
      em: gl.getAttribLocation(this._programs.mesh, "aEmissive"),
      ph: gl.getAttribLocation(this._programs.mesh, "aPhase"),
    };

    gl.enableVertexAttribArray(meshLoc.pos);
    gl.vertexAttribPointer(meshLoc.pos, 3, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(meshLoc.norm);
    gl.vertexAttribPointer(meshLoc.norm, 3, gl.FLOAT, false, stride, 12);
    gl.enableVertexAttribArray(meshLoc.col);
    gl.vertexAttribPointer(meshLoc.col, 3, gl.FLOAT, false, stride, 24);
    gl.enableVertexAttribArray(meshLoc.em);
    gl.vertexAttribPointer(meshLoc.em, 1, gl.FLOAT, false, stride, 36);
    gl.enableVertexAttribArray(meshLoc.ph);
    gl.vertexAttribPointer(meshLoc.ph, 1, gl.FLOAT, false, stride, 40);

    const mU = this._loc.mesh;
    gl.uniform2f(mU.uRes, canvas.width, canvas.height);
    gl.uniform3fv(mU.uCamPos, basis.position);
    gl.uniform3fv(mU.uCamFwd, basis.forward);
    gl.uniform3fv(mU.uCamRight, basis.right);
    gl.uniform3fv(mU.uCamUp, basis.up);
    gl.uniform1f(mU.uSatelliteYaw, pose.yaw);
    gl.uniform3fv(mU.uSatelliteOffset, pose.offset);
    gl.uniform1f(mU.uTime, elapsed);
    gl.uniform3f(mU.uDiskPos, 0, 0, 0);

    gl.drawArrays(gl.TRIANGLES, 0, this._satellite.vertexCount);

    gl.disableVertexAttribArray(meshLoc.pos);
    gl.disableVertexAttribArray(meshLoc.norm);
    gl.disableVertexAttribArray(meshLoc.col);
    gl.disableVertexAttribArray(meshLoc.em);
    gl.disableVertexAttribArray(meshLoc.ph);
    gl.disable(gl.BLEND);

    this._frameId = requestAnimationFrame((time) => this._render(time));
  }

  pause() {
    this._paused = true;
  }

  resume() {
    this._paused = false;
  }

  dispose() {
    this._running = false;
    cancelAnimationFrame(this._frameId);
    window.removeEventListener("resize", this._onResize);
    this._detachPointerHandlers();
    const gl = this._gl;
    if (!gl) return;
    if (this._programs) {
      for (const prog of Object.values(this._programs)) {
        gl.deleteProgram(prog);
      }
    }
    if (this._buffers) {
      for (const buf of Object.values(this._buffers)) {
        gl.deleteBuffer(buf);
      }
    }
    if (this._noiseTex) gl.deleteTexture(this._noiseTex);
    if (this._targets) {
      for (const t of Object.values(this._targets)) {
        t.free();
      }
    }
    this._gl = null;
  }
}
