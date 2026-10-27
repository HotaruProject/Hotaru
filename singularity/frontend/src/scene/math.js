export function vecAdd(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

export function vecSub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

export function vecScale(a, s) {
  return [a[0] * s, a[1] * s, a[2] * s];
}

export function vecDot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function vecCross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

export function vecLength(a) {
  return Math.hypot(a[0], a[1], a[2]);
}

export function vecNormalize(a) {
  const len = vecLength(a);
  if (len < 1e-8) return [0, 0, 1];
  return [a[0] / len, a[1] / len, a[2] / len];
}

export function vecLerp(a, b, t) {
  return [
    a[0] + (b[0] - a[0]) * t,
    a[1] + (b[1] - a[1]) * t,
    a[2] + (b[2] - a[2]) * t,
  ];
}

export function vecDistance(a, b) {
  return vecLength(vecSub(a, b));
}

export function slerpDirection(dirA, dirB, t) {
  const a = vecNormalize(dirA);
  const b = vecNormalize(dirB);
  const dot = Math.max(-1, Math.min(1, vecDot(a, b)));
  if (dot > 0.9995) return vecNormalize(vecLerp(a, b, t));
  const theta = Math.acos(dot);
  const sinTheta = Math.sin(theta);
  if (Math.abs(sinTheta) < 1e-6) return vecNormalize(vecLerp(a, b, t));
  const wA = Math.sin((1 - t) * theta) / sinTheta;
  const wB = Math.sin(t * theta) / sinTheta;
  return vecNormalize([
    a[0] * wA + b[0] * wB,
    a[1] * wA + b[1] * wB,
    a[2] * wA + b[2] * wB,
  ]);
}

export function clamp(value, min, max) {
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

export function rotateAroundY(point, angle) {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return [
    point[0] * cos + point[2] * sin,
    point[1],
    -point[0] * sin + point[2] * cos,
  ];
}

export function easeInOutCubic(t) {
  const x = clamp(t, 0, 1);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
}
