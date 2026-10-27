import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CameraDirector } from "./scene/camera.js";
import { SceneRenderer } from "./scene/renderer.js";
import { MetricsStore } from "./telemetry/store.js";
import Header from "./ui/Header.jsx";
import TelemetryPanel from "./ui/TelemetryPanel.jsx";

function readServerConfig() {
  if (typeof window === "undefined") return {};
  if (window.__SINGULARITY_CONFIG__) return window.__SINGULARITY_CONFIG__;
  try {
    const meta = document.querySelector('meta[name="singularity-cfg"]');
    const encoded = meta?.getAttribute("content") || "";
    if (!encoded) return {};
    return JSON.parse(atob(encoded));
  } catch {
    return {};
  }
}

function resolveRuntimeOptions() {
  const serverConfig = readServerConfig();
  let mode = serverConfig.dataMode || "live";
  let durationMs = Number(serverConfig.shotDurationMs ?? 2200);
  let sessionToken = serverConfig.sessionToken || "";

  if (typeof window !== "undefined") {
    const params = new URLSearchParams(window.location.search);
    if (params.get("mode") === "mock" || params.get("mode") === "live") {
      mode = params.get("mode");
    }
    if (params.get("duration") !== null) {
      const parsed = Number(params.get("duration"));
      if (Number.isFinite(parsed)) {
        durationMs = Math.max(0, parsed);
      }
    }
    if (params.get("session")) {
      sessionToken = params.get("session");
      try {
        window.sessionStorage?.setItem("singularity_session_token", sessionToken);
      } catch {}
      params.delete("session");
      const query = params.toString();
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${query ? `?${query}` : ""}${
          window.location.hash || ""
        }`
      );
    }
  }

  return { mode, durationMs, sessionToken };
}

export default function App() {
  const options = useMemo(() => resolveRuntimeOptions(), []);
  const canvasRef = useRef(null);
  const rendererRef = useRef(null);

  const director = useMemo(
    () => new CameraDirector({ durationMs: options.durationMs }),
    [options.durationMs]
  );

  const metricsStore = useMemo(
    () =>
      new MetricsStore({
        mode: options.mode,
        sessionToken: options.sessionToken,
      }),
    [options.mode, options.sessionToken]
  );

  const [shotState, setShotState] = useState(() => director.getState());
  const [snapshot, setSnapshot] = useState(() => metricsStore.getSnapshot());
  const [panelExpanded, setPanelExpanded] = useState(false);

  const goToSatellite = useCallback(() => {
    if (director.isInputLocked) return;
    director.goTo("satellite");
  }, [director]);

  const goToOverview = useCallback(() => {
    if (director.isInputLocked) return;
    director.goTo("overview");
  }, [director]);

  useEffect(() => {
    try {
      window.Telegram?.WebApp?.ready?.();
      window.Telegram?.WebApp?.expand?.();
    } catch {}

    window.__director = director;
    window.__metrics = metricsStore;
    window.__summary = metricsStore.getSnapshot();

    const unsubscribeDirector = director.onShotChange(() => {
      setShotState(director.getState());
    });
    const unsubscribeMetrics = metricsStore.subscribe((next) => {
      window.__summary = next;
      setSnapshot(next);
    });

    const onKeyDown = (event) => {
      if (event.key === "Escape" && director.currentShot === "satellite") {
        goToOverview();
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      window.removeEventListener("keydown", onKeyDown);
      unsubscribeDirector();
      unsubscribeMetrics();
      metricsStore.destroy();
    };
  }, [director, metricsStore, goToOverview]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const renderer = new SceneRenderer(canvas, director, {
      onSatelliteTap: goToSatellite,
      onBackgroundTap: goToOverview,
    });
    rendererRef.current = renderer;
    window.__scene = renderer;
    renderer.start();
    return () => {
      renderer.dispose();
      rendererRef.current = null;
    };
  }, [director, goToSatellite, goToOverview]);

  const showBack = shotState.currentShot === "satellite";
  const showPanel =
    shotState.currentShot === "satellite" && !shotState.isTransitioning;

  return (
    <div
      className="singularity-root"
      data-shot={shotState.currentShot}
      data-transitioning={String(shotState.isTransitioning)}
    >
      <canvas
        ref={canvasRef}
        className="scene-canvas"
        data-testid="singularity-canvas"
      />

      <div className="vignette-overlay" />

      <Header
        shot={shotState.currentShot}
        transitioning={shotState.isTransitioning}
        targetShot={shotState.targetShot}
        status={snapshot.connectionStatus || "connecting"}
        showBack={showBack}
        onBack={goToOverview}
      />

      {shotState.currentShot === "overview" && !shotState.isTransitioning ? (
        <div className="orbit-hint" data-testid="overview-hint">
          TAP THE PROBE TO INSPECT TELEMETRY
        </div>
      ) : null}

      {showPanel ? (
        <TelemetryPanel
          snapshot={snapshot}
          expanded={panelExpanded}
          onToggle={() => setPanelExpanded((value) => !value)}
        />
      ) : null}
    </div>
  );
}
