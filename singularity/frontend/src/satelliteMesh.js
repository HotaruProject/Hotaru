import {
  SATELLITE_WORLD,
  vecAdd,
  vecCross,
  vecNormalize,
  vecScale,
  vecSub,
} from "./SceneDirector";

function pushTriangle(out, p0, p1, p2, color, emissive = 0.0) {
  const e1 = vecSub(p1, p0);
  const e2 = vecSub(p2, p0);
  const n = vecNormalize(vecCross(e1, e2));
  const verts = [p0, p1, p2];
  for (const p of verts) {
    out.push(
      p[0],
      p[1],
      p[2],
      n[0],
      n[1],
      n[2],
      color[0],
      color[1],
      color[2],
      emissive
    );
  }
}

function pushQuad(out, p0, p1, p2, p3, color, emissive = 0.0) {
  pushTriangle(out, p0, p1, p2, color, emissive);
  pushTriangle(out, p0, p2, p3, color, emissive);
}

function addOrientedBox(out, center, axisX, axisY, axisZ, halfExtents, color, emissive = 0.0) {
  const [hx, hy, hz] = halfExtents;
  const corner = (sx, sy, sz) =>
    vecAdd(
      center,
      vecAdd(
        vecScale(axisX, sx * hx),
        vecAdd(vecScale(axisY, sy * hy), vecScale(axisZ, sz * hz))
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

  pushQuad(out, c001, c101, c111, c011, color, emissive);
  pushQuad(out, c100, c000, c010, c110, color, emissive);
  pushQuad(out, c101, c100, c110, c111, color, emissive);
  pushQuad(out, c000, c001, c011, c010, color, emissive);
  pushQuad(out, c011, c111, c110, c010, color, emissive);
  pushQuad(out, c000, c100, c101, c001, color, emissive);
}

function addFrustum(
  out,
  startPos,
  endPos,
  rStart,
  rEnd,
  segments,
  color,
  emissive = 0.0,
  capStart = true,
  capEnd = true
) {
  const axis = vecNormalize(vecSub(endPos, startPos));
  let u = vecCross(axis, [0, 1, 0]);
  if (Math.hypot(u[0], u[1], u[2]) < 1e-4) {
    u = vecCross(axis, [1, 0, 0]);
  }
  u = vecNormalize(u);
  const v = vecNormalize(vecCross(axis, u));

  const ringStart = [];
  const ringEnd = [];
  for (let i = 0; i < segments; i += 1) {
    const a = (i / segments) * Math.PI * 2;
    const ca = Math.cos(a);
    const sa = Math.sin(a);
    const radial = vecAdd(vecScale(u, ca), vecScale(v, sa));
    ringStart.push(vecAdd(startPos, vecScale(radial, rStart)));
    ringEnd.push(vecAdd(endPos, vecScale(radial, rEnd)));
  }

  for (let i = 0; i < segments; i += 1) {
    const next = (i + 1) % segments;
    pushQuad(
      out,
      ringStart[i],
      ringStart[next],
      ringEnd[next],
      ringEnd[i],
      color,
      emissive
    );
    if (capStart && rStart > 1e-4) {
      pushTriangle(out, startPos, ringStart[next], ringStart[i], color, emissive);
    }
    if (capEnd && rEnd > 1e-4) {
      pushTriangle(out, endPos, ringEnd[i], ringEnd[next], color, emissive);
    }
  }
}

export function buildSatelliteMesh() {
  const verts = [];
  const center = SATELLITE_WORLD.busPos;
  const lensTarget = SATELLITE_WORLD.projectorLensPos;

  const axisX = vecNormalize(vecSub(lensTarget, center));
  const worldUp = [0.06, 0.996, -0.05];
  const axisZ = vecNormalize(vecCross(axisX, worldUp));
  const axisY = vecNormalize(vecCross(axisZ, axisX));

  const TITANIUM = [0.34, 0.39, 0.46];
  const TITANIUM_LIGHT = [0.56, 0.62, 0.70];
  const FOIL_BAND = [0.68, 0.57, 0.38];
  const SOLAR_CELL = [0.14, 0.23, 0.36];
  const SOLAR_FRAME = [0.48, 0.55, 0.64];
  const PROJECTOR_HOUSING = [0.42, 0.49, 0.58];
  const PROJECTOR_LENS = [0.58, 0.93, 1.0];
  const BEACON_WARM = [0.95, 0.78, 0.52];

  const busBottom = vecAdd(center, vecScale(axisY, -0.22));
  const busTop = vecAdd(center, vecScale(axisY, 0.22));
  addFrustum(verts, busBottom, busTop, 0.21, 0.19, 8, TITANIUM, 0.05);

  addFrustum(
    verts,
    vecAdd(center, vecScale(axisY, -0.055)),
    vecAdd(center, vecScale(axisY, 0.055)),
    0.222,
    0.222,
    8,
    FOIL_BAND,
    0.12
  );

  addOrientedBox(
    verts,
    vecAdd(center, vecScale(axisX, -0.17)),
    axisX,
    axisY,
    axisZ,
    [0.08, 0.13, 0.13],
    TITANIUM_LIGHT,
    0.06
  );

  for (const sy of [-0.07, 0.07]) {
    const base = vecAdd(
      center,
      vecAdd(vecScale(axisX, -0.25), vecScale(axisY, sy))
    );
    const tip = vecAdd(base, vecScale(axisX, -0.07));
    addFrustum(verts, base, tip, 0.032, 0.05, 6, TITANIUM_LIGHT, 0.15);
  }

  const wingTiltY = vecNormalize(vecAdd(vecScale(axisY, 0.86), vecScale(axisX, -0.5)));
  const wingNormal = vecNormalize(vecCross(wingTiltY, axisZ));
  for (const side of [-1, 1]) {
    const boomStart = vecAdd(center, vecScale(axisZ, side * 0.18));
    const boomEnd = vecAdd(center, vecScale(axisZ, side * 0.86));
    addFrustum(verts, boomStart, boomEnd, 0.018, 0.015, 6, SOLAR_FRAME, 0.08);

    for (let seg = 0; seg < 3; seg += 1) {
      const dist = 0.35 + seg * 0.22;
      const segCenter = vecAdd(center, vecScale(axisZ, side * dist));
      addOrientedBox(
        verts,
        segCenter,
        wingTiltY,
        wingNormal,
        axisZ,
        [0.145, 0.011, 0.098],
        SOLAR_FRAME,
        0.10
      );
      addOrientedBox(
        verts,
        segCenter,
        wingTiltY,
        wingNormal,
        axisZ,
        [0.132, 0.014, 0.086],
        SOLAR_CELL,
        0.18
      );
    }
  }

  const mastBase = busTop;
  const mastTop = vecAdd(busTop, vecScale(axisY, 0.13));
  addFrustum(verts, mastBase, mastTop, 0.022, 0.018, 6, TITANIUM_LIGHT, 0.08);

  const dishAim = vecNormalize(vecAdd(vecScale(axisY, 0.72), vecScale(axisX, -0.68)));
  const dishRim = vecAdd(mastTop, vecScale(dishAim, 0.075));
  addFrustum(verts, mastTop, dishRim, 0.03, 0.16, 10, TITANIUM_LIGHT, 0.12, true, false);
  const hornTip = vecAdd(mastTop, vecScale(dishAim, 0.16));
  addFrustum(verts, mastTop, hornTip, 0.012, 0.008, 6, SOLAR_FRAME, 0.1);
  addFrustum(
    verts,
    hornTip,
    vecAdd(hornTip, vecScale(dishAim, 0.028)),
    0.022,
    0.004,
    6,
    BEACON_WARM,
    0.92
  );

  const podMount = vecAdd(center, vecScale(axisX, 0.18));
  const podNeck = vecAdd(center, vecScale(axisX, 0.29));
  const podBarrelEnd = vecAdd(lensTarget, vecScale(axisX, -0.025));

  addOrientedBox(
    verts,
    vecAdd(center, vecScale(axisX, 0.22)),
    axisX,
    axisY,
    axisZ,
    [0.065, 0.085, 0.085],
    PROJECTOR_HOUSING,
    0.1
  );
  addFrustum(verts, podMount, podNeck, 0.065, 0.078, 8, PROJECTOR_HOUSING, 0.14);
  addFrustum(verts, podNeck, podBarrelEnd, 0.078, 0.098, 10, TITANIUM_LIGHT, 0.22);

  addFrustum(
    verts,
    podBarrelEnd,
    lensTarget,
    0.102,
    0.092,
    12,
    PROJECTOR_LENS,
    0.95
  );
  addFrustum(
    verts,
    lensTarget,
    vecAdd(lensTarget, vecScale(axisX, 0.018)),
    0.074,
    0.032,
    12,
    [0.86, 0.98, 1.0],
    1.0
  );

  return {
    vertexData: new Float32Array(verts),
    vertexCount: verts.length / 10,
    strideBytes: 10 * 4,
  };
}

export function getTerminalPlaneQuad(cameraBasis) {
  const center = SATELLITE_WORLD.terminalCenterPos;
  const hw = SATELLITE_WORLD.terminalWidth * 0.5;
  const hh = SATELLITE_WORLD.terminalHeight * 0.5;

  const fixedRight = [-0.994, 0.0, -0.108];
  const fixedUp = [0.0, 1.0, 0.0];
  const right = cameraBasis
    ? vecNormalize(vecAdd(vecScale(fixedRight, 0.55), vecScale(cameraBasis.right, 0.45)))
    : fixedRight;
  const up = cameraBasis
    ? vecNormalize(vecAdd(vecScale(fixedUp, 0.7), vecScale(cameraBasis.up, 0.3)))
    : fixedUp;

  const tl = vecAdd(center, vecAdd(vecScale(right, -hw), vecScale(up, hh)));
  const tr = vecAdd(center, vecAdd(vecScale(right, hw), vecScale(up, hh)));
  const br = vecAdd(center, vecAdd(vecScale(right, hw), vecScale(up, -hh)));
  const bl = vecAdd(center, vecAdd(vecScale(right, -hw), vecScale(up, -hh)));

  return { center, right, up, hw, hh, corners: [tl, tr, br, bl] };
}

export function buildProjectorBeamVertices(corners) {
  const lens = SATELLITE_WORLD.projectorLensPos;
  const [tl, tr, br, bl] = corners;
  const out = [];
  const addBeamTri = (a, aAlpha, b, bAlpha, c, cAlpha) => {
    out.push(
      a[0], a[1], a[2], aAlpha,
      b[0], b[1], b[2], bAlpha,
      c[0], c[1], c[2], cAlpha
    );
  };

  const lensAlpha = 0.34;
  const planeAlpha = 0.045;
  addBeamTri(lens, lensAlpha, tl, planeAlpha, tr, planeAlpha);
  addBeamTri(lens, lensAlpha, tr, planeAlpha, br, planeAlpha);
  addBeamTri(lens, lensAlpha, br, planeAlpha, bl, planeAlpha);
  addBeamTri(lens, lensAlpha, bl, planeAlpha, tl, planeAlpha);

  const midL = vecScale(vecAdd(tl, bl), 0.5);
  const midR = vecScale(vecAdd(tr, br), 0.5);
  addBeamTri(lens, 0.22, midL, 0.03, midR, 0.03);

  return {
    data: new Float32Array(out),
    vertexCount: out.length / 4,
  };
}
