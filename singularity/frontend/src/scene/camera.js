import {
  clamp,
  easeInOutCubic,
  rotateAroundY,
  slerpDirection,
  vecAdd,
  vecCross,
  vecDot,
  vecLength,
  vecLerp,
  vecNormalize,
  vecScale,
  vecSub,
} from "./math.js";
import { SATELLITE_WORLD } from "./satellite.js";

export const BASE_FOV = 0.85;

export const SHOTS = {
  overview: {
    name: "overview",
    distance: 26.0,
    pitch: 0.18,
    yaw: 0.0,
    target: [0, 0, 0],
  },
  satellite: {
    name: "satellite",
    offsetLocal: [5.6, 1.9, 3.0],
    lookOffset: [0, -0.3, 0],
    target: SATELLITE_WORLD.position,
  },
};

function overviewPose(yaw, pitch, distance) {
  const safePitch = clamp(pitch, -1.05, 1.05);
  const safeDistance = clamp(distance, 12.0, 48);
  const position = [
    safeDistance * Math.cos(safePitch) * Math.sin(yaw),
    safeDistance * Math.sin(safePitch),
    -safeDistance * Math.cos(safePitch) * Math.cos(yaw),
  ];
  return { position, target: [0, 0, 0] };
}

function satellitePose(satellitePosition) {
  const target = vecAdd(satellitePosition, SHOTS.satellite.lookOffset);
  const offset = rotateAroundY(
    SHOTS.satellite.offsetLocal,
    SATELLITE_WORLD.yaw
  );
  const position = vecAdd(target, offset);
  return { position, target };
}

