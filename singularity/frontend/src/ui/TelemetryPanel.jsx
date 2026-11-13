import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  buildTelemetryModel,
  formatClock,
  normalizeLevel,
  truncate,
} from "../telemetry/format.js";

function StatusDot({ status }) {
  const healthy = ["connected", "polling", "mock", "live"].includes(
    String(status).toLowerCase()
  );
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

  const [sortKey, setSortKey] = useState("timeDesc");
  const [levelFilter, setLevelFilter] = useState("ALL");
  const [filterQuery, setFilterQuery] = useState("");

  const filteredEvents = useMemo(() => {
    let list = Array.isArray(model.events) ? [...model.events] : [];

    if (levelFilter !== "ALL") {
      list = list.filter((e) => normalizeLevel(e.level) === levelFilter);
    }

    if (filterQuery.trim()) {
      const q = filterQuery.toLowerCase().trim();
      list = list.filter(
        (e) =>
          (e.component && String(e.component).toLowerCase().includes(q)) ||
          (e.event && String(e.event).toLowerCase().includes(q)) ||
          (e.detail && String(e.detail).toLowerCase().includes(q))
      );
    }

    const levelRank = { ERR: 4, WRN: 3, INF: 2, DBG: 1 };
    list.sort((a, b) => {
      if (sortKey === "timeAsc") {
        return (a.ts || 0) - (b.ts || 0);
      }
      if (sortKey === "timeDesc") {
        return (b.ts || 0) - (a.ts || 0);
      }
      if (sortKey === "severity") {
        const ra = levelRank[normalizeLevel(a.level)] || 0;
        const rb = levelRank[normalizeLevel(b.level)] || 0;
        if (rb !== ra) return rb - ra;
        return (b.ts || 0) - (a.ts || 0);
      }
      if (sortKey === "component") {
        const ca = String(a.component || "").localeCompare(String(b.component || ""));
        if (ca !== 0) return ca;
        return (b.ts || 0) - (a.ts || 0);
      }
      return 0;
    });

    return list;
  }, [model.events, sortKey, levelFilter, filterQuery]);

  useEffect(() => {
    const node = logRef.current;
    if (node && expanded && sortKey === "timeAsc") {
      node.scrollTop = node.scrollHeight;
    }
  }, [filteredEvents.length, expanded, sortKey]);

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
        <span className="telemetry-sheet-title">Kernel Telemetry</span>
        <span className="telemetry-sheet-meta">
          <StatusDot status={model.status} />
          <span data-testid="telemetry-status">{model.status.toUpperCase()}</span>
          <span className="telemetry-sheet-expand">
            {expanded ? "Collapse" : "Expand"}
          </span>
        </span>
      </button>

      <div className="telemetry-grid">
        {model.cards.map((card) => (
          <Card key={card.key} card={card} />
        ))}
      </div>

      {expanded ? (
        <div className="telemetry-projection" data-testid="system-projection">
          <div className="telemetry-projection-head">
            <div className="telemetry-projection-title">
              Observatory Debug Stream
            </div>
            <span className="telemetry-counter">
              {filteredEvents.length} / {model.events.length}
            </span>
          </div>

          <div className="telemetry-toolbar">
            <div className="telemetry-sort-group">
              <span className="telemetry-filter-label">Sort:</span>
              <button
                type="button"
                className={`telemetry-tool-btn ${sortKey === "timeDesc" ? "active" : ""}`}
                onClick={() => setSortKey("timeDesc")}
                title="Newest logs first"
              >
                Newest ↓
              </button>
              <button
                type="button"
                className={`telemetry-tool-btn ${sortKey === "timeAsc" ? "active" : ""}`}
                onClick={() => setSortKey("timeAsc")}
                title="Oldest logs first"
              >
                Oldest ↑
              </button>
              <button
                type="button"
                className={`telemetry-tool-btn ${sortKey === "severity" ? "active" : ""}`}
                onClick={() => setSortKey("severity")}
                title="Errors and warnings first"
              >
                Severity ⚡
              </button>
              <button
                type="button"
                className={`telemetry-tool-btn ${sortKey === "component" ? "active" : ""}`}
                onClick={() => setSortKey("component")}
                title="Sort by component name"
              >
                Module A-Z
              </button>
            </div>

            <div className="telemetry-filter-group">
              <span className="telemetry-filter-label">Level:</span>
              {["ALL", "ERR", "WRN", "INF"].map((lvl) => (
                <button
                  type="button"
                  key={lvl}
                  className={`telemetry-tool-btn ${levelFilter === lvl ? "active" : ""}`}
                  data-level={lvl}
                  onClick={() => setLevelFilter(lvl)}
                >
                  {lvl}
                </button>
              ))}
            </div>

            <div className="telemetry-search-box">
              <input
                type="text"
                className="telemetry-search-input"
                placeholder="Search events..."
                value={filterQuery}
                onChange={(e) => setFilterQuery(e.target.value)}
              />
              {filterQuery ? (
                <button
                  type="button"
                  className="telemetry-clear-btn"
                  onClick={() => setFilterQuery("")}
                >
                  ×
                </button>
              ) : null}
            </div>
          </div>

          <div className="telemetry-log" data-testid="telemetry-log" ref={logRef}>
            {filteredEvents.length === 0 ? (
              <div className="telemetry-log-line telemetry-log-empty">
                {model.events.length === 0
                  ? "Awaiting observatory debug telemetry..."
                  : "No log events matching current filter."}
              </div>
            ) : (
              filteredEvents.map((event, index) => (
                <div className="telemetry-log-line" key={`${event.seq || index}_${event.ts}`}>
                  <span className="telemetry-log-clock">{formatClock(event.ts)}</span>
                  <span
                    className="telemetry-log-level"
                    data-level={normalizeLevel(event.level)}
                  >
                    {normalizeLevel(event.level)}
                  </span>
                  <span className="telemetry-log-component">
                    {truncate(event.component || "kernel", 18)}
                  </span>
                  <span className="telemetry-log-body">
                    {truncate(
                      `${event.event && event.event !== "log" ? `[${event.event}] ` : ""}${event.detail || ""}`,
                      120
                    )}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}
