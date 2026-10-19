import { activityPayload, activitySettings, adminPersonOptions, callWS, editHistory, errorText, localDateInput, readPoint, removeHistory } from "./data.js?v=2.0.1";
import { L, makeMap, mapStyles, popup } from "./map-common.js?v=2.0.1";
import { registerReloadBlocker, startUpdateCheck } from "./updates.js?v=2.0.1";
import "./scenarios.js?v=2.0.1";
import "./place-search.js?v=2.0.1";

const DRAFT_COLOR = "#7c3aed";
const CUSTOM_COLOR = "#1976d2";
const HA_COLOR = "#168447";
const EDIT_COLOR = "#c65d00";
const hasCoordinates = (point) => Number.isFinite(point?.latitude) && Math.abs(point.latitude) <= 90
  && Number.isFinite(point?.longitude) && Math.abs(point.longitude) <= 180;
const haKey = (row) => JSON.stringify([row.kind, row.ref]);
const sameCoordinates = (a, b) => !!a && !!b && a.latitude === b.latitude && a.longitude === b.longitude;
const templateKey = (point) => JSON.stringify([point.timestamp, point.latitude, point.longitude]);
const deletedPointKey = (point) => JSON.stringify([Date.parse(point.timestamp),
  (point.timestamp.match(/\.(\d+)/)?.[1] || "").slice(3).replace(/0+$/, ""), point.latitude, point.longitude]);
const historyKey = deletedPointKey;

