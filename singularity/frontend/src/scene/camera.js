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
    distance: 22.0,
    pitch: 0.22,
    yaw: 0.0,
    target: [0, 0, 0],
  },
  singularity: {
    name: "singularity",
    distance: 4.6,
    pitch: 0.10,
    yaw: 0.22,
    target: [0, 0, 0],
  },
  satellite: {
    name: "satellite",
    offsetLocal: [5.2, 1.8, 2.8],
    lookOffset: [0, -0.3, 0],
    target: SATELLITE_WORLD.position,
  },
};

function overviewPose(yaw, pitch, distance, turnYaw = 0, turnPitch = 0) {
  const safePitch = clamp(pitch, -1.15, 1.15);
  const safeDistance = clamp(distance, -32.0, 54.0);
  const forwardTravel = [
    -Math.cos(safePitch) * Math.sin(yaw),
    -Math.sin(safePitch),
    Math.cos(safePitch) * Math.cos(yaw),
  ];
  if (safeDistance >= 0) {
    const position = [
      safeDistance * Math.cos(safePitch) * Math.sin(yaw),
      safeDistance * Math.sin(safePitch),
      -safeDistance * Math.cos(safePitch) * Math.cos(yaw),
    ];
    return { position, target: [0, 0, 0], direction: forwardTravel };
  } else {
    const pastDist = -safeDistance;
    const position = [
      forwardTravel[0] * pastDist,
      forwardTravel[1] * pastDist,
      forwardTravel[2] * pastDist,
    ];
    const curPitch = clamp(safePitch + turnPitch, -1.25, 1.25);
    const cosY = Math.cos(turnYaw);
    const sinY = Math.sin(turnYaw);
    const dir = vecNormalize([
      -Math.cos(curPitch) * (Math.sin(yaw) * cosY + Math.cos(yaw) * sinY),
      -Math.sin(curPitch) * cosY,
      Math.cos(curPitch) * (Math.cos(yaw) * cosY - Math.sin(yaw) * sinY),
    ]);
    const target = [
      position[0] + dir[0] * 10.0,
      position[1] + dir[1] * 10.0,
      position[2] + dir[2] * 10.0,
    ];
    return { position, target, direction: dir };
  }
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
    this._turnYaw = 0;
    this._turnPitch = 0;
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

  get distance() {
    const baseDist = (SHOTS[this._currentShot] || SHOTS.overview).distance;
    return baseDist + this._orbitDistance;
  }

  get turnYaw() {
    return this._turnYaw;
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
    const shot = SHOTS[this._currentShot] || SHOTS.overview;
    return overviewPose(
      shot.yaw + this._orbitYaw,
      shot.pitch + this._orbitPitch,
      shot.distance + this._orbitDistance,
      this._turnYaw,
      this._turnPitch
    );
  }

  _destinationPose(shotName) {
    if (shotName === "satellite") {
      return satellitePose(this._satellitePosition);
    }
    const shot = SHOTS[shotName] || SHOTS.overview;
    return overviewPose(
      shot.yaw,
      shot.pitch,
      shot.distance,
      0,
      0
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
    this._turnYaw = 0;
    this._turnPitch = 0;

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
    if (this._currentShot === "overview") {
      return this.goTo("singularity", options);
    } else if (this._currentShot === "singularity") {
      return this.goTo("satellite", options);
    } else {
      return this.goTo("overview", options);
    }
  }

  zoomDelta(delta) {
    if (this._isTransitioning) return false;
    const isOrbitable =
      this._currentShot === "overview" || this._currentShot === "singularity";
    if (!isOrbitable) return false;
    if (typeof delta !== "number" || Number.isNaN(delta)) return false;
    const baseDist = (SHOTS[this._currentShot] || SHOTS.overview).distance;
    const curDist = baseDist + this._orbitDistance;
    let targetDist = clamp(curDist + delta, -32.0, 52.0);
    if (curDist < 0 && targetDist >= 0) {
      this._turnYaw = 0;
      this._turnPitch = 0;
    }
    this._orbitDistance = targetDist - baseDist;
    return true;
  }

  zoomBy(factor) {
    if (typeof factor !== "number" || Number.isNaN(factor)) return false;
    const step = factor < 1.0 ? -0.85 : 0.85;
    return this.zoomDelta(step);
  }

  toggleTurn() {
    const baseDist = (SHOTS[this._currentShot] || SHOTS.overview).distance;
    const curDist = baseDist + this._orbitDistance;
    if (curDist < 0) {
      const nearPi = Math.abs(this._turnYaw - Math.PI) < 0.6;
      this._turnYaw = nearPi ? 0 : Math.PI;
      this._turnPitch = 0;
      return true;
    }
    return false;
  }

  applyOrbit(deltaYaw, deltaPitch, deltaDistance = 0) {
    if (
      this._isTransitioning ||
      (this._currentShot !== "overview" && this._currentShot !== "singularity")
    ) {
      return false;
    }
    const baseDist = (SHOTS[this._currentShot] || SHOTS.overview).distance;
    const curDist = baseDist + this._orbitDistance;
    if (curDist >= 0) {
      this._orbitYaw += deltaYaw;
      this._orbitPitch = clamp(this._orbitPitch + deltaPitch, -0.92, 0.92);
      this._turnYaw = 0;
      this._turnPitch = 0;
    } else {
      this._turnYaw += deltaYaw * 1.25;
      this._turnPitch = clamp(this._turnPitch + deltaPitch, -0.85, 0.85);
    }
    if (deltaDistance !== 0) {
      this.zoomDelta(deltaDistance);
    }
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
    const basis = this.getBasis();
    const camPos = basis.position;
    const satPos = this._satellitePosition;
    const camToSat = vecSub(satPos, camPos);
    const totalDist = vecLength(camToSat);
    if (totalDist < 1e-4) return false;
    const rayDir = vecScale(camToSat, 1 / totalDist);

    const tOrigin = -vecDot(camPos, rayDir);
    if (tOrigin > 0 && tOrigin < totalDist) {
      const pClosest = vecAdd(camPos, vecScale(rayDir, tOrigin));
      if (vecLength(pClosest) < 2.65) {
        return false;
      }
    }
    if (Math.abs(rayDir[1]) > 1e-4) {
      const tDisk = -camPos[1] / rayDir[1];
      if (tDisk > 0 && tDisk < totalDist) {
        const pDisk = vecAdd(camPos, vecScale(rayDir, tDisk));
        if (Math.hypot(pDisk[0], pDisk[2]) < 6.5) {
          return false;
        }
      }
    }

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