export class CameraDirector {
  constructor(options = {}) {
    this._durationMs = Number(options.durationMs ?? 2200);
    this._now =
      typeof options.now === "function"
        ? options.now
        : () => (typeof performance !== "undefined" ? performance.now() : Date.now());
    this._currentShot = "overview";
    this._targetShot = "overview";
    this._isTransitioning = false;
    this._progress = 1;
    this._flight = null;
    this._listeners = new Set();
    this._orbitYaw = 0;
    this._orbitPitch = 0;
    this._orbitDistance = 0;
    this._satellitePosition = [...SATELLITE_WORLD.position];
    this._pose = overviewPose(0, SHOTS.overview.pitch, SHOTS.overview.distance);
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

  setDuration(value) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) {
      this._durationMs = parsed;
    }
  }

  getShotNames() {
    return Object.keys(SHOTS);
  }

  get satellitePosition() {
    return [...this._satellitePosition];
  }

  onShotChange(callback) {
    if (typeof callback !== "function") return () => {};
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
    for (const callback of this._listeners) {
      try {
        callback(payload);
      } catch {}
    }
  }

  setSatellitePosition(position) {
    if (!position) return;
    this._satellitePosition = [...position];
  }

  _currentPose() {
    if (this._currentShot === "satellite") {
      return satellitePose(this._satellitePosition);
    }
    return overviewPose(
      SHOTS.overview.yaw + this._orbitYaw,
      SHOTS.overview.pitch + this._orbitPitch,
      SHOTS.overview.distance + this._orbitDistance
    );
  }

  _destinationPose(shotName) {
    if (shotName === "satellite") {
      return satellitePose(this._satellitePosition);
    }
    return overviewPose(
      SHOTS.overview.yaw,
      SHOTS.overview.pitch,
      SHOTS.overview.distance
    );
  }

  goTo(shotName, options = {}) {
    if (!SHOTS[shotName]) return false;
    if (this._isTransitioning) return false;
    if (this._currentShot === shotName) return false;

    const previousShot = this._currentShot;
    const from = this._currentPose();
    const to = this._destinationPose(shotName);
    const duration =
      options.duration !== undefined ? Number(options.duration) : this._durationMs;

    this._orbitYaw = 0;
    this._orbitPitch = 0;
    this._orbitDistance = 0;

    if (!Number.isFinite(duration) || duration <= 0) {
      this._currentShot = shotName;
      this._targetShot = shotName;
      this._isTransitioning = false;
      this._progress = 1;
      this._pose = to;
      this._emit("complete", previousShot);
      return true;
    }

    this._targetShot = shotName;
    this._isTransitioning = true;
    this._progress = 0;
    this._flight = {
      previousShot,
      toShot: shotName,
      startMs: this._now(),
      durationMs: duration,
      fromPosition: from.position,
      toPosition: to.position,
      fromDirection: vecNormalize(vecSub(from.target, from.position)),
      toDirection: vecNormalize(vecSub(to.target, to.position)),
      fromTarget: from.target,
      toTarget: to.target,
    };
    this._emit("start", previousShot);
    return true;
  }

  toggle(options = {}) {
    if (this._isTransitioning) return false;
    return this.goTo(
      this._currentShot === "overview" ? "satellite" : "overview",
      options
    );
  }

  applyOrbit(deltaYaw, deltaPitch, deltaDistance = 0) {
    if (this._isTransitioning || this._currentShot !== "overview") return false;
    this._orbitYaw += deltaYaw;
    this._orbitPitch = clamp(this._orbitPitch + deltaPitch, -0.85, 0.85);
    this._orbitDistance = clamp(
      this._orbitDistance + deltaDistance,
      -6.5,
      12
    );
    return true;
  }

  step(nowMs = this._now()) {
    if (this._isTransitioning && this._flight) {
      const flight = this._flight;
      const elapsed = Math.max(0, nowMs - flight.startMs);
      const raw = Math.min(1, elapsed / flight.durationMs);
      const eased = easeInOutCubic(raw);
      const lift = Math.sin(raw * Math.PI);

      const position = vecLerp(flight.fromPosition, flight.toPosition, eased);
      position[1] += lift * 0.85;
      const direction = slerpDirection(
        flight.fromDirection,
        flight.toDirection,
        eased
      );
      const target = vecLerp(flight.fromTarget, flight.toTarget, eased);
      this._pose = { position, target, direction };
      this._progress = raw;

      if (raw >= 1) {
        this._currentShot = flight.toShot;
        this._targetShot = flight.toShot;
        this._isTransitioning = false;
        this._progress = 1;
        this._flight = null;
        this._pose = this._destinationPose(flight.toShot);
        this._emit("complete", flight.previousShot);
      }
    } else {
      this._pose = this._currentPose();
    }
    return this.getBasis();
  }

  getBasis() {
    const pose = this._pose;
    const forward = pose.direction
      ? vecNormalize(pose.direction)
      : vecNormalize(vecSub(pose.target, pose.position));
    let right = vecCross(forward, [0, 1, 0]);
    if (vecLength(right) < 1e-5) {
      right = [1, 0, 0];
    } else {
      right = vecNormalize(right);
    }
    const up = vecNormalize(vecCross(right, forward));
    return {
      position: [...pose.position],
      forward,
      right,
      up,
      target: [...pose.target],
      fov: BASE_FOV,
    };
  }

  projectWorldToScreen(point, viewportWidth, viewportHeight) {
    const basis = this.getBasis();
    const relative = vecSub(point, basis.position);
    const depth = vecDot(relative, basis.forward);
    if (depth <= 0.05) {
      return { visible: false, x: -9999, y: -9999, depth };
    }
    const horizontal = vecDot(relative, basis.right);
    const vertical = vecDot(relative, basis.up);
    const minDim = Math.min(viewportWidth, viewportHeight);
    const scale = minDim * basis.fov;
    return {
      visible: true,
      x: viewportWidth * 0.5 + (horizontal / (depth * basis.fov)) * minDim,
      y: viewportHeight * 0.5 - (vertical / (depth * basis.fov)) * minDim,
      depth,
    };
  }

  isPointerOnSatellite(clientX, clientY, viewportWidth, viewportHeight) {
    const center = this.projectWorldToScreen(
      this._satellitePosition,
      viewportWidth,
      viewportHeight
    );
    if (!center.visible) return false;
    const minDim = Math.min(viewportWidth, viewportHeight);
    const radius = Math.max(46, minDim * SATELLITE_WORLD.hitRadius * 0.16);
    return Math.hypot(clientX - center.x, clientY - center.y) <= radius;
  }

  getState() {
    const basis = this.getBasis();
    return {
      currentShot: this._currentShot,
      targetShot: this._targetShot,
      isTransitioning: this._isTransitioning,
      isInputLocked: this._isTransitioning,
      progress: Number(this._progress.toFixed(4)),
      durationMs: this._durationMs,
      position: basis.position.map((value) => Number(value.toFixed(4))),
      direction: basis.forward.map((value) => Number(value.toFixed(4))),
      target: basis.target.map((value) => Number(value.toFixed(4))),
    };
  }
}

export { vecAdd, vecScale };
