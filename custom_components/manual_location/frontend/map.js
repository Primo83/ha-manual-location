import { callWS, errorText, geoModels, historyOpacity, historyPoints, historySegments, originalIds, personModels, validateView, zoneModels } from "./data.js?v=0.1.30";
import { L, makeMap, mapStyles, personIcon, popup, setMapHass } from "./map-common.js?v=0.1.30";
import { startUpdateCheck } from "./updates.js?v=0.1.30";

const COLORS = ["#186d83", "#b05631", "#644f9a", "#42754b", "#a2376b"];

export class ManualLocationMap extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._epoch = 0;
    this._originals = {};
    this._manualView = false;
    this._onMapInput = (event) => {
      if (this._hold && ((event.type === "pointerdown" && event.pointerId !== this._hold.pointerId)
        || (event.type === "touchstart" && event.touches?.length > 1)
        || ["wheel", "keydown", "dragstart", "zoomstart"].includes(event.type))) this._cancelHold();
      if (event.type === "zoomstart") return;
      if (event.type === "keydown" && !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "+", "-", "=", "_"].includes(event.key)) return;
      if (this._view) this._manualView = true;
    };
    this._onDisconnect = () => this._invalidate("Utracono połączenie. Lokalizacje są ukryte.");
    this._onReady = () => { this._invalidate("Łączenie…"); this._refresh(); };
  }

  set layout(value) {
    this._layout = value;
    this.toggleAttribute("panel", value === "panel");
    this._applyLayout();
  }

  get layout() { return this._layout; }

  _applyLayout() {
    const frame = this.shadowRoot.querySelector(".frame");
    if (!frame) return;
    const parts = String(this._config?.aspect_ratio || "16:9").split(":").map(Number);
    frame.style.aspectRatio = this.layout === "panel" ? "auto"
      : parts.length === 2 && parts.every((n) => Number.isFinite(n) && n > 0) ? `${parts[0]} / ${parts[1]}` : "16 / 9";
  }

  setConfig(config) {
    if (!Array.isArray(config.entities) || !config.entities.length) throw new Error("Podaj listę entities dla karty mapy.");
    const hours = config.hours_to_show === undefined ? 24 : Number(config.hours_to_show);
    if (!Number.isFinite(hours) || hours < 0 || hours > 744) throw new Error("hours_to_show musi mieścić się w 0…744.");
    const previousIds = JSON.stringify(this._configuredIds);
    this._config = { ...config, hours_to_show: hours };
    this._configuredIds = [...new Set(config.entities.map((e) => typeof e === "string" ? e : e.entity))];
    this._entityIds = this._configuredIds.filter((id) => !id.startsWith("zone."));
    this._personIds = this._entityIds.filter((id) => id.startsWith("person."));
    this._invalidate("Łączenie…", previousIds === JSON.stringify(this._configuredIds));
    this._applyLayout();
    if (this.isConnected) { this._ensureMap(); this._refresh(); }
  }

  set hass(hass) {
    const sameUser = Boolean(hass.user?.id) && this._hass?.user?.id === hass.user.id;
    const changed = this._hass?.connection !== hass.connection || this._hass?.user?.id !== hass.user?.id
      || this._hass?.user?.is_admin !== hass.user?.is_admin;
    const disconnected = hass.connected === false && this._hass?.connected !== false;
    this._hass = hass;
    setMapHass(this._map, hass);
    if (changed) {
      this._unbind();
      this._connection = hass.connection;
      if (this.isConnected) this._bind();
      this._invalidate("Łączenie…", sameUser);
    }
    if (disconnected) this._onDisconnect();
    if (this.isConnected && (changed || !this._view) && hass.connected !== false) this._refresh();
  }

  connectedCallback() {
    this._bind();
    this._ensureMap();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 5000);
    this._stopUpdates?.();
    this._stopUpdates = startUpdateCheck(() => this._hass, () => this.isConnected && this.getClientRects().length > 0);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._stopUpdates?.();
    this._unbind();
    this._resize?.disconnect();
    this._invalidate("Łączenie…");
    const container = this._map?.getContainer();
    for (const event of ["pointerdown", "touchstart", "wheel", "keydown"]) container?.removeEventListener(event, this._onMapInput, true);
    this._map?.off("dragstart zoomstart", this._onMapInput);
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

  _invalidate(message, preserveCamera = true) {
    if (!preserveCamera) {
      this._manualView = false;
      this._restoreView = null;
    } else if (this._map && this._view && this._manualView) {
      this._restoreView = { center: this._map.getCenter(), zoom: this._map.getZoom() };
    }
    this._epoch++;
    this._clearMarkerHolds();
    clearTimeout(this._locationRequest?.timer);
    clearTimeout(this._noticeTimer);
    this._locationRequest = null;
    this._refreshNotice = "";
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
    const visible = message || this._refreshNotice || "";
    if (this._statusEl) { this._statusEl.textContent = visible; this._statusEl.hidden = !visible; }
  }

  _cancelHold() {
    clearTimeout(this._hold?.timer);
    this._hold = null;
  }

  _clearMarkerHolds() {
    this._cancelHold();
    for (const remove of this._markerHoldListeners || []) remove();
    this._markerHoldListeners = [];
  }

  _attachHold(marker, person) {
    if (!this._hass.user?.is_admin) return;
    const element = marker.getElement();
    if (!element) return;
    let suppressClick = false;
    const listen = (name, handler) => {
      element.addEventListener(name, handler, true);
      this._markerHoldListeners.push(() => element.removeEventListener(name, handler, true));
    };
    listen("pointerdown", (event) => {
      suppressClick = false;
      if (!event.isPrimary || event.button !== 0) { this._cancelHold(); return; }
      if (this._locationRequest || this._hold) return;
      const hold = { element, pointerId: event.pointerId, x: event.clientX, y: event.clientY };
      this._hold = hold;
      hold.timer = setTimeout(() => {
        if (this._hold !== hold) return;
        suppressClick = true;
        this._requestLocation(person);
      }, 750);
    });
    listen("pointermove", (event) => {
      const hold = this._hold;
      if (hold?.element === element && event.pointerId === hold.pointerId
        && Math.hypot(event.clientX - hold.x, event.clientY - hold.y) > 10) this._cancelHold();
    });
    for (const name of ["pointerup", "pointercancel", "pointerleave"]) listen(name, (event) => {
      if (this._hold?.element === element && event.pointerId === this._hold.pointerId) this._cancelHold();
    });
    listen("click", (event) => {
      if (suppressClick) { event.preventDefault(); event.stopImmediatePropagation(); suppressClick = false; }
    });
    listen("contextmenu", (event) => {
      if (suppressClick || this._hold?.element === element) { event.preventDefault(); event.stopImmediatePropagation(); }
    });
  }

  _finishLocationRequest(message) {
    clearTimeout(this._locationRequest?.timer);
    this._locationRequest = null;
    this._refreshNotice = message;
    this._status(this._message || "");
    this._noticeTimer = setTimeout(() => { this._refreshNotice = ""; this._status(this._message || ""); }, 6000);
  }

  async _requestLocation(person) {
    if (this._locationRequest || !this.isConnected || !this._hass.user?.is_admin) return;
    clearTimeout(this._noticeTimer);
    const parsed = Date.parse(person.current.timestamp);
    const request = { entityId: person.id, baseline: Number.isFinite(parsed) ? parsed : Date.now(), epoch: this._epoch };
    this._locationRequest = request;
    this._refreshNotice = "Sprawdzam aktualizację pozycji…";
    this._status(this._message || "");
    request.timer = setTimeout(() => {
      if (this._locationRequest === request) this._finishLocationRequest("Nie potwierdzono aktualizacji pozycji.");
    }, 60000);
    try {
      const result = await callWS(this._hass, {
        type: "manual_location/refresh_location", entity_id: person.id, wait_for_report: true,
      }, 65000);
      if (this._locationRequest !== request || request.epoch !== this._epoch || !this.isConnected) return;
      if (result?.reported === true) {
        this._finishLocationRequest("Odebrano nowsze dane.");
        this._refresh();
        return;
      }
      this._refresh();
    } catch (error) {
      if (this._locationRequest !== request || request.epoch !== this._epoch || !this.isConnected) return;
      this._finishLocationRequest(error?.code === "refresh_limited" ? "Spróbuj ponownie za chwilę."
        : "Nie udało się sprawdzić aktualizacji pozycji.");
    }
  }

  _ensureMap() {
    if (this._map || !this._config || !this.isConnected) return;
    this.shadowRoot.innerHTML = `<style>:host{display:block}ha-card{display:block;overflow:hidden;box-sizing:border-box;background:var(--ha-card-background,var(--card-background-color,#fff));border-radius:var(--ha-card-border-radius,12px);border:var(--ha-card-border-width,1px) solid var(--divider-color,#ddd)}h2{flex:none;font-size:20px;font-weight:400;margin:0;padding:20px 20px 14px;color:var(--primary-text-color,#25343b)}.frame{position:relative;min-height:230px}.map{position:absolute;inset:0;height:100%}:host([panel]){height:100%;min-height:0}:host([panel]) ha-card{display:flex;flex-direction:column;height:100%;min-height:0}:host([panel]) .frame{flex:1;min-height:0}.fit-map{width:44px;height:44px;background:white;color:#333;border:0;border-radius:2px;cursor:pointer;display:grid;place-items:center}.fit-map svg{width:24px;height:24px}.fit-map:focus-visible{outline:3px solid #186d83}.status{position:absolute;z-index:1000;left:12px;right:12px;top:12px;border-radius:6px;padding:12px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#25343b);box-shadow:0 2px 8px #0002;text-align:center}.status[hidden]{display:none}</style><ha-card><h2 hidden></h2><div class="frame"><div class="map"></div><div class="status" role="status"></div></div></ha-card>`;
    mapStyles(this.shadowRoot);
    const title = this.shadowRoot.querySelector("h2");
    title.textContent = this._config.title || "";
    title.hidden = !this._config.title;
    const frame = this.shadowRoot.querySelector(".frame");
    this._applyLayout();
    this._statusEl = this.shadowRoot.querySelector(".status");
    this._map = makeMap(this.shadowRoot.querySelector(".map"), this._hass);
    this._layers = L.layerGroup().addTo(this._map);
    const container = this._map.getContainer();
    for (const event of ["pointerdown", "touchstart", "wheel", "keydown"]) container.addEventListener(event, this._onMapInput, { capture: true, passive: true });
    this._map.on("dragstart zoomstart", this._onMapInput);
    const fit = L.control({ position: "topleft" });
    fit.onAdd = () => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "fit-map leaflet-bar";
      button.title = "Dopasuj mapę";
      button.setAttribute("aria-label", "Dopasuj mapę");
      button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 3h6v2H5v4H3V3m12 0h6v6h-2V5h-4V3M3 15h2v4h4v2H3v-6m16 0h2v6h-6v-2h4v-4M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/></svg>';
      L.DomEvent.disableClickPropagation(button);
      L.DomEvent.disableScrollPropagation(button);
      button.addEventListener("click", () => this._fitView());
      return button;
    };
    fit.addTo(this._map);
    this._resize = new ResizeObserver(() => this._map?.invalidateSize({ pan: true, animate: false }));
    this._resize.observe(frame);
    this._status(this._message || "Łączenie…");
  }

  _fitView() {
    if (!this._view) return;
    this._manualView = false;
    this._restoreView = null;
    this._fitted = false;
    const now = Date.now();
    this._render(now - this._config.hours_to_show * 3600000, now);
  }

  async _refresh() {
    if (!this.isConnected || !this._hass || !this._config || this._busy || this._hold || this._hass.connected === false) return;
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
    if (!this._map || !this._view || this._hold) return;
    this._clearMarkerHolds();
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
      marker.bindPopup(popup(person.name, person.current, person.activity)).addTo(this._layers);
      this._attachHold(marker, person);
      const request = this._locationRequest;
      // A genuinely newer displayed point is fresh data even while the command waits.
      if (request && request.entityId === person.id && Date.parse(person.current.timestamp) > request.baseline) {
        this._finishLocationRequest("Odebrano nowsze dane.");
      }
      bounds.push(current);
      if (this._config.hours_to_show > 0 && person.history.length) {
        const path = person.history.map((p) => [p.latitude, p.longitude]);
        const radius = "ontouchstart" in window || (globalThis.navigator?.maxTouchPoints || 0) > 0 ? 8 : 3;
        person.history.forEach((point, pointIndex) => {
          const opacity = historyOpacity(pointIndex, path.length);
          const label = document.createElement("div");
          label.textContent = `${person.name}\n${new Date(point.timestamp).toLocaleString()}`;
          label.style.whiteSpace = "pre-line";
          L.circleMarker(path[pointIndex], { radius, color, weight: 3, opacity, fillOpacity: opacity, interactive: true })
            .bindTooltip(label, { direction: "top" }).addTo(this._layers);
          if (pointIndex < path.length - 1) {
            for (const segment of historySegments(path[pointIndex], path[pointIndex + 1])) {
              L.polyline(segment, { color, weight: 3, opacity, interactive: false }).addTo(this._layers);
            }
          }
        });
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
    if (this._restoreView) {
      this._map.setView(this._restoreView.center, this._restoreView.zoom, { animate: false });
      this._restoreView = null;
      this._fitted = true;
    }
    if (bounds.length && !this._manualView && (!this._fitted || this._config.auto_fit)) {
      this._map.fitBounds(bounds, { padding: [35, 35], maxZoom: this._config.default_zoom || 14, animate: false });
      this._fitted = true;
    }
  }

  getCardSize() { return 5; }
}

if (!customElements.get("manual-location-map")) customElements.define("manual-location-map", ManualLocationMap);
window.customCards = window.customCards || [];
window.customCards.push({ type: "manual-location-map", name: "Manual Location Map", description: "Mapa ze wspólną lokalizacją i historią." });
