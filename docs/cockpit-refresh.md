# Continuous cockpit refresh

DC-073 keeps the hybrid cockpit current without requiring a browser reload.

## Cadence

The frontend uses a single coordinated refresh loop per active project. The default interval is **30 seconds**.

Override the interval at frontend build/dev time with:

```text
VITE_COCKPIT_REFRESH_INTERVAL_MS=30000
```

The value is interpreted as milliseconds. Missing, invalid, zero or negative values fall back to 30 seconds.

## Visibility and manual refresh

Periodic refresh is suspended while the browser tab is hidden. Returning to the tab or focusing the window requests an immediate refresh, with a short deduplication window so visibility and focus events do not create a request burst.

The cockpit also exposes an immediate manual refresh action. A refresh already in flight is never duplicated for the same active project.

## Failure behavior

Background refresh keeps the last valid cockpit snapshot visible. A temporary failure is surfaced as stale/degraded freshness information and the periodic loop continues, so a later successful observation recovers automatically.

Open hybrid-cockpit surfaces receive the same shared refresh version; they do not own independent periodic pollers. Project changes still use the existing generation/context checks so a late response from a previous project cannot overwrite the active project.
