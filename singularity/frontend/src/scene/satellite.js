import {
  rotateAroundY,
  vecAdd,
  vecCross,
  vecDot,
  vecLength,
  vecNormalize,
  vecScale,
  vecSub,
} from "./math.js";

export const SATELLITE_WORLD = {
  position: [2.5, 4.9, -5.0],
  yaw: -2.034,
  driftRadius: 0.34,
  driftHeight: 0.14,
  driftSpeed: 0.19,
  spinSpeed: 0.075,
  hitRadius: 1.05,
};

export const SATELLITE_PALETTE = {
  hull: [0.4, 0.43, 0.47],
  hullLight: [0.74, 0.77, 0.81],
  hullDark: [0.19, 0.21, 0.25],
  foil: [0.86, 0.66, 0.27],
  cell: [0.05, 0.08, 0.19],
  cellAlt: [0.09, 0.14, 0.28],
  frame: [0.6, 0.63, 0.68],
  radiator: [0.88, 0.9, 0.92],
  lens: [0.42, 0.88, 1.0],
  beaconRed: [1.0, 0.24, 0.18],
  beaconGreen: [0.32, 1.0, 0.5],
};

function makeBuilder() {
  return { data: [] };
}

function emit(builder, position, normal, color, emissive, phase) {
  builder.data.push(
    position[0],
    position[1],
    position[2],
    normal[0],
    normal[1],
    normal[2],
    color[0],
    color[1],
    color[2],
    emissive,
    phase
  );
}

function triangle(builder, p0, p1, p2, color, emissive, phase) {
  const normal = vecNormalize(vecCross(vecSub(p1, p0), vecSub(p2, p0)));
  emit(builder, p0, normal, color, emissive, phase);
  emit(builder, p1, normal, color, emissive, phase);
  emit(builder, p2, normal, color, emissive, phase);
}

function quad(builder, p0, p1, p2, p3, color, emissive, phase) {
  triangle(builder, p0, p1, p2, color, emissive, phase);
  triangle(builder, p0, p2, p3, color, emissive, phase);
}

function box(builder, center, axes, half, color, emissive, phase) {
  const corner = (sx, sy, sz) =>
    vecAdd(
      center,
      vecAdd(
        vecScale(axes.x, sx * half[0]),
        vecAdd(vecScale(axes.y, sy * half[1]), vecScale(axes.z, sz * half[2]))
      )
    );
  const c000 = corner(-1, -1, -1);
  const c100 = corner(1, -1, -1);
  const c110 = corner(1, 1, -1);
  const c010 = corner(-1, 1, -1);
  const c001 = corner(-1, -1, 1);
  const c101 = corner(1, -1, 1);
  const c111 = corner(1, 1, 1);
  const c011 = corner(-1, 1, 1);
  quad(builder, c001, c101, c111, c011, color, emissive, phase);
  quad(builder, c100, c000, c010, c110, color, emissive, phase);
  quad(builder, c101, c100, c110, c111, color, emissive, phase);
  quad(builder, c000, c001, c011, c010, color, emissive, phase);
  quad(builder, c011, c111, c110, c010, color, emissive, phase);
  quad(builder, c000, c100, c101, c001, color, emissive, phase);
}

function prism(builder, base, axis, radius, height, segments, color, emissive, phase) {
  const up = vecNormalize(axis);
  let side = vecCross(up, [0, 1, 0]);
  if (vecLength(side) < 1e-4) side = vecCross(up, [1, 0, 0]);
  side = vecNormalize(side);
  const forward = vecNormalize(vecCross(side, up));
  const bottom = [];
  const top = [];
  for (let i = 0; i < segments; i += 1) {
    const angle = (i / segments) * Math.PI * 2;
    const radial = vecAdd(vecScale(side, Math.cos(angle)), vecScale(forward, Math.sin(angle)));
    bottom.push(vecAdd(base, vecScale(radial, radius)));
    top.push(vecAdd(vecAdd(base, vecScale(up, height)), vecScale(radial, radius)));
  }
  for (let i = 0; i < segments; i += 1) {
    const next = (i + 1) % segments;
    quad(builder, bottom[i], bottom[next], top[next], top[i], color, emissive, phase);
    triangle(builder, base, bottom[next], bottom[i], color, emissive, phase);
    triangle(builder, vecAdd(base, vecScale(up, height)), top[i], top[next], color, emissive, phase);
  }
}

function cone(builder, from, to, radiusStart, radiusEnd, segments, color, emissive, phase) {
  const axis = vecNormalize(vecSub(to, from));
  let side = vecCross(axis, [0, 1, 0]);
  if (vecLength(side) < 1e-4) side = vecCross(axis, [1, 0, 0]);
  side = vecNormalize(side);
  const forward = vecNormalize(vecCross(side, axis));
  const ringStart = [];
  const ringEnd = [];
  for (let i = 0; i < segments; i += 1) {
    const angle = (i / segments) * Math.PI * 2;
    const radial = vecAdd(vecScale(side, Math.cos(angle)), vecScale(forward, Math.sin(angle)));
    ringStart.push(vecAdd(from, vecScale(radial, radiusStart)));
    ringEnd.push(vecAdd(to, vecScale(radial, radiusEnd)));
  }
  for (let i = 0; i < segments; i += 1) {
    const next = (i + 1) % segments;
    quad(builder, ringStart[i], ringStart[next], ringEnd[next], ringEnd[i], color, emissive, phase);
    if (radiusStart > 1e-4) {
      triangle(builder, from, ringStart[next], ringStart[i], color, emissive, phase);
    }
    if (radiusEnd > 1e-4) {
      triangle(builder, to, ringEnd[i], ringEnd[next], color, emissive, phase);
    }
  }
}

