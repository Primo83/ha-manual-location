import { callWS, coordinates, errorText } from "./data.js?v=0.1.17";
import "./place-search.js?v=0.1.17";

const MODES = { walk: "Pieszo", drive: "Samochód", bicycle: "Rower", transit: "Komunikacja publiczna · szacunek" };
const INTERVALS = [1, 5, 10, 15, 20, 30, 45, 60, 90, 120];

export class ManualLocationRouteImport extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this._generation = 0; this._stops = []; }
  get busy() { return !!this._pending; }
  get hasLocalChanges() { return this.busy || !!this.shadowRoot.querySelector("#import-url")?.value; }
  set context(context) {
    this._context = context;
    if (this.isConnected && !this._rendered) this._render();
    else this.sync();
  }
  connectedCallback() { if (this._context && !this._rendered) this._render(); }
  disconnectedCallback() { this.cancel(true); }
  _capture() {
    const state = this._context?.getState();
    if (!this.isConnected || !state?.allowed || !state.token || !state.hass.user?.is_admin || state.hass.connected === false) return null;
    const fields = ["url", "mode", "day", "start", "interval", "duration"].map((id) => this.shadowRoot.querySelector(`#import-${id}`)?.value || "");
    return { generation: this._generation, token: state.token, user: state.hass.user.id, connection: state.hass.connection,
      stamp: state.stamp, fields: JSON.stringify(fields), stops: JSON.stringify(this._stops) };
  }
  _matches(stamp) { const current = this._capture(); return !!current && Object.keys(current).every((key) => current[key] === stamp[key]); }
  sync() {
    const stamp = this._pending || this._resolvedContext;
    if (stamp && !this._matches(stamp)) this.cancel(true);
    this._buttons();
  }
  cancel(clearStops = false) {
    this._generation++; this._pending = null; this._resolvedContext = null;
    if (clearStops) { this._stops = []; this.shadowRoot.querySelector("#import-stops")?.replaceChildren(); }
    this._searches?.forEach((search) => search.cancel?.());
    if (!clearStops && this._stops.length) { this._showStops(); this._resolvedContext = this._capture(); }
    this._message(""); this._buttons();
  }
  _message(message) {
    const box = this.shadowRoot.querySelector("#import-status");
    if (box) { box.textContent = message; box.hidden = !message; }
  }
  _buttons() {
    if (!this._rendered) return;
    const allowed = !!this._capture(), busy = !!this._pending;
    for (const id of ["url", "mode", "day", "start", "interval", "duration", "resolve"]) this.shadowRoot.querySelector(`#import-${id}`).disabled = !allowed || busy;
    this.shadowRoot.querySelector("#import-build").disabled = !allowed || busy || this._stops.length < 2 || this._stops.some((point) => !coordinates(point));
    this.shadowRoot.querySelector("#import-resolve").setAttribute("aria-busy", String(busy));
    this._searches?.forEach((search) => search.sync?.());
  }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;border-bottom:1px solid var(--divider-color,#d9e2e0);font:inherit;color:var(--primary-text-color,#20343b)}*{box-sizing:border-box}details{padding:18px 22px}summary{font-weight:700;font-size:14px;cursor:pointer;min-height:24px}.body{margin-top:16px;max-width:900px}.link{display:flex;gap:9px;align-items:end}.link>div{flex:1;min-width:0}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,select,button{font:inherit;color:inherit;min-width:0;min-height:42px;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px;background:var(--input-fill-color,var(--card-background-color,#fff))}input,select{width:100%;padding:9px 11px}button{padding:9px 13px;font-size:13px;font-weight:600;cursor:pointer}button.primary{background:#176e82;border-color:#176e82;color:#fff}button:disabled,input:disabled,select:disabled{opacity:.5;cursor:default}input:focus,select:focus,button:focus-visible{outline:2px solid #338b9c;outline-offset:2px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}.grid>div{min-width:0}p{font-size:12px;line-height:1.6;margin:9px 0;color:var(--secondary-text-color,#657a82)}a{color:var(--primary-color,#176e82)}ol{list-style:none;margin:12px 0;padding:0}li{border-top:1px solid var(--divider-color,#d9e2e0);padding:12px 0;font-size:13px;overflow-wrap:anywhere}li strong,li small{display:block}li small{font-size:12px;line-height:1.5;margin-top:5px;color:var(--secondary-text-color,#657a82)}manual-location-place-search{display:block;margin-top:10px;border:1px solid var(--divider-color,#d9e2e0);border-radius:7px}#import-status{border-left:3px solid #176e82;padding:8px 12px;overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:600px){details{padding:16px 18px}.grid{grid-template-columns:1fr 1fr}.link{flex-wrap:wrap}.link>div{flex-basis:100%}.link>button{width:100%}}@media(max-width:360px){.grid{grid-template-columns:1fr}}
      </style><details><summary>Importuj trasę z linku Google Maps</summary><div class="body">
      <div class="link"><div><label for="import-url">Link do trasy Google Maps</label><input id="import-url" type="url" maxlength="8192" autocomplete="off" placeholder="https://maps.app.goo.gl/…"></div><button id="import-resolve" type="button">Odczytaj link</button></div>
      <p>Odczytaj link, sprawdź miejsca i wybierz czas. Import utworzy nowy szkic; zapis i Start zatwierdzasz osobno.</p>
      <ol id="import-stops" aria-label="Miejsca z linku trasy"></ol>
      <div class="grid"><div><label for="import-mode">Sposób podróży</label><select id="import-mode"><option value="">Wybierz / wykryj z linku</option>${Object.entries(MODES).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></div><div><label for="import-day">Dzień trasy</label><input id="import-day" type="date"></div><div><label for="import-start">Godzina początku</label><input id="import-start" type="time" step="60"></div><div><label for="import-interval">Punkt co</label><select id="import-interval">${INTERVALS.map((value) => `<option value="${value}"${value === 15 ? " selected" : ""}>${value} min</option>`).join("")}</select></div><div><label for="import-duration">Czas podróży z Google (min)</label><input id="import-duration" type="number" min="1" max="1440" step="1" placeholder="np. 70 · opcjonalnie"></div></div>
      <p>Wpisany czas zastąpi szacunek Geoapify i proporcjonalnie dostosuje godziny punktów. Puste pole: czas według wybranego transportu w Geoapify. Dla pociągów i autobusów to przybliżenie, bez rozkładu, konkretnych kursów i gwarancji czasu przesiadek.</p>
      <p>Godziny według strefy Home Assistanta. Trasa musi zmieścić się w jednym dniu. Droga może różnić się od wybranego wariantu Google.</p>
      <button id="import-build" type="button" class="primary">Wyznacz trasę i utwórz szkic</button><p id="import-status" role="status" aria-live="polite" hidden></p>
      <p><a href="https://www.geoapify.com/" target="_blank" rel="noopener noreferrer">Powered by Geoapify</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a> · wspólny limit zużycia z wyszukiwarką</p>
      </div></details>`;
    this._rendered = true;
    this.shadowRoot.querySelector("#import-day").value = this._context.getState().day;
    for (const id of ["url", "mode", "day", "start", "interval", "duration"]) {
      this.shadowRoot.querySelector(`#import-${id}`).addEventListener("input", () => this.cancel(id === "url"));
    }
    this.shadowRoot.querySelector("#import-resolve").addEventListener("click", () => this._resolve());
    this.shadowRoot.querySelector("#import-build").addEventListener("click", () => this._build());
    this._buttons();
  }
  _showStops() {
    const list = this.shadowRoot.querySelector("#import-stops"); list.replaceChildren(); this._searches = [];
    this._stops.forEach((point, index) => {
      const row = document.createElement("li"), label = document.createElement("strong"), detail = document.createElement("small");
      label.textContent = `${index + 1}. ${point.label || point.query || (index === 0 ? "Początek" : index === this._stops.length - 1 ? "Cel" : "Przystanek")}`;
      detail.textContent = coordinates(point) ? `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)} · sprawdź na podglądzie trasy` : "Wybierz dokładne miejsce z wyników wyszukiwania.";
      row.append(label, detail); list.append(row);
      if (!coordinates(point)) {
        const search = document.createElement("manual-location-place-search"); row.append(search); this._searches.push(search);
        const generation = this._generation;
        search.context = {
          getState: () => { const state = this._context.getState(); return { ...state, allowed: !!this._capture() && !this._pending && generation === this._generation,
            stamp: JSON.stringify([state.stamp, generation, index, point.query]) }; },
          onSelect: (selected) => {
            if (generation !== this._generation || !this._capture()) return false;
            this._stops[index] = { latitude: selected.latitude, longitude: selected.longitude, label: selected.label };
            this._showStops(); this._resolvedContext = this._capture(); this._buttons(); return true;
          }, onExpired: (message) => this._context.onExpired?.(message),
        };
        const query = search.shadowRoot?.querySelector("#place-query"); if (query) query.value = point.query;
      }
    });
  }
  async _resolve() {
    this.sync(); if (this._pending || !this._capture()) return;
    const url = this.shadowRoot.querySelector("#import-url").value.trim();
    if (!url.startsWith("https://") || url.length > 8192) { this._message("Wklej pełny link HTTPS do trasy Google Maps, także skrócony maps.app.goo.gl."); return; }
    this.cancel(true); const request = this._capture(); if (!request) return;
    this._pending = request; this._buttons(); this._message("Odczytywanie miejsc i sposobu podróży z linku…");
    try {
      const response = await callWS(this._context.getState().hass, { type: "manual_location/route_resolve", token: request.token, url });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(true); return; }
      if (!Array.isArray(response?.stops) || response.stops.length < 2 || response.stops.length > 12
        || response.stops.some((point) => !coordinates(point) && (typeof point?.query !== "string" || !point.query.trim()))) throw { code: "route_invalid_response" };
      this._pending = null;
      this._stops = response.stops.map((point) => coordinates(point) ? { latitude: point.latitude, longitude: point.longitude, label: String(point.label || "").slice(0, 300) } : { query: point.query.slice(0, 200) });
      if (Object.hasOwn(MODES, response.mode)) this.shadowRoot.querySelector("#import-mode").value = response.mode;
      this._showStops(); this._resolvedContext = this._capture();
      this._message(`Odczytano ${this._stops.length} miejsc.${MODES[response.mode] ? ` Wykryto: ${MODES[response.mode]}.` : " Wybierz sposób podróży."} Sprawdź miejsca, dzień, godzinę i czas podróży.`);
    } catch (error) { if (this._pending === request && this._matches(request)) this._failure(error); }
    finally { if (this._pending === request) this._pending = null; this._buttons(); }
  }
  _failure(error) {
    this._message(errorText(error));
    if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._context.onExpired?.(errorText(error));
  }
  async _build() {
    this.sync(); if (this._pending || !this._capture()) return;
    const value = (id) => this.shadowRoot.querySelector(`#import-${id}`).value;
    const mode = value("mode"), interval = Number(value("interval")), duration = value("duration") === "" ? null : Number(value("duration"));
    if (!Object.hasOwn(MODES, mode) || !INTERVALS.includes(interval) || !/^\d{4}-\d\d-\d\d$/.test(value("day")) || !/^\d\d:\d\d$/.test(value("start"))
      || (duration !== null && (!Number.isInteger(duration) || duration < 1 || duration > 1440))) { this._message("Wybierz transport, dzień, godzinę startu i odstęp. Czas podróży: puste pole albo 1–1440 minut."); return; }
    if (this._stops.length < 2 || this._stops.some((point) => !coordinates(point))) { this._message("Najpierw odczytaj link i wybierz dokładne miejsca."); return; }
    const request = this._capture(), settings = { day: value("day"), startTime: value("start") };
    this._pending = request; this._buttons(); this._message("Wyznaczanie trasy i orientacyjnych godzin punktów…");
    try {
      const response = await callWS(this._context.getState().hass, { type: "manual_location/route_import", token: request.token,
        points: this._stops.map(({ latitude, longitude }) => ({ latitude, longitude })), mode, interval_minutes: interval, duration_minutes: duration });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(true); return; }
      if (!Array.isArray(response?.points) || response.points.length < 2 || response.points.length > 500 || response.mode !== mode
        || !Number.isInteger(response.remaining) || !Number.isInteger(response.limit) || response.remaining < 0 || response.remaining > response.limit
        || typeof response.day !== "string"
        || !Number.isFinite(response.duration_seconds) || response.duration_seconds <= 0 || response.duration_seconds > 86400
        || response.points.some((point, index, points) => !coordinates(point) || !Number.isFinite(point.offset_seconds)
          || point.offset_seconds < 0 || (index === 0 ? point.offset_seconds !== 0 : point.offset_seconds <= points[index - 1].offset_seconds))
        || Math.abs(response.points.at(-1).offset_seconds - response.duration_seconds) > .001) throw { code: "route_invalid_response" };
      this._resolvedContext = null;
      try { this._context.onImport(response, settings); }
      catch (error) { this._message(error.message || "Nie można utworzyć szkicu dla wybranego dnia i godziny."); return; }
      this.shadowRoot.querySelector("#import-url").value = ""; this.cancel(true);
      this._message(`Utworzono szkic: ${response.points.length} punktów, około ${Math.round(response.duration_seconds / 60)} min · ${MODES[mode]}.${duration !== null ? " Użyto wpisanego czasu podróży." : " Czas oszacowało Geoapify."} Sprawdź podgląd i kliknij Zapisz plan; Start uruchamia trasę osobno. Pozostało ${response.remaining} z ${response.limit} jednostek limitu (${response.day}, UTC).`);
    } catch (error) { if (this._pending === request && this._matches(request)) this._failure(error); }
    finally { if (this._pending === request) this._pending = null; this._buttons(); }
  }
}

if (!customElements.get("manual-location-route-import")) customElements.define("manual-location-route-import", ManualLocationRouteImport);