export class ManualLocationAdmin extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._epoch = 0;
    this._gatewayStatus = null;
    this._gatewayRequest = 0;
    this._gatewayNormalRequested = null;
    this._selected = "";
    this._editIndex = -1;
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._deletedHistory = {};
    this._historySelection = new Set();
    this._historySelectionGeneration = 0;
    this._historyChecks = [];
    this._historyOlderCount = 0;
    this._haHistory = null;
    this._haSelection = new Set();
    this._haGeneration = 0;
    this._scenarioDrafts = {};
    this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    const markEditing = (event) => {
      if (["manual-location-scenarios", "manual-location-place-search"].includes(event.target?.tagName?.toLowerCase())) return;
      if (["person", "person-enabled", "person-ha", "global-enabled", "pin", "new-pin", "ha-delete-start", "ha-delete-end", "ha-delete-custom"].includes(event.target?.id) || event.target?.id?.startsWith("history-select-") || event.target?.id?.startsWith("ha-history-select-")
        || event.target?.id?.startsWith("activity-")) return;
      this._editing = true; this._syncScenarios(); this._syncHistorySelection(); this._syncLocationButtons(); this._placeSearch?.sync?.();
    };
    this.shadowRoot.addEventListener("input", markEditing);
    this.shadowRoot.addEventListener("change", markEditing);
    this._onDisconnect = () => this._clearSession("Połączenie zmieniło się. Odblokuj panel ponownie; szkice zachowano w tej karcie.", true);
    this._onReady = () => this._clearSession("Odblokuj panel po ponownym połączeniu.", true);
    this._beforeUnload = (event) => { if (this._hasDrafts()) { event.preventDefault(); event.returnValue = ""; } };
  }

  set hass(hass) {
    const sameUser = !!hass.user?.id && hass.user.id === this._hass?.user?.id && !!hass.user?.is_admin;
    const changed = this._hass?.connection !== hass.connection || this._hass?.user?.id !== hass.user?.id;
    const disconnected = hass.connected === false && this._hass?.connected !== false;
    this._hass = hass;
    if (changed) {
      this._unbind();
      this._connection = hass.connection;
      if (this.isConnected) this._bind();
      this._clearSession("", sameUser);
    } else if (disconnected || !hass.user?.is_admin) this._clearSession("", sameUser);
    else this._syncScenarios();
    this._syncPlaceSearch();
    if (this.isConnected && !this.shadowRoot.querySelector("main")) this._render();
  }

  connectedCallback() {
    this._bind(); this._render();
    window.addEventListener?.("beforeunload", this._beforeUnload);
    this._removeReloadBlocker = registerReloadBlocker(() => !this._token && !this._busy && !this._hasDrafts());
    this._stopUpdates = startUpdateCheck(() => this._hass, () => this.isConnected && this.getClientRects().length > 0);
  }
  disconnectedCallback() {
    window.removeEventListener?.("beforeunload", this._beforeUnload);
    this._stopUpdates?.(); this._stopUpdates = null;
    this._removeReloadBlocker?.(); this._removeReloadBlocker = null;
    this._unbind(); this._clearSession("", true); this._destroyMap();
    clearTimeout(this._deviceToastTimer); this._deviceToast?.remove(); this._deviceToast = null;
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
  _clearSession(message = "", preserve = false) {
    this._cancelLockedQuick();
    this._quickInfo = null;
    preserve = preserve && !!this._hass?.user?.id && !!this._hass.user.is_admin;
    if (preserve && this._snapshot) {
      this._retained = { owner: this._hass?.user?.id, snapshot: this._snapshot, saved: this._savedSnapshot,
        currentDrafts: this._currentDrafts, removedCurrent: this._removedCurrent, deletedHistory: this._deletedHistory,
        dirty: this._dirty, editing: this._editing, editIndex: this._editIndex, gatewayNormalRequested: this._gatewayNormalRequested,
        historyCurrent: !!this.shadowRoot.querySelector("#history-current")?.checked,
        fields: Object.fromEntries(["current-lat", "current-lon", "history-lat", "history-lon", "history-time"].map((id) => [id, this.shadowRoot.querySelector(`#${id}`)?.value ?? ""])) };
    } else if (!preserve) {
      this._retained = null; this._scenarioDrafts = {};
      this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    }
    this._cancelLocation();
    this._epoch++;
    this._resetHaHistory();
    this._clearHistorySelection(true);
    this._token = null;
    this._snapshot = null;
    this._savedSnapshot = null;
    this._sourceSnapshot = null;
    this._gatewayStatus = null;
    this._gatewayNormalRequested = null;
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._deletedHistory = {};
    this._editIndex = -1;
    this._busy = false;
    this._historyOlderCount = 0;
    this._dirty = false;
    this._editing = false;
    this._scenarios = null;
    clearTimeout(this._expiry);
    clearTimeout(this._pollTimer); this._pollTimer = null;
    if (this.isConnected) { this._render(); this._message(message); }
  }
  _destroyMap() { this._historySelectionLayers?.remove(); this._historySelectionLayers = null; this._editMarker?.remove(); this._editMarker = null; this._cancelLocation(); this._resize?.disconnect(); this._map?.remove(); this._map = null; this._locateButton = null; }
  _message(text, good = false) {
    const box = this.shadowRoot.querySelector("#message");
    if (box) { box.textContent = text; box.classList.toggle("good", good); box.hidden = !text; }
  }
  _toast(message, kind = "info", duration = 7000, lockedMessage = message) {
    clearTimeout(this._deviceToastTimer); this._deviceToast?.remove();
    const toast = document.createElement("div");
    toast.className = "device-toast";
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    toast.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
    Object.assign(toast.style, { position: "fixed", bottom: "calc(80px + env(safe-area-inset-bottom, 0px))",
      left: "50%", transform: "translateX(-50%)", maxWidth: "90vw", width: "420px", boxSizing: "border-box",
      zIndex: "10000", padding: "15px 18px", borderRadius: "12px", color: "#fff",
      background: kind === "success" ? "#17643b" : kind === "error" ? "#a52828" : "#29434e",
      boxShadow: "0 4px 20px #0005", font: "600 15px/1.5 sans-serif", overflowWrap: "anywhere" });
    this._deviceToastLockedMessage = lockedMessage;
    toast.textContent = this._token ? message : lockedMessage;
    this._deviceToast = toast; this.shadowRoot.append(toast);
    if (duration) this._deviceToastTimer = setTimeout(() => { toast.remove(); if (this._deviceToast === toast) this._deviceToast = null; }, duration);
  }
  _changed() { this._dirty = true; this._saveState(); }
  _placeAllowed() {
    return this.isConnected && !!this._token && !!this._hass?.user?.is_admin && this._hass.connected !== false
      && !this._busy && !this._scenarios?.busy && !!this._hass.states[this._selected];
  }
  _syncPlaceSearch() {
    if (!this._token || !this._snapshot) { this._placeSearch?.cancel?.(); return; }
    const search = this.shadowRoot.querySelector("#admin-place-search"); if (!search) return;
    this._placeSearch = search;
    search.context = {
      getState: () => ({ hass: this._hass, token: this._token, allowed: this._placeAllowed(),
        stamp: JSON.stringify([this._epoch, this._selected, this._editIndex, this._snapshot?.revision,
          this._scenarioView.selectedByEntity[this._selected], this._person(),
          ["current-lat", "current-lon", "history-lat", "history-lon", "history-time"].map((id) => this.shadowRoot.querySelector(`#${id}`)?.value)]) }),
      onBeforeSearch: () => this._cancelLocation(),
      onSelect: (point) => this._choosePlace(point),
      onExpired: (message, code) => this._sessionError(message, code),
    };
  }
  _choosePlace(point) {
    if (!this._placeAllowed()) return false;
    this._cancelLocation();
    const editing = this._editIndex >= 0;
    for (const prefix of editing ? ["history"] : ["current", "history"]) {
      this.shadowRoot.querySelector(`#${prefix}-lat`).value = point.latitude.toFixed(6);
      this.shadowRoot.querySelector(`#${prefix}-lon`).value = point.longitude.toFixed(6);
    }
    this._editing = true;
    if (editing) this._previewEdit();
    else {
      const label = document.createElement("span"); label.textContent = point.label;
      this._deviceLocation = L.layerGroup().addTo(this._map);
      L.circleMarker([point.latitude, point.longitude], { radius: 8, color: "#1976d2", fillOpacity: 1 })
        .bindTooltip(label, { permanent: true, direction: "top", className: "device-location-tooltip" }).addTo(this._deviceLocation);
    }
    this._map.setView([point.latitude, point.longitude], Math.max(this._map.getZoom(), 14));
    this._saveState();
    this._message(editing ? "Miejsce wybrane. Sprawdź podgląd i zastosuj edycję punktu; godzina pozostała bez zmian."
      : "Miejsce wybrane. Ustaw bieżącą pozycję lub dodaj punkt historii, a następnie zapisz zmiany.", true);
    return true;
  }
  _hasDrafts() { return !!(this._gatewayNormalRequested || this._dirty || this._editing || this._editIndex >= 0 || Object.values(this._deletedHistory).some((points) => points.length)
    || Object.keys(this._removedCurrent).length || Object.keys(this._retained?.removedCurrent || {}).length
    || this._retained?.dirty || this._retained?.editing || this._retained?.editIndex >= 0
    || Object.values(this._scenarioDrafts).some((draft) => draft.dirty || draft.editing || draft.routeInput?.dirty)
    || this._scenarios?.hasLocalChanges); }
  _syncScenarios() {
    const child = this.shadowRoot.querySelector("manual-location-scenarios");
    if (!child || !this._token || !this._snapshot) return;
    this._scenarios = child;
    child.context = { drafts: this._scenarioDrafts, view: this._scenarioView,
      getState: () => ({ hass: this._hass, token: this._token, snapshot: this._snapshot, entityId: this._selected,
        busy: !!this._busy, parentDirty: !!(this._dirty || this._editing || this._editIndex >= 0 || Object.keys(this._removedCurrent).length) }),
      onSnapshot: (snapshot) => this._acceptScenarioSnapshot(snapshot),
      onStateChange: () => this._saveState(),
      onSelectPerson: (entityId) => this._selectPerson(entityId),
      onExpired: (message, code) => this._sessionError(message, code),
    };
  }
  _acceptScenarioSnapshot(snapshot) {
    if (!this._token || this._dirty || this._editing || this._editIndex >= 0 || Object.keys(this._removedCurrent).length) return false;
    if (snapshot.revision !== this._snapshot?.revision) this._resetHaHistory();
    this._snapshot = snapshot; this._savedSnapshot = structuredClone(snapshot);
    this._sourceSnapshot = this._savedSnapshot;
    const global = this.shadowRoot.querySelector("#global-enabled"); if (global) global.checked = snapshot.enabled;
    this._refreshPerson(false); this._saveState(); return true;
  }
  _schedulePoll() {
    clearTimeout(this._pollTimer);
    if (!this._token || !this.isConnected) return;
    this._pollTimer = setTimeout(async () => { await this._pollGateway(); await this._poll(); this._schedulePoll(); }, 5000);
  }
  async _pollGateway(hold = false) {
    if (!this._token || this._busy || this._scenarios?.busy) return;
    const token = this._token, epoch = this._epoch, request = ++this._gatewayRequest;
    if (hold) { this._busy = true; this._saveState(); }
    try {
      const status = await callWS(this._hass, { type: "manual_location/gateway_status", token });
      if (epoch !== this._epoch || token !== this._token || request !== this._gatewayRequest) return;
      this._gatewayStatus = status;
      this._refreshSources();
    } catch (error) {
      if (epoch !== this._epoch || token !== this._token || request !== this._gatewayRequest) return;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
      else {
        this._gatewayStatus = { ...this._gatewayStatus, enabled: true, available: false };
        this._refreshSources();
      }
    } finally { if (hold && epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _poll() {
    if (!this._token || this._busy || this._scenarios?.busy || this._hasDrafts()) return;
    const token = this._token, epoch = this._epoch;
    try {
      const snapshot = await callWS(this._hass, { type: "manual_location/admin_get", token });
      if (epoch === this._epoch && token === this._token && !this._busy && !this._scenarios?.busy && !this._hasDrafts()) {
        if (snapshot.revision !== this._snapshot.revision || JSON.stringify(snapshot.scenarios) !== JSON.stringify(this._snapshot.scenarios)) this._acceptScenarioSnapshot(snapshot);
        else if (this._historyWindowMinute !== Math.floor(Date.now() / 60000)) { this._refreshHistory(); this._drawPerson(); }
        if (!this._historyOlderCount && !this._haSelection.size && !this._haHistory?.error && Date.now() - (this._haHistory?.loadedAt || 0) >= 30000) this._refreshHaHistory();
      }
    } catch (error) {
      if (epoch !== this._epoch || token !== this._token) return;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
      else this._message(errorText(error));
    }
  }
  _sessionError(message, code) {
    const preserve = !["unauthorized", "forbidden"].includes(code);
    this._clearSession(preserve ? `${message} Szkice zachowano w tej karcie.` : message, preserve);
  }
  _saveState() {
    this._refreshSources();
    const status = this.shadowRoot.querySelector("#save-state");
    if (status) status.textContent = this._editIndex >= 0 ? "Zastosuj albo anuluj edycję punktu przed zapisem" : this._dirty ? "Masz niezapisane zmiany" : `Zapisane · rewizja ${this._snapshot?.revision}`;
    const save = this.shadowRoot.querySelector("#save");
    if (save) save.disabled = this._busy || this._scenarios?.busy || !this._dirty || this._editIndex >= 0;
    const layout = this.shadowRoot.querySelector(".layout");
    if (layout) layout.inert = !!(this._busy || this._scenarios?.busy);
    const overview = this.shadowRoot.querySelector(".person-overview");
    if (overview) overview.inert = !!(this._busy || this._scenarios?.busy);
    const reload = this.shadowRoot.querySelector("#reload");
    if (reload) reload.disabled = !!(this._busy || this._scenarios?.busy);
    const pinChange = this.shadowRoot.querySelector(".pin-change");
    if (pinChange) pinChange.inert = !!(this._busy || this._scenarios?.busy);
    this._syncScenarios();
    this._syncPlaceSearch();
    this._syncHistorySelection();
    this._syncHaHistory();
    this._syncLocationButtons();
    this._syncLockedQuick();
  }
  _selectPerson(entityId) {
    const select = this.shadowRoot.querySelector("#person");
    if (this._scenarios?.busy || this._busy) { select.value = this._selected; return false; }
    if (entityId === this._selected) return true;
    if ((this._editing || this._editIndex >= 0) && !window.confirm("Zmienić osobę? Niezastosowane pola bieżącej pozycji i historii zostaną odrzucone. Przygotowane pozycje, historia i szkice planów pozostaną.")) { select.value = this._selected; return false; }
    this._clearHistorySelection(true);
    this._resetHaHistory();
    this._selected = entityId; select.value = entityId;
    this._historyOlderCount = 0;
    this._editIndex = -1; this._editing = false; this._refreshPerson(); this._saveState();
    return true;
  }

  _render() {
    this._cancelLockedQuick();
    this._personButtons = [];
    this._destroyMap();
    const admin = this._hass?.user?.is_admin;
    const unlocked = admin && this._token && this._snapshot;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;color:var(--primary-text-color,#20343b);background:var(--primary-background-color,#f2f5f4);font-family:var(--paper-font-body1_-_font-family,"Segoe UI",sans-serif)}*{box-sizing:border-box}main{max-width:1220px;margin:auto;padding:32px 28px 56px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:700;color:var(--secondary-text-color,#657a82);margin-bottom:8px}h1{font-family:Georgia,serif;font-size:32px;font-weight:400;margin:0}h2{font-size:17px;margin:0 0 18px}.sub{color:var(--secondary-text-color,#657a82);line-height:1.6;font-size:14px;margin:10px 0 0}.box{border:1px solid var(--divider-color,#d9e2e0);border-radius:12px;background:var(--card-background-color,#fff);padding:22px}.lockbox{max-width:480px;margin:58px auto}.layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}.sidebar{display:flex;flex-direction:column;gap:18px}.mapbox{padding:0;overflow:hidden}.map{height:380px}.caption{padding:12px 18px;border-top:1px solid var(--divider-color,#d9e2e0);font-size:13px;color:var(--secondary-text-color,#657a82)}label{display:block;font-size:13px;font-weight:600;margin-bottom:7px}input,select,button{font:inherit}input,select{width:100%;min-height:42px;padding:9px 11px;background:var(--input-fill-color,var(--card-background-color,#fff));color:inherit;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}input:focus,select:focus{outline:2px solid #338b9c;outline-offset:1px}.field{margin-bottom:16px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.toggle{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:500;cursor:pointer}.toggle input{width:20px;min-height:20px;accent-color:#176e82}.toggle small{display:block;font-weight:400;color:var(--secondary-text-color,#657a82);margin-top:3px}button{min-height:40px;border-radius:7px;padding:9px 14px;cursor:pointer;background:transparent;color:inherit;border:1px solid var(--divider-color,#c8d6d4);font-size:13px;font-weight:600}button:hover:not(:disabled){background:#63889a16}button:focus-visible{outline:3px solid #338b9c;outline-offset:2px}button.primary{background:#176e82;color:white;border-color:#176e82}button.primary:hover:not(:disabled){background:#105769}button:disabled{opacity:.48;cursor:default}.wide{width:100%}.actions{display:flex;gap:8px;flex-wrap:wrap}.history{margin-top:18px}.history-top{display:flex;align-items:center;justify-content:space-between;gap:16px}.history-top h2{margin:0}.history-editor{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(230px,1.5fr);gap:10px;align-items:end;padding:20px 0}.history-editor .field{margin:0}.history-editor .actions{grid-column:1/-1}.scroll{max-height:340px;overflow:auto;border-top:1px solid var(--divider-color,#d9e2e0)}.row{display:flex;align-items:center;justify-content:space-between;padding:13px 0;gap:12px;border-bottom:1px solid var(--divider-color,#e3e9e7)}.row time{display:block;font-size:13px;font-weight:600}.row small{display:block;margin-top:4px;color:var(--secondary-text-color,#657a82)}.row button{min-height:34px;padding:5px 10px}.danger{color:#b74435}.empty{padding:22px 0;color:var(--secondary-text-color,#657a82);font-size:14px;line-height:1.6}.footer{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:20px;font-size:13px;color:var(--secondary-text-color,#657a82)}#message{margin:0 0 20px;padding:13px 16px;border-radius:8px;background:#a8402715;border:1px solid #a8402750;font-size:14px;line-height:1.5}#message.good{background:#34815e13;border-color:#34815e50}[hidden]{display:none!important}.current-summary{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:0 0 18px}.pin-change{margin-top:18px}.pin-change summary{cursor:pointer;font-size:13px}.pin-change form{margin-top:16px;max-width:400px}.notice{border-left:3px solid #176e82;padding-left:12px;font-size:13px;color:var(--secondary-text-color,#657a82);line-height:1.6}
      @media(max-width:900px){.layout{grid-template-columns:1fr}.sidebar{display:grid;grid-template-columns:1fr 1fr}.history-editor{grid-template-columns:1fr 1fr}.map{height:340px}}@media(max-width:580px){main{padding:22px 14px 36px}header{align-items:flex-start}h1{font-size:27px}.sidebar{display:flex}.box{padding:18px}.mapbox{padding:0}.history-editor{grid-template-columns:1fr 1fr}.history-editor .timefield{grid-column:1/-1}.footer{align-items:flex-start;flex-direction:column}.row{align-items:flex-start}.map{height:290px}}
      .map-edit-tools{padding:14px 18px;border-top:2px solid #c65d00;background:#c65d0010}.map-edit-tools p{margin:0 0 10px;font-size:13px;line-height:1.5}.map-edit-tools .actions{align-items:center}.leaflet-tooltip.edit-point-tooltip{white-space:normal;max-width:220px;border-color:#c65d00}
      @media(max-width:580px){#ha-delete-range-fields .pair{grid-template-columns:1fr}}
      .leaflet-tooltip.device-location-tooltip{width:180px;white-space:normal;text-align:center}
      .leaflet-tooltip.history-selection-tooltip{max-width:230px;white-space:normal;text-align:center;background:#fff5b8;color:#172b4d;border:2px solid #172b4d;font-weight:600}.history-selection-times{max-height:190px;overflow:auto;margin-top:6px}
      .layout>*,.sidebar>*,.field,input{min-width:0}.scroll{position:relative}.row>div:first-child{min-width:0;overflow-wrap:anywhere}.row .actions{flex-shrink:0}.trash{width:40px;display:grid;place-items:center}.savebar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:16px 0 12px}@media(max-width:580px){.savebar{flex-direction:column;align-items:stretch}.savebar #save{order:1}}.savebar span{font-size:13px;color:var(--secondary-text-color,#657a82)}.row.draft{background:#7c3aed14;border-left:3px solid #7c3aed;padding-left:10px;padding-right:10px}.draft-badge,.current-badge{display:inline-block;margin-top:6px;font-size:12px;font-weight:700}.draft-badge::before,.current-badge::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:#7c3aed;margin-right:6px}
      .current-badge::before{background:#176e82}
      .history-current{grid-column:1/-1;margin:4px 0 8px}.history-current input{flex-shrink:0}.row>.actions{max-width:55%;justify-content:flex-end}@media(max-width:580px){.row{flex-wrap:wrap}.row>.actions{width:100%;max-width:none;justify-content:flex-start}}
      .row.deleted{background:#b7443510;border-left:3px solid #b74435;padding-left:12px}.row.deleted time{color:#a63227;text-decoration:line-through}.deleted-badge{display:block;color:#a63227;font-size:12px;font-weight:700;margin-top:6px}.scroll.editing{max-height:620px}.history-editor.editing{border:2px solid #c65d00;border-radius:8px;padding:16px;margin:10px 0;background:var(--card-background-color,#fff)}.history-editor h3{grid-column:1/-1;font-size:15px;margin:0}.history-editor .actions{align-self:end}@media(max-width:580px){.history-editor .actions{grid-column:1/-1}}
      .template-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}.template-tools span{font-size:13px}.row{position:relative;padding-left:32px}.row.draft{padding-left:32px}.row input.history-select{position:absolute;left:0;top:15px;width:22px;height:22px;min-height:22px;margin:0;padding:0;accent-color:#176e82}.row.draft input.history-select{left:4px}.template-hint{margin:0 0 12px}
      .row.custom{border-left:4px solid #1976d2;background:#1976d20c}.row.ha{border-left:4px solid #168447;background:#16844710}.row.custom,.row.ha{padding-left:32px;padding-right:8px}.source-badge{display:block;font-size:12px;font-weight:700;margin-top:6px}.custom .source-badge{color:#1976d2}.ha .source-badge{color:#168447}.history-legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin:12px 0;font-size:13px;font-weight:700}.history-legend span::before{content:"";display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;background:var(--history-color)}.history-legend .custom{--history-color:#1976d2}.history-legend .ha{--history-color:#168447}.history-legend .draft{--history-color:#7c3aed}.row.ha input.history-select{accent-color:#168447}.ha-history-tools{border-top:1px solid var(--divider-color,#d9e2e0);padding-top:12px}.ha-preview{max-width:92vw;width:580px;max-height:85vh;overflow:auto;border:1px solid var(--divider-color,#c8d6d4);border-radius:12px;padding:22px;color:var(--primary-text-color,#20343b);background:var(--card-background-color,#fff)}.ha-preview ul{max-height:34vh;overflow:auto;padding-left:22px;line-height:1.6;overflow-wrap:anywhere}.ha-preview::backdrop{background:#0008}
      .person-overview{margin-bottom:18px}.person-overview h2{margin-bottom:8px}.person-sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:16px}.person-source-card{text-align:left;display:flex;flex-direction:column;gap:6px;min-height:84px;padding:14px 16px;overflow-wrap:anywhere}.person-source-card strong{font-size:16px}.person-source-card span{font-weight:500;font-size:13px}.person-source-card[aria-pressed="true"]{border:2px solid #176e82;background:#176e820c;padding:13px 15px}.person-source-card[aria-pressed="true"] strong::after{content:" · edytujesz";font-size:12px;color:var(--secondary-text-color,#657a82)}.source-pending{font-size:13px;font-weight:700;color:var(--primary-text-color,#20343b);border-left:3px solid #c65d00;padding-left:8px;margin:4px 0 0}.source-pending[hidden]{display:none}.global-source{border-top:1px solid var(--divider-color,#d9e2e0);padding-top:16px;margin:18px 0 0}.source-options{padding:0;border:0;margin:0}.source-options legend{font-size:13px;font-weight:600;margin-bottom:10px}.source-choice{border:1px solid var(--divider-color,#c8d6d4);border-radius:8px;padding:12px;min-height:66px}.source-choice:has(input:checked){border-color:#176e82;background:#176e820c}.source-choice:has(input:disabled){opacity:.6}.source-choice input{flex-shrink:0;margin:0}.source-status{font-size:13px;font-weight:700;margin:16px 0 4px}.source-help{margin:10px 0 16px}.activity-context{margin:-8px 0 16px}@media(max-width:580px){.person-sources{grid-template-columns:1fr}.person-overview .savebar{margin-bottom:0}}
      #quick-nickname{overflow-wrap:anywhere}.gps-save{background:#b94700;color:#fff;border:2px solid #963800;min-height:52px;font-weight:750;margin-top:8px;box-shadow:0 3px 9px #b9470033}.gps-save:hover:not(:disabled){background:#9e3c00}.gps-save:disabled{opacity:.55;box-shadow:none}</style><main>${unlocked ? '<header><div><div class="eyebrow">Manual Location</div><h1>Pozycje i historia</h1><p class="sub">Jeden zapis, wspólny dla wszystkich kart mapy.</p></div><button id="lock">Zablokuj panel</button></header><div id="message" role="status" hidden></div>' : ""}
      ${!admin ? '<section class="box lockbox"><h2>Ustawienia</h2><p class="sub">Zaloguj się na konto administratora Home Assistant.</p><div id="message" role="status" hidden></div></section>' : !unlocked ? '<section class="box lockbox"><h2>Odblokuj ustawienia</h2><div id="message" role="status" hidden></div><form id="unlock-form"><div class="field" style="margin-top:22px"><label for="pin">PIN administratora (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="one-time-code" data-1p-ignore autocapitalize="none" autocorrect="off" spellcheck="false" required></div><button class="primary wide" type="submit">Odblokuj</button><button class="wide gps-save" id="quick-update" type="button" aria-describedby="quick-nickname">Aktualizuj</button><p class="sub" id="quick-nickname"></p></form></section>' : `
      <section class="box person-overview"><h2>Osoby i źródła lokalizacji</h2><p class="sub">Źródło zapisane dla każdej osoby. Wybierz osobę, aby edytować jej ustawienia.</p><div id="person-sources" class="person-sources" aria-label="Osoby i zapisane źródła"></div><label class="toggle global-source"><input type="checkbox" id="global-enabled"><span>Zezwalaj na ręczne lokalizacje<small id="global-source-help">Wspólne dla wszystkich osób. Wyłączenie przełącza wszystkie mapy na Home Assistant.</small></span></label><p class="notice" id="source-conflict" role="status" hidden></p><div class="savebar"><button class="primary" id="save">Zapisz wszystkie zmiany</button><span id="save-state"></span><button id="reload">Wczytaj zapisane dane</button></div></section>
      <div class="layout"><aside class="sidebar"><section class="box"><h2 id="person-title">Ustawienia osoby</h2><div class="field"><label for="person">Edytujesz ustawienia osoby</label><select id="person"></select></div><fieldset class="source-options"><legend id="person-source-legend">Źródło lokalizacji na mapach</legend><label class="toggle source-choice"><input type="radio" name="person-source" id="person-ha"><span>Home Assistant<small id="person-ha-help">Pozycja i historia z HA</small></span></label><label class="toggle source-choice"><input type="radio" name="person-source" id="person-enabled"><span>Ręczna (custom)<small id="person-custom-help">Pozycje przygotowane w tym panelu</small></span></label></fieldset><p class="source-status" id="person-source-status" role="status"></p><p class="source-pending" id="person-source-pending" role="status" hidden></p><p class="sub source-help" id="person-source-help"></p><p class="notice" id="missing-person-notice" hidden>Tej osoby nie ma już w Home Assistant. Usuń jej ustawienia poniżej, a następnie zapisz wszystkie zmiany. Pozostałe osoby zachowają swoje ustawienia.</p><div class="field" style="margin-top:18px"><label for="person-nickname">Pseudonim na ekranie PIN</label><input id="person-nickname" maxlength="40" autocomplete="off"></div><label class="toggle"><input type="checkbox" id="quick-target"><span>Szybki zapis na ekranie PIN dla tej osoby</span></label><p class="sub">Jedna przypisana osoba na wszystkich urządzeniach. Zapisz wszystkie zmiany, aby zastosować pseudonim i przypisanie.</p><button class="wide danger" id="remove-person">Usuń ustawienia tej osoby</button></section>
      <section class="box"><h2 id="activity-title">Aktywność telefonu</h2><p class="sub activity-context">Osobne ustawienia tej osoby, niezależne od źródła lokalizacji. Te same zaznaczenia mogą oznaczać takie same ustawienia domyślne.</p><label class="toggle"><input type="checkbox" id="activity-enabled"><span>Pokaż kropkę i czas dla tej osoby</span></label><p class="sub">Zielona przez godzinę od aktywności, potem czerwona. Brak świeżych danych oznacza szarą kropkę. To wskazówka, czy można napisać, a nie potwierdzenie snu.</p><div style="margin-top:18px"><label class="toggle"><input type="checkbox" id="activity-unlock"><span>Odblokowanie<small>Ekran aktywny i telefon odblokowany</small></span></label><label class="toggle"><input type="checkbox" id="activity-walking"><span>Rozpoznane chodzenie lub bieg<small>Wymaga rozpoznanego chodzenia lub biegu</small></span></label><label class="toggle"><input type="checkbox" id="activity-screen"><span>Włączenie ekranu<small>Może zadziałać także po powiadomieniu</small></span></label><label class="toggle"><input type="checkbox" id="activity-tilting"><span>Poruszenie / przechylenie telefonu<small>Wykryte przez Companion; ruch w kieszeni lub aucie też może zadziałać</small></span></label></div><div class="field" style="margin-top:18px"><label for="activity-match">Kiedy pokazać zieloną kropkę?</label><select id="activity-match"><option value="any">Dowolny wybrany sygnał</option><option value="all">Wszystkie wybrane sygnały</option></select><p class="sub">Liczy się ostatnia godzina. W trybie „Wszystkie” sygnały nie muszą wystąpić jednocześnie.</p></div><label class="toggle"><input type="checkbox" id="activity-manual"><span>Ustaw czerwoną do odwołania</span></label><div class="field" style="margin-top:14px"><label for="activity-since">Data i godzina ostatniej aktywności</label><input id="activity-since" type="datetime-local" step="1"></div><p class="sub">Czerwona pojawi się od razu po zapisaniu, z podanym czasem. Wyłącz przełącznik, aby wrócić do automatu.</p><p class="sub">Automat korzysta z czujników Companion włączonych przez właściciela telefonu: Interactive, Keyguard locked i Detected activity. Te ustawienia nie włączają czujników ani uprawnień Androida. Użyj wspólnego przycisku „Zapisz wszystkie zmiany”.</p><button class="wide" id="activity-reset">Przywróć domyślne dla aktywności</button></section>
      <section class="box"><h2>Bieżąca pozycja</h2><p class="current-summary" id="current-summary"></p><div class="field"><button class="wide" id="use-device-location">Użyj lokalizacji tego urządzenia</button><p class="sub">Jednorazowy odczyt z telefonu lub komputera, na którym otwarto panel. Wymaga zgody na lokalizację; dokładność zależy od urządzenia.</p><button class="wide gps-save" id="use-device-location-save" aria-describedby="gps-save-help">Pobierz GPS i zapisz pozycję</button><p class="sub" id="gps-save-help">GPS → bieżąca pozycja i historia → zapis jednym kliknięciem. Najpierw zapisz lub odrzuć pozostałe zmiany. Ręczna zmiana zatrzymuje aktywny plan tej osoby.</p></div><div class="pair"><div class="field"><label for="current-lat">Szerokość</label><input id="current-lat" type="number" step="any" min="-90" max="90"></div><div class="field"><label for="current-lon">Długość</label><input id="current-lon" type="number" step="any" min="-180" max="180"></div></div><button class="wide" id="set-current">Ustaw bieżącą pozycję</button><p class="sub">Każde ustawienie dodaje osobny szkic z godziną. Przed zapisem możesz zmienić jego czas i współrzędne albo go usunąć.</p></section></aside>
      <section><div class="box mapbox"><manual-location-place-search id="admin-place-search"></manual-location-place-search><div class="map" id="map" aria-label="Mapa wyboru pozycji"></div><div class="caption">Kliknij mapę, aby uzupełnić współrzędne. Następnie ustaw pozycję lub dodaj punkt historii.</div><div class="map-edit-tools" id="map-edit-tools" hidden><p id="map-edit-status" role="status"></p><div class="actions"><button class="primary" id="apply-map-edit" type="submit" form="history-form">Zastosuj edycję</button><button id="cancel-map-edit" type="button">Anuluj edycję</button></div></div></div><section class="box history"><div class="history-top"><h2>Historia</h2><span class="eyebrow" id="count"></span></div><p class="sub">Od najnowszych do najstarszych. Kolory źródeł są widoczne tylko w tym panelu.</p><div class="history-legend" aria-label="Źródła historii"><span class="custom">Custom</span><span class="ha">Historia HA</span><span class="draft">Niezapisany szkic</span></div><p class="sub">Custom edytujesz i usuwasz przez „Zapisz wszystkie zmiany”. Usunięcie historii HA wymaga osobnego podglądu i potwierdzenia.</p><div class="template-tools ha-history-tools" id="ha-history-tools" hidden><span id="ha-history-status" role="status"></span><button id="ha-history-reload" type="button">Odśwież historię HA</button><button id="ha-history-delete" class="danger" type="button">Usuń zaznaczone HA</button><button id="ha-history-clear" type="button">Wyczyść zaznaczenie HA</button></div><p class="sub" id="ha-history-help" hidden></p><details id="ha-delete-range-fields" hidden><summary>Usuń historię z przedziału</summary><p class="sub">Wszystkie zapisy wybranej osoby i jej przypisanego telefonu, także niewczytane na listę. Daty są lokalne, granice włączne. Domyślnie punkty Custom pozostaną.</p><div class="pair" style="margin-top:12px"><div class="field"><label for="ha-delete-start">Od</label><input id="ha-delete-start" type="datetime-local" step="1"></div><div class="field"><label for="ha-delete-end">Do</label><input id="ha-delete-end" type="datetime-local" step="1"></div></div><label class="toggle field"><input id="ha-delete-custom" type="checkbox"><span>Uwzględnij również punkty customowe<small>Z historii w podanym przedziale. Bieżąca własna pozycja pozostanie aktywna.</small></span></label><button class="danger" id="ha-delete-range" type="button">Usuń wszystkie HA z przedziału</button><p class="sub">Najwyżej 31 dni w jednej operacji. Przed usunięciem zobaczysz podgląd i potwierdzisz zakres.</p></details><div class="template-tools"><span id="history-range" role="status"></span><button id="history-more" type="button">Pokaż starsze</button><button id="history-recent" type="button" hidden>Ostatnie 48 godzin</button></div><div class="template-tools"><span id="history-selection-count" role="status"></span><button id="history-template" type="button">Utwórz szablon trasy</button><button id="history-selection-clear" type="button">Wyczyść zaznaczenie</button></div><p class="sub template-hint" id="history-template-hint"></p><div class="scroll" id="history-list"></div><div id="history-editor-home"><form id="history-form" class="history-editor"><h3 id="history-edit-title" hidden>Edycja punktu</h3><div class="field"><label for="history-lat">Szerokość</label><input id="history-lat" type="number" step="any" min="-90" max="90" required></div><div class="field"><label for="history-lon">Długość</label><input id="history-lon" type="number" step="any" min="-180" max="180" required></div><div class="field timefield"><label for="history-time">Lokalna data i godzina</label><input id="history-time" type="datetime-local" step="1" required></div><label class="toggle history-current"><input id="history-current" type="checkbox"><span>Ustaw także jako bieżącą pozycję<small>Zachowa godzinę punktu. Mapy zaktualizują się po zapisaniu wszystkich zmian. Ręczna zmiana zatrzyma aktywny plan tej osoby.</small></span></label><div class="actions"><button id="add-history" type="submit">Dodaj punkt</button><button id="cancel-edit" type="button" hidden>Anuluj edycję punktu</button></div></form></div></section></section></div>
      <manual-location-scenarios></manual-location-scenarios>
      <details class="pin-change"><summary>Zmień PIN administratora</summary><form id="pin-form"><div class="field"><label for="new-pin">Nowy PIN (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="new-pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="off" required></div><button type="submit">Zmień PIN i zablokuj sesje</button></form></details>`}</main>`;
    if (this._deviceToast) {
      if (!unlocked) this._deviceToast.textContent = this._deviceToastLockedMessage;
      this.shadowRoot.append(this._deviceToast);
    }
    if (!admin) return;
    if (!unlocked) {
      this.shadowRoot.querySelector("#unlock-form").addEventListener("submit", (e) => { e.preventDefault(); this._unlock(); });
      this.shadowRoot.querySelector("#quick-update").addEventListener("click", () => this._quickSaveLocked());
      this._syncLockedQuick(); this._loadQuickInfo();
      return;
    }
    mapStyles(this.shadowRoot);
    this._map = makeMap(this.shadowRoot.querySelector("#map"), this._hass);
    this._layers = L.layerGroup().addTo(this._map);
    const locateControl = L.control({ position: "topleft" });
    locateControl.onAdd = () => {
      const box = document.createElement("div");
      box.className = "leaflet-bar";
      const button = document.createElement("button");
      button.type = "button";
      button.title = "Użyj lokalizacji tego urządzenia";
      button.setAttribute("aria-label", button.title);
      button.style.cssText = "width:44px;height:44px;padding:9px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#20343b)";
      button.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4" stroke="currentColor" stroke-width="2"/></svg>';
      button.addEventListener("click", () => this._locate());
      box.append(button);
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      this._locateButton = button;
      return box;
    };
    locateControl.addTo(this._map);
    this._map.on("click", ({ latlng }) => {
      if (this._busy || !this._hass.states[this._selected]) return;
      this._cancelLocation();
      const editing = this._editIndex >= 0;
      for (const prefix of editing ? ["history"] : ["current", "history"]) {
        this.shadowRoot.querySelector(`#${prefix}-lat`).value = latlng.lat.toFixed(6);
        this.shadowRoot.querySelector(`#${prefix}-lon`).value = latlng.lng.toFixed(6);
      }
      this._editing = true;
      if (editing) this._previewEdit();
      this._message(editing ? "Podgląd punktu przesunięty. Zastosuj albo anuluj edycję." : "Współrzędne są gotowe. Ustaw bieżącą pozycję albo dodaj punkt historii.", true);
    });
    this._resize = new ResizeObserver(() => this._map?.invalidateSize());
    this._resize.observe(this.shadowRoot.querySelector("#map"));
    const select = this.shadowRoot.querySelector("#person");
    const scenarioOwners = Object.fromEntries(Object.values(this._snapshot.scenarios || {}).map((plan) => [plan.entity_id, {}]));
    const persons = adminPersonOptions(this._hass.states, { ...scenarioOwners, ...this._snapshot.activity, ...this._snapshot.persons });
    for (const person of persons) {
      const option = document.createElement("option");
      option.value = person.id;
      option.textContent = person.name;
      select.append(option);
      const button = document.createElement("button"); button.type = "button"; button.className = "person-source-card";
      const name = document.createElement("strong"); name.textContent = person.name;
      const source = document.createElement("span");
      const pending = document.createElement("small"); pending.className = "source-pending";
      button.append(name, source, pending);
      button.addEventListener("click", () => this._selectPerson(person.id));
      this.shadowRoot.querySelector("#person-sources").append(button);
      this._personButtons.push({ id: person.id, button, source, pending });
    }
    if (!persons.some((p) => p.id === this._selected)) this._selected = persons[0]?.id || "";
    select.value = this._selected;
    select.addEventListener("change", () => this._selectPerson(select.value));
    const global = this.shadowRoot.querySelector("#global-enabled");
    global.checked = this._snapshot.enabled;
    global.addEventListener("change", () => { this._snapshot.enabled = global.checked; if (!global.checked) this._gatewayNormalRequested = null; this._changed(); });
    this.shadowRoot.querySelector("#person-enabled").addEventListener("change", (e) => {
      if (!this._hass.states[this._selected]) return;
      const person = this._person();
      if (person) { person.enabled = e.target.checked; if (e.target.checked && this._gatewayNormalRequested === this._selected) this._gatewayNormalRequested = null; this._changed(); }
    });
    this.shadowRoot.querySelector("#person-ha").addEventListener("click", () => {
      if (!this._gatewayFor(this._selected) || !this._snapshot.enabled || !this._placeAllowed()) return;
      this._gatewayNormalRequested = this._selected;
      if (this._person()) this._person().enabled = false;
      this._changed();
    });
    this.shadowRoot.querySelector("#person-ha").addEventListener("change", (e) => {
      if (!e.target.checked || !this._hass.states[this._selected]) return;
      const person = this._person();
      if (person) { person.enabled = false; this._changed(); }
    });
    this.shadowRoot.querySelector("#person-nickname").addEventListener("input", (event) => {
      const quick = this._snapshot.quick_save ||= { nicknames: {}, entity_id: null };
      const nickname = event.target.value.trim();
      if (nickname) quick.nicknames[this._selected] = nickname;
      else { delete quick.nicknames[this._selected]; if (quick.entity_id === this._selected) quick.entity_id = null; }
      this.shadowRoot.querySelector("#quick-target").checked = quick.entity_id === this._selected;
      this._changed();
    });
    this.shadowRoot.querySelector("#quick-target").addEventListener("change", (event) => {
      const quick = this._snapshot.quick_save ||= { nicknames: {}, entity_id: null };
      if (event.target.checked && !quick.nicknames[this._selected]) {
        event.target.checked = false; this._message("Najpierw ustaw pseudonim tej osoby."); return;
      }
      quick.entity_id = event.target.checked ? this._selected : null; this._changed();
    });
    for (const id of ["enabled", "unlock", "walking", "screen", "tilting", "match", "manual", "since"]) {
      this.shadowRoot.querySelector(`#activity-${id}`).addEventListener(id === "since" ? "input" : "change", () => this._changeActivity());
    }
    this.shadowRoot.querySelector("#activity-reset").addEventListener("click", () => {
      if (this._busy || this._scenarios?.busy || !this._snapshot.activity?.[this._selected]) return;
      delete this._snapshot.activity[this._selected]; this._refreshActivity(); this._changed();
    });
    this.shadowRoot.querySelector("#set-current").addEventListener("click", () => this._setCurrent());
    this.shadowRoot.querySelector("#use-device-location").addEventListener("click", () => this._locate());
    this.shadowRoot.querySelector("#use-device-location-save").addEventListener("click", () => this._locate(true));
    for (const field of this.shadowRoot.querySelectorAll("#current-lat, #current-lon")) field.addEventListener("input", () => this._cancelLocation());
    this.shadowRoot.querySelector("#remove-person").addEventListener("click", () => this._removePerson());
    this.shadowRoot.querySelector("#history-form").addEventListener("submit", (e) => { e.preventDefault(); this._addHistory(); });
    for (const id of ["history-lat", "history-lon", "history-time"]) {
      this.shadowRoot.querySelector(`#${id}`).addEventListener("input", () => { this._cancelLocation(); this._previewEdit(id !== "history-time"); });
    }
    for (const id of ["cancel-edit", "cancel-map-edit"]) this.shadowRoot.querySelector(`#${id}`).addEventListener("click", () => this._cancelEdit());
    this.shadowRoot.querySelector("#save").addEventListener("click", () => this._save());
    this.shadowRoot.querySelector("#reload").addEventListener("click", () => this._reload());
    this.shadowRoot.querySelector("#history-template").addEventListener("click", () => this._createHistoryTemplate());
    this.shadowRoot.querySelector("#history-selection-clear").addEventListener("click", () => this._clearHistorySelection());
    this.shadowRoot.querySelector("#history-more").addEventListener("click", () => this._changeHistoryRange(false));
    this.shadowRoot.querySelector("#history-recent").addEventListener("click", () => this._changeHistoryRange(true));
    this.shadowRoot.querySelector("#ha-history-reload").addEventListener("click", () => this._refreshHaHistory());
    this.shadowRoot.querySelector("#ha-history-delete").addEventListener("click", () => this._deleteHaHistory());
    this.shadowRoot.querySelector("#ha-history-clear").addEventListener("click", () => { this._haSelection.clear(); this._syncHaHistory(); });
    this.shadowRoot.querySelector("#ha-delete-range").addEventListener("click", () => this._deleteHaRange());
    for (const id of ["ha-delete-start", "ha-delete-end"]) this.shadowRoot.querySelector(`#${id}`).addEventListener("input", () => this._syncHaHistory());
    this.shadowRoot.querySelector("#ha-delete-custom").addEventListener("change", () => this._syncHaHistory());
    this.shadowRoot.querySelector("#ha-delete-start").value = localDateInput(new Date(Date.now() - 86400000).toISOString());
    this.shadowRoot.querySelector("#ha-delete-end").value = localDateInput(new Date(Date.now()).toISOString());
    this.shadowRoot.querySelector("#ha-delete-custom").checked = false;
    this.shadowRoot.querySelector("#lock").addEventListener("click", () => this._lock());
    this.shadowRoot.querySelector("#pin-form").addEventListener("submit", (e) => { e.preventDefault(); this._changePin(); });
    this._refreshPerson();
    this._saveState();
  }

  _person() { return this._snapshot?.persons[this._selected]; }
  _gatewayFor(entityId) {
    return this._gatewayStatus?.enabled === true && this._gatewayStatus.person_id === entityId ? this._gatewayStatus : null;
  }
  _gatewaySourceLabel(entityId) {
    const status = this._gatewayFor(entityId);
    if (!status) return this._gatewayStatus?.enabled && !this._gatewayStatus.person_id ? "Nie potwierdzono stanu bramki" : null;
    if (status.available !== true) return "Nie potwierdzono stanu bramki";
    if (status.pending || status.replay_required || status.upstream_uncertain || status.recovery_retryable || status.mode === "blocked") return status.gps_blocked === true
      ? "GPS zablokowany · odzyskiwanie własnej pozycji" : "Nie potwierdzono blokady GPS · odzyskiwanie";
    if (["custom", "warmup"].includes(status.mode) && status.gps_blocked !== true) return "Nie potwierdzono blokady GPS";
    if (status.mode === "warmup") return `${status.has_custom_point ? "Ostatnia własna pozycja" : "GPS zablokowany"} · karencja ${Math.max(0, Math.ceil(Number(status.remaining_seconds) || 0))} s`;
    if (status.mode === "custom") return status.has_custom_point ? "Ręczna (custom) · GPS zablokowany" : "GPS zablokowany · brak własnego punktu";
    if (status.mode === "normal") {
      const receipt = status.last_phone_receipt;
      return receipt?.origin === "phone" && receipt.verified === true && receipt.person_id === entityId && receipt.replay !== true
        ? "Home Assistant" : "GPS dopuszczony · oczekiwanie na raport";
    }
    return "Nie potwierdzono stanu bramki";
  }
  _pendingSourceLabel(entityId) {
    if (!this._gatewayFor(entityId)) return this._sourceLabel(this._snapshot, entityId);
    if (!this._snapshot.enabled) return "Żądanie blokady GPS";
    if (this._snapshot.persons[entityId]?.enabled) return "Ręczna (custom)";
    return this._gatewayNormalRequested === entityId ? "Home Assistant · karencja 5 minut" : "Bez żądania włączenia GPS";
  }
  _sourceLabel(snapshot, entityId) {
    if (!this._hass.states[entityId]) return "Osoba niedostępna w HA";
    return snapshot?.enabled && snapshot.persons[entityId]?.enabled ? "Ręczna (custom)" : "Home Assistant";
  }
  _refreshSources() {
    if (!this._snapshot || !this._token) return;
    const saved = this._sourceSnapshot || this._savedSnapshot;
    const conflict = saved?.revision !== this._snapshot.revision;
    const pendingLabel = conflict ? "Szkic (konflikt rewizji): " : "Po zapisie: ";
    for (const { id, button, source, pending } of this._personButtons || []) {
      const current = this._gatewaySourceLabel(id) || this._sourceLabel(saved, id), next = this._pendingSourceLabel(id);
      button.setAttribute("aria-pressed", String(id === this._selected));
      source.textContent = `${this._gatewayFor(id) ? "Stan" : "Zapisane"}: ${current}`;
      pending.textContent = `${pendingLabel}${next}`;
      pending.hidden = this._sourceLabel(saved, id) === this._sourceLabel(this._snapshot, id) && this._gatewayNormalRequested !== id;
    }
    const field = id => this.shadowRoot.querySelector(`#${id}`);
    const name = this._hass.states[this._selected]?.attributes.friendly_name || this._selected;
    field("person-title").textContent = `Ustawienia: ${name}`;
    field("activity-title").textContent = `Aktywność telefonu: ${name}`;
    const gateway = this._gatewayFor(this._selected), gatewayLabel = this._gatewaySourceLabel(this._selected);
    field("person-source-status").textContent = gatewayLabel ? `Stan: ${gatewayLabel}` : `Zapisane na mapach: ${this._sourceLabel(saved, this._selected)}`;
    const next = this._pendingSourceLabel(this._selected);
    field("person-source-pending").textContent = `${pendingLabel}${next}`;
    field("person-source-pending").hidden = this._sourceLabel(saved, this._selected) === this._sourceLabel(this._snapshot, this._selected)
      && this._gatewayNormalRequested !== this._selected;
    field("global-source-help").textContent = this._gatewayStatus?.enabled
      ? "Wyłączenie zatrzymuje własne trasy i blokuje GPS chronionej osoby. Powrót GPS wymaga wybrania Home Assistant i zapisania zmian."
      : "Wspólne dla wszystkich osób. Wyłączenie przełącza wszystkie mapy na Home Assistant.";
    field("person-source-legend").textContent = gateway ? "Źródło lokalizacji w Home Assistant" : "Źródło lokalizacji na mapach";
    field("person-ha-help").textContent = gateway ? "Normalny GPS po 5-minutowej karencji" : "Pozycja i historia z HA";
    field("person-custom-help").textContent = gateway ? "Własna pozycja dla całego HA" : "Pozycje przygotowane w tym panelu";
    field("source-conflict").textContent = "Dane HA zmieniły się podczas blokady. Zapisane źródła pokazują ostatni odczyt HA; pola poniżej zawierają starszy szkic. Wczytaj zapisane dane, aby odrzucić szkic i rozpocząć od aktualnych ustawień.";
    field("source-conflict").hidden = !conflict;
    const person = this._person(), missing = !this._hass.states[this._selected];
    field("person-enabled").checked = !!person?.enabled;
    field("person-ha").checked = !person?.enabled;
    field("person-enabled").disabled = missing || !person;
    field("person-ha").disabled = missing;
    field("person-source-help").textContent = missing ? "Tej osoby nie ma w Home Assistant. Nie można zmienić jej źródła lokalizacji."
      : gateway && gateway.available !== true ? "Nie potwierdzono stanu bramki. Zapis ustawień nie potwierdza zastosowania pozycji ani blokady GPS."
      : gateway && !this._snapshot.enabled ? "Po zapisaniu własne trasy zostaną zatrzymane i zostanie wysłane żądanie blokady GPS."
      : gateway && !person?.enabled ? this._gatewayNormalRequested === this._selected
        ? "Po zapisaniu zmian zacznie się 5-minutowa karencja. Dopiero potem Companion będzie mógł przesyłać lokalizację."
        : gateway.mode === "warmup" && gateway.gps_blocked === true
          ? "Trwa karencja po świadomym powrocie do GPS. Raporty Companion są jeszcze odrzucane; ponowne kliknięcie Home Assistant i zapis zacznie karencję od nowa."
        : gateway.mode === "normal" && !gateway.pending && !gateway.upstream_uncertain && !gateway.replay_required
          ? "GPS jest dopuszczony. Ręczne punkty pozostają w panelu; raporty Companion mogą aktualizować pozycję całego HA."
        : "Powrót GPS wymaga świadomego kliknięcia Home Assistant i zapisania zmian. Sam zapis historii lub aktywności nie włącza GPS."
      : gateway && gateway.available && gateway.mode === "custom" && !gateway.has_custom_point
        ? "Nie zapisano jeszcze własnego punktu; pozostaje ostatnia pozycja HA. Nowe raporty GPS są zablokowane."
      : !person
      ? "Brak ręcznej pozycji. Ustaw pierwszą pozycję poniżej, aby przygotować źródło ręczne, a następnie zapisz zmiany."
      : person.enabled && !this._snapshot.enabled
        ? "W szkicu wybrano źródło ręczne, ale wspólny przełącznik jest wyłączony. Ta konfiguracja używa danych Home Assistant."
        : person.enabled ? "Wybrana konfiguracja: pozycje i historia przygotowane w tym panelu."
          : "Wybrana konfiguracja: lokalizacja i historia Home Assistant. Zachowane ręczne punkty pozostają w panelu.";
  }
  _currentChanged() {
    const current = this._person()?.current;
    return !!current && (!!this._person().current_history_timestamp || !sameCoordinates(current, this._savedSnapshot?.persons[this._selected]?.current));
  }
  _resetHaHistory() {
    this._haGeneration++;
    this._haHistory = null;
    this._haSelection.clear();
    this._haConfirmation?.();
  }
  _haContext() {
    return { epoch: this._epoch, generation: this._haGeneration, token: this._token,
      entityId: this._selected, connection: this._hass?.connection, user: this._hass?.user?.id,
      revision: this._snapshot?.revision };
  }
  _haContextValid(request) {
    return this.isConnected && request.epoch === this._epoch && request.generation === this._haGeneration
      && request.token === this._token && !!this._token && request.entityId === this._selected
      && request.connection === this._hass?.connection && request.user === this._hass?.user?.id
      && this._hass?.user?.is_admin && this._hass.connected !== false && request.revision === this._snapshot?.revision;
  }
  _ensureHaHistory() {
    if (!this._token || !this._gatewayFor(this._selected) || this._haHistory) return;
    const end = new Date(Date.now()).toISOString();
    this._haHistory = { entityId: this._selected, rows: [], cursor: null, hasMore: true, loading: false,
      start: new Date(Date.parse(end) - 31 * 86400000).toISOString(), end };
    this._loadHaHistory();
  }
  _refreshHaHistory() {
    if (this._busy || this._scenarios?.busy || this._haHistory?.loading) return;
    this._resetHaHistory();
    this._ensureHaHistory();
    if (this._token && this._snapshot) { this._refreshHistory(); this._drawPerson(); }
  }
  async _loadHaHistory() {
    const history = this._haHistory;
    if (!history || history.loading || !history.hasMore || history.supported === false) return;
    const request = this._haContext();
    history.loading = true; history.error = null; this._syncHaHistory();
    try {
      const page = await callWS(this._hass, { type: "manual_location/ha_history", token: request.token,
        entity_id: request.entityId, start: history.start, end: history.end, cursor: history.cursor });
      if (!this._haContextValid(request) || history !== this._haHistory) return;
      if (page?.supported === false) { history.supported = false; history.hasMore = false; history.rows = []; return; }
      if (!page || page.revision !== request.revision || !Array.isArray(page.rows) || page.rows.length > 100
        || typeof page.has_more !== "boolean" || !Number.isInteger(page.history_revision)
        || (history.revision !== undefined && page.history_revision !== history.revision)
        || (page.has_more && (typeof page.next_cursor !== "string" || !page.next_cursor || page.next_cursor === history.cursor))) {
        throw new Error("ha_history_changed");
      }
      const rows = page.rows.map((row) => {
        const timestamp = row?.timestamp || row?.point?.timestamp;
        if (!["accepted", "native"].includes(row?.kind) || typeof row.ref !== "string" || !row.ref || row.ref.length > 256
          || typeof row.entity_id !== "string" || !/^(person|device_tracker)\./.test(row.entity_id)
          || typeof timestamp !== "string" || !Number.isFinite(Date.parse(timestamp))
          || Date.parse(timestamp) < Date.parse(history.start) || Date.parse(timestamp) > Date.parse(history.end)
          || (row.point !== null && (!hasCoordinates(row.point) || row.point.timestamp !== timestamp))) throw new Error("ha_history_invalid");
        return { kind: row.kind, ref: row.ref, entity_id: row.entity_id, timestamp,
          point: row.point === null ? null : { latitude: row.point.latitude, longitude: row.point.longitude, timestamp } };
      });
      const merged = new Map(history.rows.map((row) => [haKey(row), row]));
      for (const row of rows) {
        if (merged.has(haKey(row)) && JSON.stringify(merged.get(haKey(row))) !== JSON.stringify(row)) throw new Error("ha_history_changed");
        merged.set(haKey(row), row);
      }
      history.rows = [...merged.values()].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
      history.cursor = page.next_cursor ?? null; history.hasMore = page.has_more;
      history.revision = page.history_revision; history.loadedAt = Date.now(); history.supported = true;
    } catch (error) {
      if (!this._haContextValid(request) || history !== this._haHistory) return;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
      else { history.error = "Historia HA nie została wczytana w całości. Odśwież ją przed wyborem lub usuwaniem rekordów."; this._haSelection.clear(); }
    } finally {
      if (this._haContextValid(request) && history === this._haHistory) {
        history.loading = false; this._refreshHistory(); this._drawPerson();
      }
    }
  }
  _haOperationAllowed() {
    const gateway = this._gatewayFor(this._selected);
    return this._placeAllowed() && !this._hasDrafts() && this._haHistory?.supported === true
      && !this._haHistory.loading && !this._haHistory.error && gateway?.available === true
      && gateway.mode === "custom" && gateway.gps_blocked === true && gateway.has_custom_point === true
      && !gateway.pending && !gateway.replay_required && !gateway.upstream_uncertain && !gateway.recovery_retryable;
  }
  _syncHaHistory() {
    const tools = this.shadowRoot.querySelector("#ha-history-tools");
    if (!tools) return;
    const history = this._haHistory, enabled = !!this._gatewayFor(this._selected);
    tools.hidden = !enabled;
    const help = this.shadowRoot.querySelector("#ha-history-help"); help.hidden = !enabled;
    this.shadowRoot.querySelector("#ha-delete-range-fields").hidden = !enabled;
    if (!enabled) return;
    const allowed = this._haOperationAllowed();
    this.shadowRoot.querySelector("#ha-history-status").textContent = history?.loading ? "Wczytywanie historii HA…"
      : history?.supported === false ? "Historia HA niedostępna w tej wersji."
      : history?.error || `HA: ${history?.rows.length || 0} pobranych · zaznaczono ${this._haSelection.size}${history?.hasMore ? " · są kolejne wpisy" : ""}`;
    help.textContent = "Historia HA obejmuje ostatnie 31 dni; starsze punkty Custom pozostają dostępne. Wiersz HA może być także publikacją pozycji Custom. "
      + (allowed ? "Wybierz konkretne rekordy HA do osobnego podglądu usunięcia."
        : "Usuwanie HA wymaga zapisanych zmian i potwierdzonego trybu custom z blokadą GPS.");
    this.shadowRoot.querySelector("#ha-history-reload").disabled = !!(this._busy || this._scenarios?.busy || history?.loading);
    this.shadowRoot.querySelector("#ha-history-delete").disabled = !allowed || !this._haSelection.size;
    this.shadowRoot.querySelector("#ha-history-clear").disabled = !!this._busy || !this._haSelection.size;
    this.shadowRoot.querySelector("#ha-delete-range").disabled = !allowed;
    this.shadowRoot.querySelector("#ha-delete-range").textContent = this.shadowRoot.querySelector("#ha-delete-custom").checked
      ? "Usuń HA i custom z przedziału" : "Usuń wszystkie HA z przedziału";
    for (const { input, row, remove } of this._haChecks || []) {
      input.checked = this._haSelection.has(haKey(row)); input.disabled = !allowed;
      remove.disabled = !allowed;
    }
  }
  _confirmHaDelete(preview, range = null) {
    return new Promise((resolve) => {
      const dialog = document.createElement("dialog"); dialog.className = "ha-preview";
      dialog.setAttribute("aria-label", "Potwierdź usunięcie historii HA");
      const title = document.createElement("h2"); title.textContent = range ? "Usuń historię z przedziału" : "Usuń wybrane zapisy HA";
      const text = document.createElement("p");
      text.textContent = "Poniżej jest dokładny zakres usunięcia z aktywnej historii HA i naszych map. Własne punkty Custom pozostaną. Operacja nie usuwa wcześniejszych kopii zapasowych ani danych już wysłanych poza HA.";
      if (range) text.textContent = `Usunąć wszystkie zapisy HA tej osoby i przypisanego telefonu od ${new Date(range.start).toLocaleString()} do ${new Date(range.end).toLocaleString()} (włącznie)? `
        + (range.include_custom ? "Usunięcie obejmie również punkty historii Custom z tego przedziału. Bieżąca własna pozycja pozostanie aktywna. " : "Punkty Custom pozostaną. ")
        + "Operacja nie usuwa wcześniejszych kopii zapasowych ani danych już wysłanych poza HA. Usunięcie historii nie wyłącza zapisywania kolejnych danych HA.";
      const list = document.createElement("ul");
      for (const row of preview.rows.slice(0, 200)) {
        const item = document.createElement("li");
        const timestamp = row.timestamp || row.point?.timestamp;
        const label = row.kind === "custom" ? "Custom" : row.entity_id.startsWith("person.") ? "HA · osoba" : "HA · telefon";
        item.style.color = row.kind === "custom" ? CUSTOM_COLOR : HA_COLOR;
        item.textContent = `${label} · ${new Date(timestamp).toLocaleString()}${row.point === null ? " · bez pozycji" : ""}`;
        list.append(item);
      }
      const summary = document.createElement("p");
      summary.textContent = `Zapisy osoby: ${preview.counts.persons}. Zapisy telefonu: ${preview.counts.trackers}. Odebrane punkty na naszych mapach: ${preview.counts.accepted}.`
        + ` Punkty historii Custom: ${preview.counts.custom}.`
        + (preview.truncated ? ` Lista pokazuje ${preview.rows.length} wpisów; potwierdzenie obejmie cały podany zakres i wszystkie powyższe liczby.` : " Potwierdź tylko ten zakres.");
      const actions = document.createElement("div"); actions.className = "actions";
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "Anuluj";
      const confirm = document.createElement("button"); confirm.type = "button"; confirm.className = "danger";
      confirm.textContent = range?.include_custom ? "Potwierdź usunięcie HA i custom" : "Potwierdź usunięcie z HA";
      let done = false;
      const settle = (value) => {
        if (done) return; done = true; this._haConfirmation = null;
        if (dialog.open) dialog.close(); dialog.remove(); resolve(value);
      };
      this._haConfirmation = () => settle(false);
      cancel.addEventListener("click", () => settle(false)); confirm.addEventListener("click", () => settle(true));
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); settle(false); });
      dialog.addEventListener("close", () => settle(false));
      actions.append(cancel, confirm); dialog.append(title, text, list, summary, actions); this.shadowRoot.append(dialog);
      try { dialog.showModal(); cancel.focus?.(); } catch { settle(false); }
    });
  }
  async _deleteHaRange() {
    const start = Date.parse(this.shadowRoot.querySelector("#ha-delete-start").value);
    const end = Date.parse(this.shadowRoot.querySelector("#ha-delete-end").value);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || end > Date.now() || end - start > 31 * 86400000) {
      this._message("Podaj poprawny przedział Od–Do, bez przyszłych dat, o długości najwyżej 31 dni."); return false;
    }
    return this._deleteHaHistory(null, { start: new Date(start).toISOString(), end: new Date(end).toISOString(),
      include_custom: this.shadowRoot.querySelector("#ha-delete-custom").checked === true });
  }
  async _deleteHaHistory(selection = null, range = null) {
    if (!this._haOperationAllowed()) {
      this._message("Najpierw zapisz lub odrzuć szkice i potwierdź tryb custom z blokadą GPS. Historia HA nie została usunięta."); return false;
    }
    const selected = selection || this._haHistory.rows.filter((row) => this._haSelection.has(haKey(row)));
    if (!range && (!selected.length || selected.length > 100 || selected.some((row) => !this._haHistory.rows.some((current) => haKey(current) === haKey(row))))) return false;
    const request = this._haContext();
    this._busy = true; this._cancelLocation(); this._saveState();
    let sent = false;
    try {
      const preview = await callWS(this._hass, { type: range ? "manual_location/ha_history_range_preview" : "manual_location/ha_history_preview", token: request.token,
        entity_id: request.entityId, revision: request.revision,
        ...(range || { selection: selected.map(({ kind, ref }) => ({ kind, ref })) }) }, 30000);
      if (!this._haContextValid(request)) return false;
      const expires = typeof preview?.expires_at === "number" ? preview.expires_at * 1000 : Date.parse(preview?.expires_at);
      if (typeof preview?.preview_token !== "string" || !preview.preview_token || typeof preview.operation_id !== "string" || !preview.operation_id
        || !Array.isArray(preview.rows) || preview.rows.length > 200
        || preview.rows.some((row) => typeof row?.entity_id !== "string" || !Number.isFinite(Date.parse(row.timestamp || row.point?.timestamp)))
        || ["native", "accepted", "persons", "trackers", "custom"].some((key) => !Number.isInteger(preview.counts?.[key]) || preview.counts[key] < 0 || preview.counts[key] > 100000)
        || preview.counts.native !== preview.counts.persons + preview.counts.trackers || !preview.counts.native && !preview.counts.accepted && !preview.counts.custom
        || (!range?.include_custom && preview.counts.custom !== 0)
        || (range?.include_custom && preview.current_preserved !== true)
        || (range && (Date.parse(preview.selected_range?.start) !== Date.parse(range.start) || Date.parse(preview.selected_range?.end) !== Date.parse(range.end)))
        || !Number.isFinite(expires) || expires <= Date.now()) throw new Error("ha_preview_invalid");
      if (!await this._confirmHaDelete(preview, range) || !this._haContextValid(request) || Date.now() >= expires) return false;
      sent = true;
      const result = await callWS(this._hass, { type: "manual_location/ha_history_execute", token: request.token,
        entity_id: request.entityId, revision: request.revision, preview_token: preview.preview_token, operation_id: preview.operation_id }, 120000);
      if (!this._haContextValid(request)) return false;
      if (result?.deleted !== true || result.operation_id !== preview.operation_id) throw new Error("ha_delete_unconfirmed");
      if (range?.include_custom) {
        const snapshot = await callWS(this._hass, { type: "manual_location/admin_get", token: request.token });
        if (!this._haContextValid(request)) return false;
        if (!snapshot || !Number.isInteger(snapshot.revision) || !snapshot.persons || !snapshot.persons[request.entityId]) throw new Error("ha_delete_unconfirmed");
        this._snapshot = snapshot; this._savedSnapshot = structuredClone(snapshot); this._sourceSnapshot = this._savedSnapshot;
      }
      this._haSelection.clear(); this._resetHaHistory();
      this._message(range?.include_custom
        ? "Potwierdzono usunięcie historii HA i wybranych punktów Custom z przedziału. Bieżąca własna pozycja pozostała aktywna."
        : "Potwierdzono usunięcie wskazanych rekordów z aktywnej historii HA. Własne punkty Custom pozostały.", true);
      return true;
    } catch (error) {
      if (!this._haContextValid(request)) return false;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
      else {
        const message = sent ? "Nie potwierdzono zakończenia usuwania. Odśwież historię HA i sprawdź wynik przed ponowną próbą."
          : (error?.code?.startsWith("history_") ? errorText(error) + " Niczego nie usunięto." : "Nie udało się przygotować aktualnego podglądu. Niczego nie usunięto. Odśwież historię HA i wybierz rekordy ponownie.");
        if (this._haHistory) this._haHistory.error = message;
        this._haSelection.clear(); this._message(message);
      }
      return false;
    } finally {
      if (request.epoch === this._epoch) {
        this._busy = false;
        if (this._token && this._snapshot) { this._ensureHaHistory(); this._refreshHistory(); this._drawPerson(); this._saveState(); }
      }
    }
  }
  _historyRows() {
    const saved = new Set((this._savedSnapshot?.persons[this._selected]?.history || []).map(historyKey));
    const current = this._person()?.current;
    const rows = (this._person()?.history || []).map((point, index) => ({ point, index, source: "custom",
      current: !!current && templateKey(point) === templateKey(current), draft: !saved.has(historyKey(point)) }));
    if (current && !rows.some((row) => row.current)) rows.push({ point: current, index: -1, source: "custom", current: true, draft: this._currentChanged() });
    for (const row of this._haHistory?.rows || []) rows.push({ source: "ha", ha: row, index: -2,
      point: row.point || { timestamp: row.timestamp, latitude: null, longitude: null } });
    return rows.sort((a, b) => Date.parse(b.point.timestamp) - Date.parse(a.point.timestamp));
  }
  _historyWindow(rows = this._historyRows()) {
    const cutoff = Date.now() - 48 * 3600000;
    const keep = (row) => row.current || row.draft || row.deleted || (row.index >= 0 && row.index === this._editIndex);
    const frontier = this._haHistory?.hasMore ? Date.parse(this._haHistory.rows.at(-1)?.timestamp) : NaN;
    const available = rows.filter((row) => keep(row) || row.source === "ha" || !Number.isFinite(frontier) || Date.parse(row.point.timestamp) >= frontier);
    const older = available.filter((row) => !keep(row) && Date.parse(row.point.timestamp) < cutoff);
    const shown = new Set(older.slice(0, this._historyOlderCount));
    return { rows: available.filter((row) => keep(row) || Date.parse(row.point.timestamp) >= cutoff || shown.has(row)),
      hidden: Math.max(0, older.length - this._historyOlderCount) + rows.length - available.length };
  }
  _changeHistoryRange(recent) {
    if (this._busy || this._scenarios?.busy) return;
    if (recent) { this._historyOlderCount = 0; this._refreshHaHistory(); }
    else {
      const recentHa = this._haHistory?.hasMore && Date.parse(this._haHistory.rows.at(-1)?.timestamp) >= Date.now() - 48 * 3600000;
      if (!recentHa) this._historyOlderCount += 100;
      if (this._haHistory?.hasMore) this._loadHaHistory();
    }
    this._refreshHistory();
    this._drawPerson();
  }
  _syncCurrentDrafts() {
    const person = this._person();
    const times = this._currentDrafts[this._selected] || [];
    const drafts = person.history.filter((point) => times.includes(point.timestamp));
    const saved = this._savedSnapshot?.persons[this._selected];
    const removed = saved && this._removedCurrent[this._selected] === historyKey(saved.current);
    if (!drafts.length && (!saved || removed) && person.history.length) {
      // A new person or a removed baseline needs a remaining historical point.
      drafts.push(person.history.at(-1));
      this._currentDrafts[this._selected] = [drafts[0].timestamp];
      this._message("Pozostałe szkice zachowano. Najnowszy z nich jest teraz bieżącą pozycją tej nowej osoby.", true);
    }
    if (drafts.length) {
      person.current = { ...(drafts.find((point) => point.timestamp === person.current_history_timestamp) || drafts.at(-1)) };
      person.current_history_timestamp = person.current.timestamp;
    } else {
      delete person.current_history_timestamp;
      if (saved && !removed) person.current = { ...saved.current };
      else delete this._snapshot.persons[this._selected];
    }
  }
  _useHistoryCurrent(point) {
    const person = this._person();
    person.current = { ...point };
    person.current_history_timestamp = point.timestamp;
    const times = this._currentDrafts[this._selected] ||= [];
    if (!times.includes(point.timestamp)) times.push(point.timestamp);
  }
  _setHistoryCurrent(entityId, key) {
    if (entityId !== this._selected || !this._placeAllowed()) return;
    if (this._editing || this._editIndex >= 0) {
      this._message("Najpierw zastosuj lub anuluj edycję punktu albo dodaj przygotowany punkt historii.");
      return;
    }
    const row = this._historyRows().find(({ point, source }) => source !== "ha" && templateKey(point) === key);
    if (!row || row.current || row.index < 0) return;
    this._cancelLocation();
    this._useHistoryCurrent(row.point);
    this._changed(); this._refreshPerson();
    this._message("Wybrano bieżącą pozycję z godziną tego punktu. Zapisz wszystkie zmiany, aby zaktualizować mapy. Ręczna zmiana zatrzyma aktywny plan tej osoby.", true);
  }
  _refreshPerson(fit = true) {
    this._cancelLocation();
    const person = this._person();
    const missing = !this._hass.states[this._selected];
    this.shadowRoot.querySelector("#missing-person-notice").hidden = !missing || !person;
    this.shadowRoot.querySelector("#remove-person").disabled = !person;
    for (const field of this.shadowRoot.querySelectorAll("#person-enabled, #current-lat, #current-lon, #set-current, #history-form input, #history-form button")) field.disabled = missing;
    this._refreshSources();
    this._refreshActivity();
    this.shadowRoot.querySelector("#person-nickname").value = this._snapshot.quick_save?.nicknames[this._selected] || "";
    this.shadowRoot.querySelector("#quick-target").checked = this._snapshot.quick_save?.entity_id === this._selected;
    this.shadowRoot.querySelector("#person-nickname").disabled = missing;
    this.shadowRoot.querySelector("#quick-target").disabled = missing;
    for (const prefix of ["current", "history"]) {
      this.shadowRoot.querySelector(`#${prefix}-lat`).value = person?.current.latitude ?? "";
      this.shadowRoot.querySelector(`#${prefix}-lon`).value = person?.current.longitude ?? "";
    }
    this.shadowRoot.querySelector("#current-summary").textContent = person
      ? `${person.current.latitude.toFixed(5)}, ${person.current.longitude.toFixed(5)} · ${new Date(person.current.timestamp).toLocaleString()}${this._currentChanged() ? " · Niezapisane" : ""}`
      : "Wybierz punkt na mapie, aby ustawić pierwszą pozycję.";
    this.shadowRoot.querySelector("#history-time").value = localDateInput();
    this.shadowRoot.querySelector("#history-current").checked = false;
    this._refreshHistory();
    this._drawPerson(fit);
    this._syncScenarios();
    this._ensureHaHistory();
  }
  _refreshActivity() {
    const settings = activitySettings(this._snapshot.activity?.[this._selected]);
    const field = id => this.shadowRoot.querySelector(`#activity-${id}`);
    const missing = !this._hass.states[this._selected];
    field("enabled").checked = settings.enabled;
    field("enabled").disabled = missing;
    field("reset").disabled = !this._snapshot.activity?.[this._selected];
    for (const signal of ["unlock", "walking", "screen", "tilting"]) {
      field(signal).checked = settings.signals.includes(signal);
      field(signal).disabled = missing || !settings.enabled || settings.manual_since !== null;
    }
    field("match").value = settings.match;
    field("match").disabled = missing || !settings.enabled || settings.manual_since !== null;
    field("manual").checked = settings.manual_since !== null;
    field("manual").disabled = missing || !settings.enabled;
    field("since").value = settings.manual_since && Number.isFinite(Date.parse(settings.manual_since)) ? localDateInput(settings.manual_since) : "";
    field("since").disabled = missing || !settings.enabled || settings.manual_since === null;
  }
  _changeActivity() {
    if (!this._hass.states[this._selected] || this._busy || this._scenarios?.busy) return;
    const field = id => this.shadowRoot.querySelector(`#activity-${id}`);
    const localTime = field("since").value;
    const time = localTime && Number.isFinite(new Date(localTime).getTime()) ? new Date(localTime).toISOString() : "";
    (this._snapshot.activity ||= {})[this._selected] = { enabled: field("enabled").checked,
      signals: ["unlock", "walking", "screen", "tilting"].filter(signal => field(signal).checked), match: field("match").value,
      manual_since: field("manual").checked ? time : null };
    // Preserve partial datetime input while editing; switching people restores its validated draft.
    const blocked = !field("enabled").checked || field("manual").checked;
    for (const id of ["unlock", "walking", "screen", "tilting", "match"]) field(id).disabled = blocked;
    field("manual").disabled = !field("enabled").checked;
    field("since").disabled = !field("enabled").checked || !field("manual").checked;
    field("reset").disabled = false;
    this._changed();
  }
  _cancelLocation(announce = true) {
    this._placeSearch?.cancel?.();
    if (this._locationRequest) this._message("Pobieranie lokalizacji anulowane.");
    if (announce && this._locationRequest?.saveImmediately) this._toast("Pobieranie GPS anulowane. Nic nie zapisano.", "info", 7000, "Aktualizacja anulowana.");
    clearTimeout(this._locationRequest?.timer);
    this._locationRequest = null;
    this._deviceLocation?.remove();
    this._deviceLocation = null;
    this._syncLocationButtons();
  }
  _syncLocationButtons() {
    const blocked = !this._placeAllowed() || !!this._locationRequest;
    for (const button of [this.shadowRoot.querySelector("#use-device-location"), this.shadowRoot.querySelector("#use-device-location-save"), this._locateButton]) {
      if (!button) continue;
      button.disabled = blocked || (button === this.shadowRoot.querySelector("#use-device-location-save") && this._hasDrafts());
      if (this._locationRequest) button.setAttribute("aria-busy", "true");
      else button.removeAttribute("aria-busy");
    }
  }
  _locate(saveImmediately = false) {
    if (!this._placeAllowed() || this._locationRequest) return;
    const notify = (message, good = false) => { this._message(message, good); if (saveImmediately) this._toast(message, good ? "success" : "error", 7000,
      good ? "Zaktualizowano." : "Nie udało się wykonać aktualizacji. Sprawdź stan w ustawieniach."); };
    if (saveImmediately && this._hasDrafts()) { notify("Najpierw zapisz lub odrzuć pozostałe zmiany."); return; }
    this._cancelLocation();
    if (!window.isSecureContext) { notify("Lokalizacja urządzenia wymaga otwarcia Home Assistanta przez HTTPS."); return; }
    if (!navigator.geolocation) { notify("Ta przeglądarka nie udostępnia lokalizacji. Wybierz punkt na mapie lub wpisz współrzędne."); return; }
    const request = { epoch: this._epoch, person: this._selected, token: this._token,
      snapshot: this._snapshot, revision: this._snapshot.revision, saveImmediately };
    this._locationRequest = request;
    this._syncLocationButtons();
    this._message("Pobieranie lokalizacji urządzenia… Jeśli pojawi się pytanie przeglądarki, zezwól na dostęp.");
    if (saveImmediately) this._toast("Pobieranie GPS…", "info", 0, "Przetwarzanie…");
    const finish = async (position, error) => {
      if (this._locationRequest !== request) return;
      this._cancelLocation(false);
      if (!this.isConnected || request.epoch !== this._epoch || request.person !== this._selected
        || !this._token || !this._hass.user?.is_admin || this._hass.connected === false || !this._hass.states[request.person]) return;
      if (saveImmediately && (request.token !== this._token || request.snapshot !== this._snapshot
        || request.revision !== this._snapshot.revision || !this._placeAllowed() || this._hasDrafts())) {
        notify("Dane lub edycja zmieniły się podczas pobierania GPS. Nic nie zapisano. Spróbuj ponownie."); return;
      }
      if (error) {
        const messages = {
          1: "Brak zgody na lokalizację. Zezwól na dostęp dla tej strony i sprawdź uprawnienia lokalizacji przeglądarki lub aplikacji w systemie.",
          2: "Urządzenie nie mogło ustalić położenia. Sprawdź, czy lokalizacja jest włączona, lub wybierz punkt na mapie.",
          3: "Nie udało się pobrać lokalizacji w wyznaczonym czasie. Spróbuj ponownie lub wybierz punkt na mapie.",
        };
        notify(messages[error.code] || "Nie udało się pobrać lokalizacji. Wybierz punkt na mapie lub spróbuj ponownie.");
        return;
      }
      const { latitude, longitude, accuracy } = position?.coords || {};
      if (!Number.isFinite(latitude) || Math.abs(latitude) > 90 || !Number.isFinite(longitude) || Math.abs(longitude) > 180
        || !Number.isFinite(accuracy) || accuracy < 0) { notify("Urządzenie zwróciło nieprawidłową lokalizację. Spróbuj ponownie lub wybierz punkt na mapie."); return; }
      if (saveImmediately) {
        let persons;
        try {
          persons = this._devicePersons(this._snapshot, request.person, position.coords);
        } catch (error) { notify(error.message); return; }
        this._toast("Zapisywanie pozycji GPS…", "info", 0, "Przetwarzanie…");
        let saved = false;
        try { saved = await this._save(persons); } catch { /* A lost confirmation must not be presented as a rollback. */ }
        if (this.isConnected) {
          const message = saved ? "Zapisano pozycję GPS i punkt historii."
            : "Nie potwierdzono zapisu. Wczytaj zapisane dane przed ponowną próbą.";
          const lockedMessage = saved ? "Zaktualizowano."
            : "Nie potwierdzono aktualizacji. Sprawdź stan w ustawieniach przed ponowną próbą.";
          this._message(this._token ? message : lockedMessage, saved);
          this._toast(message, saved ? "success" : "error", 7000, lockedMessage);
        }
        return;
      }
      this.shadowRoot.querySelector("#current-lat").value = latitude.toFixed(6);
      this.shadowRoot.querySelector("#current-lon").value = longitude.toFixed(6);
      const label = `Lokalizacja tego urządzenia · dokładność około ${Math.ceil(accuracy)} m`;
      this._deviceLocation = L.layerGroup().addTo(this._map);
      const area = L.circle([latitude, longitude], { radius: accuracy, color: "#1976d2", weight: 1, fillOpacity: 0.12 }).addTo(this._deviceLocation);
      L.circleMarker([latitude, longitude], { radius: 7, color: "white", weight: 2, fillColor: "#1976d2", fillOpacity: 1 })
        .bindTooltip(label, { permanent: true, direction: "top", className: "device-location-tooltip" }).addTo(this._deviceLocation);
      this._map.fitBounds(area.getBounds(), { maxZoom: 16, padding: [30, 30] });
      this._message(`${label}. Sprawdź punkt, kliknij „Ustaw bieżącą pozycję”, a następnie „Zapisz wszystkie zmiany”.`, true);
    };
    // The browser timeout excludes waiting for permission; keep the UI bounded too.
    request.timer = setTimeout(() => finish(null, { code: 3 }), 30000);
    try {
      navigator.geolocation.getCurrentPosition((position) => finish(position), (error) => finish(null, error),
        { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    } catch { finish(null, {}); }
  }
  _historyTemplateAllowed() {
    return this._placeAllowed() && !this._dirty && !this._editing && this._editIndex < 0 && !Object.keys(this._removedCurrent).length;
  }
  _savedHistoryPoints() {
    const saved = this._savedSnapshot?.persons[this._selected];
    return saved ? [...saved.history, saved.current].filter(Boolean) : [];
  }
  _clearHistorySelection(invalidate = false) {
    this._historySelection.clear();
    if (invalidate) this._historySelectionGeneration++;
    this._syncHistorySelection();
  }
  _syncHistorySelection() {
    const allowed = this._historyTemplateAllowed();
    for (const { input, key, saved } of this._historyChecks) {
      input.checked = this._historySelection.has(key); input.disabled = !saved || !allowed;
    }
    const count = this.shadowRoot.querySelector("#history-selection-count");
    if (count) count.textContent = `Zaznaczono: ${this._historySelection.size}`;
    const create = this.shadowRoot.querySelector("#history-template");
    if (create) create.disabled = !allowed || !this._historySelection.size;
    const clear = this.shadowRoot.querySelector("#history-selection-clear");
    if (clear) clear.disabled = !this._historySelection.size || !!this._busy || !!this._scenarios?.busy;
    const hint = this.shadowRoot.querySelector("#history-template-hint");
    if (hint) hint.textContent = this._dirty || this._editing || this._editIndex >= 0 || Object.keys(this._removedCurrent).length
      ? "Najpierw zapisz lub odrzuć zmiany pozycji i historii, aby wybrać punkty szablonu."
      : "Zaznacz zapisane punkty z jednego dnia. Szablon zachowa ich godziny i sekundy; historię pozostawi bez zmian.";
    this._drawHistorySelection();
  }
  _drawHistorySelection() {
    this._historySelectionLayers?.clearLayers();
    if (!this._map || !this._historySelection.size || !this._token || !this._hass?.user?.is_admin
      || this._hass.connected === false || !this._hass.states[this._selected]) return;
    const saved = new Set(this._savedHistoryPoints().map(templateKey)), places = new Map();
    for (const { point, draft, source } of this._historyRows()) {
      if (source === "ha") continue;
      const key = templateKey(point);
      if (draft || !this._historySelection.has(key) || !saved.has(key)) continue;
      const place = JSON.stringify([point.latitude, point.longitude]);
      if (!places.has(place)) places.set(place, []);
      places.get(place).push(point);
    }
    if (!places.size) return;
    this._historySelectionLayers ||= L.layerGroup().addTo(this._map);
    for (const points of places.values()) {
      points.sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
      const position = [points[0].latitude, points[0].longitude];
      const title = points.length === 1 ? "Zaznaczono" : `Zaznaczono (${points.length})`;
      const label = document.createElement("span");
      label.textContent = `${title} · ${points.slice(0, 3).map((point) => new Date(point.timestamp).toLocaleTimeString()).join(" · ")}${points.length > 3 ? ` · +${points.length - 3} kolejnych (kliknij)` : ""}`;
      const details = document.createElement("div"), heading = document.createElement("strong"), times = document.createElement("div");
      heading.textContent = title; times.className = "history-selection-times";
      for (const point of points) { const time = document.createElement("div"); time.textContent = new Date(point.timestamp).toLocaleString(); times.append(time); }
      details.append(heading, times);
      L.circleMarker(position, { radius: 14, color: "#fff", weight: 3, fill: false, interactive: false }).addTo(this._historySelectionLayers);
      L.circleMarker(position, { radius: 11, color: "#172b4d", weight: 3, fillColor: "#ffd633", fillOpacity: 1, bubblingMouseEvents: false })
        .bindTooltip(label, { permanent: true, direction: "top", offset: [0, -12], className: "history-selection-tooltip" })
        .bindPopup(details).addTo(this._historySelectionLayers);
    }
  }
  _createHistoryTemplate() {
    if (!this._historyTemplateAllowed() || !this._historySelection.size) return false;
    try {
      const baseline = this._savedHistoryPoints(), rows = this._historyRows();
      const points = [...this._historySelection].map((key) => {
        const point = baseline.find((entry) => templateKey(entry) === key);
        if (!point || !rows.some((row) => row.source !== "ha" && !row.draft && templateKey(row.point) === key)) {
          throw new Error("Wybrane punkty zmieniły się. Odśwież historię i zaznacz je ponownie.");
        }
        // Route templates use millisecond instants. Never copy an ambiguous row.
        if (baseline.some((entry) => sameCoordinates(entry, point) && Date.parse(entry.timestamp) === Date.parse(point.timestamp)
          && templateKey(entry) !== key)) {
          throw new Error("Punkty o tych samych współrzędnych różnią się czasem o mniej niż milisekundę. Wybierz inne punkty szablonu.");
        }
        return { latitude: point.latitude, longitude: point.longitude, timestamp: point.timestamp };
      });
      if (!this._scenarios?.createHistoryTemplate(this._selected, points)) return false;
      this._clearHistorySelection();
      this._scenarios.scrollIntoView?.({ block: "start", behavior: "smooth" });
      this._message("Utworzono niezależny szkic „Szablon z historii”. Ustaw dzień i godzinę startu przed ponownym użyciem, potem wybierz „Zapisz plan” i osobno „Start”.", true);
      return true;
    } catch (error) { this._message(error.message || errorText(error)); return false; }
  }
  _refreshHistory(focusPoint = null) {
    this._historySelectionGeneration++;
    const live = this._historyRows(), saved = new Set(this._savedHistoryPoints().map(templateKey));
    const liveKeys = new Set(live.filter((row) => row.source !== "ha").map((row) => deletedPointKey(row.point)));
    const removed = (this._deletedHistory[this._selected] || []).filter((point) => !liveKeys.has(deletedPointKey(point)));
    const window = this._historyWindow([...live, ...removed.map((point) => ({ point, index: -1, source: "custom", deleted: true }))]
      .sort((a, b) => Date.parse(b.point.timestamp) - Date.parse(a.point.timestamp)));
    const history = window.rows;
    const eligible = new Set(history.filter((row) => row.source !== "ha" && !row.draft && !row.deleted && saved.has(templateKey(row.point))).map((row) => templateKey(row.point)));
    for (const key of this._historySelection) if (!eligible.has(key)) this._historySelection.delete(key);
    this._historyChecks = [];
    const haEligible = new Set(history.filter((row) => row.source === "ha").map((row) => haKey(row.ha)));
    for (const key of this._haSelection) if (!haEligible.has(key)) this._haSelection.delete(key);
    this._haChecks = [];
    this.shadowRoot.querySelector("#count").textContent = `${history.length - removed.length} z ${live.length} punktów${removed.length ? ` · ${removed.length} do usunięcia` : ""}`;
    this.shadowRoot.querySelector("#history-range").textContent = this._historyOlderCount
      ? "Ostatnie 48 godzin i odsłonięte starsze wpisy. Bieżąca pozycja i niezapisane zmiany są zawsze widoczne."
      : "Ostatnie 48 godzin. Bieżąca pozycja i niezapisane zmiany są zawsze widoczne.";
    const more = this.shadowRoot.querySelector("#history-more");
    more.hidden = !window.hidden && !this._haHistory?.hasMore;
    const recentHa = this._haHistory?.hasMore && Date.parse(this._haHistory.rows.at(-1)?.timestamp) >= Date.now() - 48 * 3600000;
    more.textContent = recentHa ? "Pokaż kolejne wpisy HA" : this._haHistory?.hasMore ? "Pokaż starsze (kolejna partia)" : `Pokaż starsze (${Math.min(100, window.hidden)})`;
    more.disabled = !!(this._haHistory?.loading || this._haHistory?.error);
    this.shadowRoot.querySelector("#history-recent").hidden = !this._historyOlderCount;
    this._historyWindowMinute = Math.floor(Date.now() / 60000);
    this.shadowRoot.querySelector("#add-history").textContent = this._editIndex < 0 ? "Dodaj punkt" : "Zastosuj edycję";
    this.shadowRoot.querySelector("#cancel-edit").hidden = this._editIndex < 0;
    const list = this.shadowRoot.querySelector("#history-list");
    const form = this.shadowRoot.querySelector("#history-form"), home = this.shadowRoot.querySelector("#history-editor-home");
    const editing = this._editIndex >= 0 && live.some((row) => row.index === this._editIndex);
    if (editing) {
      if (form.parentElement !== list) list.append(form);
      for (const child of [...list.children]) if (child !== form) child.remove();
    } else { if (form.parentElement !== home) home.append(form); list.replaceChildren(); }
    list.classList.toggle("editing", editing);
    form.classList.toggle("editing", editing);
    this.shadowRoot.querySelector("#history-edit-title").hidden = !editing;
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = live.length ? "Brak punktów w ostatnich 48 godzinach. Wybierz Pokaż starsze, aby zobaczyć wcześniejsze wpisy." : "Brak ręcznych punktów tej osoby.";
      list.append(empty);
    }
    this._previewEdit();
    this._saveState();
    const entityId = this._selected, generation = this._historySelectionGeneration;
    let afterEditor = false;
    history.forEach(({ point, index, draft, current, deleted, source, ha }) => {
      if (source === "ha") { this._appendHaHistoryRow(ha, list, editing && !afterEditor ? form : null); return; }
      const row = document.createElement("div");
      const pendingCurrent = current && this._currentChanged();
      row.className = deleted ? "row deleted" : draft ? "row draft" : "row custom";
      const info = document.createElement("div");
      const time = document.createElement("time");
      time.dateTime = point.timestamp;
      time.textContent = new Date(point.timestamp).toLocaleString();
      const coords = document.createElement("small");
      coords.textContent = `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
      info.append(time, coords);
      if (deleted) {
        const badge = document.createElement("span"); badge.className = "deleted-badge";
        badge.textContent = "Do usunięcia po zapisie"; info.append(badge); row.append(info);
        if (editing && !afterEditor) list.insertBefore(row, form); else list.append(row);
        return;
      }
      if (draft || current) {
        const badge = document.createElement("span");
        badge.className = draft || pendingCurrent ? "draft-badge" : "current-badge";
        badge.textContent = draft || pendingCurrent ? current ? "Niezapisane · bieżąca pozycja" : "Niezapisane" : "Bieżąca pozycja";
        info.append(badge);
      }
      const sourceBadge = document.createElement("span"); sourceBadge.className = "source-badge";
      sourceBadge.textContent = "Custom"; info.append(sourceBadge);
      const actions = document.createElement("div");
      actions.className = "actions";
      const edit = document.createElement("button");
      edit.disabled = !this._hass.states[this._selected];
      edit.textContent = "Edytuj";
      edit.setAttribute("aria-label", `Edytuj punkt ${time.textContent}`);
      edit.addEventListener("click", () => {
        this._cancelLocation();
        this._editIndex = index;
        this.shadowRoot.querySelector("#history-current").checked = false;
        this.shadowRoot.querySelector("#history-lat").value = point.latitude;
        this.shadowRoot.querySelector("#history-lon").value = point.longitude;
        this.shadowRoot.querySelector("#history-time").value = localDateInput(point.timestamp);
        this._refreshHistory();
        this._previewEdit(true);
        form.scrollIntoView?.({ block: "center", behavior: "smooth" });
        this.shadowRoot.querySelector("#history-time").focus?.({ preventScroll: true });
      });
      const remove = document.createElement("button");
      remove.disabled = !this._hass.states[this._selected];
      remove.className = "danger trash";
      remove.title = "Usuń punkt";
      remove.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      remove.setAttribute("aria-label", `Usuń punkt ${time.textContent}`);
      remove.addEventListener("click", () => this._removePoint(entityId, historyKey(point)));
      if (index >= 0) {
        const setCurrent = document.createElement("button");
        setCurrent.type = "button";
        setCurrent.textContent = "Ustaw bieżącą";
        setCurrent.disabled = current || !this._hass.states[this._selected] || editing;
        setCurrent.setAttribute("aria-label", `Ustaw jako bieżącą pozycję punkt ${time.textContent}`);
        setCurrent.addEventListener("click", () => {
          if (generation === this._historySelectionGeneration) this._setHistoryCurrent(entityId, templateKey(point));
        });
        actions.append(edit, setCurrent);
      }
      const key = templateKey(point), select = document.createElement("input");
      select.type = "checkbox"; select.className = "history-select"; select.id = `history-select-${this._historyChecks.length}`;
      select.setAttribute("aria-label", `Wybierz punkt ${time.textContent} do szablonu trasy`);
      select.title = eligible.has(key) ? "Wybierz do szablonu trasy" : "Najpierw zapisz punkt historii";
      select.addEventListener("change", () => {
        if (entityId !== this._selected || generation !== this._historySelectionGeneration || !this._historyTemplateAllowed()
          || !this._savedHistoryPoints().some((entry) => templateKey(entry) === key)
          || !this._historyRows().some((entry) => entry.source !== "ha" && !entry.draft && templateKey(entry.point) === key)) return;
        if (select.checked) this._historySelection.add(key); else this._historySelection.delete(key);
        this._syncHistorySelection();
      });
      this._historyChecks.push({ input: select, key, saved: eligible.has(key) });
      actions.append(remove); row.append(info, actions, select);
      if (editing && !afterEditor) list.insertBefore(row, form); else list.append(row);
      if (editing && index === this._editIndex) afterEditor = true;
      if (point === focusPoint) list.scrollTop = row.offsetTop;
    });
    this._syncHistorySelection();
    this._syncHaHistory();
  }
  _appendHaHistoryRow(entry, list, before = null) {
    const request = this._haContext(), generation = this._historySelectionGeneration;
    const row = document.createElement("div"); row.className = "row ha";
    const info = document.createElement("div"), time = document.createElement("time"), coords = document.createElement("small");
    time.dateTime = entry.timestamp; time.textContent = new Date(entry.timestamp).toLocaleString();
    coords.textContent = entry.point ? `${entry.point.latitude.toFixed(5)}, ${entry.point.longitude.toFixed(5)}` : "Brak zapisanych współrzędnych";
    const badge = document.createElement("span"); badge.className = "source-badge";
    badge.textContent = entry.kind === "accepted" ? "Historia HA · odebrany raport" : "Historia HA · zapis encji";
    const entity = document.createElement("small");
    const entityLabel = entry.entity_id.startsWith("person.") ? `${this._hass.states[entry.entity_id]?.attributes.friendly_name || "Wybrana osoba"} · osoba` : "Przypisany telefon";
    entity.textContent = entityLabel;
    info.append(time, coords, badge, entity);
    const actions = document.createElement("div"); actions.className = "actions";
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "danger"; remove.textContent = "Usuń z HA";
    remove.disabled = !this._haOperationAllowed();
    remove.setAttribute("aria-label", `Podgląd usunięcia zapisu HA: ${entityLabel}, ${time.textContent}`);
    const valid = () => this._haContextValid(request) && generation === this._historySelectionGeneration
      && this._haHistory?.rows.some((current) => haKey(current) === haKey(entry));
    remove.addEventListener("click", () => { if (valid()) this._deleteHaHistory([entry]); });
    const select = document.createElement("input"); select.type = "checkbox"; select.className = "history-select";
    select.id = `ha-history-select-${this._haChecks.length}`;
    select.setAttribute("aria-label", `Wybierz zapis HA: ${entityLabel}, ${time.textContent} do usunięcia`);
    select.addEventListener("change", () => {
      if (!valid() || !this._haOperationAllowed()) return;
      const key = haKey(entry);
      if (select.checked && this._haSelection.size >= 100 && !this._haSelection.has(key)) {
        select.checked = false; this._message("Wybierz najwyżej 100 rekordów HA w jednej operacji."); return;
      }
      if (select.checked) this._haSelection.add(key); else this._haSelection.delete(key);
      this._syncHaHistory();
    });
    this._haChecks.push({ input: select, row: entry, remove });
    actions.append(remove); row.append(info, actions, select);
    if (before) list.insertBefore(row, before); else list.append(row);
  }
  _recordDeletedPoints(entityId, points) {
    const deleted = this._deletedHistory[entityId] ||= [];
    for (const point of points) if (!deleted.some((entry) => deletedPointKey(entry) === deletedPointKey(point))) {
      deleted.push({ latitude: point.latitude, longitude: point.longitude, timestamp: point.timestamp });
    }
  }
  _removePoint(entityId, key) {
    if (entityId !== this._selected || this._busy || this._scenarios?.busy || !this._token
      || !this._hass.user?.is_admin || this._hass.connected === false || !this._hass.states[entityId]) return;
    const row = this._historyRows().find(({ point, source }) => source !== "ha" && historyKey(point) === key);
    if (!row) return;
    const person = this._person(), saved = this._savedSnapshot?.persons[entityId];
    const remaining = row.index >= 0 ? removeHistory(person, row.index).history : person.history.slice();
    const times = this._currentDrafts[entityId] || [];
    const remainingTimes = row.index >= 0 ? times.filter((time) => time !== row.point.timestamp) : times;
    const removingBaseline = !!saved && key === historyKey(saved.current);
    const baselineAvailable = saved && !removingBaseline && this._removedCurrent[entityId] !== historyKey(saved.current);
    const undoDraft = row.current && row.index >= 0 && times.includes(row.point.timestamp) && !removingBaseline;
    const next = undoDraft ? remaining.filter((point) => remainingTimes.includes(point.timestamp)).at(-1)
      || (baselineAvailable ? saved.current : null) || remaining.at(-1) : remaining.at(-1);
    // Confirm before changing either points or local undo metadata.
    if (row.current && !next) { this._removePerson(); return; }
    this._cancelLocation();
    this._recordDeletedPoints(entityId, [row.point]);
    person.history = remaining; this._currentDrafts[entityId] = remainingTimes;
    if (removingBaseline) this._removedCurrent[entityId] = key;
    if (row.current && !undoDraft) {
      person.current = { ...next }; person.current_history_timestamp = next.timestamp;
      this._currentDrafts[entityId] = [next.timestamp];
    } else if (person.current_history_timestamp) this._syncCurrentDrafts();
    this._editIndex = -1; this._editing = false;
    this._changed(); this._refreshPerson();
  }
  _readHistoryPoint() {
    const localTime = this.shadowRoot.querySelector("#history-time").value;
    if (!localTime) throw new Error("Wpisz datę i godzinę punktu.");
    const point = readPoint(this.shadowRoot.querySelector("#history-lat").value, this.shadowRoot.querySelector("#history-lon").value, localTime);
    const priorTime = this._person()?.history[this._editIndex]?.timestamp;
    // The editor displays seconds; changing only coordinates must preserve fractions.
    if (priorTime && localTime === localDateInput(priorTime)) point.timestamp = priorTime;
    return point;
  }
  _previewEdit(center = false) {
    this._editMarker?.remove(); this._editMarker = null;
    const editing = this._editIndex >= 0 && !!this._person()?.history[this._editIndex];
    const tools = this.shadowRoot.querySelector("#map-edit-tools");
    if (tools) tools.hidden = !editing;
    if (!editing || !this._map) return;
    const status = this.shadowRoot.querySelector("#map-edit-status");
    try {
      const point = this._readHistoryPoint();
      const position = [point.latitude, point.longitude];
      this._editMarker = L.circleMarker(position, { color: EDIT_COLOR, fillColor: "#ffb347", radius: 11, weight: 3, fillOpacity: 1, interactive: false })
        .bindTooltip(popup("Edytowany punkt", point), { permanent: true, direction: "top", className: "edit-point-tooltip" }).addTo(this._map);
      status.textContent = `Edytujesz punkt: ${new Date(point.timestamp).toLocaleString()}. Kliknij mapę, aby zmienić jego miejsce.`;
      if (center) this._map.setView(position, Math.max(this._map.getZoom(), 14));
    } catch {
      status.textContent = "Uzupełnij poprawne współrzędne oraz datę i godzinę, aby zobaczyć podgląd punktu.";
    }
  }
  _cancelEdit() {
    if (this._busy) return;
    this._placeSearch?.cancel?.();
    this._editIndex = -1;
    const current = this._person()?.current;
    this.shadowRoot.querySelector("#history-lat").value = current?.latitude ?? "";
    this.shadowRoot.querySelector("#history-lon").value = current?.longitude ?? "";
    this.shadowRoot.querySelector("#history-time").value = localDateInput();
    this.shadowRoot.querySelector("#history-current").checked = false;
    this._editing = false;
    this._refreshHistory();
    this._saveState();
    this._message("Edycja punktu anulowana.", true);
    this._drawPerson();
  }
  _drawPerson(fit = false) {
    this._layers.clearLayers();
    const person = this._person();
    if (!person) { this._drawHistorySelection(); this._map.setView([25, 0], 2); return; }
    const current = [person.current.latitude, person.current.longitude];
    const name = this._hass.states[this._selected]?.attributes.friendly_name || this._selected;
    const currentChanged = this._currentChanged();
    L.circleMarker(current, { color: currentChanged ? DRAFT_COLOR : CUSTOM_COLOR, radius: 9, fillOpacity: 1 })
      .bindPopup(popup(currentChanged ? `${name} · Niezapisane` : name, person.current)).addTo(this._layers);
    const rows = this._historyWindow().rows.reverse();
    const path = rows.filter((row) => (row.index >= 0 || row.source === "ha") && hasCoordinates(row.point)).map(({ point }) => [point.latitude, point.longitude]);
    if (currentChanged && !person.current_history_timestamp) path.push(current);
    if (path.length > 1) L.polyline(path, { color: CUSTOM_COLOR, weight: 3 }).addTo(this._layers);
    for (const { point, draft, index, source } of rows) if ((index >= 0 || source === "ha") && hasCoordinates(point)) {
      L.circleMarker([point.latitude, point.longitude], { color: draft ? DRAFT_COLOR : source === "ha" ? HA_COLOR : CUSTOM_COLOR,
        radius: draft ? 6 : 4, fillOpacity: 1 }).bindPopup(popup(draft ? "Custom · Niezapisane" : source === "ha" ? "Historia HA" : "Custom", point)).addTo(this._layers);
    }
    this._drawHistorySelection();
    if (fit) this._map.fitBounds([current, ...path], { padding: [35, 35], maxZoom: 14 });
    this._previewEdit();
  }
  _setCurrent() {
    if (this._busy) return;
    this._cancelLocation();
    try {
      if (!this._hass.states[this._selected]) throw new Error("Najpierw dodaj osobę w Home Assistant.");
      const point = readPoint(this.shadowRoot.querySelector("#current-lat").value, this.shadowRoot.querySelector("#current-lon").value);
      const old = this._person();
      if (sameCoordinates(point, old?.current)) {
        this._message("Pozycja ma te same współrzędne. Aby dodać ją z inną godziną, użyj formularza historii.", true);
        return;
      }
      if (!old && Object.keys(this._snapshot.persons).length >= 20) throw new Error("Osiągnięto limit 20 osób.");
      const person = editHistory(old || { enabled: true, current: point, history: [] }, point);
      person.enabled = old ? this.shadowRoot.querySelector("#person-enabled").checked : true;
      this._snapshot.persons[this._selected] = person;
      this._useHistoryCurrent(point);
      this._syncCurrentDrafts();
      this._editIndex = -1;
      this._editing = false;
      this._changed(); this._refreshPerson(); this._refreshHistory(point);
      this._message("Dodano szkic z godziną. Możesz dodać kolejne, edytować lub usunąć je przed wspólnym zapisem.", true);
    } catch (error) { this._message(error.message); }
  }
  _addHistory() {
    if (this._busy || this._scenarios?.busy || !this._token || !this._hass.user?.is_admin || this._hass.connected === false) return;
    try {
      if (!this._hass.states[this._selected]) throw new Error("Tej osoby nie ma w Home Assistant. Usuń jej ustawienia przed zapisem.");
      if (!this._person()) throw new Error("Najpierw ustaw bieżącą pozycję tej osoby.");
      const point = this._readHistoryPoint();
      const priorTime = this._person().history[this._editIndex]?.timestamp;
      this._snapshot.persons[this._selected] = editHistory(this._person(), point, this._editIndex);
      const currentDrafts = this._currentDrafts[this._selected] || [];
      this._currentDrafts[this._selected] = currentDrafts.map((time) => time === priorTime ? point.timestamp : time);
      const setCurrent = this.shadowRoot.querySelector("#history-current").checked;
      if (setCurrent) this._useHistoryCurrent(point);
      else if (priorTime && this._person().current_history_timestamp === priorTime) this._person().current_history_timestamp = point.timestamp;
      if (this._person().current_history_timestamp) {
        this._syncCurrentDrafts(); this._refreshPerson();
      }
      this.shadowRoot.querySelector("#history-current").checked = false;
      this._editIndex = -1;
      this._editing = false;
      this._changed(); this._refreshHistory(point); this._drawPerson();
      this._message(setCurrent
        ? "Punkt przygotowany także jako bieżąca pozycja z podaną godziną. Zapisz wszystkie zmiany, aby zaktualizować mapy. Ręczna zmiana zatrzyma aktywny plan tej osoby."
        : "Punkt przygotowany. Zapisz wszystkie zmiany, aby zaktualizować karty.", true);
    } catch (error) { this._message(error.message); }
  }

  _removePerson() {
    if (this._busy || !this._person()) return;
    const message = this._gatewayFor(this._selected)
      ? "Usunąć ręczną pozycję i całą historię tej osoby? To działanie nie włączy GPS ani nie usunie ostatniej pozycji z HA."
      : "Usunąć ręczną pozycję i całą historię tej osoby? Po zapisaniu wszystkich zmian karty wrócą do danych HA, jeśli osoba nadal istnieje.";
    if (!window.confirm(message)) return;
    if (this._gatewayNormalRequested === this._selected) this._gatewayNormalRequested = null;
    this._recordDeletedPoints(this._selected, this._historyRows().filter((row) => row.source !== "ha").map((row) => row.point));
    const saved = this._savedSnapshot?.persons[this._selected];
    if (saved) this._removedCurrent[this._selected] = historyKey(saved.current);
    delete this._snapshot.persons[this._selected];
    delete this._currentDrafts[this._selected];
    this._editIndex = -1;
    this._changed();
    this._render();
    this._message("Ustawienia osoby oznaczono do usunięcia. Zapisz wszystkie zmiany, aby potwierdzić usunięcie.", true);
  }

  _syncLockedQuick() {
    const button = this.shadowRoot.querySelector("#quick-update");
    if (!button) return;
    const allowed = this.isConnected && this._hass?.user?.is_admin && this._hass.connected !== false;
    button.disabled = !allowed || !!this._busy || !this._quickInfo?.configured;
    const input = this.shadowRoot.querySelector("#pin");
    if (input) input.disabled = !!this._busy;
    const unlock = this.shadowRoot.querySelector("#unlock-form button[type=submit]");
    if (unlock) unlock.disabled = !!this._busy;
    const nickname = this.shadowRoot.querySelector("#quick-nickname");
    if (nickname) nickname.textContent = this._quickInfo?.configured ? this._quickInfo.nickname : "";
  }
  async _loadQuickInfo() {
    if (!this._hass?.user?.is_admin || this._hass.connected === false) return;
    const epoch = this._epoch, hass = this._hass;
    try {
      const info = await callWS(hass, { type: "manual_location/quick_save_info" });
      if (epoch !== this._epoch || hass.connection !== this._hass.connection || this._token) return;
      this._quickInfo = { configured: info.configured === true && typeof info.nickname === "string" && !!info.nickname.trim(),
        nickname: typeof info.nickname === "string" ? info.nickname : "" };
      this._syncLockedQuick();
    } catch { if (epoch === this._epoch) { this._quickInfo = null; this._syncLockedQuick(); } }
  }
  _devicePersons(snapshot, entityId, coords) {
    const { latitude, longitude, accuracy } = coords || {};
    if (!Number.isFinite(latitude) || Math.abs(latitude) > 90 || !Number.isFinite(longitude) || Math.abs(longitude) > 180
      || !Number.isFinite(accuracy) || accuracy < 0) throw new Error("Urządzenie zwróciło nieprawidłową lokalizację.");
    const point = { latitude: Number(latitude.toFixed(6)), longitude: Number(longitude.toFixed(6)) };
    const persons = structuredClone(snapshot.persons);
    const old = persons[entityId];
    if (!old && Object.keys(persons).length >= 20) throw new Error("Osiągnięto limit 20 osób.");
    if (old?.history.length >= 5000) throw new Error("Osiągnięto limit 5000 punktów.");
    // HA supplies one timestamp for current and history, independently of the device clock.
    persons[entityId] = { enabled: old?.enabled ?? true, current: point, history: old?.history || [], record_current_now: true };
    return persons;
  }
  _confirmQuickUpdate(nickname, request) {
    return new Promise((resolve, reject) => {
      const dialog = document.createElement("dialog");
      dialog.className = "quick-confirm"; dialog.setAttribute("aria-label", "Potwierdź aktualizację");
      Object.assign(dialog.style, { maxWidth: "90vw", width: "400px", boxSizing: "border-box", padding: "22px",
        border: "1px solid #c8d6d4", borderRadius: "12px", color: "var(--primary-text-color,#20343b)",
        background: "var(--card-background-color,#fff)", overflowWrap: "anywhere" });
      const text = document.createElement("p");
      text.textContent = "Zastosować aktualizację dla " + nickname + "?";
      const actions = document.createElement("div"); actions.className = "actions";
      const no = document.createElement("button"); no.id = "quick-confirm-no"; no.type = "button"; no.textContent = "Anuluj";
      const yes = document.createElement("button"); yes.id = "quick-confirm-yes"; yes.type = "button";
      yes.className = "gps-save"; yes.textContent = "Potwierdź";
      let done = false;
      const settle = (accepted) => {
        if (done) return;
        done = true; request.cancelConfirmation = null;
        if (dialog.open) dialog.close();
        dialog.remove(); resolve(accepted);
      };
      request.cancelConfirmation = () => settle(false);
      no.addEventListener("click", () => settle(false)); yes.addEventListener("click", () => settle(true));
      dialog.addEventListener("cancel", (event) => { event.preventDefault(); settle(false); });
      dialog.addEventListener("close", () => settle(false));
      actions.append(no, yes); dialog.append(text, actions); this.shadowRoot.append(dialog);
      try { dialog.showModal(); no.focus?.(); }
      catch (error) { dialog.remove(); request.cancelConfirmation = null; reject(error); }
    });
  }
  _cancelLockedQuick() {
    const request = this._lockedQuick;
    if (!request) return;
    this._lockedQuick = null; this._busy = false; request.cancelConfirmation?.(); request.cancelFix?.(); clearTimeout(request.timer);
    this._syncLockedQuick();
    this._toast(request.sent ? "Nie potwierdzono aktualizacji. Sprawdź stan w ustawieniach przed ponowną próbą."
      : "Aktualizacja anulowana.", request.sent ? "error" : "info");
  }
  async _quickSaveLocked() {
    if (this._busy || this._lockedQuick || this._token || !this.isConnected || !this._hass?.user?.is_admin
      || this._hass.connected === false || !this._quickInfo?.configured) return;
    const input = this.shadowRoot.querySelector("#pin"), pin = input.value;
    input.value = "";
    if (!/^[A-Za-z0-9]{4,12}$/.test(pin)) { this._toast("Wpisz poprawny PIN.", "error"); return; }
    const hass = this._hass, connection = hass.connection, user = hass.user.id;
    const transport = { callWS: typeof connection?.sendMessagePromise === "function"
      ? (message) => connection.sendMessagePromise(message) : hass.callWS.bind(hass) };
    const request = { epoch: this._epoch, sent: false };
    this._lockedQuick = request; this._busy = true; this._syncLockedQuick();
    const valid = () => this._lockedQuick === request && request.epoch === this._epoch && this.isConnected
      && this._hass?.connection === connection && this._hass.user?.id === user && this._hass.user?.is_admin
      && this._hass.connected !== false && !this._token;
    let session;
    try {
      this._toast("Sprawdzanie…", "info", 0);
      session = await callWS(transport, { type: "manual_location/unlock", pin });
      if (!valid()) return;
      const expiresAt = Date.now() + Number(session.expires_in) * 1000;
      const snapshot = await callWS(transport, { type: "manual_location/admin_get", token: session.token });
      if (!valid()) return;
      const entityId = snapshot.quick_save?.entity_id, nickname = snapshot.quick_save?.nicknames[entityId];
      if (!entityId || typeof nickname !== "string" || !nickname.trim() || !hass.states[entityId]) throw { code: "invalid_input" };
      if (!await this._confirmQuickUpdate(nickname, request)) { if (valid()) this._toast("Aktualizacja anulowana."); return; }
      if (!valid()) return;
      if (!Number.isFinite(expiresAt) || Date.now() >= expiresAt) throw { code: "invalid_token" };
      if (!window.isSecureContext || !navigator.geolocation) throw { code: "device_unavailable" };
      this._toast("Przetwarzanie…", "info", 0);
      let position;
      try {
        position = await new Promise((resolve, reject) => {
          request.cancelFix = () => reject({ code: "cancelled" });
          request.timer = setTimeout(() => reject({ code: "device_timeout" }), 30000);
          navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
        });
      } finally { clearTimeout(request.timer); request.cancelFix = null; }
      if (!valid() || !hass.states[entityId]) return;
      if (Date.now() >= expiresAt) throw { code: "invalid_token" };
      const persons = this._devicePersons(snapshot, entityId, position?.coords);
      request.sent = true;
      await callWS(transport, { type: "manual_location/admin_save", token: session.token, revision: snapshot.revision,
        enabled: snapshot.enabled, persons, quick_save: structuredClone(snapshot.quick_save) });
      if (valid()) { this._selected = entityId; this._toast("Zaktualizowano.", "success"); }
    } catch (error) {
      if (valid()) this._toast(request.sent
        ? "Nie potwierdzono aktualizacji. Sprawdź stan w ustawieniach przed ponowną próbą."
        : ["invalid_pin", "pin_locked", "rate_limited", "unauthorized", "forbidden"].includes(error?.code)
          ? errorText(error) : "Nie udało się wykonać aktualizacji. Sprawdź dostęp i spróbuj ponownie.", "error");
    } finally {
      clearTimeout(request.timer);
      if (session?.token) { try { await callWS(transport, { type: "manual_location/lock", token: session.token }); } catch { /* The short-lived token is never kept by this screen. */ } }
      if (this._lockedQuick === request) { this._lockedQuick = null; this._busy = false; this._syncLockedQuick(); }
    }
  }
  async _unlock() {
    if (this._busy || !this._hass?.user?.is_admin) return;
    const input = this.shadowRoot.querySelector("#pin");
    const pin = input.value;
    input.value = "";
    const epoch = this._epoch;
    this._busy = true;
    this._syncLockedQuick();
    this._message("Odblokowywanie…");
    try {
      const session = await callWS(this._hass, { type: "manual_location/unlock", pin });
      if (epoch !== this._epoch) return;
      const snapshot = await callWS(this._hass, { type: "manual_location/admin_get", token: session.token });
      if (epoch !== this._epoch) return;
      const gateway = await callWS(this._hass, { type: "manual_location/gateway_status", token: session.token });
      if (epoch !== this._epoch) return;
      this._token = session.token;
      this._gatewayStatus = gateway;
      this._snapshot = snapshot;
      this._savedSnapshot = structuredClone(snapshot);
      this._sourceSnapshot = this._savedSnapshot;
      const retained = this._retained?.owner === this._hass.user.id ? this._retained : null;
      const restoreParent = retained && (retained.dirty || retained.editing || retained.editIndex >= 0 || Object.keys(retained.removedCurrent || {}).length);
      if (restoreParent) {
        this._snapshot = retained.snapshot; this._savedSnapshot = retained.saved;
        this._currentDrafts = retained.currentDrafts; this._removedCurrent = retained.removedCurrent || {};
        this._deletedHistory = retained.deletedHistory || {};
        this._dirty = retained.dirty; this._editing = retained.editing; this._editIndex = retained.editIndex;
        if (gateway?.enabled && gateway.person_id && retained.gatewayNormalRequested === gateway.person_id && this._snapshot.enabled
          && !this._snapshot.persons[gateway.person_id]?.enabled) this._gatewayNormalRequested = gateway.person_id;
      } else { this._dirty = false; this._editing = false; this._editIndex = -1; }
      this._retained = null;
      this._expiry = setTimeout(() => this._clearSession("Sesja wygasła. Odblokuj panel ponownie; szkice zachowano w tej karcie.", true), Math.max(1, session.expires_in - 2) * 1000);
      this._render();
      if (restoreParent) {
        for (const [id, value] of Object.entries(retained.fields)) this.shadowRoot.querySelector(`#${id}`).value = value;
        this.shadowRoot.querySelector("#history-current").checked = !!retained.historyCurrent;
        this._previewEdit();
      }
      if (retained && this._hasDrafts()) this._message(snapshot.revision !== this._snapshot.revision
        ? "Szkice przywrócono. Dane HA zmieniły się podczas blokady; zapis może wymagać ponownego wczytania danych."
        : "Szkice przywrócono. Możesz kontynuować edycję.", true);
      this._schedulePoll();
    } catch (error) {
      if (epoch === this._epoch) {
        if (["unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
        else this._message(errorText(error));
      }
    }
    finally { if (epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _admin(type, extra = {}) {
    if (this._busy || this._scenarios?.busy || !this._token) return null;
    this._cancelLocation();
    const epoch = this._epoch;
    this._busy = true;
    this._saveState();
    try {
      const data = await callWS(this._hass, { type: `manual_location/${type}`, token: this._token, ...extra });
      return epoch === this._epoch ? { data } : null;
    } catch (error) {
      if (epoch === this._epoch) {
        if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._sessionError(errorText(error), error.code);
        else this._message(errorText(error));
      }
      return null;
    } finally { if (epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _save(persons = this._snapshot.persons) {
    if (this._editIndex >= 0) { this._message("Najpierw zastosuj albo anuluj edycję punktu."); return false; }
    let activity;
    try { if (this._snapshot.activity !== undefined) activity = activityPayload(this._snapshot.activity); }
    catch (error) { this._message(error.message); return false; }
    this._gatewayRequest++;
    const normal = this._gatewayNormalRequested && this._gatewayFor(this._gatewayNormalRequested) && this._snapshot.enabled
      && !persons[this._gatewayNormalRequested]?.enabled && !!this._hass.states[this._gatewayNormalRequested];
    const result = await this._admin("admin_save", { revision: this._snapshot.revision, enabled: this._snapshot.enabled, persons: structuredClone(persons),
      ...(normal ? { gateway_mode: "normal" } : {}),
      ...(this._snapshot.quick_save ? { quick_save: structuredClone(this._snapshot.quick_save) } : {}),
      ...(activity !== undefined ? { activity } : {}) });
    if (!result) { if (this._gatewayStatus?.enabled) await this._pollGateway(); return false; }
    this._snapshot = result.data;
    this._savedSnapshot = structuredClone(result.data);
    this._sourceSnapshot = this._savedSnapshot;
    this._gatewayNormalRequested = null;
    this._resetHaHistory();
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._deletedHistory = {};
    this._dirty = false;
    this._editing = false;
    this._editIndex = -1;
    if (this._gatewayStatus?.enabled) await this._pollGateway(true);
    if (!this._token || !this._snapshot) return false;
    this._refreshPerson(); this._saveState();
    const gateway = this._gatewayStatus;
    let message = "Zapisano. Wszystkie karty odczytają zmiany w ciągu 5 sekund.";
    if (gateway?.enabled) {
      if (gateway.available !== true) message = "Ustawienia zapisane. Nie potwierdzono stanu bramki. Sprawdź połączenie.";
      else if (gateway.pending || gateway.replay_required || gateway.upstream_uncertain || gateway.recovery_retryable || gateway.mode === "blocked") message = gateway.gps_blocked === true
        ? "Ustawienia zapisane. GPS pozostaje zablokowany; trwa odzyskiwanie własnej pozycji."
        : "Ustawienia zapisane. Trwa odzyskiwanie; nie potwierdzono blokady GPS.";
      else if (["custom", "warmup"].includes(gateway.mode) && gateway.gps_blocked !== true) message = "Ustawienia zapisane. Nie potwierdzono blokady GPS.";
      else if (gateway.mode === "warmup") message = "Ustawienia zapisane. Trwa 5-minutowa karencja; raporty GPS pozostają zablokowane.";
      else if (gateway.mode === "custom") message = gateway.has_custom_point
        ? "Ustawienia zapisane. Własna pozycja potwierdzona w HA; GPS pozostaje zablokowany."
        : "Ustawienia zapisane. GPS zablokowany. Nie zapisano jeszcze własnego punktu; pozostaje ostatnia pozycja HA.";
      else message = "Ustawienia zapisane. GPS dopuszczony; oczekiwanie na kolejne raporty Companion.";
    }
    this._message(message, gateway?.enabled !== true || gateway.available === true);
    return true;
  }
  async _reload() {
    if (this._hasDrafts() && !window.confirm("Wczytać zapisane dane? Niezapisane pozycje, historia i szkice planów w tej karcie zostaną odrzucone.")) return;
    const result = await this._admin("admin_get");
    if (!result) return;
    this._snapshot = result.data;
    this._savedSnapshot = structuredClone(result.data);
    this._sourceSnapshot = this._savedSnapshot;
    this._gatewayNormalRequested = null;
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._deletedHistory = {};
    this._scenarioDrafts = {};
    this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    this._dirty = false; this._editing = false; this._editIndex = -1;
    this._historyOlderCount = 0;
    this._resetHaHistory();
    this._clearHistorySelection(true);
    if (this._gatewayStatus?.enabled) await this._pollGateway(true);
    if (!this._token || !this._snapshot) return;
    this._render();
    this._message("Wczytano zapisane dane.", true);
  }
  async _lock() {
    if (this._busy || this._scenarios?.busy) return;
    if (this._hasDrafts() && !window.confirm("Zablokować panel i odrzucić niezapisane pozycje, historię oraz szkice planów? Zapisane dane pozostaną w HA.")) return;
    const token = this._token;
    this._clearSession("Panel zablokowany.");
    if (token) { try { await callWS(this._hass, { type: "manual_location/lock", token }); } catch { /* The local token is already forgotten. */ } }
  }
  async _changePin() {
    if (this._hasDrafts() && !window.confirm("Zmiana PIN-u zablokuje panel i odrzuci niezapisane pozycje, historię oraz szkice planów. Kontynuować?")) return;
    const input = this.shadowRoot.querySelector("#new-pin");
    const new_pin = input.value;
    input.value = "";
    const result = await this._admin("change_pin", { new_pin });
    if (result) this._clearSession("PIN zmieniony. Wszystkie sesje zostały zablokowane.");
  }
}

if (!customElements.get("manual-location-admin")) customElements.define("manual-location-admin", ManualLocationAdmin);
