import React from 'react';
import { CAMERA_PRESETS } from './CameraDirector';

export function DirectorPanel({ activePreset, onSelectPreset, onClose }) {
  if (!activePreset) return null;

  const presets = Object.values(CAMERA_PRESETS);

  return (
    <div className="director-panel" role="dialog" aria-label="Black Hole Telemetry">
      <div className="panel-header">
        <div>
          <div className="panel-tag">{activePreset.tag}</div>
          <h2 className="panel-title">{activePreset.title}</h2>
        </div>
        <button
          type="button"
          className="panel-close"
          onClick={onClose}
          aria-label="Reset camera"
        >
          ×
        </button>
      </div>

      <p className="panel-body">{activePreset.desc}</p>

      {activePreset.telemetry && (
        <div className="telemetry-row">
          {Object.entries(activePreset.telemetry).map(([key, val]) => (
            <div key={key} className="telemetry-item">
              <span>{key}</span>
              <b>{val}</b>
            </div>
          ))}
        </div>
      )}

      <div className="preset-bar">
        {presets.map(p => (
          <button
            key={p.id}
            type="button"
            className={`preset-chip ${activePreset.id === p.id ? 'active' : ''}`}
            onClick={() => onSelectPreset(p)}
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
