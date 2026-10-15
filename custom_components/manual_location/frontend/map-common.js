import * as L from "./vendor/leaflet.js?v=0.1.4";
export { L };

export function mapStyles(root) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("./vendor/leaflet.css?v=0.1.4", import.meta.url).href;
  root.append(link);
  const style = document.createElement("style");
  style.textContent = `.leaflet-container{font-family:inherit;background:#dce5e5}.person-icon{box-sizing:border-box;border:3px solid white;border-radius:50%;background:#186d83;color:#fff;box-shadow:0 2px 7px #1237;display:block;font-weight:700;overflow:hidden}.person-icon img{display:block;width:100%;height:100%;min-width:0;object-fit:cover}.leaflet-popup-content{line-height:1.5}.map-dark .leaflet-tile-pane{filter:brightness(.66) saturate(.7)}.leaflet-control-attribution{color:#222!important;background:#fffe!important}`;
  root.append(style);
}

const mapTiles = new WeakMap();

export function makeMap(element, hass) {
  const map = L.map(element, { zoomControl: true, attributionControl: true }).setView([25, 0], 2);
  const notice = document.createElement("div");
  notice.setAttribute("role", "status");
  notice.style.cssText = "background:var(--card-background-color,#fff);color:var(--primary-text-color,#25343b);padding:8px;max-width:240px;border-radius:4px";
  notice.textContent = "Ładowanie podkładu mapy…";
  const control = L.control({ position: "bottomleft" });
  control.onAdd = () => notice;
  control.addTo(map);
  const state = { map, notice, generation: 0, pending: false, disposed: false, failed: false };
  mapTiles.set(map, state);
  state.ready = () => {
    state.generation++;
    state.pending = false;
    state.connected = true;
    refreshMapTiles(state);
  };
  state.disconnected = () => {
    state.generation++;
    state.pending = false;
    state.connected = false;
    clearTimeout(state.timer);
  };
  map.on("unload", () => {
    state.disposed = true;
    clearTimeout(state.timer);
    state.connection?.removeEventListener("ready", state.ready);
    state.connection?.removeEventListener("disconnected", state.disconnected);
    mapTiles.delete(map);
  });
  setMapHass(map, hass);
  return map;
}

export function setMapHass(map, hass) {
  const state = mapTiles.get(map);
  if (!state || !hass) return;
  state.hass = hass;
  if (state.connection === hass.connection) return;
  state.connection?.removeEventListener("ready", state.ready);
  state.connection?.removeEventListener("disconnected", state.disconnected);
  state.connection = hass.connection;
  state.connection?.addEventListener("ready", state.ready);
  state.connection?.addEventListener("disconnected", state.disconnected);
  state.generation++;
  state.pending = false;
  state.connected = hass.connected !== false;
  clearTimeout(state.timer);
  state.layer?.off();
  state.layer?.remove();
  state.layer = null;
  state.url = null;
  refreshMapTiles(state);
}

async function refreshMapTiles(state) {
  if (state.disposed || !state.connected || state.pending) return;
  clearTimeout(state.timer);
  const generation = state.generation;
  state.pending = true;
  let delay = 20 * 60 * 1000; // HA rotates every 30 minutes and retains two tokens.
  try {
    const { token } = await state.hass.callWS({ type: "map_tiles/access_token" });
    if (state.disposed || generation !== state.generation) return;
    if (typeof token !== "string" || !token) throw new Error("missing_map_token");
    const origin = (state.connection?.options?.auth?.data?.hassUrl || window.location.origin).replace(/\/+$/, "");
    const url = `${origin}/api/map_tiles/raster/{z}/{x}/{y}.png?token=${encodeURIComponent(token)}`;
    const retryTiles = state.failed;
    state.failed = false;
    if (!state.layer) {
      state.layer = L.tileLayer(url, {
        maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
      });
      state.layer.on("tileload", () => { state.notice.hidden = true; });
      state.layer.on("tileerror", () => {
        if (state.disposed || state.failed) return;
        state.failed = true;
        state.notice.textContent = "Podkład mapy jest niedostępny. Ponawiam połączenie…";
        state.notice.hidden = false;
        clearTimeout(state.timer);
        state.timer = setTimeout(() => refreshMapTiles(state), 10000);
      });
      state.layer.addTo(state.map);
    } else if (url !== state.url) state.layer.setUrl(url);
    else if (retryTiles) state.layer.redraw();
    state.url = url;
  } catch {
    if (state.disposed || generation !== state.generation) return;
    state.notice.textContent = "Podkład mapy jest niedostępny. Ponawiam połączenie…";
    state.notice.hidden = false;
    delay = 10000;
  } finally {
    if (!state.disposed && generation === state.generation) {
      state.pending = false;
      clearTimeout(state.timer);
      state.timer = setTimeout(() => refreshMapTiles(state), state.failed ? 10000 : delay);
    }
  }
}

export function popup(name, point) {
  const box = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = name;
  box.append(title, document.createElement("br"), `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`);
  if (point.timestamp) box.append(document.createElement("br"), new Date(point.timestamp).toLocaleString());
  return box;
}

export function personIcon(name, picture, color) {
  const element = document.createElement("div");
  element.textContent = name.slice(0, 2).toLocaleUpperCase();
  if (picture) {
    try {
      const url = new URL(picture, window.location.origin);
      if (["http:", "https:"].includes(url.protocol)) {
        const img = document.createElement("img");
        img.src = url.href;
        img.alt = name;
        img.addEventListener("error", () => { element.textContent = name.slice(0, 2).toLocaleUpperCase(); }, { once: true });
        element.replaceChildren(img);
      }
    } catch { /* Invalid pictures leave readable initials. */ }
  }
  element.style.cssText = `width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:${color}`;
  return L.divIcon({ className: "person-icon", html: element, iconSize: [34, 34], iconAnchor: [17, 17] });
}
