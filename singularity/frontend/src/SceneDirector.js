export const CAMERA_FOV = 0.85;

export const SATELLITE_WORLD = {
  busPos: [7.50, 2.05, -9.18],
  projectorLensPos: [7.08, 2.03, -9.08],
  terminalCenterPos: [6.20, 2.00, -8.92],
  terminalWidth: 1.52,
  terminalHeight: 1.01,
};

function vecSub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function vecAdd(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

function vecScale(a, s) {
  return [a[0] * s, a[1] * s, a[2] * s];
}

function vecDot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function vecCross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

function vecLen(a) {
  return Math.hypot(a[0], a[1], a[2]);
}

function vecNormalize(a) {
  const len = vecLen(a);
  if (len < 1e-8) return [0, 0, 1];
  return [a[0] / len, a[1] / len, a[2] / len];
}

function vecLerp(a, b, t) {
  return [
    a[0] + (b[0] - a[0]) * t,
    a[1] + (b[1] - a[1]) * t,
    a[2] + (b[2] - a[2]) * t,
  ];
}

function slerpDirection(dirA, dirB, t) {
  const a = vecNormalize(dirA);
  const b = vecNormalize(dirB);
  const dot = Math.max(-1, Math.min(1, vecDot(a, b)));
  if (dot > 0.9995) {
    return vecNormalize(vecLerp(a, b, t));
  }
  const theta = Math.acos(dot);
  const sinTheta = Math.sin(theta);
  if (Math.abs(sinTheta) < 1e-6) {
    return vecNormalize(vecLerp(a, b, t));
  }
  const wA = Math.sin((1 - t) * theta) / sinTheta;
  const wB = Math.sin(t * theta) / sinTheta;
  return vecNormalize([
    a[0] * wA + b[0] * wB,
    a[1] * wA + b[1] * wB,
    a[2] * wA + b[2] * wB,
  ]);
}

export function easeInOutCubic(t) {
  const x = Math.max(0, Math.min(1, t));
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}

const OVERVIEW_PITCH = 0.18;
const OVERVIEW_YAW = 0.0;
const OVERVIEW_DIST = 26.0;
const OVERVIEW_POS = [
  OVERVIEW_DIST * Math.cos(OVERVIEW_PITCH) * Math.sin(OVERVIEW_YAW),
  OVERVIEW_DIST * Math.sin(OVERVIEW_PITCH),
  -OVERVIEW_DIST * Math.cos(OVERVIEW_PITCH) * Math.cos(OVERVIEW_YAW),
];
const OVERVIEW_TARGET = [0.0, 0.0, 0.0];

const SATELLITE_CAM_POS = [6.65, 2.22, -12.15];
const SATELLITE_CAM_TARGET = [6.65, 2.01, -8.98];

export const NAMED_SHOTS = {
  overview: {
    name: "overview",
    label: "Overview",
    pos: OVERVIEW_POS,
    target: OVERVIEW_TARGET,
    dir: vecNormalize(vecSub(OVERVIEW_TARGET, OVERVIEW_POS)),
    fov: CAMERA_FOV,
  },
  satellite: {
    name: "satellite",
    label: "Satellite",
    pos: SATELLITE_CAM_POS,
    target: SATELLITE_CAM_TARGET,
    dir: vecNormalize(vecSub(SATELLITE_CAM_TARGET, SATELLITE_CAM_POS)),
    fov: CAMERA_FOV,
  },
};

export class SceneDirector {
  constructor(options = {}) {
    const initialShot = NAMED_SHOTS[options.initialShot]
      ? options.initialShot
      : "overview";
    const defaultDur = Number(options.durationMs ?? 2400);
    this._durationMs =
      Number.isFinite(defaultDur) && defaultDur >= 0 ? defaultDur : 2400;
    this._now =
      typeof options.now === "function"
        ? options.now
        : () => (typeof performance !== "undefined" ? performance.now() : Date.now());

    this._currentShot = initialShot;
    this._targetShot = initialShot;
    this._isTransitioning = false;
    this._progress = 1;

    const shot = NAMED_SHOTS[initialShot];
    this._pos = [...shot.pos];
    this._dir = [...shot.dir];
    this._target = [...shot.target];

    this._orbitYawOffset = 0;
    this._orbitPitchOffset = 0;
    this._orbitDistOffset = 0;

    this._flight = null;
    this._listeners = new Set();
  }

  get currentShot() {
    return this._currentShot;
  }

  get targetShot() {
    return this._targetShot;
  }

  get isTransitioning() {
    return this._isTransitioning;
  }

  get isInputLocked() {
    return this._isTransitioning;
  }

  get progress() {
    return this._progress;
  }

  get durationMs() {
    return this._durationMs;
  }

  setDuration(ms) {
    const val = Number(ms);
    if (Number.isFinite(val) && val >= 0) {
      this._durationMs = val;
    }
  }

  getShotNames() {
    return Object.keys(NAMED_SHOTS);
  }

  onShotChange(callback) {
    if (typeof callback !== "function") {
      return () => {};
    }
    this._listeners.add(callback);
    return () => {
      this._listeners.delete(callback);
    };
  }

  _emit(phase, previousShot) {
    const payload = {
      phase,
      shot: this._currentShot,
      targetShot: this._targetShot,
      previousShot,
      isTransitioning: this._isTransitioning,
      isInputLocked: this._isTransitioning,
      progress: this._progress,
    };
    for (const cb of this._listeners) {
      try {
        cb(payload);
      } catch {}
    }
  }

  _computeCurrentPose() {
    if (this._isTransitioning || this._currentShot !== "overview") {
      return {
        pos: [...this._pos],
        dir: [...this._dir],
        target: [...this._target],
      };
    }

    const yaw = OVERVIEW_YAW + this._orbitYawOffset;
    const pitch = Math.max(
      -1.2,
      Math.min(1.2, OVERVIEW_PITCH + this._orbitPitchOffset)
    );
    const dist = Math.max(
      14.0,
      Math.min(42.0, OVERVIEW_DIST + this._orbitDistOffset)
    );
    const pos = [
      dist * Math.cos(pitch) * Math.sin(yaw),
      dist * Math.sin(pitch),
      -dist * Math.cos(pitch) * Math.cos(yaw),
    ];
    const target = [...OVERVIEW_TARGET];
    const dir = vecNormalize(vecSub(target, pos));
    return { pos, dir, target };
  }

  goTo(shotName, options = {}) {
    if (!NAMED_SHOTS[shotName]) {
      return false;
    }

    if (this._isTransitioning) {
      return false;
    }

    if (this._currentShot === shotName) {
      return false;
    }

    const destination = NAMED_SHOTS[shotName];
    const fromPose = this._computeCurrentPose();
    const previousShot = this._currentShot;

    this._orbitYawOffset = 0;
    this._orbitPitchOffset = 0;
    this._orbitDistOffset = 0;

    const duration =
      options.duration !== undefined ? Number(options.duration) : this._durationMs;

    if (!Number.isFinite(duration) || duration <= 0) {
      this._currentShot = shotName;
      this._targetShot = shotName;
      this._isTransitioning = false;
      this._progress = 1;
      this._pos = [...destination.pos];
      this._dir = [...destination.dir];
      this._target = [...destination.target];
      this._flight = null;
      this._emit("complete", previousShot);
      return true;
    }

    this._targetShot = shotName;
    this._isTransitioning = true;
    this._progress = 0;
    this._pos = [...fromPose.pos];
    this._dir = [...fromPose.dir];
    this._target = [...fromPose.target];

    this._flight = {
      previousShot,
      toShot: shotName,
      startMs: this._now(),
      durationMs: duration,
      fromPos: fromPose.pos,
      toPos: [...destination.pos],
      fromDir: fromPose.dir,
      toDir: [...destination.dir],
      fromTarget: fromPose.target,
      toTarget: [...destination.target],
    };

    this._emit("start", previousShot);
    return true;
  }

  toggle(options = {}) {
    if (this._isTransitioning) return false;
    const next = this._currentShot === "overview" ? "satellite" : "overview";
    return this.goTo(next, options);
  }

  applyOverviewOrbit(deltaYaw, deltaPitch, deltaDist = 0) {
    if (this._isTransitioning || this._currentShot !== "overview") {
      return false;
    }
    this._orbitYawOffset += deltaYaw;
    this._orbitPitchOffset = Math.max(
      -1.15 - OVERVIEW_PITCH,
      Math.min(1.15 - OVERVIEW_PITCH, this._orbitPitchOffset + deltaPitch)
    );
    this._orbitDistOffset = Math.max(
      14.0 - OVERVIEW_DIST,
      Math.min(42.0 - OVERVIEW_DIST, this._orbitDistOffset + deltaDist)
    );
    return true;
  }

  step(nowMs = this._now()) {
    if (!this._isTransitioning || !this._flight) {
      const pose = this._computeCurrentPose();
      this._pos = pose.pos;
      this._dir = pose.dir;
      this._target = pose.target;
      return this.getCameraBasis();
    }

    const f = this._flight;
    const elapsed = Math.max(0, nowMs - f.startMs);
    const rawT = Math.min(1, elapsed / f.durationMs);
    const eased = easeInOutCubic(rawT);

    const arcLift = Math.sin(rawT * Math.PI);
    const basePos = vecLerp(f.fromPos, f.toPos, eased);
    this._pos = [
      basePos[0] + arcLift * 0.55,
      basePos[1] + arcLift * 0.28,
      basePos[2],
    ];
    this._dir = slerpDirection(f.fromDir, f.toDir, eased);
    this._target = vecLerp(f.fromTarget, f.toTarget, eased);
    this._progress = rawT;

    if (rawT >= 1) {
      const previousShot = f.previousShot;
      this._currentShot = f.toShot;
      this._targetShot = f.toShot;
      this._isTransitioning = false;
      this._progress = 1;
      this._pos = [...f.toPos];
      this._dir = [...f.toDir];
      this._target = [...f.toTarget];
      this._flight = null;
      this._emit("complete", previousShot);
    }

    return this.getCameraBasis();
  }

  getCameraBasis() {
    const pose = this._isTransitioning
      ? { pos: this._pos, dir: this._dir, target: this._target }
      : this._computeCurrentPose();
    const fwd = vecNormalize(pose.dir);
    let right = vecCross(fwd, [0, 1, 0]);
    if (vecLen(right) < 1e-5) {
      right = [1, 0, 0];
    } else {
      right = vecNormalize(right);
    }
    const up = vecNormalize(vecCross(right, fwd));
    return {
      pos: pose.pos,
      fwd,
      right,
      up,
      target: pose.target,
      fov: CAMERA_FOV,
    };
  }

  projectWorldToScreen(worldPt, viewportWidth, viewportHeight) {
    const { pos, fwd, right, up, fov } = this.getCameraBasis();
    const v = vecSub(worldPt, pos);
    const zCam = vecDot(v, fwd);
    if (zCam <= 0.05) {
      return { visible: false, x: -9999, y: -9999, depth: zCam };
    }
    const xCam = vecDot(v, right);
    const yCam = vecDot(v, up);
    const uvX = xCam / (zCam * fov);
    const uvY = yCam / (zCam * fov);
    const minDim = Math.min(viewportWidth, viewportHeight);
    const screenX = viewportWidth * 0.5 + uvX * minDim;
    const screenY = viewportHeight * 0.5 - uvY * minDim;
    return {
      visible: true,
      x: screenX,
      y: screenY,
      depth: zCam,
    };
  }

  isClickNearBlackHole(clientX, clientY, viewportWidth, viewportHeight) {
    const bhScreen = this.projectWorldToScreen(
      [0, 0, 0],
      viewportWidth,
      viewportHeight
    );
    if (!bhScreen.visible) return false;
    const minDim = Math.min(viewportWidth, viewportHeight);
    const radiusPx = Math.max(96, minDim * 0.36);
    const distPx = Math.hypot(clientX - bhScreen.x, clientY - bhScreen.y);
    return distPx <= radiusPx;
  }

  getState() {
    const basis = this.getCameraBasis();
    return {
      currentShot: this._currentShot,
      targetShot: this._targetShot,
      isTransitioning: this._isTransitioning,
      isInputLocked: this._isTransitioning,
      progress: Number(this._progress.toFixed(4)),
      durationMs: this._durationMs,
      pos: basis.pos.map((n) => Number(n.toFixed(4))),
      dir: basis.fwd.map((n) => Number(n.toFixed(4))),
      target: basis.target.map((n) => Number(n.toFixed(4))),
    };
  }
}

export { vecAdd, vecSub, vecScale, vecDot, vecCross, vecNormalize };
