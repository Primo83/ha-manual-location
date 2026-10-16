import { callWS, coordinates, errorText } from "./data.js?v=0.1.10";
import { L, makeMap, mapStyles, setMapHass } from "./map-common.js?v=0.1.10";

const STATUS = { draft: "Przygotowany", running: "Uruchomiony", stopped: "Zatrzymany", completed: "Ukończony" };
const REASON = { global_disabled: "Wyłączono ręczne lokalizacje.", person_missing: "Tej osoby nie ma w HA.",
  person_disabled: "Wyłączono lokalizacje tej osoby.", history_limit: "Historia osiągnęła limit 5000 punktów.",
  person_limit: "Osiągnięto limit 20 osób z ręczną lokalizacją.",
  history_conflict: "Godzina punktu koliduje z historią.", manual_change: "Ręczna zmiana zatrzymała plan.",
  user_stop: "Plan zatrzymano przyciskiem Stop.", storage_error: "Błąd zapisu zatrzymał wykonanie. Po przywróceniu zapisu uruchom plan ponownie; wykonane punkty nie zostaną zdublowane." };
const clone = (value) => structuredClone(value);

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
    const server = state.snapshot?.scenarios?.[this._selected];
    let draft = context.drafts[this._selected];
    const signature = JSON.stringify(server || null);
    if (!draft || (!draft.dirty && !draft.editing && draft.signature !== signature)) {
      const plan = clone(server || { name: "", points: [], status: "draft", cursor: 0, reason: "" });
      draft = context.drafts[this._selected] = { plan, signature, dirty: false, editing: false,
        day: scenarioLocal(plan.points[0]?.timestamp || Date.now(), this._zone()).slice(0, 10), editor: { index: -1, latitude: "", longitude: "", time: "" } };
    }
    this._draft = draft;
    this._server = server;
    if (!this.isConnected) return;
    if (!this._map) this._render();
    else { setMapHass(this._map, state.hass); this._refresh(selectedChanged); }
  }
  connectedCallback() { if (this._context && !this._map) this._render(); }
  disconnectedCallback() {
    this._generation++;
    this._resize?.disconnect(); this._map?.remove(); this._map = null; this._layers = null;
  }
  _state() { return this._context.getState(); }
  _zone() { return this._state().hass.config?.time_zone || Intl.DateTimeFormat().resolvedOptions().timeZone; }
  _notify() { this._context.onStateChange?.(); }
  _message(text, good = false) {
    const box = this.shadowRoot.querySelector("#scenario-message");
    if (box) { box.textContent = text; box.hidden = !text; box.classList.toggle("good", good); }
  }
  _editable() { return !this._busy && !this._state().busy && this._server?.status !== "running" && !!this._state().hass.states[this._selected]; }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;margin-top:24px;color:var(--primary-text-color,#20343b);font:inherit}*{box-sizing:border-box}h2{font-size:22px;font-family:Georgia,serif;font-weight:400;margin:0}h3{font-size:15px;margin:0}.box{border:1px solid var(--divider-color,#d9e2e0);border-radius:12px;background:var(--card-background-color,#fff);overflow:hidden}.top{padding:22px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.15em;color:var(--secondary-text-color,#657a82);font-weight:700;margin-bottom:8px}.sub{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:8px 0 0}.badge{font-size:12px;font-weight:700;border:1px solid var(--divider-color,#c8d6d4);border-radius:20px;padding:7px 12px;white-space:nowrap}.badge.running{background:#176e821a;color:var(--primary-text-color,#176e82)}.body{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(0,1.4fr);gap:0;border-top:1px solid var(--divider-color,#d9e2e0)}.settings{padding:22px;border-right:1px solid var(--divider-color,#d9e2e0)}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,button{font:inherit}input{width:100%;min-width:0;min-height:42px;padding:9px 11px;color:inherit;background:var(--input-fill-color,var(--card-background-color,#fff));border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}input:focus{outline:2px solid #338b9c;outline-offset:1px}.field{margin-bottom:16px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.map{height:340px}.caption{font-size:12px;line-height:1.5;color:var(--secondary-text-color,#657a82);padding:12px 18px;border-top:1px solid var(--divider-color,#d9e2e0)}button{min-height:40px;padding:9px 13px;border-radius:7px;color:inherit;background:transparent;border:1px solid var(--divider-color,#c8d6d4);font-size:13px;font-weight:600;cursor:pointer}button:hover:not(:disabled){background:#63889a16}button:focus-visible{outline:3px solid #338b9c;outline-offset:2px}button.primary{background:#176e82;border-color:#176e82;color:white}button:disabled{opacity:.48;cursor:default}.actions{display:flex;gap:8px;flex-wrap:wrap}.danger{color:var(--error-color,#b74435)}.timeline{padding:0 22px 22px}.timeline-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:20px 0 12px}.rows{max-height:330px;overflow:auto}.row{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--divider-color,#d9e2e0)}.row.active{padding-left:10px;border-left:3px solid #c65d00;background:#c65d0010}.row time{font-size:14px;font-weight:700}.row small{display:block;font-size:12px;margin-top:4px;color:var(--secondary-text-color,#657a82)}.row .actions{flex-shrink:0}.row button{min-height:34px;padding:5px 10px}.point-state{font-size:12px;color:var(--secondary-text-color,#657a82);margin-left:8px}.footer{padding:18px 22px;border-top:1px solid var(--divider-color,#d9e2e0);display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.save-state{font-size:12px;color:var(--secondary-text-color,#657a82)}.notice{border-left:3px solid #176e82;padding-left:12px;margin-top:18px}#scenario-message{margin:0 22px 18px;padding:12px 14px;border:1px solid #a8402750;border-radius:7px;background:#a8402715;font-size:13px;line-height:1.5}#scenario-message.good{background:#34815e13;border-color:#34815e50}fieldset{border:0;padding:0;margin:0;min-width:0}fieldset:disabled{opacity:.65}[hidden]{display:none!important}.empty{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);padding:15px 0}.leaflet-tooltip{white-space:normal;max-width:220px}
      @media(max-width:760px){.body{grid-template-columns:1fr}.settings{border-right:0;border-bottom:1px solid var(--divider-color,#d9e2e0)}.map{height:300px}.top{padding:18px}.settings,.timeline{padding:18px}.timeline{padding-top:0}.footer{padding:18px}.row{align-items:flex-start}.row .actions{gap:5px}.row button{padding:5px 8px}.footer .actions{width:100%}#scenario-message{margin:0 18px 18px}}@media(max-width:400px){.top{flex-wrap:wrap}.map{height:270px}}
      </style><section class="box" aria-label="Scenariusz trasy"><div class="top"><div><div class="eyebrow">Plan na konkretny dzień</div><h2>Scenariusz trasy</h2><p class="sub" id="scenario-person"></p></div><span class="badge" id="scenario-status"></span></div><div id="scenario-message" role="status" hidden></div>
      <div class="body"><div class="settings"><fieldset id="scenario-fields"><div class="field"><label for="scenario-name">Nazwa planu</label><input id="scenario-name" maxlength="80" placeholder="np. Popołudniowa trasa"></div><div class="field"><label for="scenario-day">Dzień trasy</label><input id="scenario-day" type="date" required><p class="sub" id="scenario-zone"></p></div><form id="scenario-point-form"><div class="field"><label for="scenario-time">Godzina punktu</label><input id="scenario-time" type="time" step="1" required></div><div class="pair"><div class="field"><label for="scenario-lat">Szerokość</label><input id="scenario-lat" type="number" step="any" min="-90" max="90" required></div><div class="field"><label for="scenario-lon">Długość</label><input id="scenario-lon" type="number" step="any" min="-180" max="180" required></div></div><div class="actions"><button id="scenario-add" type="submit">Dodaj punkt do planu</button><button id="scenario-cancel" type="button" hidden>Anuluj edycję</button></div></form></fieldset><p class="sub notice">Zapisz plan, a następnie świadomie kliknij Start. Plan wykonuje HA także po zamknięciu przeglądarki. Po przerwie HA uzupełni należne punkty z oryginalnymi godzinami; mapa pokaże ostatnią pozycję.</p></div>
      <div><div class="map" id="scenario-map" aria-label="Podgląd całej zaplanowanej trasy"></div><div class="caption">Kliknij mapę, aby wybrać lub przesunąć edytowany punkt. Cała trasa jest widoczna wyłącznie w tym podglądzie administratora.</div></div></div>
      <div class="timeline"><div class="timeline-head"><h3>Punkty po kolei</h3><span class="save-state" id="scenario-count"></span></div><div class="rows" id="scenario-list"></div><p class="sub" id="scenario-reason"></p><p class="sub">Stop pozostaje aktywny po restarcie. Ręczna zmiana pozycji, historii lub ustawień osoby także zatrzymuje jej plan. Ponowne włączenie lokalizacji nie uruchamia planu.</p></div>
      <div class="footer"><span class="save-state" id="scenario-save-state"></span><div class="actions"><button class="primary" id="scenario-save">Zapisz plan</button><button id="scenario-start">Start</button><button id="scenario-stop">Stop</button><button class="danger" id="scenario-delete">Usuń plan</button><button id="scenario-discard">Odrzuć szkic planu</button></div></div></section>`;
    mapStyles(this.shadowRoot);
    this._map = makeMap(this.shadowRoot.querySelector("#scenario-map"), this._state().hass);
    this._layers = L.layerGroup().addTo(this._map);
    this._map.on("click", ({ latlng }) => {
      if (!this._editable()) return;
      this._draft.editor.latitude = latlng.lat.toFixed(6); this._draft.editor.longitude = latlng.lng.toFixed(6);
      this._draft.editing = true; this._refresh(); this._notify();
      this._message("Miejsce wybrane. Uzupełnij godzinę i dodaj punkt albo zastosuj edycję.", true);
    });
    this._resize = new ResizeObserver(() => this._map?.invalidateSize()); this._resize.observe(this.shadowRoot.querySelector("#scenario-map"));
    this.shadowRoot.querySelector("#scenario-name").addEventListener("input", (event) => {
      this._draft.plan.name = event.target.value; this._draft.dirty = true; this._buttons(); this._notify();
    });
    this.shadowRoot.querySelector("#scenario-day").addEventListener("change", () => this._changeDay());
    for (const [id, key] of [["scenario-lat", "latitude"], ["scenario-lon", "longitude"], ["scenario-time", "time"]]) {
      this.shadowRoot.querySelector(`#${id}`).addEventListener("input", (event) => {
        this._draft.editor[key] = event.target.value; this._draft.editing = true; this._draw(); this._buttons(); this._notify();
      });
    }
    this.shadowRoot.querySelector("#scenario-point-form").addEventListener("submit", (event) => { event.preventDefault(); this._add(); });
    this.shadowRoot.querySelector("#scenario-cancel").addEventListener("click", () => this._cancel());
    this.shadowRoot.querySelector("#scenario-save").addEventListener("click", () => this._save());
    for (const action of ["start", "stop", "delete"]) this.shadowRoot.querySelector(`#scenario-${action}`).addEventListener("click", () => this._action(action));
    this.shadowRoot.querySelector("#scenario-discard").addEventListener("click", () => this._discard());
    this._refresh(true);
  }
  _refresh(fit = false) {
    if (!this._map || !this._draft) return;
    const { plan, editor, day } = this._draft;
    const state = this._state();
    this.shadowRoot.querySelector("#scenario-person").textContent = state.hass.states[this._selected]?.attributes.friendly_name || this._selected || "Wybierz osobę w ustawieniach powyżej.";
    this.shadowRoot.querySelector("#scenario-zone").textContent = `Godziny według Home Assistant: ${this._zone()}. Jeden dzień, bez powtarzania.`;
    for (const [id, value] of [["scenario-name", plan.name], ["scenario-day", day], ["scenario-lat", editor.latitude], ["scenario-lon", editor.longitude], ["scenario-time", editor.time]]) this.shadowRoot.querySelector(`#${id}`).value = value;
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
  _buttons() {
    const state = this._state(), running = this._server?.status === "running", busy = this._busy || state.busy;
    const blocked = busy || state.parentDirty || !state.token || !this._selected;
    this.shadowRoot.querySelector("#scenario-fields").disabled = !this._editable();
    this.shadowRoot.querySelector("#scenario-day").disabled = !this._editable() || (this._server?.cursor || 0) > 0;
    this.shadowRoot.querySelector("#scenario-save").disabled = blocked || running || !this._draft.dirty || this._draft.editing;
    this.shadowRoot.querySelector("#scenario-start").disabled = blocked || running || !this._server || this._draft.dirty || this._draft.editing || this._server.cursor >= this._server.points.length || !state.snapshot.enabled || state.snapshot.persons[this._selected]?.enabled === false || !state.hass.states[this._selected];
    this.shadowRoot.querySelector("#scenario-stop").disabled = blocked || !running;
    this.shadowRoot.querySelector("#scenario-delete").disabled = blocked || !this._server || running;
    this.shadowRoot.querySelector("#scenario-discard").disabled = busy || (!this._draft.dirty && !this._draft.editing);
    this.shadowRoot.querySelector("#scenario-save-state").textContent = state.parentDirty ? "Najpierw zapisz lub odrzuć zmiany pozycji i historii powyżej." : this._draft.editing ? "Dodaj / zastosuj albo anuluj edycję punktu." : this._draft.dirty ? "Niezapisany plan · Zapisz plan nie uruchamia trasy." : this._server ? "Plan zapisany · Start uruchamia wykonanie w HA." : "Plan będzie uruchamiany osobnym przyciskiem Start.";
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
  _resetEditor() { this._draft.editor = { index: -1, latitude: "", longitude: "", time: "" }; this._draft.editing = false; }
  _cancel() { if (this._busy) return; this._resetEditor(); this._refresh(); this._notify(); this._message("Edycja punktu anulowana.", true); }
  _edit(index) {
    if (!this._editable() || index < (this._server?.cursor || 0)) return;
    if (this._draft.editing && !window.confirm("Odrzucić bieżący niezastosowany punkt i edytować wybrany? Pozostałe punkty planu pozostaną.")) return;
    const point = this._draft.plan.points[index];
    this._draft.editor = { index, latitude: String(point.latitude), longitude: String(point.longitude), time: scenarioLocal(point.timestamp, this._zone()).slice(11) };
    this._draft.editing = true; this._refresh(); this._notify(); this._map.setView([point.latitude, point.longitude], Math.max(this._map.getZoom(), 14));
    this.shadowRoot.querySelector("#scenario-map").scrollIntoView?.({ block: "center", behavior: "smooth" });
  }
  _remove(index) {
    if (!this._editable() || index < (this._server?.cursor || 0)) return;
    this._draft.plan.points.splice(index, 1); this._draft.dirty = true;
    if (this._draft.editor.index === index) this._resetEditor();
    else if (this._draft.editor.index > index) this._draft.editor.index--;
    this._refresh(); this._notify();
  }
  _discard() {
    if (this._busy || !window.confirm("Odrzucić niezapisany szkic tego planu? Wczytasz ostatni plan z HA; wykonana historia pozostanie.")) return;
    delete this._context.drafts[this._selected]; this.context = this._context; this._notify(); this._message("Wczytano zapisany plan.", true);
  }
  async _save() {
    if (!this._draft.dirty || this._draft.editing || this._server?.status === "running") return;
    if (!this._draft.plan.name.trim() || this._draft.plan.name.trim().length > 80 || !this._draft.plan.points.length) { this._message("Wpisz nazwę (1–80 znaków) i dodaj co najmniej jeden punkt."); return; }
    await this._mutate("scenario_save", { name: this._draft.plan.name.trim(), points: clone(this._draft.plan.points) }, true);
  }
  async _action(action) {
    if (action === "start" && (this._draft.dirty || this._draft.editing)) { this._message("Najpierw zapisz plan lub odrzuć szkic. Start uruchamia plan zapisany w HA."); return; }
    if (action === "delete" && !window.confirm("Usunąć plan i jego oczekujące punkty? Wykonane punkty pozostaną w historii. Niezapisany szkic tego planu zostanie odrzucony.")) return;
    await this._mutate("scenario_action", { action }, action === "delete");
  }
  async _mutate(type, extra, replaceDraft = false) {
    const state = this._state();
    if (this._busy || state.busy || !state.token) return;
    if (state.parentDirty) { this._message("Najpierw zapisz albo odrzuć zmiany pozycji i historii powyżej. Szkic planu pozostaje bez zmian."); return; }
    const generation = this._generation;
    this._busy = true; this._buttons(); this._notify();
    try {
      const snapshot = await callWS(state.hass, { type: `manual_location/${type}`, token: state.token, revision: state.snapshot.revision, entity_id: this._selected, ...extra });
      if (generation !== this._generation || this._state().token !== state.token) return;
      const accepted = this._context.onSnapshot(snapshot);
      if (accepted === false) { this._message("Operacja zapisana w HA. Zachowano lokalny szkic, ponieważ formularz pozycji zmienił się w trakcie operacji. Wczytaj stan po zabezpieczeniu zmian."); return; }
      if (replaceDraft) delete this._context.drafts[this._selected];
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
