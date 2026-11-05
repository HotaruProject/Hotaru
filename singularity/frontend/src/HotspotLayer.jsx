import React from 'react';

export const HOTSPOTS = [
  {
    id: 'event-horizon',
    label: 'Event horizon',
    style: { left: '50%', top: '50%' },
    shot: { yaw: 0.05, pitch: 0.08, dist: 14.0 }
  },
  {
    id: 'accretion-disk',
    label: 'Accretion disk',
    style: { left: '74%', top: '50%' },
    shot: { yaw: 0.85, pitch: 0.12, dist: 18.0 }
  },
  {
    id: 'photon-sphere',
    label: 'Photon ring',
    style: { left: '50%', top: '27%' },
    shot: { yaw: 0.0, pitch: 0.75, dist: 20.0 }
  },
  {
    id: 'lensing-arc',
    label: 'Einstein ring',
    style: { left: '26%', top: '65%' },
    shot: { yaw: -0.95, pitch: 0.32, dist: 19.0 }
  }
];

export function HotspotLayer({ active, onSelect }) {
  return (
    <div className="hotspot-layer" aria-label="Black hole interactive features">
      {HOTSPOTS.map((h) => (
        <button
          key={h.id}
          type="button"
          className={`hotspot ${active === h.id ? 'hotspot-active' : ''}`}
          style={h.style}
          aria-label={h.label}
          aria-pressed={active === h.id}
          onClick={() => onSelect(active === h.id ? null : h)}
        >
          <span className="hotspot-dot" aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}
