import { callWS } from "./data.js?v=0.1.22";

export const VERSION = "0.1.22";

// Shared even when HA loads the card and panel through different module URLs.
const stateKey = Symbol.for("manual-location-updates");
const state = globalThis[stateKey] ||= { blockers: new Set(), reloading: false };

export function registerReloadBlocker(canReload) {
  state.blockers.add(canReload);
  return () => state.blockers.delete(canReload);
}

function newerVersion(version) {
  if (typeof version !== "string" || !/^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(version)) return false;
  const remote = version.split(".").map(Number), current = VERSION.split(".").map(Number);
  for (let i = 0; i < current.length; i++) {
    if (remote[i] !== current[i]) return remote[i] > current[i];
  }
  return false;
}

export function startUpdateCheck(getHass, canReload = () => true) {
  let disposed = false, pending = false, connection, generation = 0;
  const allowed = () => !disposed && !state.reloading && document.visibilityState === "visible"
    && canReload() && [...state.blockers].every((check) => check());
  const check = async () => {
    if (disposed) return;
    const hass = getHass();
    if (connection !== hass?.connection) {
      connection?.removeEventListener("ready", ready);
      connection?.removeEventListener("disconnected", disconnected);
      connection = hass?.connection;
      generation++;
      connection?.addEventListener("ready", ready);
      connection?.addEventListener("disconnected", disconnected);
    }
    if (pending || !hass || hass.connected === false || !allowed()) return;
    pending = true;
    const requestConnection = connection, requestGeneration = generation;
    try {
      const result = await callWS(hass, { type: "manual_location/version" });
      if (!allowed() || getHass()?.connection !== requestConnection || generation !== requestGeneration
        || getHass()?.connected === false
        || !newerVersion(result?.version)) return;
      const url = new URL(window.location.href);
      const key = `manual-location-reloaded:${VERSION}:${result.version}`;
      if (url.searchParams.get("manual_location_version") === result.version) return;
      try { if (window.sessionStorage.getItem(key)) return; } catch { /* Query also prevents a loop. */ }
      try { window.sessionStorage.setItem(key, "1"); } catch { /* Storage may be disabled in a WebView. */ }
      url.searchParams.set("manual_location_version", result.version);
      state.reloading = true;
      window.location.replace(url.href);
    } catch { /* Old servers, restart, and offline clients retry without disrupting the map. */ }
    finally { pending = false; }
  };
  const ready = () => { generation++; check(); };
  const disconnected = () => { generation++; };
  const timer = setInterval(check, 30000);
  document.addEventListener("visibilitychange", check);
  check();
  return () => {
    disposed = true;
    clearInterval(timer);
    document.removeEventListener("visibilitychange", check);
    connection?.removeEventListener("ready", ready);
    connection?.removeEventListener("disconnected", disconnected);
  };
}
