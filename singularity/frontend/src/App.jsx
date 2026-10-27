import React from 'react';
import { Scene } from './Scene.jsx';
import { HotspotLayer } from './HotspotLayer.jsx';

function usePrefersReducedMotion() {
  const [reduced, setReduced] = React.useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  );
  React.useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (e) => setReduced(e.matches);
    mq.addEventListener?.('change', onChange);
    return () => mq.removeEventListener?.('change', onChange);
  }, []);
  return reduced;
}

export default function App() {
  const reducedMotion = usePrefersReducedMotion();
  const [active, setActive] = React.useState(null);
  const sceneRef = React.useRef(null);

  const handleSelect = React.useCallback((hotspot) => {
    if (!hotspot) {
      setActive(null);
      sceneRef.current?.recenter();
    } else {
      setActive(hotspot.id);
      sceneRef.current?.flyTo(hotspot.shot, 1200);
    }
  }, []);

  return (
    <div className="observatory-root">

      <Scene reducedMotion={reducedMotion} ref={sceneRef} />

      <div className="vignette-overlay" />
      <div className="scanlines-overlay" />

      <header className="crt-header">
        <div className="crt-badge">
          <span className="crt-dot" />
          <span>NGC 4459 // SINGULARITY</span>
        </div>
      </header>

      <HotspotLayer active={active} onSelect={handleSelect} />
    </div>
  );
}