function beacon(builder, center, radius, color, phase) {
  const segments = 8;
  const rings = 4;
  const points = [];
  for (let r = 0; r < rings; r += 1) {
    const phi = (r / (rings - 1)) * Math.PI;
    const ring = [];
    for (let s = 0; s < segments; s += 1) {
      const theta = (s / segments) * Math.PI * 2;
      const x = Math.sin(phi) * Math.cos(theta);
      const y = Math.cos(phi);
      const z = Math.sin(phi) * Math.sin(theta);
      ring.push(vecAdd(center, [x * radius, y * radius, z * radius]));
    }
    points.push(ring);
  }
  for (let r = 0; r < rings - 1; r += 1) {
    for (let s = 0; s < segments; s += 1) {
      const next = (s + 1) % segments;
      quad(
        builder,
        points[r][s],
        points[r][next],
        points[r + 1][next],
        points[r + 1][s],
        color,
        1,
        phase
      );
    }
  }
}

function buildLocal(builder) {
  const palette = SATELLITE_PALETTE;
  const forward = [1, 0, 0];
  const up = [0, 1, 0];
  const side = [0, 0, 1];
  const axes = { x: forward, y: up, z: side };

  prism(builder, [0, -0.46, 0], up, 0.42, 0.26, 10, palette.hullDark, 0.0, 0);
  prism(builder, [0, -0.2, 0], up, 0.5, 0.62, 10, palette.hull, 0.0, 0);
  prism(builder, [0, 0.06, 0], up, 0.52, 0.18, 10, palette.foil, 0.08, 0);
  prism(builder, [0, 0.24, 0], up, 0.46, 0.2, 10, palette.hullLight, 0.0, 0);
  prism(builder, [0, 0.44, 0], up, 0.28, 0.1, 10, palette.hullDark, 0.0, 0);

  box(builder, [-0.42, -0.02, 0.3], axes, [0.16, 0.26, 0.12], palette.hullDark, 0.0, 0);
  box(builder, [-0.42, -0.02, -0.3], axes, [0.16, 0.26, 0.12], palette.hullDark, 0.0, 0);
  box(builder, [0.16, 0.3, 0.4], axes, [0.14, 0.08, 0.08], palette.hullLight, 0.0, 0);
  box(builder, [0.16, 0.3, -0.4], axes, [0.14, 0.08, 0.08], palette.hullLight, 0.0, 0);

  box(builder, [-0.52, 0.02, 0], axes, [0.02, 0.34, 0.26], palette.radiator, 0.0, 0);
  box(builder, [-0.5, 0.02, 0], axes, [0.035, 0.36, 0.28], palette.frame, 0.0, 0);

  const tankLeft = vecAdd([-0.12, -0.3, 0], vecScale(side, 0.62));
  const tankRight = vecAdd([-0.12, -0.3, 0], vecScale(side, -0.62));
  cone(builder, tankLeft, vecAdd(tankLeft, [0.5, 0, 0]), 0.13, 0.13, 10, palette.hullLight, 0.0, 0);
  cone(builder, tankRight, vecAdd(tankRight, [0.5, 0, 0]), 0.13, 0.13, 10, palette.hullLight, 0.0, 0);

  const wingTilt = vecNormalize([0.1, 0.3, 1]);
  const wingNormal = vecNormalize(vecCross(wingTilt, side));
  const wingAxes = { x: wingNormal, y: wingTilt, z: side };

  for (const direction of [-1, 1]) {
    const boomStart = [0, 0.02, direction * 0.42];
    const boomEnd = [0, 0.1, direction * 0.78];
    cone(builder, boomStart, boomEnd, 0.045, 0.03, 8, palette.frame, 0.0, 0);

    for (let index = 0; index < 3; index += 1) {
      const distance = 0.92 + index * 0.38;
      const center = [0, 0.1 + index * 0.035, direction * distance];
      box(builder, center, wingAxes, [0.66, 0.012, 0.17], palette.frame, 0.0, 0);
      for (let row = 0; row < 3; row += 1) {
        for (let column = 0; column < 2; column += 1) {
          const cellCenter = vecAdd(
            center,
            vecAdd(
              vecScale(wingNormal, (row - 1) * 0.21),
              vecScale(side, (column - 0.5) * 0.6)
            )
          );
          box(
            builder,
            vecAdd(cellCenter, vecScale(wingNormal, 0.014)),
            wingAxes,
            [0.095, 0.005, 0.29],
            (row + column) % 2 === 0 ? palette.cell : palette.cellAlt,
            0.14,
            0
          );
        }
      }
      box(
        builder,
        vecAdd(center, vecScale(wingNormal, 0.02)),
        wingAxes,
        [0.02, 0.01, 0.17],
        palette.frame,
        0.0,
        0
      );
    }
  }

  const mastBase = [0, 0.54, 0];
  const mastTop = [0, 0.76, 0];
  cone(builder, mastBase, mastTop, 0.035, 0.024, 8, palette.frame, 0.0, 0);

  const dishAim = vecNormalize([-0.36, 0.9, 0.24]);
  const dishRim = vecAdd(mastTop, vecScale(dishAim, 0.16));
  cone(builder, mastTop, dishRim, 0.03, 0.17, 14, palette.hullLight, 0.05, 0);
  cone(
    builder,
    vecAdd(mastTop, vecScale(dishAim, 0.145)),
    vecAdd(mastTop, vecScale(dishAim, 0.17)),
    0.175,
    0.175,
    14,
    palette.frame,
    0.1,
    0
  );
  const feedTip = vecAdd(mastTop, vecScale(dishAim, 0.2));
  cone(builder, mastTop, feedTip, 0.016, 0.01, 6, palette.hullLight, 0.0, 0);
  cone(
    builder,
    feedTip,
    vecAdd(feedTip, vecScale(dishAim, 0.04)),
    0.024,
    0.01,
    8,
    palette.beaconGreen,
    0.85,
    1.4
  );

  const podBase = [0.42, -0.04, 0];
  const podMid = [0.82, -0.04, 0];
  const podEnd = [1.02, -0.04, 0];
  cone(builder, podBase, podMid, 0.16, 0.18, 12, palette.hull, 0.0, 0);
  cone(builder, podMid, podEnd, 0.18, 0.15, 12, palette.hullLight, 0.04, 0);
  cone(builder, podEnd, [1.09, -0.04, 0], 0.15, 0.13, 12, palette.lens, 0.85, 0);
  cone(builder, [1.09, -0.04, 0], [1.14, -0.04, 0], 0.12, 0.04, 12, [0.82, 0.96, 1.0], 1.0, 0);

  const thrusterPositions = [
    [-0.26, -0.44, 0.2],
    [-0.26, -0.44, -0.2],
    [0.26, -0.44, 0.2],
    [0.26, -0.44, -0.2],
  ];
  for (const position of thrusterPositions) {
    cone(
      builder,
      position,
      [position[0], position[1] - 0.16, position[2]],
      0.07,
      0.1,
      8,
      palette.hullDark,
      0.0,
      0
    );
  }

  const beaconPositions = [
    [-0.44, 0.3, 0.22],
    [-0.44, 0.3, -0.22],
    [0.22, 0.3, 0.4],
    [0.22, 0.3, -0.4],
  ];
  const beaconPhases = [0, 1.9, 3.4, 5.1];
  beaconPositions.forEach((position, index) => {
    beacon(
      builder,
      position,
      0.035,
      index % 2 === 0 ? palette.beaconRed : palette.beaconGreen,
      beaconPhases[index]
    );
  });

  const antennaBase = [-0.3, 0.42, 0.18];
  const antennaTip = [-0.58, 0.82, 0.28];
  cone(builder, antennaBase, antennaTip, 0.014, 0.007, 6, palette.frame, 0.0, 0);
  cone(
    builder,
    antennaTip,
    vecAdd(antennaTip, [-0.05, 0.09, 0.03]),
    0.018,
    0.005,
    6,
    palette.beaconRed,
    0.7,
    2.7
  );

  const strutBase = [-0.16, 0.56, 0.2];
  const strutTip = [-0.52, 0.62, 0.38];
  cone(builder, strutBase, strutTip, 0.012, 0.009, 6, palette.frame, 0.0, 0);
}

export function buildSatelliteMesh() {
  const builder = makeBuilder();
  buildLocal(builder);
  return {
    vertexData: new Float32Array(builder.data),
    vertexCount: builder.data.length / 11,
    strideBytes: 11 * 4,
  };
}

export function satellitePose(timeSec) {
  const drift = SATELLITE_WORLD.driftSpeed;
  return {
    offset: vecAdd(SATELLITE_WORLD.position, [
      Math.sin(timeSec * drift) * SATELLITE_WORLD.driftRadius,
      Math.sin(timeSec * drift * 0.73 + 1.1) * SATELLITE_WORLD.driftHeight,
      Math.cos(timeSec * drift * 0.61) * SATELLITE_WORLD.driftRadius * 0.6,
    ]),
    yaw: SATELLITE_WORLD.yaw + Math.sin(timeSec * SATELLITE_WORLD.spinSpeed) * 0.22,
  };
}

export function satelliteDistanceTo(point) {
  return vecDistance(point, SATELLITE_WORLD.position);
}

export function satelliteFacingDirection() {
  const forward = rotateAroundY([1, 0, 0], SATELLITE_WORLD.yaw);
  return vecNormalize(forward);
}

export function dotAgainstDirection(a, b) {
  return vecDot(a, b);
}
