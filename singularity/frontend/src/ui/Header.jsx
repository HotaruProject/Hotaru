import React from "react";

export default function Header({
  status = "live",
}) {
  const isHealthy = ["connected", "polling", "live", "mock"].includes(
    String(status).toLowerCase()
  );

  return (
    <header className="nav-header">
      <div className="nav-group">
        <div
          className="nav-status-dot"
          data-state={isHealthy ? "live" : "sync"}
          title={isHealthy ? "System telemetry live" : "Synchronizing"}
        />
      </div>
    </header>
  );
}
