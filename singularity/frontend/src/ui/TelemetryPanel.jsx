import React, { useEffect, useMemo, useRef } from "react";
import {
  buildTelemetryModel,
  formatClock,
  normalizeLevel,
  truncate,
} from "../telemetry/format.js";

function StatusDot({ status }) {
  const healthy = ["connected", "polling", "mock"].includes(status);
  return (
    <span
      className="telemetry-dot"
      data-state={healthy ? "healthy" : "pending"}
    />
  );
}

function Card({ card }) {
  const percent =
    card.percent === null ? null : Math.max(0, Math.min(100, card.percent));
  return (
    <div className="telemetry-card" data-card={card.key}>
      <div className="telemetry-card-head">
        <span className="telemetry-card-label">{card.label}</span>
        <span className="telemetry-card-value">{card.value}</span>
      </div>
      {percent !== null ? (
        <div className="telemetry-bar">
          <i
            className="telemetry-bar-fill"
            data-level={percent >= 88 ? "high" : percent >= 70 ? "mid" : "low"}
            style={{ width: `${percent}%` }}
          />
        </div>
      ) : null}
      <div className="telemetry-card-detail">{card.detail}</div>
    </div>
  );
}

export default function TelemetryPanel({ snapshot, expanded, onToggle }) {
  const model = useMemo(() => buildTelemetryModel(snapshot), [snapshot]);
  const logRef = useRef(null);

  useEffect(() => {
    const node = logRef.current;
    if (node && expanded) {
      node.scrollTop = node.scrollHeight;
    }
  }, [model.events, expanded]);

  return (
    <section
      className="telemetry-sheet"
      data-testid="system-summary"
      data-expanded={String(expanded)}
    >
      <button
        type="button"
        className="telemetry-sheet-head"
        data-testid="telemetry-toggle"
        onClick={onToggle}
      >
        <span className="telemetry-sheet-title">
          HOTARU // KERNEL TELEMETRY
        </span>
        <span className="telemetry-sheet-meta">
          <StatusDot status={model.status} />
          <span data-testid="telemetry-status">{model.status.toUpperCase()}</span>
          <span className="telemetry-sheet-expand">
            {expanded ? "COLLAPSE" : "EXPAND"}
          </span>
        </span>
      </button>

      <div className="telemetry-grid">
        {model.cards.map((card) => (
          <Card key={card.key} card={card} />
        ))}
      </div>

      <div className="telemetry-projection" data-testid="system-projection">
        <div className="telemetry-projection-title">
          OBSERVATORY // READ-ONLY DEBUG STREAM
        </div>
          <div className="telemetry-log" data-testid="telemetry-log" ref={logRef}>
            {model.events.length === 0 ? (
              <div className="telemetry-log-line telemetry-log-empty">
                awaiting observatory debug telemetry
              </div>
            ) : (
              model.events.map((event, index) => (
                <div className="telemetry-log-line" key={`${event.seq || index}`}>
                  <span className="telemetry-log-clock">{formatClock(event.ts)}</span>
                  <span
                    className="telemetry-log-level"
                    data-level={normalizeLevel(event.level)}
                  >
                    {normalizeLevel(event.level)}
                  </span>
                  <span className="telemetry-log-component">
                    {truncate(event.component || "kernel", 16)}
                  </span>
                  <span className="telemetry-log-body">
                    {truncate(
                      `${event.event || "event"}${event.detail ? ` | ${event.detail}` : ""}`,
                      64
                    )}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
    </section>
  );
}
