import * as L from "./vendor/leaflet.js";
export { L };

export function mapStyles(root) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("./vendor/leaflet.css", import.meta.url).href;
  root.append(link);
  const style = document.createElement("style");
  style.textContent = `.leaflet-container{font-family:inherit;background:#dce5e5}.person-icon{border:3px solid white;border-radius:50%;background:#186d83;color:#fff;box-shadow:0 2px 7px #1237;display:grid;place-items:center;font-weight:700;overflow:hidden}.person-icon img{width:100%;height:100%;object-fit:cover}.leaflet-popup-content{line-height:1.5}.map-dark .leaflet-tile-pane{filter:brightness(.66) saturate(.7)}.leaflet-control-attribution{color:#222!important;background:#fffe!important}`;
  root.append(style);
}

export function makeMap(element) {
  const map = L.map(element, { zoomControl: true, attributionControl: true }).setView([25, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);
  return map;
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
  element.style.cssText = `width:100%;height:100%;display:grid;place-items:center;background:${color}`;
  return L.divIcon({ className: "person-icon", html: element, iconSize: [34, 34], iconAnchor: [17, 17] });
}
