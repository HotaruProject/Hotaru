import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';
import { transformWithEsbuild } from 'vite';

const source = await readFile(new URL('../src/Scene.jsx', import.meta.url), 'utf8');
const { code } = await transformWithEsbuild(source, 'Scene.jsx', { loader: 'jsx', format: 'cjs' });

function mount(reducedMotion) {
  let draws = 0;
  let nextFrame = 0;
  let cleanup;
  let resize;
  const frames = new Map();
  const events = {};
  const uniforms = {};
  const refs = [];
  const gl = new Proxy({
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    getUniformLocation: (_, name) => name,
    uniform1f: (name, value) => { uniforms[name] = value; },
    drawArrays: () => { draws++; },
  }, { get: (target, key) => key in target ? target[key] : () => ({}) });
  const canvas = {
    getContext: () => gl,
    getBoundingClientRect: () => ({ width: 800, height: 600 }),
    style: {},
    addEventListener: (name, handler) => { events[name] = handler; },
    removeEventListener: (name) => { delete events[name]; },
  };
  const React = {
    memo: (component) => component,
    forwardRef: (component) => component,
    useRef: (value) => {
      const ref = { current: refs.length === 0 ? canvas : value };
      refs.push(ref);
      return ref;
    },
    useImperativeHandle: (ref, create) => { ref.current = create(); },
    useEffect: (effect) => { cleanup = effect(); },
    createElement: () => ({}),
  };
  const context = {
    module: { exports: {} },
    require: () => React,
    performance: { now: () => 0 },
    window: {
      devicePixelRatio: 1,
      addEventListener: (name, handler) => { events[name] = handler; },
      removeEventListener: (name) => { delete events[name]; },
    },
    ResizeObserver: class {
      constructor(callback) { resize = callback; }
      observe() {}
      disconnect() {}
    },
    requestAnimationFrame: (callback) => {
      frames.set(++nextFrame, callback);
      return nextFrame;
    },
    cancelAnimationFrame: (id) => { frames.delete(id); },
    console,
    Float32Array,
  };
  vm.runInNewContext(code, context);
  const ref = {};
  context.module.exports.Scene({ reducedMotion }, ref);
  return {
    ref, frames, events, uniforms,
    get draws() { return draws; },
    resize: () => resize(),
    close: () => cleanup(),
    frame: (time = 0) => {
      const [id, callback] = frames.entries().next().value;
      frames.delete(id);
      callback(time);
    },
  };
}

test('reduced motion redraws camera controls on demand and stops when idle', () => {
  const scene = mount(true);
  scene.frame();
  assert.equal(scene.draws, 1);
  assert.equal(scene.frames.size, 0);
  scene.events.wheel({ preventDefault() {}, deltaY: 1 });
  scene.frame();
  assert.equal(scene.uniforms.uDist, 27.5);
  scene.ref.current.flyTo({ yaw: 1, pitch: 0.3, dist: 15 });
  scene.frame();
  assert.equal(scene.uniforms.uYaw, 1);
  assert.equal(scene.uniforms.uDist, 15);
  scene.events.pointerdown({ button: 0, pointerType: 'mouse', clientX: 0, clientY: 0 });
  scene.events.pointermove({ clientX: 20, clientY: 10 });
  scene.frame();
  assert.equal(scene.uniforms.uYaw, 0.9);
  scene.events.pointerup();
  scene.ref.current.recenter();
  scene.frame();
  assert.equal(scene.uniforms.uYaw, 0);
  assert.equal(scene.uniforms.uDist, 26);
  scene.resize();
  scene.frame();
  assert.equal(scene.frames.size, 0);
  assert.equal(scene.uniforms.uTime, 4.2);
  scene.close();
});

test('normal animation keeps rendering and cleanup cancels its pending frame', () => {
  const scene = mount(false);
  scene.frame();
  assert.equal(scene.frames.size, 1);
  scene.ref.current.flyTo({ yaw: 1, pitch: 0.3, dist: 15 }, 1000);
  assert.equal(scene.frames.size, 1);
  scene.frame(500);
  assert.ok(scene.uniforms.uYaw > 0 && scene.uniforms.uYaw < 1);
  scene.frame(1000);
  assert.equal(scene.uniforms.uYaw, 1);
  scene.close();
  assert.equal(scene.frames.size, 0);
  assert.deepEqual(Object.keys(scene.events), []);
});
