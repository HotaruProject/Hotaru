import { BLACK_HOLE_VERTEX_SOURCE, blackHoleFragmentSource } from "./blackhole.js";
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
  float farPlane = 140.0;
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
  vec3 n = normalize(vNormal);
  vec3 view = normalize(uCamPos - vWorldPos);
  vec3 toDisk = normalize(uDiskPos - vWorldPos);

  float diskLight = max(dot(n, toDisk), 0.0);
  float headLight = max(dot(n, view), 0.0);
  vec3 fillDir = normalize(vec3(0.42, 0.68, -0.6));
  float fillLight = max(dot(n, fillDir), 0.0);

  vec3 halfVector = normalize(toDisk + view);
  float specular = pow(max(dot(n, halfVector), 0.0), 46.0);
  vec3 halfHead = normalize(view + view);
  float specularHead = pow(max(dot(n, halfHead), 0.0), 22.0);
  float fresnel = pow(1.0 - max(dot(n, view), 0.0), 3.0);

  vec3 ambient = vec3(0.05, 0.065, 0.095);
  vec3 lit = vColor * (ambient + diskLight * 1.35 + headLight * 0.62 + fillLight * 0.22);
  lit += vec3(0.92, 0.96, 1.0) * specular * 0.5;
  lit += vec3(0.75, 0.85, 1.0) * specularHead * 0.16;
  lit += vec3(0.32, 0.72, 1.0) * fresnel * 0.42;

  float blink = 1.0;
  if (vEmissive > 0.5) {
    blink = 0.18 + 0.82 * pow(max(sin(uTime * 3.1 + vPhase), 0.0), 8.0);
  }
  float pulse = 0.92 + 0.08 * sin(uTime * 2.1 + vPhase);
  vec3 emissive = vColor * vEmissive * 1.7 * blink * pulse;

  gl_FragColor = vec4(clamp(lit + emissive, 0.0, 1.0), 1.0);
}
`;

const BLIT_FRAGMENT_SOURCE = `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uRes;
void main() {
  gl_FragColor = texture2D(uTex, gl_FragCoord.xy / uRes);
}
`;

const QUALITY_TIERS = [
  { scale: 1.0, steps: 320 },
  { scale: 0.78, steps: 280 },
  { scale: 0.58, steps: 240 },
  { scale: 0.44, steps: 195 },
  { scale: 0.32, steps: 150 },
  { scale: 0.24, steps: 110 },
  { scale: 0.16, steps: 80 },
];

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

function uniformMap(gl, program, names) {
  const map = {};
  for (const name of names) {
    map[name] = gl.getUniformLocation(program, name);
  }
  return map;
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
    this._averageFrameMs = 16;
    this._tierIndex = 2;
    this._framesSinceAdjust = 0;
    this._blackHoleRenderedAt = -1;
    this._blackHoleCameraKey = "";
    this._blackHoleInterval = 1 / 12;
    this._slowFrames = 0;
    this._frameCounter = 0;
    this._pointer = { active: false, moved: false, x: 0, y: 0, startX: 0, startY: 0 };
    this._gl = null;
    this._programs = null;
    this._buffers = null;
    this._framebuffer = null;
    this._framebufferTexture = null;
    this._framebufferWidth = 0;
    this._framebufferHeight = 0;
    this._onResize = () => this._resize();
  }

  get quality() {
    return QUALITY_TIERS[this._tierIndex];
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
      const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
      const renderer = debugInfo
        ? String(gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) || "")
        : String(gl.getParameter(gl.RENDERER) || "");
      this._softwareRenderer = /swiftshader|llvmpipe|software|softpipe|basic render/i.test(
        renderer
      );
    } catch {
      this._softwareRenderer = false;
    }
    if (this._softwareRenderer) {
      this._tierIndex = QUALITY_TIERS.length - 1;
      this._tierLocked = true;
    }
    const rawQuality = new URLSearchParams(window.location.search).get(
      "quality"
    );
    const requestedTier = rawQuality === null ? Number.NaN : Number(rawQuality);
    if (Number.isFinite(requestedTier) && requestedTier >= 0) {
      this._tierIndex = Math.min(
        QUALITY_TIERS.length - 1,
        Math.floor(requestedTier)
      );
      this._tierLocked = true;
    }

    try {
      this._programs = {
        blackHole: linkProgram(
          gl,
          BLACK_HOLE_VERTEX_SOURCE,
          blackHoleFragmentSource({ steps: 320, octaves: 4 })
        ),
        blit: linkProgram(
          gl,
          BLACK_HOLE_VERTEX_SOURCE,
          BLIT_FRAGMENT_SOURCE
        ),
        mesh: linkProgram(gl, MESH_VERTEX_SOURCE, MESH_FRAGMENT_SOURCE),
      };
    } catch (error) {
      this._running = false;
      return false;
    }

    this._uniforms = {
      blackHole: uniformMap(gl, this._programs.blackHole, [
        "uRes",
        "uTime",
        "uMaxSteps",
        "uCamPos",
        "uCamFwd",
        "uCamRight",
        "uCamUp",
      ]),
      blit: uniformMap(gl, this._programs.blit, ["uTex", "uRes"]),
      mesh: uniformMap(gl, this._programs.mesh, [
        "uRes",
        "uCamPos",
        "uCamFwd",
        "uCamRight",
        "uCamUp",
        "uSatelliteYaw",
        "uSatelliteOffset",
        "uTime",
        "uDiskPos",
      ]),
    };

    const satellite = buildSatelliteMesh();
    this._satellite = satellite;

    this._buffers = {
      quad: this._createBuffer(
        gl,
        new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1])
      ),
      satellite: this._createBuffer(gl, satellite.vertexData),
    };

    this._framebuffer = gl.createFramebuffer();
    this._framebufferTexture = gl.createTexture();

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
    const onPointerDown = (event) => {
      this._pointer.active = true;
      this._pointer.moved = false;
      this._pointer.x = event.clientX;
      this._pointer.y = event.clientY;
      this._pointer.startX = event.clientX;
      this._pointer.startY = event.clientY;
    };
    const onPointerMove = (event) => {
      if (!this._pointer.active) return;
      const deltaX = event.clientX - this._pointer.x;
      const deltaY = event.clientY - this._pointer.y;
      this._pointer.x = event.clientX;
      this._pointer.y = event.clientY;
      if (
        Math.hypot(
          event.clientX - this._pointer.startX,
          event.clientY - this._pointer.startY
        ) > 6
      ) {
        this._pointer.moved = true;
      }
      if (
        this._pointer.moved &&
        this.director.currentShot === "overview" &&
        !this.director.isInputLocked
      ) {
        this.director.applyOrbit(-deltaX * 0.0042, deltaY * 0.0032, 0);
      }
    };
    const onPointerUp = (event) => {
      if (!this._pointer.active) return;
      const wasTap = !this._pointer.moved;
      this._pointer.active = false;
      if (!wasTap || this.director.isInputLocked) return;
      const width = window.innerWidth;
      const height = window.innerHeight;
      if (this.director.currentShot === "overview") {
        if (
          this.director.isPointerOnSatellite(
            event.clientX,
            event.clientY,
            width,
            height
          )
        ) {
          this.onSatelliteTap?.();
        }
      } else {
        this.onBackgroundTap?.();
      }
    };
    const onWheel = (event) => {
      event.preventDefault();
      if (this.director.isInputLocked || this.director.currentShot !== "overview") {
        return;
      }
      this.director.applyOrbit(0, 0, event.deltaY * 0.011);
    };

    canvas.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    this._pointerHandlers = { onPointerDown, onPointerMove, onPointerUp, onWheel };
  }

  _detachPointerHandlers() {
    const canvas = this.canvas;
    const handlers = this._pointerHandlers;
    if (!handlers) return;
    canvas.removeEventListener("pointerdown", handlers.onPointerDown);
    window.removeEventListener("pointermove", handlers.onPointerMove);
    window.removeEventListener("pointerup", handlers.onPointerUp);
    canvas.removeEventListener("wheel", handlers.onWheel);
    this._pointerHandlers = null;
  }

  _resize() {
    const gl = this._gl;
    if (!gl) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.floor(window.innerWidth * dpr));
    const height = Math.max(1, Math.floor(window.innerHeight * dpr));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
      gl.viewport(0, 0, width, height);
    }
    this._resizeFramebuffer();
  }

  _resizeFramebuffer() {
    this._blackHoleRenderedAt = -1;
    const gl = this._gl;
    if (!gl || !this._framebufferTexture) return;
    const scale = this.quality.scale;
    const width = Math.max(32, Math.floor(this.canvas.width * scale));
    const height = Math.max(32, Math.floor(this.canvas.height * scale));
    if (width === this._framebufferWidth && height === this._framebufferHeight) {
      return;
    }
    this._framebufferWidth = width;
    this._framebufferHeight = height;
    gl.bindTexture(gl.TEXTURE_2D, this._framebufferTexture);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      width,
      height,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      null
    );
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._framebuffer);
    gl.framebufferTexture2D(
      gl.FRAMEBUFFER,
      gl.COLOR_ATTACHMENT0,
      gl.TEXTURE_2D,
      this._framebufferTexture,
      0
    );
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  _adjustQuality(deltaMs) {
    this._averageFrameMs =
      this._averageFrameMs * 0.9 + deltaMs * 0.1;
    this._framesSinceAdjust += 1;
    if (this._tierLocked) return;
    if (deltaMs > 120) {
      this._slowFrames += 1;
      if (this._tierIndex < QUALITY_TIERS.length - 1) {
        this._tierIndex += 1;
        this._framesSinceAdjust = 0;
        this._averageFrameMs = 24;
        this._resizeFramebuffer();
        return;
      }
      if (this._slowFrames > 8) {
        this._tierLocked = true;
      }
      return;
    }
    if (this._slowFrames > 0) {
      this._slowFrames = 0;
    }
    if (this._framesSinceAdjust < 24) return;
    this._framesSinceAdjust = 0;
    if (this._averageFrameMs > 26 && this._tierIndex < QUALITY_TIERS.length - 1) {
      this._tierIndex += 1;
      this._resizeFramebuffer();
    } else if (
      this._averageFrameMs < 13 &&
      this._tierIndex > 0 &&
      !this._softwareRenderer
    ) {
      this._tierIndex -= 1;
      this._resizeFramebuffer();
    }
  }

  _cameraKey(basis) {
    return [
      basis.position[0],
      basis.position[1],
      basis.position[2],
      basis.forward[0],
      basis.forward[1],
      basis.forward[2],
    ]
      .map((value) => Number(value).toFixed(4))
      .join(",");
  }

  _blackHoleNeedsRender(elapsed, basis) {
    const key = this._cameraKey(basis);
    if (key !== this._blackHoleCameraKey) return true;
    if (this._blackHoleRenderedAt < 0) return true;
    return elapsed - this._blackHoleRenderedAt >= this._blackHoleInterval;
  }

  _render(now) {
    if (!this._running) return;
    if (this._paused) {
      this._frameId = requestAnimationFrame((time) => this._render(time));
      return;
    }
    const gl = this._gl;
    const canvas = this.canvas;
    const basis = this.director.step(now);
    const elapsed = (now - this._startTime) * 0.001;
    const pose = satellitePose(elapsed);
    this.director.setSatellitePosition(pose.offset);

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.depthMask(false);

    const blackHoleDue = this._blackHoleNeedsRender(elapsed, basis);
    if (blackHoleDue) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, this._framebuffer);
    gl.viewport(0, 0, this._framebufferWidth, this._framebufferHeight);
    gl.useProgram(this._programs.blackHole);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._buffers.quad);
    const blackHoleLocation = gl.getAttribLocation(this._programs.blackHole, "aPos");
    gl.enableVertexAttribArray(blackHoleLocation);
    gl.vertexAttribPointer(blackHoleLocation, 2, gl.FLOAT, false, 0, 0);
    gl.uniform2f(
      this._uniforms.blackHole.uRes,
      this._framebufferWidth,
      this._framebufferHeight
    );
    gl.uniform1f(this._uniforms.blackHole.uTime, elapsed);
    gl.uniform1i(this._uniforms.blackHole.uMaxSteps, this.quality.steps);
    gl.uniform3fv(this._uniforms.blackHole.uCamPos, basis.position);
    gl.uniform3fv(this._uniforms.blackHole.uCamFwd, basis.forward);
    gl.uniform3fv(this._uniforms.blackHole.uCamRight, basis.right);
    gl.uniform3fv(this._uniforms.blackHole.uCamUp, basis.up);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.disableVertexAttribArray(blackHoleLocation);
    this._blackHoleRenderedAt = elapsed;
    this._blackHoleCameraKey = this._cameraKey(basis);
    }

    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.useProgram(this._programs.blit);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._buffers.quad);
    const blitLocation = gl.getAttribLocation(this._programs.blit, "aPos");
    gl.enableVertexAttribArray(blitLocation);
    gl.vertexAttribPointer(blitLocation, 2, gl.FLOAT, false, 0, 0);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this._framebufferTexture);
    gl.uniform1i(this._uniforms.blit.uTex, 0);
    gl.uniform2f(this._uniforms.blit.uRes, canvas.width, canvas.height);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.disableVertexAttribArray(blitLocation);

    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.depthMask(true);
    gl.clear(gl.DEPTH_BUFFER_BIT);

    gl.useProgram(this._programs.mesh);
    gl.bindBuffer(gl.ARRAY_BUFFER, this._buffers.satellite);
    const stride = this._satellite.strideBytes;
    const meshLocations = {
      position: gl.getAttribLocation(this._programs.mesh, "aPosition"),
      normal: gl.getAttribLocation(this._programs.mesh, "aNormal"),
      color: gl.getAttribLocation(this._programs.mesh, "aColor"),
      emissive: gl.getAttribLocation(this._programs.mesh, "aEmissive"),
      phase: gl.getAttribLocation(this._programs.mesh, "aPhase"),
    };
    gl.enableVertexAttribArray(meshLocations.position);
    gl.vertexAttribPointer(meshLocations.position, 3, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(meshLocations.normal);
    gl.vertexAttribPointer(meshLocations.normal, 3, gl.FLOAT, false, stride, 12);
    gl.enableVertexAttribArray(meshLocations.color);
    gl.vertexAttribPointer(meshLocations.color, 3, gl.FLOAT, false, stride, 24);
    gl.enableVertexAttribArray(meshLocations.emissive);
    gl.vertexAttribPointer(meshLocations.emissive, 1, gl.FLOAT, false, stride, 36);
    gl.enableVertexAttribArray(meshLocations.phase);
    gl.vertexAttribPointer(meshLocations.phase, 1, gl.FLOAT, false, stride, 40);

    gl.uniform2f(this._uniforms.mesh.uRes, canvas.width, canvas.height);
    gl.uniform3fv(this._uniforms.mesh.uCamPos, basis.position);
    gl.uniform3fv(this._uniforms.mesh.uCamFwd, basis.forward);
    gl.uniform3fv(this._uniforms.mesh.uCamRight, basis.right);
    gl.uniform3fv(this._uniforms.mesh.uCamUp, basis.up);
    gl.uniform1f(this._uniforms.mesh.uSatelliteYaw, pose.yaw);
    gl.uniform3fv(this._uniforms.mesh.uSatelliteOffset, pose.offset);
    gl.uniform1f(this._uniforms.mesh.uTime, elapsed);
    gl.uniform3f(this._uniforms.mesh.uDiskPos, 0, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, this._satellite.vertexCount);

    gl.disableVertexAttribArray(meshLocations.position);
    gl.disableVertexAttribArray(meshLocations.normal);
    gl.disableVertexAttribArray(meshLocations.color);
    gl.disableVertexAttribArray(meshLocations.emissive);
    gl.disableVertexAttribArray(meshLocations.phase);

    const delta = now - this._lastFrameTime;
    this._lastFrameTime = now;
    this._frameCounter += 1;
    if (delta > 0 && this._frameCounter > 3) {
      this._adjustQuality(delta);
      this._blackHoleInterval = Math.min(
        1,
        Math.max(1 / 12, (delta * 1.6) / 1000)
      );
    }
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
      for (const program of Object.values(this._programs)) {
        gl.deleteProgram(program);
      }
    }
    if (this._buffers) {
      for (const buffer of Object.values(this._buffers)) {
        gl.deleteBuffer(buffer);
      }
    }
    if (this._framebufferTexture) gl.deleteTexture(this._framebufferTexture);
    if (this._framebuffer) gl.deleteFramebuffer(this._framebuffer);
    this._gl = null;
  }
}
