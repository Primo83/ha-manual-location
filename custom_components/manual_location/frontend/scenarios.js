import { callWS, coordinates, errorText } from "./data.js?v=0.1.12";
import { L, makeMap, mapStyles, setMapHass } from "./map-common.js?v=0.1.12";

const STATUS = { draft: "Przygotowany", running: "Uruchomiony", stopped: "Zatrzymany", completed: "Ukończony" };
const REASON = { global_disabled: "Wyłączono ręczne lokalizacje.", person_missing: "Tej osoby nie ma w HA.",
  person_disabled: "Wyłączono lokalizacje tej osoby.", history_limit: "Historia osiągnęła limit 5000 punktów.",
  person_limit: "Osiągnięto limit 20 osób z ręczną lokalizacją.",
  history_conflict: "Godzina punktu koliduje z historią.", manual_change: "Ręczna zmiana zatrzymała plan.",
  user_stop: "Plan zatrzymano przyciskiem Stop.", storage_error: "Błąd zapisu zatrzymał wykonanie. Po przywróceniu zapisu uruchom plan ponownie; wykonane punkty nie zostaną zdublowane." };
const clone = (value) => structuredClone(value);
const INTERVALS = [5, 10, 15, 20, 30, 45, 60, 90, 120];

export function scenarioLocal(iso, timeZone) {
  const parts = new Intl.DateTimeFormat("sv-SE", { timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}T${value.hour}:${value.minute}:${value.second}`;
}

// Resolve the HA wall clock explicitly, independently of the browser's time zone.
export function scenarioUTC(day, time, timeZone) {
  const local = `${day}T${time.length === 5 ? `${time}:00` : time}`;
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(local)) throw new Error("Wybierz poprawny dzień i godzinę.");
  const nominal = Date.parse(`${local}Z`);
  if (!Number.isFinite(nominal) || new Date(nominal).toISOString().slice(0, 19) !== local) throw new Error("Wybierz poprawny dzień i godzinę.");
  const offsets = new Set();
  for (let hours = -36; hours <= 36; hours += 6) {
    const sample = nominal + hours * 3600000;
    offsets.add(Date.parse(`${scenarioLocal(sample, timeZone)}Z`) - sample);
  }
  const matches = [...offsets].map((offset) => nominal - offset).filter((utc) => scenarioLocal(utc, timeZone) === local);
  if (!matches.length) throw new Error("Ta godzina nie istnieje w strefie HA z powodu zmiany czasu. Wybierz inną godzinę.");
  if (matches.length > 1) throw new Error("Ta godzina występuje dwukrotnie przy zmianie czasu w HA. Wybierz jednoznaczną godzinę.");
  return new Date(matches[0]).toISOString();
}

export class ManualLocationScenarios extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this._generation = 0; }
  get hasLocalChanges() { return Object.values(this._context?.drafts || {}).some((draft) => draft.dirty || draft.editing); }
  get busy() { return !!this._busy; }
  set context(context) {
    this._context = context;
    const state = context.getState();
    const selectedChanged = this._selected !== state.entityId;
    this._selected = state.entityId;
    this._view = context.view ||= { selectedByEntity: {}, personFilter: "", statusFilter: "", nextLocal: 0 };
    this._view.intervalMinutes ||= 10;
    const plans = state.snapshot?.scenarios || {};
    let id = this._view.selectedByEntity[this._selected];
    if (!id || (!plans[id] && !context.drafts[id])) {
      id = Object.keys(plans).find((key) => plans[key].entity_id === this._selected)
        || Object.keys(context.drafts).find((key) => context.drafts[key].plan.entity_id === this._selected)
        || this._newDraft(this._selected);
      this._view.selectedByEntity[this._selected] = id;
    }
    const planChanged = this._planId !== id;
    this._planId = id;
    const server = plans[id];
    let draft = context.drafts[id];
    if (!draft || (!draft.dirty && !draft.editing && draft.signature !== JSON.stringify(server || null))) {
      draft = context.drafts[id] = this._draftFor(server);
    }
    this._draft = draft;
    this._server = server;
    this._syncLocation();
    if (!this.isConnected) return;
    if (!this._map) this._render();
    else { setMapHass(this._map, state.hass); this._refresh(selectedChanged || planChanged); }
  }
  connectedCallback() { if (this._context && !this._map) this._render(); }
  disconnectedCallback() {
    this._cancelLocation();
    this._generation++;
    this._resize?.disconnect(); this._map?.remove(); this._map = null; this._layers = null;
    this._locateButton = null;
  }
  _state() { return this._context.getState(); }
  _zone() { return this._state().hass.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone; }
  _draftFor(server) {
    const plan = clone(server || { entity_id: this._selected, name: "", points: [], status: "draft", cursor: 0, reason: "" });
    return { plan, signature: JSON.stringify(server || null), dirty: false, editing: false,
      day: plan.day || scenarioLocal(plan.points[0]?.timestamp || Date.now(), this._zone()).slice(0, 10),
      editor: { index: -1, latitude: "", longitude: "", time: "" } };
  }
  _newDraft(entityId, dirty = false) {
    const id = `local:${++this._view.nextLocal}`;
    const draft = this._draftFor(); draft.plan.entity_id = entityId; draft.dirty = dirty;
    this._context.drafts[id] = draft;
    return id;
  }
  _newPlan() {
    if (this._busy || this._state().busy || !this._state().hass.states[this._selected]) return;
    this._cancelLocation();
    this._view.personFilter = ""; this._view.statusFilter = "";
    this._view.selectedByEntity[this._selected] = this._newDraft(this._selected, true);
    this.context = this._context; this._notify();
    this._message("Nowy szkic przygotowany. Zapisz plan, aby zachować go w HA także po odświeżeniu.", true);
    this.shadowRoot.querySelector("#scenario-name").focus?.();
  }
  _openPlan(id) {
    if (this._busy || this._state().busy) return;
    const plan = this._context.drafts[id]?.plan || this._state().snapshot.scenarios[id];
    if (!plan) return;
    if (plan.entity_id !== this._selected && this._context.onSelectPerson?.(plan.entity_id) === false) return;
    this._cancelLocation();
    this._view.selectedByEntity[plan.entity_id] = id;
    this.context = this._context; this._message("");
  }
  _duplicate(id) {
    const state = this._state(), draft = this._context.drafts[id];
    const local = !!(draft?.dirty || draft?.editing);
    const source = local ? draft.plan : state.snapshot?.scenarios?.[id];
    if (this._busy || state.busy || !this.isConnected || !state.token || !state.hass.user?.is_admin
      || state.hass.connected === false || !source || !state.hass.states[source.entity_id]) return;
    if (source.entity_id !== this._selected && this._context.onSelectPerson?.(source.entity_id) !== true) return;
    this._cancelLocation();
    const copyId = this._newDraft(source.entity_id, true), copy = this._context.drafts[copyId];
    const suffix = " (kopia)";
    copy.day = local ? draft.day : source.day;
    copy.plan = { entity_id: source.entity_id, name: `${(source.name.trim() || "Nowy plan").slice(0, 80 - suffix.length).trimEnd()}${suffix}`,
      day: copy.day, points: clone(source.points).map(({ latitude, longitude, timestamp }) => ({ latitude, longitude, timestamp })),
      status: "draft", cursor: 0, reason: "" };
    this._view.personFilter = ""; this._view.statusFilter = "";
    this._view.selectedByEntity[source.entity_id] = copyId;
    this.context = this._context; this._notify();
    this._message(`Utworzono niezależny szkic z zastosowanych punktów planu.${draft?.editing ? " Niezastosowany punkt pozostał w edytorze źródła." : ""} Wybierz dzień trasy, dostosuj punkty i kliknij „Zapisz plan”. Start uruchamia kopię osobno.`, true);
    this.shadowRoot.querySelector("#scenario-day").focus?.();
  }
  _notify() { this._context.onStateChange?.(); }
  _message(text, good = false) {
    const box = this.shadowRoot.querySelector("#scenario-message");
    if (box) { box.textContent = text; box.hidden = !text; box.classList.toggle("good", good); }
  }
  _editable() { return !this._busy && !this._state().busy && this._server?.status !== "running" && !!this._state().hass.states[this._selected]; }
  _canLocate() {
    const state = this._state();
    return this.isConnected && this._editable() && state.snapshot?.scenarios?.[this._planId]?.status !== "running"
      && !!state.token && !!state.hass.user?.is_admin && state.hass.connected !== false;
  }
  _locationStamp() {
    const state = this._state();
    return { generation: this._generation, person: this._selected, entityId: state.entityId,
      planId: this._planId, activePlan: this._view.selectedByEntity[this._selected], draft: this._draft,
      signature: JSON.stringify([this._draft.day, this._draft.plan, this._draft.editor, this._draft.editing]),
      server: JSON.stringify(state.snapshot?.scenarios?.[this._planId] || null),
      token: state.token, user: state.hass.user?.id, connection: state.hass.connection };
  }
  _locationCurrent(stamp) {
    if (!stamp || !this._canLocate()) return false;
    const current = this._locationStamp();
    return Object.keys(current).every((key) => current[key] === stamp[key]);
  }
  _syncLocation() {
    if ((this._locationRequest && !this._locationCurrent(this._locationRequest))
      || (this._deviceLocationContext && !this._locationCurrent(this._deviceLocationContext))) this._cancelLocation();
  }
  _cancelLocation() {
    if (this._locationRequest) this._message("Pobieranie lokalizacji anulowane.");
    clearTimeout(this._locationRequest?.timer);
    this._locationRequest = null;
    this._deviceLocation?.remove(); this._deviceLocation = null; this._deviceLocationContext = null;
    if (this._locateButton) {
      this._locateButton.disabled = !this._canLocate();
      this._locateButton.removeAttribute("aria-busy");
    }
  }
  _locate() {
    if (this._locationRequest || !this._canLocate()) return;
    this._cancelLocation();
    if (!window.isSecureContext) { this._message("Lokalizacja urządzenia wymaga otwarcia Home Assistanta przez HTTPS."); return; }
    if (!navigator.geolocation) { this._message("Ta przeglądarka nie udostępnia lokalizacji. Wybierz punkt na mapie lub wpisz współrzędne."); return; }
    const request = this._locationStamp();
    this._locationRequest = request;
    this._locateButton.disabled = true; this._locateButton.setAttribute("aria-busy", "true");
    this._message("Pobieranie lokalizacji urządzenia… Jeśli pojawi się pytanie przeglądarki, zezwól na dostęp.");
    const finish = (position, error) => {
      if (this._locationRequest !== request) return;
      const current = this._locationCurrent(request);
      this._cancelLocation();
      if (!current) return;
      if (error) {
        const messages = {
          1: "Brak zgody na lokalizację. Zezwól na dostęp dla tej strony i sprawdź uprawnienia lokalizacji przeglądarki lub aplikacji w systemie.",
          2: "Urządzenie nie mogło ustalić położenia. Sprawdź, czy lokalizacja jest włączona, lub wybierz punkt na mapie.",
          3: "Nie udało się pobrać lokalizacji w wyznaczonym czasie. Spróbuj ponownie lub wybierz punkt na mapie.",
        };
        this._message(messages[error.code] || "Nie udało się pobrać lokalizacji. Wybierz punkt na mapie lub spróbuj ponownie."); return;
      }
      const { latitude, longitude, accuracy } = position?.coords || {};
      if (!Number.isFinite(latitude) || Math.abs(latitude) > 90 || !Number.isFinite(longitude) || Math.abs(longitude) > 180
        || !Number.isFinite(accuracy) || accuracy < 0) { this._message("Urządzenie zwróciło nieprawidłową lokalizację. Spróbuj ponownie lub wybierz punkt na mapie."); return; }
      this._draft.editor.latitude = latitude.toFixed(6); this._draft.editor.longitude = longitude.toFixed(6);
      this._draft.editing = true; this._refresh();
      const label = `Lokalizacja tego urządzenia · dokładność około ${Math.ceil(accuracy)} m`;
      const preview = this._deviceLocation = L.layerGroup().addTo(this._map);
      const area = L.circle([latitude, longitude], { radius: accuracy, color: "#1976d2", weight: 1, fillOpacity: .12 }).addTo(preview);
      L.circleMarker([latitude, longitude], { radius: 7, color: "white", weight: 2, fillColor: "#1976d2", fillOpacity: 1 })
        .bindTooltip(label, { permanent: true, direction: "top", className: "device-location-tooltip" }).addTo(preview);
      this._deviceLocationContext = this._locationStamp(); this._notify();
      if (this._deviceLocation !== preview) return;
      this._map.fitBounds(area.getBounds(), { maxZoom: 16, padding: [30, 30] });
      this._message(`${label}. Sprawdź godzinę, kliknij „${this._draft.editor.index >= 0 ? "Zastosuj edycję punktu" : "Dodaj punkt do planu"}”, następnie „Zapisz plan”. Start uruchamia trasę osobno.`, true);
    };
    // The browser timeout excludes waiting for permission; keep the UI bounded too.
    request.timer = setTimeout(() => finish(null, { code: 3 }), 30000);
    try {
      navigator.geolocation.getCurrentPosition((position) => finish(position), (error) => finish(null, error),
        { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 });
    } catch { finish(null, {}); }
  }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;margin-top:24px;color:var(--primary-text-color,#20343b);font:inherit}*{box-sizing:border-box}h2{font-size:22px;font-family:Georgia,serif;font-weight:400;margin:0}h3{font-size:15px;margin:0}.box{border:1px solid var(--divider-color,#d9e2e0);border-radius:12px;background:var(--card-background-color,#fff);overflow:hidden}.top{padding:22px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.15em;color:var(--secondary-text-color,#657a82);font-weight:700;margin-bottom:8px}.sub{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:8px 0 0}.badge{font-size:12px;font-weight:700;border:1px solid var(--divider-color,#c8d6d4);border-radius:20px;padding:7px 12px;white-space:nowrap}.badge.running{background:#176e821a;color:var(--primary-text-color,#176e82)}.body{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(0,1.4fr);gap:0;border-top:1px solid var(--divider-color,#d9e2e0)}.settings{padding:22px;border-right:1px solid var(--divider-color,#d9e2e0)}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,button{font:inherit}input{width:100%;min-width:0;min-height:42px;padding:9px 11px;color:inherit;background:var(--input-fill-color,var(--card-background-color,#fff));border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}input:focus{outline:2px solid #338b9c;outline-offset:1px}.field{margin-bottom:16px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.map{height:340px}.caption{font-size:12px;line-height:1.5;color:var(--secondary-text-color,#657a82);padding:12px 18px;border-top:1px solid var(--divider-color,#d9e2e0)}button{min-height:40px;padding:9px 13px;border-radius:7px;color:inherit;background:transparent;border:1px solid var(--divider-color,#c8d6d4);font-size:13px;font-weight:600;cursor:pointer}button:hover:not(:disabled){background:#63889a16}button:focus-visible{outline:3px solid #338b9c;outline-offset:2px}button.primary{background:#176e82;border-color:#176e82;color:white}button:disabled{opacity:.48;cursor:default}.actions{display:flex;gap:8px;flex-wrap:wrap}.danger{color:var(--error-color,#b74435)}.timeline{padding:0 22px 22px}.timeline-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:20px 0 12px}.rows{max-height:330px;overflow:auto}.row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--divider-color,#d9e2e0)}.row.active{padding-left:10px;border-left:3px solid #c65d00;background:#c65d0010}.row time{font-size:14px;font-weight:700}.row small{display:block;font-size:12px;margin-top:4px;color:var(--secondary-text-color,#657a82)}.row .actions{flex-shrink:0}.row button{min-height:34px;padding:5px 10px}.point-state{font-size:12px;color:var(--secondary-text-color,#657a82);margin-left:8px}.footer{padding:18px 22px;border-top:1px solid var(--divider-color,#d9e2e0);display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.save-state{font-size:12px;color:var(--secondary-text-color,#657a82)}.notice{border-left:3px solid #176e82;padding-left:12px;margin-top:18px}#scenario-message{margin:0 22px 18px;padding:12px 14px;border:1px solid #a8402750;border-radius:7px;background:#a8402715;font-size:13px;line-height:1.5}#scenario-message.good{background:#34815e13;border-color:#34815e50}fieldset{border:0;padding:0;margin:0;min-width:0}fieldset:disabled{opacity:.65}[hidden]{display:none!important}.empty{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);padding:15px 0}.leaflet-tooltip{white-space:normal;max-width:220px}
      .time-tools{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.time-tools select{min-width:0;min-height:40px;padding:8px;font:inherit;color:inherit;background:var(--input-fill-color,var(--card-background-color,#fff));border:1px solid var(--divider-color,#c8d6d4);border-radius:7px;flex:1 0 80px}.time-tools button{flex:1 0 145px}.leaflet-tooltip.device-location-tooltip{width:180px;white-space:normal;text-align:center}
      @media(max-width:760px){.body{grid-template-columns:1fr}.settings{border-right:0;border-bottom:1px solid var(--divider-color,#d9e2e0)}.map{height:300px}.top{padding:18px}.settings,.timeline{padding:18px}.timeline{padding-top:0}.footer{padding:18px}.row{align-items:flex-start}.row .actions{gap:5px}.row button{padding:5px 8px}.footer .actions{width:100%}#scenario-message{margin:0 18px 18px}}@media(max-width:400px){.top{flex-wrap:wrap}.map{height:270px}}
      .library{padding:22px;border-bottom:1px solid var(--divider-color,#d9e2e0)}.library-head{display:flex;align-items:center;justify-content:space-between;gap:14px}.filters{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0}.filters label{margin:0}.filters select{display:block;width:100%;margin-top:7px;min-height:42px;padding:9px 11px;font:inherit;color:inherit;background:var(--input-fill-color,var(--card-background-color,#fff));border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}.plans{max-height:410px;overflow:auto}.plan-row{padding:14px 0;display:flex;align-items:center;justify-content:space-between;gap:16px;border-top:1px solid var(--divider-color,#d9e2e0)}.plan-row.selected{border-left:3px solid #176e82;padding-left:12px;background:#176e8209}.plan-row.local{border-left:3px solid #7c3aed;padding-left:12px;background:#7c3aed0b}.plan-row strong,.plan-row small{display:block;overflow-wrap:anywhere}.plan-row small{font-size:12px;line-height:1.6;margin-top:4px;color:var(--secondary-text-color,#657a82)}.plan-row .actions{flex-shrink:0}.plan-row button{min-height:34px;padding:5px 10px}.library-note{font-size:12px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:14px 0 0}@media(max-width:760px){.library{padding:18px}.plan-row{align-items:flex-start;flex-direction:column;gap:10px}.plan-row .actions{width:100%}.library-head{align-items:flex-start}.filters{gap:10px}}@media(max-width:400px){.filters{grid-template-columns:1fr}}
      </style><section class="box" aria-label="Scenariusze tras"><div class="library"><div class="library-head"><div><div class="eyebrow">Plany i zapisane szkice</div><h2>Biblioteka tras</h2></div><button id="scenario-new">Nowy plan</button></div><div class="filters"><label for="scenario-filter-person">Osoba<select id="scenario-filter-person" aria-label="Filtr osób w bibliotece"></select></label><label for="scenario-filter-status">Status<select id="scenario-filter-status" aria-label="Filtr statusu planów"></select></label></div><div class="plans" id="scenario-library" aria-label="Lista planów i szkiców"></div><p class="library-note">Zapisane plany i szkice pozostają w HA po odświeżeniu. Fioletowe szkice mają lokalne niezapisane zmiany. Możesz zapisać wiele wariantów, ale uruchomione trasy tej samej osoby nie mogą nakładać się godzinami. Różne osoby mogą mieć trasy równocześnie.</p></div><div class="top"><div><div class="eyebrow">Plan na konkretny dzień</div><h2>Edytor trasy</h2><p class="sub" id="scenario-person"></p></div><span class="badge" id="scenario-status"></span></div><div id="scenario-message" role="status" hidden></div>
      <div class="body"><div class="settings"><fieldset id="scenario-fields"><div class="field"><label for="scenario-name">Nazwa planu</label><input id="scenario-name" maxlength="80" placeholder="np. Popołudniowa trasa"></div><div class="field"><label for="scenario-day">Dzień trasy</label><input id="scenario-day" type="date" required><p class="sub" id="scenario-zone"></p></div><form id="scenario-point-form"><div class="field"><label for="scenario-time">Godzina punktu</label><input id="scenario-time" type="time" step="1" required><div class="time-tools"><select id="scenario-interval" aria-label="Odstęp od ostatniego punktu">${INTERVALS.map((minutes) => `<option value="${minutes}">${minutes} min</option>`).join("")}</select><button id="scenario-fill-time" type="button">Uzupełnij godzinę</button></div><p class="sub">Pierwszą godzinę wpisz ręcznie. Kolejną uzupełnisz o wybrany odstęp od ostatniego punktu planu.</p></div><div class="pair"><div class="field"><label for="scenario-lat">Szerokość</label><input id="scenario-lat" type="number" step="any" min="-90" max="90" required></div><div class="field"><label for="scenario-lon">Długość</label><input id="scenario-lon" type="number" step="any" min="-180" max="180" required></div></div><div class="actions"><button id="scenario-add" type="submit">Dodaj punkt do planu</button><button id="scenario-cancel" type="button" hidden>Anuluj edycję</button></div></form></fieldset><p class="sub notice">Zapisz plan, a następnie świadomie kliknij Start. Plan wykonuje HA także po zamknięciu przeglądarki. Po przerwie HA uzupełni należne punkty z oryginalnymi godzinami; mapa pokaże ostatnią pozycję.</p></div>
      <div><div class="map" id="scenario-map" aria-label="Podgląd całej zaplanowanej trasy"></div><div class="caption">Kliknij mapę, aby wybrać lub przesunąć edytowany punkt. Cała trasa jest widoczna wyłącznie w tym podglądzie administratora.</div></div></div>
      <div class="timeline"><div class="timeline-head"><h3>Punkty po kolei</h3><span class="save-state" id="scenario-count"></span></div><div class="rows" id="scenario-list"></div><p class="sub" id="scenario-reason"></p><p class="sub">Stop pozostaje aktywny po restarcie. Ręczna zmiana pozycji, historii lub ustawień osoby także zatrzymuje jej plan. Ponowne włączenie lokalizacji nie uruchamia planu.</p></div>
      <div class="footer"><span class="save-state" id="scenario-save-state"></span><div class="actions"><button class="primary" id="scenario-save">Zapisz plan</button><button id="scenario-start">Start</button><button id="scenario-stop">Stop</button><button class="danger" id="scenario-delete">Usuń plan</button><button id="scenario-discard">Odrzuć szkic planu</button></div></div></section>`;
    mapStyles(this.shadowRoot);
    this._map = makeMap(this.shadowRoot.querySelector("#scenario-map"), this._state().hass);
    this._layers = L.layerGroup().addTo(this._map);
    const locateControl = L.control({ position: "topleft" });
    locateControl.onAdd = () => {
      const box = document.createElement("div"); box.className = "leaflet-bar";
      const button = document.createElement("button"); button.type = "button";
      button.title = "Użyj lokalizacji tego urządzenia"; button.setAttribute("aria-label", button.title);
      button.style.cssText = "width:44px;height:44px;padding:9px;background:var(--card-background-color,#fff);color:var(--primary-text-color,#20343b)";
      button.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M12 1v4m0 14v4M1 12h4m14 0h4" stroke="currentColor" stroke-width="2"/></svg>';
      button.addEventListener("click", () => this._locate()); box.append(button);
      L.DomEvent.disableClickPropagation(box); L.DomEvent.disableScrollPropagation(box);
      this._locateButton = button; return box;
    };
    locateControl.addTo(this._map);
    this._map.on("click", ({ latlng }) => {
      if (!this._editable()) return;
      this._cancelLocation();
      this._draft.editor.latitude = latlng.lat.toFixed(6); this._draft.editor.longitude = latlng.lng.toFixed(6);
      this._draft.editing = true; this._refresh(); this._notify();
      this._message("Miejsce wybrane. Uzupełnij godzinę i dodaj punkt albo zastosuj edycję.", true);
    });
    this._resize = new ResizeObserver(() => this._map?.invalidateSize()); this._resize.observe(this.shadowRoot.querySelector("#scenario-map"));
    this.shadowRoot.querySelector("#scenario-name").addEventListener("input", (event) => {
      this._cancelLocation();
      this._draft.plan.name = event.target.value; this._draft.dirty = true; this._buttons(); this._notify();
    });
    this.shadowRoot.querySelector("#scenario-day").addEventListener("input", () => this._cancelLocation());
    this.shadowRoot.querySelector("#scenario-day").addEventListener("change", () => this._changeDay());
    this.shadowRoot.querySelector("#scenario-interval").addEventListener("change", (event) => { this._view.intervalMinutes = Number(event.target.value); });
    this.shadowRoot.querySelector("#scenario-fill-time").addEventListener("click", () => this._fillTime());
    for (const [id, key] of [["scenario-lat", "latitude"], ["scenario-lon", "longitude"], ["scenario-time", "time"]]) {
      this.shadowRoot.querySelector(`#${id}`).addEventListener("input", (event) => {
        this._cancelLocation();
        this._draft.editor[key] = event.target.value; this._draft.editing = true; this._draw(); this._buttons(); this._notify();
      });
    }
    this.shadowRoot.querySelector("#scenario-point-form").addEventListener("submit", (event) => { event.preventDefault(); this._add(); });
    this.shadowRoot.querySelector("#scenario-cancel").addEventListener("click", () => this._cancel());
    this.shadowRoot.querySelector("#scenario-save").addEventListener("click", () => this._save());
    for (const action of ["start", "stop", "delete"]) this.shadowRoot.querySelector(`#scenario-${action}`).addEventListener("click", () => this._action(action));
    this.shadowRoot.querySelector("#scenario-discard").addEventListener("click", () => this._discard());
    this.shadowRoot.querySelector("#scenario-new").addEventListener("click", () => this._newPlan());
    for (const [id, key] of [["scenario-filter-person", "personFilter"], ["scenario-filter-status", "statusFilter"]]) {
      this.shadowRoot.querySelector(`#${id}`).addEventListener("change", (event) => {
        this._view[key] = event.target.value; this._library();
      });
    }
    this._refresh(true);
  }
  _refresh(fit = false) {
    if (!this._map || !this._draft) return;
    const { plan, editor, day } = this._draft;
    const state = this._state();
    this.shadowRoot.querySelector("#scenario-person").textContent = state.hass.states[this._selected]?.attributes.friendly_name || this._selected || "Wybierz osobę w ustawieniach powyżej.";
    this.shadowRoot.querySelector("#scenario-zone").textContent = `Godziny według Home Assistant: ${this._zone()}. Jeden dzień, bez powtarzania.`;
    for (const [id, value] of [["scenario-name", plan.name], ["scenario-day", day], ["scenario-lat", editor.latitude], ["scenario-lon", editor.longitude], ["scenario-time", editor.time]]) this.shadowRoot.querySelector(`#${id}`).value = value;
    this.shadowRoot.querySelector("#scenario-interval").value = String(this._view.intervalMinutes);
    const status = this.shadowRoot.querySelector("#scenario-status");
    status.textContent = this._draft.dirty ? "Niezapisany szkic" : STATUS[this._server?.status || "draft"];
    status.className = `badge ${this._server?.status || "draft"}`;
    this.shadowRoot.querySelector("#scenario-reason").textContent = REASON[this._server?.reason] || (this._server?.reason ? `Plan zatrzymany: ${this._server.reason}` : "");
    this.shadowRoot.querySelector("#scenario-count").textContent = `${this._server?.cursor || 0} wykonano / ${plan.points.length} punktów`;
    const list = this.shadowRoot.querySelector("#scenario-list"); list.replaceChildren();
    if (!plan.points.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "Wybierz dzień, godzinę i miejsce na mapie. Każdy punkt ma własną godzinę."; list.append(empty); }
    plan.points.forEach((point, index) => {
      const done = index < (this._server?.cursor || 0);
      const row = document.createElement("div"); row.className = `row${index === editor.index ? " active" : ""}`;
      const info = document.createElement("div"), time = document.createElement("time"), badge = document.createElement("span"), coords = document.createElement("small");
      time.dateTime = point.timestamp; time.textContent = scenarioLocal(point.timestamp, this._zone()).slice(11);
      badge.className = "point-state"; badge.textContent = done ? "Wykonano" : "Oczekuje";
      coords.textContent = `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`; info.append(time, badge, coords);
      const actions = document.createElement("div"); actions.className = "actions";
      for (const [label, handler] of [["Edytuj", () => this._edit(index)], ["Usuń", () => this._remove(index)]]) {
        const button = document.createElement("button"); button.textContent = label; button.disabled = done || !this._editable();
        button.setAttribute("aria-label", `${label} punkt ${time.textContent}`); button.addEventListener("click", handler); actions.append(button);
      }
      row.append(info, actions); list.append(row);
    });
    this.shadowRoot.querySelector("#scenario-add").textContent = editor.index >= 0 ? "Zastosuj edycję punktu" : "Dodaj punkt do planu";
    this.shadowRoot.querySelector("#scenario-cancel").hidden = !this._draft.editing && editor.index < 0;
    this._buttons(); this._draw(fit);
  }
  _library() {
    const state = this._state(), plans = state.snapshot.scenarios || {};
    const active = this.shadowRoot.activeElement;
    const focusedId = active?.closest?.(".plan-row")?.getAttribute("data-scenario-id");
    const focusedAction = active?.textContent;
    const entries = new Map(Object.entries(plans));
    for (const [id, draft] of Object.entries(this._context.drafts)) {
      if (draft.dirty || draft.editing) entries.set(id, draft.plan);
    }
    const personFilter = this.shadowRoot.querySelector("#scenario-filter-person");
    const statusFilter = this.shadowRoot.querySelector("#scenario-filter-status");
    const personName = (id) => state.hass.states[id]?.attributes.friendly_name || `${id} (brak w HA)`;
    const personIds = new Set([...Object.keys(state.hass.states).filter((id) => id.startsWith("person.")),
      ...[...entries.values()].map((plan) => plan.entity_id)]);
    const personOptions = [["", "Wszystkie osoby"], ...[...personIds].map((id) => [id, personName(id)])];
    const statusOptions = [["", "Wszystkie statusy"], ["local", "Niezapisane zmiany"],
      ...Object.entries(STATUS).map(([key, label]) => [key, key === "draft" ? "Szkice i przygotowane" : label])];
    personFilter.replaceChildren(); statusFilter.replaceChildren();
    for (const [value, label] of personOptions) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; personFilter.append(option);
    }
    for (const [value, label] of statusOptions) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; statusFilter.append(option);
    }
    personFilter.value = this._view.personFilter; statusFilter.value = this._view.statusFilter;
    const list = this.shadowRoot.querySelector("#scenario-library"); list.replaceChildren();
    for (const [id, plan] of entries) {
      const server = plans[id], draft = this._context.drafts[id];
      const local = !!(draft?.dirty || draft?.editing), status = server?.status || "draft";
      if (this._view.personFilter && this._view.personFilter !== plan.entity_id) continue;
      if (this._view.statusFilter && (this._view.statusFilter === "local" ? !local : status !== this._view.statusFilter)) continue;
      const row = document.createElement("div"); row.className = `plan-row${id === this._planId ? " selected" : ""}${local ? " local" : ""}`;
      row.setAttribute("data-scenario-id", id);
      const info = document.createElement("div"), name = document.createElement("strong"), detail = document.createElement("small"), progress = document.createElement("small");
      name.textContent = plan.name || "Nowy szkic bez nazwy";
      const day = local ? draft.day : plan.day;
      const times = plan.points.length ? `${scenarioLocal(plan.points[0].timestamp, this._zone()).slice(11)}–${scenarioLocal(plan.points.at(-1).timestamp, this._zone()).slice(11)}` : "bez punktów";
      detail.textContent = `${personName(plan.entity_id)} · ${day} · ${times}`;
      progress.textContent = `${local ? "Niezapisane zmiany · " : ""}${server ? (!plan.points.length && status === "draft" ? "Zapisany szkic" : STATUS[status]) : "Szkic tylko w tej karcie"} · ${server?.cursor || 0}/${plan.points.length} punktów wykonano`;
      info.append(name, detail, progress);
      const actions = document.createElement("div"); actions.className = "actions";
      const blocked = this._busy || state.busy || state.parentDirty || !state.token;
      for (const [label, action, disabled] of [["Otwórz", "open", this._busy || state.busy],
        ["Start", "start", blocked || local || !server || status === "running" || server.cursor >= server.points.length || !state.snapshot.enabled || state.snapshot.persons[plan.entity_id]?.enabled === false || !state.hass.states[plan.entity_id]],
        ["Stop", "stop", blocked || status !== "running"], ["Usuń", "delete", blocked || !server || status === "running"],
        ["Duplikuj", "duplicate", this._busy || state.busy || !state.token || !state.hass.user?.is_admin || state.hass.connected === false || !state.hass.states[plan.entity_id]]]) {
        const button = document.createElement("button"); button.textContent = label; button.disabled = !!disabled;
        button.setAttribute("aria-label", `${label} plan ${plan.name || "bez nazwy"} osoby ${personName(plan.entity_id)}`);
        if (action === "delete") button.className = "danger";
        button.addEventListener("click", () => action === "open" ? this._openPlan(id) : action === "duplicate" ? this._duplicate(id) : this._action(action, id)); actions.append(button);
      }
      row.append(info, actions); list.append(row);
      if (id === focusedId) {
        const button = [...actions.children].find((button) => button.textContent === focusedAction && !button.disabled) || actions.children[0];
        button.focus?.({ preventScroll: true });
      }
    }
    if (!list.children.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "Brak planów w tym widoku. Kliknij Nowy plan, aby przygotować kolejny szkic."; list.append(empty); }
    this.shadowRoot.querySelector("#scenario-new").disabled = !!(this._busy || state.busy || !state.hass.states[this._selected]);
  }
  _buttons() {
    this._syncLocation();
    const state = this._state(), running = this._server?.status === "running", busy = this._busy || state.busy;
    const blocked = busy || state.parentDirty || !state.token || !this._selected;
    this.shadowRoot.querySelector("#scenario-fields").disabled = !this._editable();
    this.shadowRoot.querySelector("#scenario-day").disabled = !this._editable() || (this._server?.cursor || 0) > 0;
    this.shadowRoot.querySelector("#scenario-fill-time").disabled = !this._editable() || this._draft.editor.index >= 0 || !this._draft.plan.points.length;
    this.shadowRoot.querySelector("#scenario-interval").disabled = !this._editable() || this._draft.editor.index >= 0;
    if (this._locateButton) this._locateButton.disabled = !!this._locationRequest || !this._canLocate();
    this.shadowRoot.querySelector("#scenario-save").disabled = blocked || running || !this._draft.dirty || this._draft.editing;
    this.shadowRoot.querySelector("#scenario-start").disabled = blocked || running || !this._server || this._draft.dirty || this._draft.editing || this._server.cursor >= this._server.points.length || !state.snapshot.enabled || state.snapshot.persons[this._selected]?.enabled === false || !state.hass.states[this._selected];
    this.shadowRoot.querySelector("#scenario-stop").disabled = blocked || !running;
    this.shadowRoot.querySelector("#scenario-delete").disabled = blocked || !this._server || running;
    this.shadowRoot.querySelector("#scenario-discard").disabled = busy || (!this._draft.dirty && !this._draft.editing);
    this.shadowRoot.querySelector("#scenario-save-state").textContent = state.parentDirty ? "Najpierw zapisz lub odrzuć zmiany pozycji i historii powyżej." : this._draft.editing ? "Dodaj / zastosuj albo anuluj edycję punktu." : this._draft.dirty ? "Niezapisany plan · Zapisz plan nie uruchamia trasy." : this._server ? "Plan zapisany · Start uruchamia wykonanie w HA." : "Plan będzie uruchamiany osobnym przyciskiem Start.";
    this._library();
  }
  _draw(fit = false) {
    if (!this._layers) return;
    this._layers.clearLayers();
    const path = this._draft.plan.points.map((point) => [point.latitude, point.longitude]);
    if (path.length > 1) L.polyline(path, { color: "#176e82", weight: 3, opacity: .7 }).addTo(this._layers);
    this._draft.plan.points.forEach((point, index) => {
      const label = document.createElement("span"); label.textContent = `${scenarioLocal(point.timestamp, this._zone()).slice(11)} · ${index < (this._server?.cursor || 0) ? "Wykonano" : "Oczekuje"}`;
      L.circleMarker([point.latitude, point.longitude], { radius: 6, color: index < (this._server?.cursor || 0) ? "#657a82" : "#176e82", fillOpacity: 1 }).bindTooltip(label).addTo(this._layers);
    });
    const editor = this._draft.editor;
    const preview = editor.latitude !== "" && editor.longitude !== "" && coordinates({ latitude: Number(editor.latitude), longitude: Number(editor.longitude) });
    if (preview && this._draft.editing) {
      const label = document.createElement("span"); label.textContent = `${editor.index >= 0 ? "Edytowany punkt" : "Nowy punkt"} · ${editor.time || "wybierz godzinę"}`;
      L.circleMarker([preview.latitude, preview.longitude], { radius: 10, color: "#c65d00", fillColor: "#ffb347", weight: 3, fillOpacity: 1 }).bindTooltip(label, { permanent: true, direction: "top" }).addTo(this._layers);
    }
    const mapElement = this.shadowRoot.querySelector("#scenario-map"); mapElement.classList.toggle("map-dark", !!this._state().hass.themes?.darkMode);
    if (fit && path.length) this._map.fitBounds(path, { padding: [35, 35], maxZoom: 14 });
    else if (fit) this._map.setView([25, 0], 2);
  }
  _changeDay() {
    if (!this._editable() || this._server?.cursor > 0) return;
    this._cancelLocation();
    const day = this.shadowRoot.querySelector("#scenario-day").value;
    try {
      scenarioUTC(day, "12:00:00", this._zone());
      const points = this._draft.plan.points.map((point) => ({ ...point, timestamp: scenarioUTC(day, scenarioLocal(point.timestamp, this._zone()).slice(11), this._zone()) }));
      this._draft.day = day; this._draft.plan.points = points; this._draft.dirty = true;
      this._refresh(); this._notify(); this._message("");
    } catch (error) { this.shadowRoot.querySelector("#scenario-day").value = this._draft.day; this._message(error.message); }
  }
  _add() {
    if (!this._editable()) return;
    this._cancelLocation();
    try {
      const { editor, plan, day } = this._draft;
      if (editor.latitude === "" || editor.longitude === "") throw new Error("Kliknij mapę albo wpisz obie współrzędne.");
      const position = coordinates({ latitude: Number(editor.latitude), longitude: Number(editor.longitude) });
      if (!position) throw new Error("Sprawdź współrzędne: szerokość −90…90, długość −180…180.");
      const prior = plan.points[editor.index];
      const unchangedTime = prior && scenarioLocal(prior.timestamp, this._zone()) === `${day}T${editor.time.length === 5 ? `${editor.time}:00` : editor.time}`;
      const timestamp = unchangedTime ? prior.timestamp : scenarioUTC(day, editor.time, this._zone());
      const prefix = this._server?.points.slice(0, this._server.cursor) || [];
      if (prefix.length && Date.parse(timestamp) <= Date.parse(prefix.at(-1).timestamp)) throw new Error("Nowy punkt musi mieć godzinę późniejszą od ostatniego wykonanego punktu.");
      const points = plan.points.filter((_, index) => index !== editor.index);
      if (points.some((point) => Date.parse(point.timestamp) === Date.parse(timestamp))) throw new Error("Istnieje już punkt o tej godzinie.");
      if (points.length >= 500) throw new Error("Jeden plan może zawierać maksymalnie 500 punktów.");
      points.push({ ...position, timestamp }); points.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
      plan.points = points; this._draft.dirty = true; this._resetEditor(); this._refresh(true); this._notify();
      this._message("Punkt przygotowany. Zapisz plan, aby zachować trasę w HA.", true);
    } catch (error) { this._message(error.message); }
  }
  _fillTime() {
    if (!this._editable() || this._draft.editor.index >= 0 || !this._draft.plan.points.length) return;
    try {
      const minutes = this._view.intervalMinutes;
      if (!INTERVALS.includes(minutes)) throw new Error("Wybierz odstęp z listy.");
      const latest = Math.max(...this._draft.plan.points.map((point) => Date.parse(point.timestamp)));
      const local = scenarioLocal(latest + minutes * 60000, this._zone()), [day, time] = local.split("T");
      if (day !== this._draft.day) throw new Error("Ten odstęp wykracza poza wybrany dzień trasy. Wybierz krótszy odstęp albo wpisz godzinę ręcznie.");
      scenarioUTC(day, time, this._zone());
      this._cancelLocation();
      this._draft.editor.time = time; this._draft.editing = true; this._refresh(); this._notify();
      this._message(`Godzina uzupełniona: ${time}, ${minutes} min po ostatnim punkcie. Sprawdź miejsce i kliknij „Dodaj punkt do planu”.`, true);
    } catch (error) { this._message(error.message); }
  }
  _resetEditor() { this._cancelLocation(); this._draft.editor = { index: -1, latitude: "", longitude: "", time: "" }; this._draft.editing = false; }
  _cancel() { if (this._busy) return; this._resetEditor(); this._refresh(); this._notify(); this._message("Edycja punktu anulowana.", true); }
  _edit(index) {
    if (!this._editable() || index < (this._server?.cursor || 0)) return;
    if (this._draft.editing && !window.confirm("Odrzucić bieżący niezastosowany punkt i edytować wybrany? Pozostałe punkty planu pozostaną.")) return;
    this._cancelLocation();
    const point = this._draft.plan.points[index];
    this._draft.editor = { index, latitude: String(point.latitude), longitude: String(point.longitude), time: scenarioLocal(point.timestamp, this._zone()).slice(11) };
    this._draft.editing = true; this._refresh(); this._notify(); this._map.setView([point.latitude, point.longitude], Math.max(this._map.getZoom(), 14));
    this.shadowRoot.querySelector("#scenario-map").scrollIntoView?.({ block: "center", behavior: "smooth" });
  }
  _remove(index) {
    if (!this._editable() || index < (this._server?.cursor || 0)) return;
    this._cancelLocation();
    this._draft.plan.points.splice(index, 1); this._draft.dirty = true;
    if (this._draft.editor.index === index) this._resetEditor();
    else if (this._draft.editor.index > index) this._draft.editor.index--;
    this._refresh(); this._notify();
  }
  _discard() {
    if (this._busy || !window.confirm("Odrzucić niezapisany szkic tego planu? Wczytasz ostatni plan z HA; wykonana historia pozostanie.")) return;
    this._cancelLocation();
    delete this._context.drafts[this._planId]; this.context = this._context; this._notify(); this._message("Niezapisane zmiany tego planu odrzucono.", true);
  }
  async _save() {
    if (!this._draft.dirty || this._draft.editing || this._server?.status === "running") return;
    if (!this._draft.plan.name.trim() || this._draft.plan.name.trim().length > 80) { this._message("Wpisz nazwę planu (1–80 znaków). Szkic może nie mieć jeszcze punktów."); return; }
    await this._mutate("scenario_save", { scenario_id: this._server ? this._planId : null,
      entity_id: this._selected, day: this._draft.day, name: this._draft.plan.name.trim(), points: clone(this._draft.plan.points) }, true);
  }
  async _action(action, id = this._planId) {
    const draft = this._context.drafts[id], server = this._state().snapshot.scenarios[id];
    if (action === "start" && (draft?.dirty || draft?.editing)) { this._message("Najpierw zapisz plan lub odrzuć szkic. Start uruchamia plan zapisany w HA."); return; }
    if (!server) return;
    if (action === "delete" && !window.confirm("Usunąć plan i jego oczekujące punkty? Wykonane punkty pozostaną w historii. Niezapisany szkic tego planu zostanie odrzucony.")) return;
    await this._mutate("scenario_action", { scenario_id: id, action }, action === "delete", id);
  }
  async _mutate(type, extra, replaceDraft = false, id = this._planId) {
    this._cancelLocation();
    const state = this._state();
    if (this._busy || state.busy || !state.token) return;
    if (state.parentDirty) { this._message("Najpierw zapisz albo odrzuć zmiany pozycji i historii powyżej. Szkic planu pozostaje bez zmian."); return; }
    const generation = this._generation;
    const previousIds = new Set(Object.keys(state.snapshot.scenarios || {}));
    const entityId = this._context.drafts[id]?.plan.entity_id || state.snapshot.scenarios[id]?.entity_id;
    this._busy = true; this._buttons(); this._notify();
    try {
      const snapshot = await callWS(state.hass, { type: `manual_location/${type}`, token: state.token, revision: state.snapshot.revision, ...extra });
      if (generation !== this._generation || this._state().token !== state.token) return;
      const accepted = this._context.onSnapshot(snapshot);
      if (accepted === false) { this._message("Operacja zapisana w HA. Zachowano lokalny szkic, ponieważ formularz pozycji zmienił się w trakcie operacji. Wczytaj stan po zabezpieczeniu zmian."); return; }
      if (replaceDraft) delete this._context.drafts[id];
      if (type === "scenario_save" && extra.scenario_id === null) {
        const savedId = Object.keys(snapshot.scenarios).find((key) => !previousIds.has(key));
        if (savedId) this._view.selectedByEntity[entityId] = savedId;
      }
      this.context = this._context;
      this._message(type === "scenario_save" ? "Plan zapisany. Kliknij Start, gdy chcesz uruchomić wykonanie w HA." : extra.action === "start" ? "Plan uruchomiony w HA. Możesz zamknąć przeglądarkę." : extra.action === "stop" ? "Plan zatrzymany. Wykonana historia pozostaje." : "Plan usunięty. Wykonana historia pozostaje.", true);
    } catch (error) {
      if (generation !== this._generation) return;
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._context.onExpired(errorText(error));
      else {
        if (error?.code === "revision_conflict") {
          try {
            const snapshot = await callWS(state.hass, { type: "manual_location/admin_get", token: state.token });
            if (generation === this._generation && this._state().token === state.token) this._context.onSnapshot(snapshot);
          } catch (refreshError) {
            if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(refreshError?.code)) this._context.onExpired(errorText(refreshError));
          }
        }
        this._message(error?.code === "revision_conflict" ? "Dane HA zmieniły się. Odświeżono stan; Twój szkic pozostał. Sprawdź wykonane punkty przed ponowieniem zapisu." : errorText(error));
      }
    } finally { this._busy = false; if (generation === this._generation) { this._refresh(); this._notify(); } }
  }
}

if (!customElements.get("manual-location-scenarios")) customElements.define("manual-location-scenarios", ManualLocationScenarios);
