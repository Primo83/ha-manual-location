import { adminPersonOptions, callWS, editHistory, errorText, localDateInput, readPoint, removeHistory } from "./data.js?v=0.1.19";
import { L, makeMap, mapStyles, popup } from "./map-common.js?v=0.1.19";
import { registerReloadBlocker, startUpdateCheck } from "./updates.js?v=0.1.19";
import "./scenarios.js?v=0.1.19";
import "./place-search.js?v=0.1.19";

const DRAFT_COLOR = "#7c3aed";
const EDIT_COLOR = "#c65d00";
const sameCoordinates = (a, b) => !!a && !!b && a.latitude === b.latitude && a.longitude === b.longitude;
const templateKey = (point) => JSON.stringify([point.timestamp, point.latitude, point.longitude]);
const historyKey = (point) => `${Date.parse(point.timestamp)}:${point.latitude}:${point.longitude}`;

export class ManualLocationAdmin extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._epoch = 0;
    this._selected = "";
    this._editIndex = -1;
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._historySelection = new Set();
    this._historySelectionGeneration = 0;
    this._historyChecks = [];
    this._scenarioDrafts = {};
    this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    const markEditing = (event) => {
      if (["manual-location-scenarios", "manual-location-place-search"].includes(event.target?.tagName?.toLowerCase())) return;
      if (["person", "pin", "new-pin"].includes(event.target?.id) || event.target?.id?.startsWith("history-select-")) return;
      this._editing = true; this._syncScenarios(); this._placeSearch?.sync?.();
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
    this._unbind(); this._clearSession(); this._destroyMap();
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
    preserve = preserve && !!this._hass?.user?.id && !!this._hass.user.is_admin;
    if (preserve && this._snapshot) {
      this._retained = { owner: this._hass?.user?.id, snapshot: this._snapshot, saved: this._savedSnapshot,
        currentDrafts: this._currentDrafts, removedCurrent: this._removedCurrent, dirty: this._dirty, editing: this._editing, editIndex: this._editIndex,
        fields: Object.fromEntries(["current-lat", "current-lon", "history-lat", "history-lon", "history-time"].map((id) => [id, this.shadowRoot.querySelector(`#${id}`)?.value ?? ""])) };
    } else if (!preserve) {
      this._retained = null; this._scenarioDrafts = {};
      this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    }
    this._cancelLocation();
    this._epoch++;
    this._clearHistorySelection(true);
    this._token = null;
    this._snapshot = null;
    this._savedSnapshot = null;
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._editIndex = -1;
    this._busy = false;
    this._dirty = false;
    this._editing = false;
    this._scenarios = null;
    clearTimeout(this._expiry);
    clearTimeout(this._pollTimer); this._pollTimer = null;
    if (this.isConnected) { this._render(); this._message(message); }
  }
  _destroyMap() { this._editMarker?.remove(); this._editMarker = null; this._cancelLocation(); this._resize?.disconnect(); this._map?.remove(); this._map = null; this._locateButton = null; }
  _message(text, good = false) {
    const box = this.shadowRoot.querySelector("#message");
    if (box) { box.textContent = text; box.classList.toggle("good", good); box.hidden = !text; }
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
      onExpired: (message) => this._clearSession(`${message} Szkice zachowano w tej karcie.`, true),
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
  _hasDrafts() { return !!(this._dirty || this._editing || this._editIndex >= 0
    || Object.keys(this._removedCurrent).length || Object.keys(this._retained?.removedCurrent || {}).length
    || this._retained?.dirty || this._retained?.editing || this._retained?.editIndex >= 0
    || Object.values(this._scenarioDrafts).some((draft) => draft.dirty || draft.editing)); }
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
      onExpired: (text) => this._clearSession(`${text} Szkice zachowano w tej karcie.`, true),
    };
  }
  _acceptScenarioSnapshot(snapshot) {
    if (!this._token || this._dirty || this._editing || this._editIndex >= 0 || Object.keys(this._removedCurrent).length) return false;
    this._snapshot = snapshot; this._savedSnapshot = structuredClone(snapshot);
    const global = this.shadowRoot.querySelector("#global-enabled"); if (global) global.checked = snapshot.enabled;
    this._refreshPerson(false); this._saveState(); return true;
  }
  _schedulePoll() {
    clearTimeout(this._pollTimer);
    if (!this._token || !this.isConnected) return;
    this._pollTimer = setTimeout(async () => { await this._poll(); this._schedulePoll(); }, 5000);
  }
  async _poll() {
    if (!this._token || this._busy || this._scenarios?.busy || this._hasDrafts()) return;
    const token = this._token, epoch = this._epoch;
    try {
      const snapshot = await callWS(this._hass, { type: "manual_location/admin_get", token });
      if (epoch === this._epoch && token === this._token && !this._busy && !this._scenarios?.busy && !this._hasDrafts()
        && (snapshot.revision !== this._snapshot.revision || JSON.stringify(snapshot.scenarios) !== JSON.stringify(this._snapshot.scenarios))) this._acceptScenarioSnapshot(snapshot);
    } catch (error) {
      if (epoch !== this._epoch || token !== this._token) return;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._clearSession(errorText(error), true);
      else this._message(errorText(error));
    }
  }
  _saveState() {
    const status = this.shadowRoot.querySelector("#save-state");
    if (status) status.textContent = this._editIndex >= 0 ? "Zastosuj albo anuluj edycję punktu przed zapisem" : this._dirty ? "Masz niezapisane zmiany" : `Zapisane · rewizja ${this._snapshot?.revision}`;
    const save = this.shadowRoot.querySelector("#save");
    if (save) save.disabled = this._busy || this._scenarios?.busy || !this._dirty || this._editIndex >= 0;
    const layout = this.shadowRoot.querySelector(".layout");
    if (layout) layout.inert = !!(this._busy || this._scenarios?.busy);
    const reload = this.shadowRoot.querySelector("#reload");
    if (reload) reload.disabled = !!(this._busy || this._scenarios?.busy);
    const pinChange = this.shadowRoot.querySelector(".pin-change");
    if (pinChange) pinChange.inert = !!(this._busy || this._scenarios?.busy);
    this._syncScenarios();
    this._syncPlaceSearch();
    this._syncHistorySelection();
  }
  _selectPerson(entityId) {
    const select = this.shadowRoot.querySelector("#person");
    if (this._scenarios?.busy || this._busy) { select.value = this._selected; return false; }
    if (entityId === this._selected) return true;
    if ((this._editing || this._editIndex >= 0) && !window.confirm("Zmienić osobę? Niezastosowane pola bieżącej pozycji i historii zostaną odrzucone. Przygotowane pozycje, historia i szkice planów pozostaną.")) { select.value = this._selected; return false; }
    this._clearHistorySelection(true);
    this._selected = entityId; select.value = entityId;
    this._editIndex = -1; this._editing = false; this._refreshPerson(); this._saveState();
    return true;
  }

  _render() {
    this._destroyMap();
    const admin = this._hass?.user?.is_admin;
    const unlocked = admin && this._token && this._snapshot;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;color:var(--primary-text-color,#20343b);background:var(--primary-background-color,#f2f5f4);font-family:var(--paper-font-body1_-_font-family,"Segoe UI",sans-serif)}*{box-sizing:border-box}main{max-width:1220px;margin:auto;padding:32px 28px 56px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:700;color:var(--secondary-text-color,#657a82);margin-bottom:8px}h1{font-family:Georgia,serif;font-size:32px;font-weight:400;margin:0}h2{font-size:17px;margin:0 0 18px}.sub{color:var(--secondary-text-color,#657a82);line-height:1.6;font-size:14px;margin:10px 0 0}.box{border:1px solid var(--divider-color,#d9e2e0);border-radius:12px;background:var(--card-background-color,#fff);padding:22px}.lockbox{max-width:480px;margin:58px auto}.layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}.sidebar{display:flex;flex-direction:column;gap:18px}.mapbox{padding:0;overflow:hidden}.map{height:380px}.caption{padding:12px 18px;border-top:1px solid var(--divider-color,#d9e2e0);font-size:13px;color:var(--secondary-text-color,#657a82)}label{display:block;font-size:13px;font-weight:600;margin-bottom:7px}input,select,button{font:inherit}input,select{width:100%;min-height:42px;padding:9px 11px;background:var(--input-fill-color,var(--card-background-color,#fff));color:inherit;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}input:focus,select:focus{outline:2px solid #338b9c;outline-offset:1px}.field{margin-bottom:16px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.toggle{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:500;cursor:pointer}.toggle input{width:20px;min-height:20px;accent-color:#176e82}.toggle small{display:block;font-weight:400;color:var(--secondary-text-color,#657a82);margin-top:3px}button{min-height:40px;border-radius:7px;padding:9px 14px;cursor:pointer;background:transparent;color:inherit;border:1px solid var(--divider-color,#c8d6d4);font-size:13px;font-weight:600}button:hover:not(:disabled){background:#63889a16}button:focus-visible{outline:3px solid #338b9c;outline-offset:2px}button.primary{background:#176e82;color:white;border-color:#176e82}button.primary:hover:not(:disabled){background:#105769}button:disabled{opacity:.48;cursor:default}.wide{width:100%}.actions{display:flex;gap:8px;flex-wrap:wrap}.history{margin-top:18px}.history-top{display:flex;align-items:center;justify-content:space-between;gap:16px}.history-top h2{margin:0}.history-editor{display:grid;grid-template-columns:1fr 1fr 1.5fr auto;gap:10px;align-items:end;padding:20px 0}.history-editor .field{margin:0}.scroll{max-height:340px;overflow:auto;border-top:1px solid var(--divider-color,#d9e2e0)}.row{display:flex;align-items:center;justify-content:space-between;padding:13px 0;gap:12px;border-bottom:1px solid var(--divider-color,#e3e9e7)}.row time{display:block;font-size:13px;font-weight:600}.row small{display:block;margin-top:4px;color:var(--secondary-text-color,#657a82)}.row button{min-height:34px;padding:5px 10px}.danger{color:#b74435}.empty{padding:22px 0;color:var(--secondary-text-color,#657a82);font-size:14px;line-height:1.6}.footer{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:20px;font-size:13px;color:var(--secondary-text-color,#657a82)}#message{margin:0 0 20px;padding:13px 16px;border-radius:8px;background:#a8402715;border:1px solid #a8402750;font-size:14px;line-height:1.5}#message.good{background:#34815e13;border-color:#34815e50}[hidden]{display:none!important}.current-summary{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:0 0 18px}.pin-change{margin-top:18px}.pin-change summary{cursor:pointer;font-size:13px}.pin-change form{margin-top:16px;max-width:400px}.notice{border-left:3px solid #176e82;padding-left:12px;font-size:13px;color:var(--secondary-text-color,#657a82);line-height:1.6}
      @media(max-width:900px){.layout{grid-template-columns:1fr}.sidebar{display:grid;grid-template-columns:1fr 1fr}.history-editor{grid-template-columns:1fr 1fr}.map{height:340px}}@media(max-width:580px){main{padding:22px 14px 36px}header{align-items:flex-start}h1{font-size:27px}.sidebar{display:flex}.box{padding:18px}.mapbox{padding:0}.history-editor{grid-template-columns:1fr 1fr}.history-editor .timefield{grid-column:1/-1}.footer{align-items:flex-start;flex-direction:column}.row{align-items:flex-start}.map{height:290px}}
      .map-edit-tools{padding:14px 18px;border-top:2px solid #c65d00;background:#c65d0010}.map-edit-tools p{margin:0 0 10px;font-size:13px;line-height:1.5}.map-edit-tools .actions{align-items:center}.leaflet-tooltip.edit-point-tooltip{white-space:normal;max-width:220px;border-color:#c65d00}
      .leaflet-tooltip.device-location-tooltip{width:180px;white-space:normal;text-align:center}
      .layout>*,.sidebar>*,.field,input{min-width:0}.scroll{position:relative}.row>div:first-child{min-width:0;overflow-wrap:anywhere}.row .actions{flex-shrink:0}.trash{width:40px;display:grid;place-items:center}.savebar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:16px 0 12px}@media(max-width:580px){.savebar{flex-direction:column;align-items:stretch}.savebar #save{order:1}}.savebar span{font-size:13px;color:var(--secondary-text-color,#657a82)}.row.draft{background:#7c3aed14;border-left:3px solid #7c3aed;padding-left:10px;padding-right:10px}.draft-badge,.current-badge{display:inline-block;margin-top:6px;font-size:12px;font-weight:700}.draft-badge::before,.current-badge::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:#7c3aed;margin-right:6px}
      .current-badge::before{background:#176e82}
      .template-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}.template-tools span{font-size:13px}.row{position:relative;padding-left:32px}.row.draft{padding-left:32px}.row input.history-select{position:absolute;left:0;top:15px;width:22px;height:22px;min-height:22px;margin:0;padding:0;accent-color:#176e82}.row.draft input.history-select{left:4px}.template-hint{margin:0 0 12px}
      </style><main>${unlocked ? '<header><div><div class="eyebrow">Manual Location</div><h1>Pozycje i historia</h1><p class="sub">Jeden zapis, wspólny dla wszystkich kart mapy.</p></div><button id="lock">Zablokuj panel</button></header><div id="message" role="status" hidden></div>' : ""}
      ${!admin ? '<section class="box lockbox"><h2>Ustawienia</h2><p class="sub">Zaloguj się na konto administratora Home Assistant.</p><div id="message" role="status" hidden></div></section>' : !unlocked ? '<section class="box lockbox"><h2>Odblokuj ustawienia</h2><div id="message" role="status" hidden></div><form id="unlock-form"><div class="field" style="margin-top:22px"><label for="pin">PIN administratora (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="one-time-code" data-1p-ignore autocapitalize="none" autocorrect="off" spellcheck="false" required></div><button class="primary wide" type="submit">Odblokuj</button></form></section>' : `
      <div class="layout"><aside class="sidebar"><section class="box"><h2>Ustawienia map</h2><label class="toggle"><input type="checkbox" id="global-enabled"><span>Włącz ręczne lokalizacje<small>Wyłączenie przywraca dane HA</small></span></label><div class="field" style="margin-top:24px"><label for="person">Osoba</label><select id="person"></select></div><label class="toggle"><input type="checkbox" id="person-enabled"><span>Włącz dla tej osoby</span></label><p class="notice">Nowa osoba jest włączona po ustawieniu pierwszej pozycji i zapisaniu zmian.</p><p class="notice" id="missing-person-notice" hidden>Tej osoby nie ma już w Home Assistant. Usuń jej ustawienia poniżej, a następnie zapisz wszystkie zmiany. Pozostałe osoby zachowają swoje ustawienia.</p><button class="wide danger" id="remove-person">Usuń ustawienia tej osoby</button></section>
      <section class="box"><h2>Bieżąca pozycja</h2><p class="current-summary" id="current-summary"></p><div class="field"><button class="wide" id="use-device-location">Użyj lokalizacji tego urządzenia</button><p class="sub">Jednorazowy odczyt z telefonu lub komputera, na którym otwarto panel. Wymaga zgody na lokalizację; dokładność zależy od urządzenia.</p></div><div class="pair"><div class="field"><label for="current-lat">Szerokość</label><input id="current-lat" type="number" step="any" min="-90" max="90"></div><div class="field"><label for="current-lon">Długość</label><input id="current-lon" type="number" step="any" min="-180" max="180"></div></div><button class="wide" id="set-current">Ustaw bieżącą pozycję</button><p class="sub">Każde ustawienie dodaje osobny szkic z godziną. Przed zapisem możesz zmienić jego czas i współrzędne albo go usunąć.</p></section></aside>
      <section><div class="box mapbox"><manual-location-place-search id="admin-place-search"></manual-location-place-search><div class="map" id="map" aria-label="Mapa wyboru pozycji"></div><div class="caption">Kliknij mapę, aby uzupełnić współrzędne. Następnie ustaw pozycję lub dodaj punkt historii.</div><div class="map-edit-tools" id="map-edit-tools" hidden><p id="map-edit-status" role="status"></p><div class="actions"><button class="primary" id="apply-map-edit" type="submit" form="history-form">Zastosuj edycję</button><button id="cancel-map-edit" type="button">Anuluj edycję</button></div></div></div><section class="box history"><div class="history-top"><h2>Historia</h2><span class="eyebrow" id="count"></span></div><p class="sub">Od najnowszych do najstarszych. Fioletowe pozycje czekają na zapis.</p><div class="savebar"><button class="primary" id="save">Zapisz wszystkie zmiany</button><span id="save-state"></span><button id="reload">Wczytaj zapisane dane</button></div><div class="template-tools"><span id="history-selection-count" role="status"></span><button id="history-template" type="button">Utwórz szablon trasy</button><button id="history-selection-clear" type="button">Wyczyść zaznaczenie</button></div><p class="sub template-hint" id="history-template-hint"></p><div class="scroll" id="history-list"></div><form id="history-form" class="history-editor"><div class="field"><label for="history-lat">Szerokość</label><input id="history-lat" type="number" step="any" min="-90" max="90" required></div><div class="field"><label for="history-lon">Długość</label><input id="history-lon" type="number" step="any" min="-180" max="180" required></div><div class="field timefield"><label for="history-time">Lokalna data i godzina</label><input id="history-time" type="datetime-local" step="1" required></div><button id="add-history" type="submit">Dodaj punkt</button></form><button id="cancel-edit" hidden>Anuluj edycję punktu</button></section></section></div>
      <manual-location-scenarios></manual-location-scenarios>
      <details class="pin-change"><summary>Zmień PIN administratora</summary><form id="pin-form"><div class="field"><label for="new-pin">Nowy PIN (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="new-pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="off" required></div><button type="submit">Zmień PIN i zablokuj sesje</button></form></details>`}</main>`;
    if (!admin) return;
    if (!unlocked) {
      this.shadowRoot.querySelector("#unlock-form").addEventListener("submit", (e) => { e.preventDefault(); this._unlock(); });
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
    const persons = adminPersonOptions(this._hass.states, { ...scenarioOwners, ...this._snapshot.persons });
    for (const person of persons) {
      const option = document.createElement("option");
      option.value = person.id;
      option.textContent = person.name;
      select.append(option);
    }
    if (!persons.some((p) => p.id === this._selected)) this._selected = persons[0]?.id || "";
    select.value = this._selected;
    select.addEventListener("change", () => this._selectPerson(select.value));
    const global = this.shadowRoot.querySelector("#global-enabled");
    global.checked = this._snapshot.enabled;
    global.addEventListener("change", () => { this._snapshot.enabled = global.checked; this._changed(); });
    this.shadowRoot.querySelector("#person-enabled").addEventListener("change", (e) => {
      if (!this._hass.states[this._selected]) return;
      const person = this._person();
      if (person) { person.enabled = e.target.checked; this._changed(); }
    });
    this.shadowRoot.querySelector("#set-current").addEventListener("click", () => this._setCurrent());
    this.shadowRoot.querySelector("#use-device-location").addEventListener("click", () => this._locate());
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
    this.shadowRoot.querySelector("#lock").addEventListener("click", () => this._lock());
    this.shadowRoot.querySelector("#pin-form").addEventListener("submit", (e) => { e.preventDefault(); this._changePin(); });
    this._refreshPerson();
    this._saveState();
  }

  _person() { return this._snapshot?.persons[this._selected]; }
  _currentChanged() {
    const current = this._person()?.current;
    return !!current && (!!this._person().current_history_timestamp || !sameCoordinates(current, this._savedSnapshot?.persons[this._selected]?.current));
  }
  _historyRows() {
    const saved = new Set((this._savedSnapshot?.persons[this._selected]?.history || []).map(historyKey));
    const current = this._person()?.current;
    const rows = (this._person()?.history || []).map((point, index) => ({ point, index,
      current: !!current && historyKey(point) === historyKey(current), draft: !saved.has(historyKey(point)) }));
    if (current && !rows.some((row) => row.current)) rows.push({ point: current, index: -1, current: true, draft: this._currentChanged() });
    return rows.sort((a, b) => Date.parse(b.point.timestamp) - Date.parse(a.point.timestamp));
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
      person.current = { ...drafts.at(-1) };
      person.current_history_timestamp = person.current.timestamp;
    } else {
      delete person.current_history_timestamp;
      if (saved && !removed) person.current = { ...saved.current };
      else delete this._snapshot.persons[this._selected];
    }
  }
  _refreshPerson(fit = true) {
    this._cancelLocation();
    const person = this._person();
    const missing = !this._hass.states[this._selected];
    this.shadowRoot.querySelector("#missing-person-notice").hidden = !missing || !person;
    this.shadowRoot.querySelector("#remove-person").disabled = !person;
    for (const field of this.shadowRoot.querySelectorAll("#person-enabled, #current-lat, #current-lon, #set-current, #history-form input, #history-form button")) field.disabled = missing;
    this.shadowRoot.querySelector("#person-enabled").checked = person?.enabled ?? true;
    for (const prefix of ["current", "history"]) {
      this.shadowRoot.querySelector(`#${prefix}-lat`).value = person?.current.latitude ?? "";
      this.shadowRoot.querySelector(`#${prefix}-lon`).value = person?.current.longitude ?? "";
    }
    this.shadowRoot.querySelector("#current-summary").textContent = person
      ? `${person.current.latitude.toFixed(5)}, ${person.current.longitude.toFixed(5)} · ${new Date(person.current.timestamp).toLocaleString()}${this._currentChanged() ? " · Niezapisane" : ""}`
      : "Wybierz punkt na mapie, aby ustawić pierwszą pozycję.";
    this.shadowRoot.querySelector("#history-time").value = localDateInput();
    this._refreshHistory();
    this._drawPerson(fit);
    this._syncScenarios();
  }
  _cancelLocation() {
    this._placeSearch?.cancel?.();
    if (this._locationRequest) this._message("Pobieranie lokalizacji anulowane.");
    clearTimeout(this._locationRequest?.timer);
    this._locationRequest = null;
    this._deviceLocation?.remove();
    this._deviceLocation = null;
    for (const button of [this.shadowRoot.querySelector("#use-device-location"), this._locateButton]) {
      if (!button) continue;
      button.disabled = !this._hass?.states[this._selected];
      button.removeAttribute("aria-busy");
    }
  }
  _locate() {
    if (this._busy || this._locationRequest || !this._token || !this._hass?.user?.is_admin || !this._hass.states[this._selected]) return;
    this._cancelLocation();
    if (!window.isSecureContext) { this._message("Lokalizacja urządzenia wymaga otwarcia Home Assistanta przez HTTPS."); return; }
    if (!navigator.geolocation) { this._message("Ta przeglądarka nie udostępnia lokalizacji. Wybierz punkt na mapie lub wpisz współrzędne."); return; }
    const request = { epoch: this._epoch, person: this._selected };
    this._locationRequest = request;
    for (const button of [this.shadowRoot.querySelector("#use-device-location"), this._locateButton]) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    this._message("Pobieranie lokalizacji urządzenia… Jeśli pojawi się pytanie przeglądarki, zezwól na dostęp.");
    const finish = (position, error) => {
      if (this._locationRequest !== request) return;
      this._cancelLocation();
      if (!this.isConnected || request.epoch !== this._epoch || request.person !== this._selected
        || !this._token || !this._hass.user?.is_admin || this._hass.connected === false || !this._hass.states[request.person]) return;
      if (error) {
        const messages = {
          1: "Brak zgody na lokalizację. Zezwól na dostęp dla tej strony i sprawdź uprawnienia lokalizacji przeglądarki lub aplikacji w systemie.",
          2: "Urządzenie nie mogło ustalić położenia. Sprawdź, czy lokalizacja jest włączona, lub wybierz punkt na mapie.",
          3: "Nie udało się pobrać lokalizacji w wyznaczonym czasie. Spróbuj ponownie lub wybierz punkt na mapie.",
        };
        this._message(messages[error.code] || "Nie udało się pobrać lokalizacji. Wybierz punkt na mapie lub spróbuj ponownie.");
        return;
      }
      const { latitude, longitude, accuracy } = position?.coords || {};
      if (!Number.isFinite(latitude) || Math.abs(latitude) > 90 || !Number.isFinite(longitude) || Math.abs(longitude) > 180
        || !Number.isFinite(accuracy) || accuracy < 0) { this._message("Urządzenie zwróciło nieprawidłową lokalizację. Spróbuj ponownie lub wybierz punkt na mapie."); return; }
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
  }
  _createHistoryTemplate() {
    if (!this._historyTemplateAllowed() || !this._historySelection.size) return false;
    try {
      const baseline = this._savedHistoryPoints(), rows = this._historyRows();
      const points = [...this._historySelection].map((key) => {
        const point = baseline.find((entry) => templateKey(entry) === key);
        if (!point || !rows.some((row) => !row.draft && templateKey(row.point) === key)) {
          throw new Error("Wybrane punkty zmieniły się. Odśwież historię i zaznacz je ponownie.");
        }
        // The existing history editor identifies instants to milliseconds. Never copy an ambiguous row.
        if (baseline.some((entry) => historyKey(entry) === historyKey(point) && templateKey(entry) !== key)) {
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
    const history = this._historyRows(), saved = new Set(this._savedHistoryPoints().map(templateKey));
    const eligible = new Set(history.filter((row) => !row.draft && saved.has(templateKey(row.point))).map((row) => templateKey(row.point)));
    for (const key of this._historySelection) if (!eligible.has(key)) this._historySelection.delete(key);
    this._historyChecks = [];
    this.shadowRoot.querySelector("#count").textContent = `${history.length} punktów`;
    this.shadowRoot.querySelector("#add-history").textContent = this._editIndex < 0 ? "Dodaj punkt" : "Zastosuj edycję";
    this.shadowRoot.querySelector("#cancel-edit").hidden = this._editIndex < 0;
    const list = this.shadowRoot.querySelector("#history-list");
    list.replaceChildren();
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Brak ręcznych punktów tej osoby.";
      list.append(empty);
    }
    this._previewEdit();
    this._saveState();
    const entityId = this._selected, generation = this._historySelectionGeneration;
    history.forEach(({ point, index, draft, current }) => {
      const row = document.createElement("div");
      row.className = draft ? "row draft" : "row";
      const info = document.createElement("div");
      const time = document.createElement("time");
      time.dateTime = point.timestamp;
      time.textContent = new Date(point.timestamp).toLocaleString();
      const coords = document.createElement("small");
      coords.textContent = `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
      info.append(time, coords);
      if (draft || current) {
        const badge = document.createElement("span");
        badge.className = draft ? "draft-badge" : "current-badge";
        badge.textContent = draft ? current ? "Niezapisane · bieżąca pozycja" : "Niezapisane" : "Bieżąca pozycja";
        info.append(badge);
      }
      const actions = document.createElement("div");
      actions.className = "actions";
      const edit = document.createElement("button");
      edit.disabled = !this._hass.states[this._selected];
      edit.textContent = "Edytuj";
      edit.setAttribute("aria-label", `Edytuj punkt ${time.textContent}`);
      edit.addEventListener("click", () => {
        this._cancelLocation();
        this._editIndex = index;
        this.shadowRoot.querySelector("#history-lat").value = point.latitude;
        this.shadowRoot.querySelector("#history-lon").value = point.longitude;
        this.shadowRoot.querySelector("#history-time").value = localDateInput(point.timestamp);
        this._refreshHistory();
        this._previewEdit(true);
        this.shadowRoot.querySelector("#map").scrollIntoView?.({ block: "center", behavior: "smooth" });
      });
      const remove = document.createElement("button");
      remove.disabled = !this._hass.states[this._selected];
      remove.className = "danger trash";
      remove.title = "Usuń punkt";
      remove.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      remove.setAttribute("aria-label", `Usuń punkt ${time.textContent}`);
      remove.addEventListener("click", () => this._removePoint(entityId, historyKey(point)));
      if (index >= 0) actions.append(edit);
      const key = templateKey(point), select = document.createElement("input");
      select.type = "checkbox"; select.className = "history-select"; select.id = `history-select-${this._historyChecks.length}`;
      select.setAttribute("aria-label", `Wybierz punkt ${time.textContent} do szablonu trasy`);
      select.title = eligible.has(key) ? "Wybierz do szablonu trasy" : "Najpierw zapisz punkt historii";
      select.addEventListener("change", () => {
        if (entityId !== this._selected || generation !== this._historySelectionGeneration || !this._historyTemplateAllowed()
          || !this._savedHistoryPoints().some((entry) => templateKey(entry) === key)
          || !this._historyRows().some((entry) => !entry.draft && templateKey(entry.point) === key)) return;
        if (select.checked) this._historySelection.add(key); else this._historySelection.delete(key);
        this._syncHistorySelection();
      });
      this._historyChecks.push({ input: select, key, saved: eligible.has(key) });
      actions.append(remove); row.append(info, actions, select); list.append(row);
      if (point === focusPoint) list.scrollTop = row.offsetTop;
    });
    this._syncHistorySelection();
  }
  _removePoint(entityId, key) {
    if (entityId !== this._selected || this._busy || this._scenarios?.busy || !this._token
      || !this._hass.user?.is_admin || this._hass.connected === false || !this._hass.states[entityId]) return;
    const row = this._historyRows().find(({ point }) => historyKey(point) === key);
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
    this._editing = false;
    this._refreshHistory();
    this._saveState();
    this._message("Edycja punktu anulowana.", true);
  }
  _drawPerson(fit = false) {
    this._layers.clearLayers();
    const person = this._person();
    if (!person) { this._map.setView([25, 0], 2); return; }
    const current = [person.current.latitude, person.current.longitude];
    const name = this._hass.states[this._selected]?.attributes.friendly_name || this._selected;
    const currentChanged = this._currentChanged();
    L.circleMarker(current, { color: currentChanged ? DRAFT_COLOR : "#176e82", radius: 9, fillOpacity: 1 })
      .bindPopup(popup(currentChanged ? `${name} · Niezapisane` : name, person.current)).addTo(this._layers);
    const path = person.history.map((p) => [p.latitude, p.longitude]);
    if (currentChanged && !person.current_history_timestamp) path.push(current);
    if (path.length > 1) L.polyline(path, { color: "#176e82", weight: 3 }).addTo(this._layers);
    for (const { point, draft, index } of this._historyRows().reverse()) if (index >= 0) L.circleMarker([point.latitude, point.longitude], { color: draft ? DRAFT_COLOR : "#ad6c34", radius: draft ? 6 : 4, fillOpacity: 1 }).bindPopup(popup(draft ? "Punkt historii · Niezapisane" : "Punkt historii", point)).addTo(this._layers);
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
      person.enabled = this.shadowRoot.querySelector("#person-enabled").checked;
      this._snapshot.persons[this._selected] = person;
      (this._currentDrafts[this._selected] ||= []).push(point.timestamp);
      this._syncCurrentDrafts();
      this._editIndex = -1;
      this._editing = false;
      this._changed(); this._refreshPerson(); this._refreshHistory(point);
      this._message("Dodano szkic z godziną. Możesz dodać kolejne, edytować lub usunąć je przed wspólnym zapisem.", true);
    } catch (error) { this._message(error.message); }
  }
  _addHistory() {
    if (this._busy) return;
    try {
      if (!this._hass.states[this._selected]) throw new Error("Tej osoby nie ma w Home Assistant. Usuń jej ustawienia przed zapisem.");
      if (!this._person()) throw new Error("Najpierw ustaw bieżącą pozycję tej osoby.");
      const point = this._readHistoryPoint();
      const priorTime = this._person().history[this._editIndex]?.timestamp;
      this._snapshot.persons[this._selected] = editHistory(this._person(), point, this._editIndex);
      const currentDrafts = this._currentDrafts[this._selected] || [];
      this._currentDrafts[this._selected] = currentDrafts.map((time) => time === priorTime ? point.timestamp : time);
      if (this._person().current_history_timestamp) {
        this._syncCurrentDrafts(); this._refreshPerson();
      }
      this._editIndex = -1;
      this._editing = false;
      this._changed(); this._refreshHistory(point); this._drawPerson();
      this._message("Punkt przygotowany. Zapisz wszystkie zmiany, aby zaktualizować karty.", true);
    } catch (error) { this._message(error.message); }
  }

  _removePerson() {
    if (this._busy || !this._person()) return;
    if (!window.confirm("Usunąć ręczną pozycję i całą historię tej osoby? Po zapisaniu wszystkich zmian karty wrócą do danych HA, jeśli osoba nadal istnieje.")) return;
    const saved = this._savedSnapshot?.persons[this._selected];
    if (saved) this._removedCurrent[this._selected] = historyKey(saved.current);
    delete this._snapshot.persons[this._selected];
    delete this._currentDrafts[this._selected];
    this._editIndex = -1;
    this._changed();
    this._render();
    this._message("Ustawienia osoby usunięto z formularza. Zapisz wszystkie zmiany, aby potwierdzić usunięcie.", true);
  }

  async _unlock() {
    if (this._busy || !this._hass?.user?.is_admin) return;
    const input = this.shadowRoot.querySelector("#pin");
    const pin = input.value;
    input.value = "";
    const epoch = this._epoch;
    this._busy = true;
    this._message("Odblokowywanie…");
    try {
      const session = await callWS(this._hass, { type: "manual_location/unlock", pin });
      if (epoch !== this._epoch) return;
      const snapshot = await callWS(this._hass, { type: "manual_location/admin_get", token: session.token });
      if (epoch !== this._epoch) return;
      this._token = session.token;
      this._snapshot = snapshot;
      this._savedSnapshot = structuredClone(snapshot);
      const retained = this._retained?.owner === this._hass.user.id ? this._retained : null;
      const restoreParent = retained && (retained.dirty || retained.editing || retained.editIndex >= 0 || Object.keys(retained.removedCurrent || {}).length);
      if (restoreParent) {
        this._snapshot = retained.snapshot; this._savedSnapshot = retained.saved;
        this._currentDrafts = retained.currentDrafts; this._removedCurrent = retained.removedCurrent || {};
        this._dirty = retained.dirty; this._editing = retained.editing; this._editIndex = retained.editIndex;
      } else { this._dirty = false; this._editing = false; this._editIndex = -1; }
      this._retained = null;
      this._expiry = setTimeout(() => this._clearSession("Sesja wygasła. Odblokuj panel ponownie; szkice zachowano w tej karcie.", true), Math.max(1, session.expires_in - 2) * 1000);
      this._render();
      if (restoreParent) {
        for (const [id, value] of Object.entries(retained.fields)) this.shadowRoot.querySelector(`#${id}`).value = value;
        this._previewEdit();
        this._message(snapshot.revision !== this._snapshot.revision ? "Szkice przywrócono. Dane HA zmieniły się podczas blokady; zapis może wymagać ponownego wczytania danych." : "Szkice przywrócono. Możesz kontynuować edycję.", true);
      }
      this._schedulePoll();
    } catch (error) { if (epoch === this._epoch) this._message(errorText(error)); }
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
        if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._clearSession(`${errorText(error)} Szkice zachowano w tej karcie.`, true);
        else this._message(errorText(error));
      }
      return null;
    } finally { if (epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _save() {
    if (this._editIndex >= 0) { this._message("Najpierw zastosuj albo anuluj edycję punktu."); return; }
    const result = await this._admin("admin_save", { revision: this._snapshot.revision, enabled: this._snapshot.enabled, persons: structuredClone(this._snapshot.persons) });
    if (!result) return;
    this._snapshot = result.data;
    this._savedSnapshot = structuredClone(result.data);
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._dirty = false;
    this._editing = false;
    this._editIndex = -1;
    this._refreshPerson(); this._saveState();
    this._message("Zapisano. Wszystkie karty odczytają zmiany w ciągu 5 sekund.", true);
  }
  async _reload() {
    if (this._hasDrafts() && !window.confirm("Wczytać zapisane dane? Niezapisane pozycje, historia i szkice planów w tej karcie zostaną odrzucone.")) return;
    const result = await this._admin("admin_get");
    if (!result) return;
    this._snapshot = result.data;
    this._savedSnapshot = structuredClone(result.data);
    this._currentDrafts = {};
    this._removedCurrent = {};
    this._scenarioDrafts = {};
    this._scenarioView = { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    this._dirty = false; this._editing = false; this._editIndex = -1;
    this._clearHistorySelection(true);
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
