import { callWS, errorText, geoModels, historyPoints, originalIds, personModels, validateView, zoneModels } from "./data.js";
import { L, makeMap, mapStyles, personIcon, popup } from "./map-common.js";

const COLORS = ["#186d83", "#b05631", "#644f9a", "#42754b", "#a2376b"];

export class ManualLocationMap extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._epoch = 0;
    this._originals = {};
    this._onDisconnect = () => this._invalidate("Utracono połączenie. Lokalizacje są ukryte.");
    this._onReady = () => { this._invalidate("Łączenie…"); this._refresh(); };
  }

  setConfig(config) {
    if (!Array.isArray(config.entities) || !config.entities.length) throw new Error("Podaj listę entities dla karty mapy.");
    const hours = config.hours_to_show === undefined ? 24 : Number(config.hours_to_show);
    if (!Number.isFinite(hours) || hours < 0 || hours > 744) throw new Error("hours_to_show musi mieścić się w 0…744.");
    this._config = { ...config, hours_to_show: hours };
    this._configuredIds = [...new Set(config.entities.map((e) => typeof e === "string" ? e : e.entity))];
    this._entityIds = this._configuredIds.filter((id) => !id.startsWith("zone."));
    this._personIds = this._entityIds.filter((id) => id.startsWith("person."));
    this._invalidate("Łączenie…");
    if (this.isConnected) { this._ensureMap(); this._refresh(); }
  }

  set hass(hass) {
    const changed = this._hass?.connection !== hass.connection || this._hass?.user?.id !== hass.user?.id;
    const disconnected = hass.connected === false && this._hass?.connected !== false;
    this._hass = hass;
    if (changed) {
      this._unbind();
      this._connection = hass.connection;
      if (this.isConnected) this._bind();
      this._invalidate("Łączenie…");
    }
    if (disconnected) this._onDisconnect();
    if (this.isConnected && (changed || !this._view) && hass.connected !== false) this._refresh();
  }

  connectedCallback() {
    this._bind();
    this._ensureMap();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 5000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._unbind();
    this._resize?.disconnect();
    this._invalidate("Łączenie…");
    this._map?.remove();
    this._map = null;
    this.shadowRoot.replaceChildren();
  }

  _bind() {
    if (!this._connection || this._bound) return;
    this._connection.addEventListener("disconnected", this._onDisconnect);
    this._connection.addEventListener("ready", this._onReady);
    this._bound = true;
  }

  _unbind() {
    if (!this._connection || !this._bound) return;
    this._connection.removeEventListener("disconnected", this._onDisconnect);
    this._connection.removeEventListener("ready", this._onReady);
    this._bound = false;
  }

  _invalidate(message) {
    this._epoch++;
    this._busy = false;
    this._view = null;
    this._originals = {};
    this._historyKey = null;
    this._layers?.clearLayers();
    this._map?.closePopup();
    if (this._map) { this._map.stop(); this._map.setView([25, 0], 2, { animate: false }); this._fitted = false; }
    this._status(message);
  }

  _status(message) {
    this._message = message;
    if (this._statusEl) { this._statusEl.textContent = message; this._statusEl.hidden = !message; }
  }

  _ensureMap() {
    if (this._map || !this._config || !this.isConnected) return;
    this.shadowRoot.innerHTML = `<style>:host{display:block}ha-card{display:block;overflow:hidden;background:var(--ha-card-background,var(--card-background-color,#fff));border-radius:var(--ha-card-border-radius,12px);border:1px solid var(--divider-color,#ddd)}h2{font-size:20px;font-weight:400;margin:0;padding:20px 20px 14px;color:var(--primary-text-color,#25343b)}.frame{position:relative}.map{height:100%;min-height:230px}.status{position:absolute;z-index:1000;left:12px;right:12px;top:12px;border-radius:6px;padding:12px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#25343b);box-shadow:0 2px 8px #0002;text-align:center}.status[hidden]{display:none}</style><ha-card><h2 hidden></h2><div class="frame"><div class="map"></div><div class="status" role="status"></div></div></ha-card>`;
    mapStyles(this.shadowRoot);
    const title = this.shadowRoot.querySelector("h2");
    title.textContent = this._config.title || "";
    title.hidden = !this._config.title;
    const frame = this.shadowRoot.querySelector(".frame");
    const ratio = String(this._config.aspect_ratio || "16:9");
    const parts = ratio.split(":").map(Number);
    frame.style.aspectRatio = parts.length === 2 && parts.every((n) => n > 0) ? `${parts[0]} / ${parts[1]}` : "16 / 9";
    this._statusEl = this.shadowRoot.querySelector(".status");
    this._map = makeMap(this.shadowRoot.querySelector(".map"));
    this._layers = L.layerGroup().addTo(this._map);
    this._resize = new ResizeObserver(() => this._map?.invalidateSize());
    this._resize.observe(frame);
    this._status(this._message || "Łączenie…");
  }

  async _refresh() {
    if (!this.isConnected || !this._hass || !this._config || this._busy || this._hass.connected === false) return;
    this._ensureMap();
    const epoch = this._epoch;
    this._busy = true;
    const now = Date.now();
    const start = now - this._config.hours_to_show * 3600000;
    try {
      const response = await callWS(this._hass, { type: "manual_location/view", entity_ids: this._personIds,
        start: new Date(start).toISOString(), end: new Date(now).toISOString() });
      if (epoch !== this._epoch || !this.isConnected) return;
      const previousOverrides = Object.keys(this._view?.overrides || {}).sort().join(",");
      this._view = validateView(response, this._personIds);
      if (previousOverrides !== Object.keys(this._view.overrides).sort().join(",")) this._fitted = false;
      const ids = originalIds(this._entityIds, this._view);
      // Drop cached GPS as soon as an override is received, before any other await.
      this._originals = Object.fromEntries(Object.entries(this._originals).filter(([id]) => ids.includes(id)));
      this._render(start, now);
      const key = ids.join(",") + ":" + Math.floor(now / 60000);
      if (ids.length && this._config.hours_to_show > 0 && key !== this._historyKey) {
        try {
          const history = await callWS(this._hass, { type: "history/history_during_period", start_time: new Date(start).toISOString(),
            end_time: new Date(now).toISOString(), entity_ids: ids, minimal_response: false, no_attributes: false,
            significant_changes_only: false, include_start_time_state: true });
          if (epoch !== this._epoch || !this.isConnected) return;
          this._originals = Object.fromEntries(ids.map((id) => [id, historyPoints(history[id])]));
          this._historyKey = key;
        } catch {
          if (epoch !== this._epoch) return;
          this._originals = {};
          this._render(start, now);
          this._status("Historia HA jest niedostępna. Widoczne są bieżące pozycje.");
          return;
        }
      }
      this._render(start, now);
      this._status("");
    } catch (error) {
      if (epoch === this._epoch) this._invalidate("Błąd połączenia z integracją. " + errorText(error));
    } finally { if (epoch === this._epoch) this._busy = false; }
  }

  _render(start, end) {
    if (!this._map || !this._view) return;
    this._layers.clearLayers();
    const bounds = [];
    const states = this._hass.states;
    for (const zone of zoneModels(states, this._configuredIds)) {
      const circle = L.circle([zone.current.latitude, zone.current.longitude], { radius: zone.radius, color: "#63889a", weight: 1, fillOpacity: .08 }).addTo(this._layers);
      const label = document.createElement("span");
      label.textContent = zone.name;
      circle.bindTooltip(label);
    }
    personModels(states, this._entityIds, this._view, this._originals, start, end).forEach((person, index) => {
      const color = COLORS[index % COLORS.length];
      const current = [person.current.latitude, person.current.longitude];
      const marker = L.marker(current, { icon: personIcon(person.name, person.picture, color), title: person.name, bubblingMouseEvents: false });
      marker.bindPopup(popup(person.name, person.current)).addTo(this._layers);
      bounds.push(current);
      if (this._config.hours_to_show > 0 && person.history.length > 1) {
        const path = person.history.map((p) => [p.latitude, p.longitude]);
        L.polyline(path, { color, weight: 3, opacity: .75 }).addTo(this._layers);
        bounds.push(...path);
      }
    });
    for (const geo of geoModels(states, this._config.geo_location_sources || [])) {
      const point = [geo.current.latitude, geo.current.longitude];
      L.circleMarker(point, { radius: 6, color: "#a76100", fillColor: "#f8c543", fillOpacity: .9, weight: 2 })
        .bindPopup(popup(geo.name, geo.current)).addTo(this._layers);
      bounds.push(point);
    }
    const dark = this._config.theme_mode === "dark" || (this._config.theme_mode !== "light" && (this._config.dark_mode ?? this._hass.themes?.darkMode));
    this.shadowRoot.querySelector(".map").classList.toggle("map-dark", Boolean(dark));
    if (bounds.length && (!this._fitted || this._config.auto_fit)) {
      this._map.fitBounds(bounds, { padding: [35, 35], maxZoom: this._config.default_zoom || 14, animate: false });
      this._fitted = true;
    }
  }

  getCardSize() { return 5; }
}

if (!customElements.get("manual-location-map")) customElements.define("manual-location-map", ManualLocationMap);
window.customCards = window.customCards || [];
window.customCards.push({ type: "manual-location-map", name: "Manual Location Map", description: "Mapa ze wspólną lokalizacją i historią." });
