import React from "react";

export default function Header({
  shot,
  transitioning,
  targetShot,
  status,
  showBack,
  onBack,
}) {
  const label = transitioning
    ? `FLIGHT -> ${String(targetShot).toUpperCase()}`
    : String(shot).toUpperCase();

  return (
    <header className="crt-header">
      <div className="crt-brand">
        <span className="crt-dot" />
        <span className="crt-title">HOTARU // SINGULARITY</span>
        <span className="crt-shot-tag" data-testid="shot-indicator">
          {label}
        </span>
      </div>

      <div className="crt-actions">
        <span className="crt-mode-pill" data-testid="data-mode-indicator">
          {String(status).toUpperCase()}
        </span>
        {showBack ? (
          <button
            type="button"
            className="crt-btn crt-back-btn"
            data-testid="back-button"
            disabled={transitioning}
            onClick={onBack}
          >
            HORIZON
          </button>
        ) : null}
      </div>
    </header>
  );
}
