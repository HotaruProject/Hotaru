import React, { useCallback, useEffect, useMemo, useState } from "react";
import Scene from "./Scene";
import { SceneDirector } from "./SceneDirector";
import { MetricsStore } from "./metricsStore";
import { TelemetryProjector } from "./TelemetryProjector";

function readMetaConfig() {
  if (typeof window === "undefined") return {};
  if (window.__SINGULARITY_CONFIG__) return window.__SINGULARITY_CONFIG__;
  try {
    const meta = document.querySelector('meta[name="singularity-cfg"]');
    const b64 = meta?.getAttribute("content") || "";
    if (!b64) return {};
    return JSON.parse(atob(b64));
  } catch {
    return {};
  }
}

function resolveRuntimeOptions() {
  const serverCfg = readMetaConfig();
  let mode = serverCfg.dataMode || "live";
  let durationMs = Number(serverCfg.shotDurationMs ?? 2400);
  let sessionToken = serverCfg.sessionToken || "";

  if (typeof window !== "undefined") {
    const qp = new URLSearchParams(window.location.search);
    if (qp.get("mode") === "mock" || qp.get("mode") === "live") {
      mode = qp.get("mode");
    }
    if (qp.get("duration") !== null && Number.isFinite(Number(qp.get("duration")))) {
      durationMs = Math.max(0, Number(qp.get("duration")));
    }
    if (qp.get("session")) {
      sessionToken = qp.get("session");
      try {
        window.sessionStorage?.setItem("singularity_session_token", sessionToken);
      } catch {}
      qp.delete("session");
      const cleanQuery = qp.toString();
      const cleanUrl =
        window.location.pathname +
        (cleanQuery ? `?${cleanQuery}` : "") +
        (window.location.hash || "");
      window.history.replaceState(null, "", cleanUrl);
    }
  }

  return { mode, durationMs, sessionToken };
}

export default function App() {
  const initialOpts = useMemo(() => resolveRuntimeOptions(), []);

  const director = useMemo(
    () => new SceneDirector({ durationMs: initialOpts.durationMs }),
    [initialOpts.durationMs]
  );

  const projector = useMemo(() => new TelemetryProjector(), []);

  const metricsStore = useMemo(
    () =>
      new MetricsStore({
        mode: initialOpts.mode,
        sessionToken: initialOpts.sessionToken,
      }),
    [initialOpts.mode, initialOpts.sessionToken]
  );

  const [shotState, setShotState] = useState(() => director.getState());
  const [summaryState, setSummaryState] = useState(() => projector.getState());
  const [projectionBounds, setProjectionBounds] = useState({
    visible: false,
    left: 0,
    top: 0,
    width: 0,
    height: 0,
  });

  useEffect(() => {
    try {
      window.Telegram?.WebApp?.ready?.();
      window.Telegram?.WebApp?.expand?.();
    } catch {}

    window.__director = director;
    window.__metrics = metricsStore;
    window.__summary = projector;

    projector.updateSnapshot(metricsStore.getSnapshot());
    setSummaryState(projector.getState());

    const unsubDirector = director.onShotChange(() => {
      setShotState(director.getState());
    });

    const unsubMetrics = metricsStore.subscribe((snap) => {
      projector.updateSnapshot(snap);
    });

    const unsubProjector = projector.onChange((st) => {
      setSummaryState(st);
    });

    const onKeyDown = (e) => {
      if (
        e.key === "Escape" &&
        director.currentShot === "satellite" &&
        !director.isInputLocked
      ) {
        director.goTo("overview");
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      window.removeEventListener("keydown", onKeyDown);
      unsubDirector();
      unsubMetrics();
      unsubProjector();
      metricsStore.destroy();
    };
  }, [director, metricsStore, projector]);

  const handleBlackHoleClick = useCallback(() => {
    if (director.isInputLocked) return;
    if (director.currentShot === "overview") {
      director.goTo("satellite");
    }
  }, [director]);

  const handleBackgroundClickInSatellite = useCallback(() => {
    if (director.isInputLocked) return;
    if (director.currentShot === "satellite") {
      director.goTo("overview");
    }
  }, [director]);

  const handleBackClick = useCallback(() => {
    if (director.isInputLocked) return;
    director.goTo("overview");
  }, [director]);

  const showBackButton =
    shotState.currentShot === "satellite" ||
    (shotState.isTransitioning && shotState.targetShot === "satellite");

  return (
    <div
      className="singularity-root"
      data-shot={shotState.currentShot}
      data-transitioning={String(shotState.isTransitioning)}
    >
      <Scene
        director={director}
        projector={projector}
        onBlackHoleClick={handleBlackHoleClick}
        onBackgroundClickInSatellite={handleBackgroundClickInSatellite}
        onProjectionBoundsChange={setProjectionBounds}
      />

      <div className="vignette-overlay" />

      <header className="crt-header">
        <div className="crt-brand">
          <span className="crt-dot" />
          <span className="crt-title">{"HOTARU // SINGULARITY"}</span>
          <span className="crt-shot-tag" data-testid="shot-indicator">
            {shotState.isTransitioning
              ? `FLIGHT -> ${shotState.targetShot.toUpperCase()}`
              : shotState.currentShot.toUpperCase()}
          </span>
        </div>

        <div className="crt-actions">
          <span className="crt-mode-pill" data-testid="data-mode-indicator">
            {String(summaryState.collector || summaryState.source || "live").toUpperCase()}
          </span>
          {showBackButton && (
            <button
              type="button"
              className="crt-btn crt-back-btn"
              data-testid="back-button"
              disabled={shotState.isTransitioning}
              onClick={handleBackClick}
            >
              ← HORIZON
            </button>
          )}
        </div>
      </header>

      {shotState.currentShot === "overview" && !shotState.isTransitioning && (
        <div className="orbit-hint" data-testid="overview-hint">
          {"SINGULARITY // НАЖМИТЕ НА ГОРИЗОНТ СОБЫТИЙ ДЛЯ ОТКРЫТИЯ PROBE-ТЕЛЕМЕТРИИ"}
        </div>
      )}

      {projectionBounds.visible &&
        shotState.currentShot === "satellite" &&
        !shotState.isTransitioning && (
          <div
            className="projection-hit-layer"
            data-testid="system-projection"
            style={{
              left: `${projectionBounds.left}px`,
              top: `${projectionBounds.top}px`,
              width: `${projectionBounds.width}px`,
              height: `${projectionBounds.height}px`,
            }}
            onClick={(e) => {
              e.stopPropagation();
            }}
          >
            <pre className="sr-telemetry-mirror" data-testid="system-summary">
              {summaryState.text}
            </pre>
          </div>
        )}
    </div>
  );
}
