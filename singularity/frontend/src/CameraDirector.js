export const CAMERA_PRESETS = {
  overview: {
    id: 'overview',
    label: 'Deep Orbit',
    tag: 'NGC 4459 - ACCRETION VORTEX',
    title: 'Supermassive Singularity',
    desc: 'Deep spacetime observation of a Kerr black hole. Relativistic gravitational lensing bends background starlight into upper and lower Einstein arcs.',
    yaw: 0.0,
    pitch: 0.18,
    dist: 26.0,
    pos3D: [0.0, 0.0, 0.0],
    telemetry: { mass: '4.1 × 10⁶ M⊙', spin: 'a* = 0.94', dist: '26,000 ly' }
  },
  horizon: {
    id: 'event-horizon',
    label: 'Event Horizon',
    tag: 'SCHWARZSCHILD RADIUS - r = 1.0',
    title: 'The Event Horizon',
    desc: 'The absolute boundary beyond which no radiation or matter can escape. Surrounded by a razor-sharp photon sphere where light orbits unstably.',
    yaw: 0.05,
    pitch: 0.08,
    dist: 14.0,
    pos3D: [0.0, 0.0, 0.0],
    telemetry: { radius: '12.4 × 10⁶ km', dilation: '∞ (T0 -> 0)', temp: '1.4 × 10⁻¹⁴ K' }
  },
  disk: {
    id: 'accretion-disk',
    label: 'Accretion Disk',
    tag: 'LAMINAR PLASMA - r = 3.2',
    title: 'Accretion Disk',
    desc: 'Superheated matter spiraling inward, sheared by differential rotation into intense radiant silver bands with noticeable Doppler beaming on the approaching side.',
    yaw: 0.85,
    pitch: 0.12,
    dist: 18.0,
    pos3D: [3.2, 0.0, 0.0],
    telemetry: { velocity: '0.68 c', temp: '1.2 × 10⁷ K', flux: '4.8 × 10³⁸ W' }
  },
  photon: {
    id: 'photon-sphere',
    label: 'Photon Ring',
    tag: 'CAUSTIC SPHERE - r = 1.52',
    title: 'Photon Ring',
    desc: 'A razor-thin luminous boundary where photons orbit unstably multiple times before escaping to the observer.',
    yaw: 0.0,
    pitch: 0.75,
    dist: 20.0,
    pos3D: [0.0, 3.0, 0.0],
    telemetry: { radius: '1.52 r_s', orbits: 'n -> ∞', temp: '2.8 × 10⁷ K' }
  },
  lensing: {
    id: 'lensing-arc',
    label: 'Einstein Ring',
    tag: 'GEODESIC WARPING - PHOTON ARC',
    title: 'Gravitational Lensing',
    desc: 'Extreme spacetime curvature bends null geodesics from behind the event horizon over the top and bottom, projecting a double image of the rear disk.',
    yaw: -0.95,
    pitch: 0.32,
    dist: 19.0,
    pos3D: [-2.6, 0.6, 1.2],
    telemetry: { deflection: '4GM / c²r', arcRadius: '2.6 r_s', redshift: 'z = 2.41' }
  }
};

export class CameraDirector {
  constructor(onChange) {
    this.yaw = CAMERA_PRESETS.overview.yaw;
    this.pitch = CAMERA_PRESETS.overview.pitch;
    this.dist = CAMERA_PRESETS.overview.dist;

    this.fromYaw = this.yaw;
    this.fromPitch = this.pitch;
    this.fromDist = this.dist;

    this.targetYaw = this.yaw;
    this.targetPitch = this.pitch;
    this.targetDist = this.dist;

    this.animating = false;
    this.startTime = 0;
    this.duration = 1400;
    this.onChange = onChange;

    this.yawVel = 0;
    this.pitchVel = 0;
    this.autoRotate = true;
    this.dragging = false;
  }

  flyTo(preset, duration = 1400) {
    this.fromYaw = this.yaw;
    this.fromPitch = this.pitch;
    this.fromDist = this.dist;

    this.targetYaw = preset.yaw;
    this.targetPitch = preset.pitch;
    this.targetDist = preset.dist;

    this.duration = duration;
    this.startTime = performance.now();
    this.animating = true;
    this.autoRotate = false;
    this.yawVel = 0;
    this.pitchVel = 0;
  }

  update(now) {
    if (this.animating) {
      const elapsed = now - this.startTime;
      const progress = Math.min(1.0, elapsed / this.duration);
      const t = 1 - Math.pow(1 - progress, 4);

      let dy = this.targetYaw - this.fromYaw;
      this.yaw = this.fromYaw + dy * t;
      this.pitch = this.fromPitch + (this.targetPitch - this.fromPitch) * t;
      this.dist = this.fromDist + (this.targetDist - this.fromDist) * t;

      if (progress >= 1.0) {
        this.animating = false;
        this.yaw = this.targetYaw;
        this.pitch = this.targetPitch;
        this.dist = this.targetDist;
      }
    } else if (!this.dragging) {
      this.yawVel *= 0.92;
      this.pitchVel *= 0.92;
      this.yaw += this.yawVel;
      this.pitch = Math.max(-1.42, Math.min(1.42, this.pitch + this.pitchVel));
      if (this.autoRotate) {
        this.yaw += 0.0009;
      }
    }

    if (this.onChange) {
      this.onChange(this.yaw, this.pitch, this.dist);
    }
  }
}
