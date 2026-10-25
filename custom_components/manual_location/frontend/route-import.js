import { callWS, coordinates, errorText } from "./data.js?v=2.2.1";
import "./place-search.js?v=2.2.1";

const MODES = { walk: "Pieszo", drive: "Samochód", bicycle: "Rower", transit: "Komunikacja publiczna · szacunek" };
const INTERVALS = [1, 5, 10, 15, 20, 30, 45, 60, 90, 120];
const FIELDS = ["name", "source", "url", "mode", "day", "start", "spacing", "interval", "min", "max", "duration"];
const validStop = (value) => typeof value === "string" && value.trim().length > 0 && value.length <= 300 && !/\p{C}/u.test(value);

export class ManualLocationRouteImport extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this._generation = 0; this._stops = []; }
  get busy() { return !!this._pending; }
  get picking() { return !!this._mapPick; }
  get hasLocalChanges() {
    this._rememberQueries();
    return this.busy || this.picking || !!this._dirty || !!this.shadowRoot.querySelector("#import-url")?.value
      || this._stops.some((point) => coordinates(point) || point.query);
  }
  set context(context) {
    const changed = this._input !== context.input;
    if (changed && this._rendered) { this._rememberInput(); this.cancel(false); }
    this._context = context;
    this._input = context.input;
    if (this.isConnected && !this._rendered) this._render();
    else if (changed && this._rendered) this._restoreInput();
    else this.sync();
  }
  connectedCallback() { if (this._context && !this._rendered) this._render(); }
  disconnectedCallback() { this._rememberInput(); this.cancel(false); }
  _rememberInput() {
    if (!this._rendered || !this._input) return;
    this._rememberQueries();
    Object.assign(this._input, {
      fields: Object.fromEntries(FIELDS.map((id) => [id, this.shadowRoot.querySelector(`#import-${id}`).value])),
      stops: structuredClone(this._stops), open: this.shadowRoot.querySelector("details").open,
      dirty: !!this._dirty, replan: !!this._replan, startFromNow: !!this._startFromNow,
      lockedFields: this._lockedFields ? { ...this._lockedFields } : null, preparedInterval: this._preparedInterval,
    });
  }
  _restoreInput() {
    const saved = this._input?.fields ? structuredClone(this._input) : null;
    this.resetRoute();
    if (!saved) return;
    for (const id of FIELDS) if (typeof saved.fields[id] === "string") this.shadowRoot.querySelector(`#import-${id}`).value = saved.fields[id];
    this._stops = saved.stops || [];
    this._dirty = !!saved.dirty; this._replan = !!saved.replan; this._startFromNow = !!saved.startFromNow;
    this._lockedFields = saved.lockedFields; this._preparedInterval = saved.preparedInterval;
    this._showPreparedInterval();
    this.shadowRoot.querySelector("details").open = !!saved.open;
    this._showStops(); this._resolvedContext = this._capture(); this._buttons();
  }
  _showPreparedInterval() {
    this._intervalOption?.remove?.(); this._intervalOption = null;
    if (this._preparedInterval != null && !INTERVALS.includes(this._preparedInterval)) {
      const option = document.createElement("option"); option.value = String(this._preparedInterval); option.textContent = `${this._preparedInterval} min`;
      this.shadowRoot.querySelector("#import-interval").append(option); this._intervalOption = option;
      this.shadowRoot.querySelector("#import-interval").value = String(this._preparedInterval);
    }
  }
  prepareRoute(input) {
    if (!input || typeof input !== "object") return false;
    const { stops, routing: suppliedRouting, day, startTime, replan = false, startFromNow = false } = input;
    const routing = suppliedRouting === undefined ? { mode: "", interval_minutes: 9, interval_max_minutes: 15 } : suppliedRouting;
    if (!this._rendered || !this._capture() || !Array.isArray(stops) || stops.length < 2 || stops.length > 12
      || stops.some((point) => !coordinates(point) && typeof point?.query !== "string")
      || (replan && !coordinates(stops[0])) || (suppliedRouting !== undefined && !Object.hasOwn(MODES, routing?.mode))
      || !Number.isInteger(routing.interval_minutes) || routing.interval_minutes < 1 || routing.interval_minutes > 1440
      || (routing.interval_max_minutes != null && (!Number.isInteger(routing.interval_max_minutes)
        || routing.interval_max_minutes < routing.interval_minutes || routing.interval_max_minutes > 1440))
      || !/^\d{4}-\d\d-\d\d$/.test(day) || !/^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(startTime)) return false;
    this.cancel(true);
    const set = (id, value) => { this.shadowRoot.querySelector(`#import-${id}`).value = value; };
    set("source", "direct"); set("url", ""); set("mode", routing.mode); set("day", day); set("start", startTime); set("duration", "");
    const random = routing.interval_max_minutes != null;
    this._preparedInterval = random ? null : routing.interval_minutes;
    this._showPreparedInterval();
    set("spacing", random ? "random" : "fixed"); set("interval", String(routing.interval_minutes));
    set("min", String(routing.interval_minutes)); set("max", String(routing.interval_max_minutes ?? routing.interval_minutes));
    this._stops = stops.map((point) => coordinates(point) ? { latitude: point.latitude, longitude: point.longitude,
      label: String(point.label || "").slice(0, 300), ...(validStop(point.name) ? { name: point.name } : {}) } : { query: point.query });
    this._replan = !!replan; this._startFromNow = !!replan && startFromNow === true;
    this._lockedFields = replan ? { source: "direct", day, start: startTime } : null;
    this._dirty = true; this._showStops(); this._resolvedContext = this._capture(); this._buttons();
    this.shadowRoot.querySelector("details").open = true;
    this._rememberInput();
    this._message(replan ? `Przebudowa dalszej części: początek i jego czas są zablokowane. Zmień cel lub dodaj przystanki, wyznacz podgląd i zatwierdź go przez Zapisz plan.${this._startFromNow ? " Godzina początku zostanie odświeżona przy wyznaczeniu podglądu." : ""}` : "Miejsca przygotowane. Sprawdź ustawienia i wyznacz trasę.");
    return true;
  }
  resetRoute() {
    this._replan = false; this._startFromNow = false; this._lockedFields = null; this._preparedInterval = null; this._dirty = false;
    this._intervalOption?.remove?.(); this._intervalOption = null;
    if (!this._rendered) { this.cancel(true); return; }
    for (const [id, value] of [["name", ""], ["source", "direct"], ["url", ""], ["mode", ""], ["day", this._context.getState().day],
      ["start", ""], ["spacing", "random"], ["interval", "15"], ["min", "9"], ["max", "15"], ["duration", ""]]) {
      this.shadowRoot.querySelector(`#import-${id}`).value = value;
    }
    this.shadowRoot.querySelector("details").open = false;
    this.cancel(true);
  }
  _capture() {
    const state = this._context?.getState();
    if (!this.isConnected || !state?.allowed || !state.token || !state.hass.user?.is_admin || state.hass.connected === false) return null;
    this._rememberQueries();
    const fields = FIELDS.map((id) => this.shadowRoot.querySelector(`#import-${id}`)?.value || "");
    return { generation: this._generation, token: state.token, user: state.hass.user.id, connection: state.hass.connection,
      stamp: state.stamp, fields: JSON.stringify(fields), stops: JSON.stringify(this._stops) };
  }
  _matches(stamp) { const current = this._capture(); return !!current && Object.keys(current).every((key) => current[key] === stamp[key]); }
  sync() {
    const stamp = this._pending || this._mapPick?.stamp || this._resolvedContext;
    if (stamp && !this._matches(stamp)) this.cancel(false);
    this._buttons();
  }
  _rememberQueries() {
    this._searches?.forEach((search) => {
      const point = this._stops[search._stopIndex], input = search.shadowRoot?.querySelector("#place-query");
      if (point && !coordinates(point) && input) point.query = input.value;
    });
  }
  cancel(clearStops = false) {
    if (!clearStops) this._rememberQueries();
    this._generation++; this._pending = null; this._resolvedContext = null; this._mapPick = null;
    this._searches?.forEach((search) => search.cancel?.()); this._searches = [];
    if (clearStops) this._stops = this.shadowRoot.querySelector("#import-source")?.value === "direct" ? [{ query: "" }, { query: "" }] : [];
    if (this._rendered) {
      this._showStops();
      if (this._stops.length) this._resolvedContext = this._capture();
    }
    this._message(""); this._buttons();
  }
  cancelMapPick() { if (this.picking) this.cancel(false); }
  _pickOnMap(index, generation = this._generation) {
    if (generation !== this._generation || this._pending || !this._capture() || !this._stops[index] || (this._replan && index === 0)) return;
    const wasPicking = this._mapPick?.index === index;
    this.cancel(false);
    if (wasPicking) return;
    this._mapPick = { index, stamp: this._capture() };
    this._buttons(); this._context.onPickMap?.();
  }
  chooseMapPoint(point) {
    const pick = this._mapPick;
    if (!pick) return false;
    if (!this._matches(pick.stamp)) { this.cancel(false); return true; }
    const selected = coordinates(point);
    if (!selected) return true;
    this._stops[pick.index] = { latitude: selected.latitude, longitude: selected.longitude,
      label: pick.index === 0 ? "Początek z mapy" : pick.index === this._stops.length - 1 ? "Cel z mapy" : "Przystanek z mapy" };
    this._dirty = true; this.cancel(false);
    this._message("Miejsce wskazane na mapie. Wybierz pozostałe miejsca lub wyznacz trasę i sprawdź szkic.");
    return true;
  }
  _message(message) {
    const box = this.shadowRoot.querySelector("#import-status");
    if (box) { box.textContent = message; box.hidden = !message; }
  }
  _buttons() {
    if (!this._rendered) return;
    const allowed = !!this._capture(), busy = !!this._pending;
    const direct = this.shadowRoot.querySelector("#import-source").value === "direct";
    const random = this.shadowRoot.querySelector("#import-spacing").value === "random";
    this.shadowRoot.querySelector("#import-name-field").hidden = !!this._replan;
    for (const id of [...FIELDS, "resolve"]) this.shadowRoot.querySelector("#import-" + id).disabled = !allowed || busy;
    this.shadowRoot.querySelector("#import-link").hidden = direct;
    this.shadowRoot.querySelector("#import-fixed").hidden = random;
    this.shadowRoot.querySelector("#import-range-min").hidden = !random;
    this.shadowRoot.querySelector("#import-range-max").hidden = !random;
    this.shadowRoot.querySelector("#import-url").disabled ||= direct;
    this.shadowRoot.querySelector("#import-resolve").disabled ||= direct;
    this.shadowRoot.querySelector("#import-interval").disabled ||= random;
    this.shadowRoot.querySelector("#import-min").disabled ||= !random;
    this.shadowRoot.querySelector("#import-max").disabled ||= !random;
    if (this._replan) for (const id of ["source", "day", "start"]) this.shadowRoot.querySelector(`#import-${id}`).disabled = true;
    this.shadowRoot.querySelector("#import-add-stop").disabled = !allowed || busy || this._stops.length < 2 || this._stops.length >= 12;
    this.shadowRoot.querySelector("#import-cancel-replan").hidden = !this._replan;
    this.shadowRoot.querySelector("#import-cancel-replan").disabled = busy;
    this.shadowRoot.querySelector("#import-build").disabled = !allowed || busy || this.picking || this._stops.length < 2 || this._stops.some((point) => !coordinates(point));
    this.shadowRoot.querySelector("#import-resolve").setAttribute("aria-busy", String(busy));
    this._changeButtons?.forEach((button) => { button.disabled = !allowed || busy; });
    this._stopButtons?.forEach(({ button, enabled }) => { button.disabled = !allowed || busy || !enabled; });
    this._pickButtons?.forEach((button, index) => {
      button.disabled = !allowed || busy;
      button.setAttribute("aria-pressed", String(this._mapPick?.index === index));
    });
    this._searches?.forEach((search) => search.sync?.());
    this._context.onMapStateChange?.({ stops: allowed ? this._stops.flatMap((point, index) => coordinates(point) ? [{ ...point, index }] : []) : [],
      stopCount: this._stops.length, generation: this._generation, picking: allowed ? this._mapPick?.index ?? null : null });
    this._rememberInput();
  }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;border-bottom:1px solid var(--divider-color,#d9e2e0);font:inherit;color:var(--primary-text-color,#20343b)}*{box-sizing:border-box}details{padding:18px 22px}summary{font-weight:700;font-size:14px;cursor:pointer;min-height:24px}.body{margin-top:16px;max-width:900px}.link{display:flex;gap:9px;align-items:end}.link>div{flex:1;min-width:0}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,select,button{font:inherit;color:inherit;min-width:0;min-height:42px;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px;background:var(--input-fill-color,var(--card-background-color,#fff))}input,select{width:100%;padding:9px 11px}button{padding:9px 13px;font-size:13px;font-weight:600;cursor:pointer}button.primary{background:#176e82;border-color:#176e82;color:#fff}button:disabled,input:disabled,select:disabled{opacity:.5;cursor:default}input:focus,select:focus,button:focus-visible{outline:2px solid #338b9c;outline-offset:2px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}.grid>div{min-width:0}p{font-size:12px;line-height:1.6;margin:9px 0;color:var(--secondary-text-color,#657a82)}a{color:var(--primary-color,#176e82)}ol{list-style:none;margin:12px 0;padding:0}li{border-top:1px solid var(--divider-color,#d9e2e0);padding:12px 0;font-size:13px;overflow-wrap:anywhere}li strong,li small{display:block}li small{font-size:12px;line-height:1.5;margin-top:5px;color:var(--secondary-text-color,#657a82)}manual-location-place-search{display:block;margin-top:10px;border:1px solid var(--divider-color,#d9e2e0);border-radius:7px}#import-status{border-left:3px solid #176e82;padding:8px 12px;overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:600px){details{padding:16px 18px}.grid{grid-template-columns:1fr 1fr}.link{flex-wrap:wrap}.link>div{flex-basis:100%}.link>button{width:100%}}@media(max-width:360px){.grid{grid-template-columns:1fr}}
      #import-name-field{margin-bottom:16px}li>button{margin:8px 8px 0 0}button[aria-pressed=true]{background:#176e82;color:#fff;border-color:#176e82}
      </style><details><summary>Wyznacz lub importuj trasę</summary><div class="body">
      <div id="import-name-field"><label for="import-name">Nazwa wyznaczanej trasy</label><input id="import-name" maxlength="80" placeholder="np. Popołudniowa trasa"></div>
      <label for="import-source">Źródło trasy</label><select id="import-source"><option value="direct">Wyznacz we wtyczce</option><option value="google">Link Google Maps</option></select>
      <div class="link" id="import-link"><div><label for="import-url">Link do trasy Google Maps</label><input id="import-url" type="url" maxlength="8192" autocomplete="off" placeholder="https://maps.app.goo.gl/…"></div><button id="import-resolve" type="button">Odczytaj link</button></div>
      <p>Wybierz początek, cel i przystanki z wyników wyszukiwania lub wskaż je na mapie — możesz łączyć obie metody. Możesz też odczytać miejsca z linku Google Maps. Wyznaczenie tworzy tylko podgląd; zapis i Start zatwierdzasz osobno. Wyszukiwanie i wyznaczanie używają wspólnego limitu Geoapify.</p>
      <ol id="import-stops" aria-label="Miejsca trasy"></ol>
      <button id="import-add-stop" type="button">Dodaj przystanek pośredni</button>
      <div class="grid"><div><label for="import-mode">Sposób podróży</label><select id="import-mode"><option value="">Wybierz sposób podróży</option>${Object.entries(MODES).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></div><div><label for="import-day">Dzień trasy</label><input id="import-day" type="date"></div><div><label for="import-start">Godzina początku</label><input id="import-start" type="time" step="1"></div><div><label for="import-spacing">Odstępy punktów</label><select id="import-spacing"><option value="random">Losowe w przedziale</option><option value="fixed">Stały odstęp</option></select></div><div id="import-range-min"><label for="import-min">Od (minuty)</label><input id="import-min" type="number" min="1" max="1440" step="1" value="9"></div><div id="import-range-max"><label for="import-max">Do (minuty)</label><input id="import-max" type="number" min="1" max="1440" step="1" value="15"></div><div id="import-fixed"><label for="import-interval">Punkt co</label><select id="import-interval">${INTERVALS.map((value) => `<option value="${value}"${value === 15 ? " selected" : ""}>${value} min</option>`).join("")}</select></div><div><label for="import-duration">Własny czas podróży (min)</label><input id="import-duration" type="number" min="1" max="1440" step="1" placeholder="np. 70 · opcjonalnie"></div></div>
      <p>Losowe odstępy uwzględniają sekundy, np. 9 min 37 s. Ostatni odcinek do celu może być krótszy od minimum. Obowiązkowe przystanki dodawane są między pomiarami i także mogą skrócić odstęp. Godziny losowane są raz przy wyznaczaniu szkicu; późniejsza edycja nie losuje ich ponownie.</p>
      <p>Wpisany czas, np. z Google Maps, zastąpi szacunek Geoapify i proporcjonalnie dostosuje godziny punktów. Puste pole: czas według wybranego transportu w Geoapify. Dla pociągów i autobusów to przybliżenie, bez rozkładu, konkretnych kursów i gwarancji czasu przesiadek.</p>
      <p>Godziny według strefy Home Assistanta. Trasa musi zmieścić się w jednym dniu. Droga może różnić się od wybranego wariantu Google.</p>
      <button id="import-build" type="button" class="primary">Wyznacz trasę i utwórz szkic</button><button id="import-cancel-replan" type="button" hidden>Anuluj przebudowę</button><p id="import-status" role="status" aria-live="polite" hidden></p>
      <p><a href="https://www.geoapify.com/" target="_blank" rel="noopener noreferrer">Powered by Geoapify</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a> · wspólny limit zużycia z wyszukiwarką</p>
      </div></details>`;
    this._rendered = true;
    this.shadowRoot.querySelector("details").addEventListener("toggle", () => this._rememberInput());
    this.shadowRoot.querySelector("#import-day").value = this._context.getState().day;
    for (const [id, value] of [["source", "direct"], ["spacing", "random"], ["min", "9"], ["max", "15"], ["interval", "15"]]) this.shadowRoot.querySelector("#import-" + id).value = value;
    for (const id of FIELDS) {
      this.shadowRoot.querySelector("#import-" + id).addEventListener("input", () => {
        if (this._lockedFields && Object.hasOwn(this._lockedFields, id)) {
          this.shadowRoot.querySelector(`#import-${id}`).value = this._lockedFields[id]; return;
        }
        this._dirty = true; this.cancel(id === "url" || id === "source");
      });
    }
    this.shadowRoot.querySelector("#import-resolve").addEventListener("click", () => this._resolve());
    this.shadowRoot.querySelector("#import-build").addEventListener("click", () => this._build());
    this.shadowRoot.querySelector("#import-add-stop").addEventListener("click", () => this._addStop());
    this.shadowRoot.querySelector("#import-cancel-replan").addEventListener("click", () => {
      if (!this._replan || this._pending) return;
      this.resetRoute(); this._context.onCancelReplan?.();
    });
    this._restoreInput();
  }
  _showStops() {
    const list = this.shadowRoot.querySelector("#import-stops"); list.replaceChildren(); this._searches = []; this._changeButtons = []; this._pickButtons = []; this._stopButtons = [];
    const generation = this._generation;
    this._stops.forEach((point, index) => {
      const row = document.createElement("li"), label = document.createElement("strong"), detail = document.createElement("small");
      label.textContent = `${index + 1}. ${point.label || point.query || (index === 0 ? "Początek" : index === this._stops.length - 1 ? "Cel" : "Przystanek")}`;
      detail.textContent = coordinates(point) ? `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)} · sprawdź na podglądzie trasy` : "Wybierz dokładne miejsce z wyników wyszukiwania lub wskaż je na mapie.";
      row.append(label, detail); list.append(row);
      if (this._replan && index === 0) {
        detail.textContent += " · początek przebudowy, zablokowany";
      } else {
        const pick = document.createElement("button"); pick.type = "button";
        pick.textContent = index === 0 ? "Wskaż początek" : index === this._stops.length - 1 ? "Wskaż cel" : "Wskaż przystanek";
        pick.setAttribute("aria-label", `Wskaż na mapie miejsce ${index + 1}`);
        pick.addEventListener("click", () => this._pickOnMap(index, generation)); row.append(pick); this._pickButtons[index] = pick;
      }
      if (coordinates(point) && !(this._replan && index === 0)) {
        const change = document.createElement("button"); change.type = "button"; change.textContent = "Zmień miejsce";
        change.setAttribute("aria-label", "Zmień miejsce " + (index + 1));
        change.addEventListener("click", () => this._changeStop(index, generation)); row.append(change); this._changeButtons.push(change);
      } else if (!coordinates(point)) {
        const search = document.createElement("manual-location-place-search"); search._stopIndex = index; row.append(search);
        const rawQuery = point.query || "";
        search.context = {
          getState: () => { const state = this._context.getState(); return { ...state, allowed: !!this._capture() && !this._pending && generation === this._generation,
            stamp: JSON.stringify([state.stamp, generation, index, point.query]) }; },
          onSelect: (selected) => {
            if (generation !== this._generation || !this._capture()) return false;
            if (!coordinates(selected)) return false;
            this._rememberQueries();
            this._stops[index] = { latitude: selected.latitude, longitude: selected.longitude, label: String(selected.label || "").slice(0, 300) };
            this._dirty = true; this.cancel(false); this._context.onShowStops?.(); return true;
          }, onExpired: (message, code) => this._context.onExpired?.(message, code),
        };
        const query = search.shadowRoot?.querySelector("#place-query");
        if (query) {
          query.value = rawQuery;
          query.addEventListener("input", () => {
            if (generation !== this._generation) return;
            point.query = query.value; this._dirty = true; this._resolvedContext = null; this._mapPick = null; this._buttons();
          });
        }
        this._searches.push(search);
      }
      if (index > 0 && index < this._stops.length - 1) {
        for (const [text, enabled, action] of [["W górę", index > 1, () => this._moveStop(index, -1, generation)],
          ["W dół", index < this._stops.length - 2, () => this._moveStop(index, 1, generation)],
          ["Usuń przystanek", true, () => this._removeStop(index, generation)]]) {
          const button = document.createElement("button"); button.type = "button"; button.textContent = text;
          button.setAttribute("aria-label", `${text}: miejsce ${index + 1}`);
          button.addEventListener("click", action); row.append(button); this._stopButtons.push({ button, enabled });
        }
      }
    });
  }
  _editStops(change) {
    this._rememberQueries();
    this._searches?.forEach((search) => search.cancel?.()); this._searches = [];
    change();
    this._dirty = true; this.cancel(false);
  }
  _changeStop(index, generation = this._generation) {
    if (generation !== this._generation || this._pending || !this._capture() || !this._stops[index] || (this._replan && index === 0)) return;
    this._editStops(() => { this._stops[index] = { query: this._stops[index].label || this._stops[index].query || "" }; });
  }
  _addStop() {
    if (this._pending || !this._capture() || this._stops.length < 2 || this._stops.length >= 12) return;
    this._editStops(() => this._stops.splice(this._stops.length - 1, 0, { query: "" }));
    this._searches.find((search) => search._stopIndex === this._stops.length - 2)?.shadowRoot?.querySelector("#place-query")?.focus?.();
  }
  _removeStop(index, generation = this._generation) {
    if (generation !== this._generation || this._pending || !this._capture() || index < 1 || index >= this._stops.length - 1) return;
    this._editStops(() => this._stops.splice(index, 1));
  }
  _moveStop(index, delta, generation = this._generation) {
    const destination = index + delta;
    if (generation !== this._generation || this._pending || !this._capture() || ![-1, 1].includes(delta)
      || index < 1 || index >= this._stops.length - 1 || destination < 1 || destination >= this._stops.length - 1) return;
    this._editStops(() => { [this._stops[index], this._stops[destination]] = [this._stops[destination], this._stops[index]]; });
  }
  async _resolve() {
    this.sync(); if (this.shadowRoot.querySelector("#import-source").value !== "google" || this._pending || !this._capture()) return;
    const url = this.shadowRoot.querySelector("#import-url").value.trim();
    if (!url.startsWith("https://") || url.length > 8192) { this._message("Wklej pełny link HTTPS do trasy Google Maps, także skrócony maps.app.goo.gl."); return; }
    this.cancel(true); const request = this._capture(); if (!request) return;
    this._pending = request; this._buttons(); this._message("Odczytywanie miejsc i sposobu podróży z linku…");
    try {
      const response = await callWS(this._context.getState().hass, { type: "manual_location/route_resolve", token: request.token, url });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(false); return; }
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
    if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._context.onExpired?.(errorText(error), error.code);
  }
  async _build() {
    this.sync(); if (this._pending || !this._capture()) return;
    const value = (id) => this.shadowRoot.querySelector(`#import-${id}`).value;
    const startedAt = Date.now();
    if (this._replan && this._startFromNow) {
      try {
        const timeZone = this._context.getState().timeZone;
        if (typeof timeZone !== "string" || !timeZone) throw new Error();
        const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone, hourCycle: "h23", year: "numeric",
          month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(startedAt).map(({ type, value }) => [type, value]));
        if (`${parts.year}-${parts.month}-${parts.day}` !== this._lockedFields.day) {
          this._message("Zmienił się dzień w Home Assistant. Przygotuj przebudowę od teraz ponownie dla dzisiejszego dnia."); return;
        }
        const start = `${parts.hour}:${parts.minute}:${parts.second}`;
        this.shadowRoot.querySelector("#import-start").value = start; this._lockedFields.start = start;
        this._resolvedContext = this._capture();
      } catch { this._message("Nie można odczytać godziny w strefie Home Assistanta. Sprawdź ustawienia i przygotuj przebudowę ponownie."); return; }
    }
    const mode = value("mode"), random = value("spacing") === "random";
    const interval = Number(value(random ? "min" : "interval")), maximum = random ? Number(value("max")) : null;
    const duration = value("duration") === "" ? null : Number(value("duration"));
    if (value("name").trim().length > 80) { this._message("Nazwa trasy może mieć najwyżej 80 znaków."); return; }
    if (!["direct", "google"].includes(value("source")) || !["random", "fixed"].includes(value("spacing"))
      || !Object.hasOwn(MODES, mode) || (random ? !Number.isInteger(interval) || interval < 1 || interval > 1440 || !Number.isInteger(maximum) || maximum < interval || maximum > 1440 : !INTERVALS.includes(interval) && interval !== this._preparedInterval)
      || !/^\d{4}-\d\d-\d\d$/.test(value("day")) || !/^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(value("start"))
      || (duration !== null && (!Number.isInteger(duration) || duration < 1 || duration > 1440))) { this._message("Wybierz transport, dzień i godzinę startu. Losowy przedział: całe minuty 1–1440, Do nie mniejsze od Od. Czas podróży: puste pole albo 1–1440 minut."); return; }
    if (this.picking) { this._message("Wskaż miejsce na mapie albo anuluj wskazywanie przed wyznaczeniem trasy."); return; }
    if (this._stops.length < 2 || this._stops.length > 12 || this._stops.some((point) => !coordinates(point))) { this._message("Wybierz dokładny początek i cel oraz wszystkie przystanki z wyników wyszukiwania, wskaż je na mapie lub odczytaj miejsca z linku. Maksymalnie 12 miejsc."); return; }
    const request = this._capture(), routing = { mode, interval_minutes: interval, ...(random ? { interval_max_minutes: maximum } : {}) };
    const settings = { name: value("name").trim(), day: value("day"), startTime: value("start"), routing, startedAt }, replan = this._replan;
    this._pending = request; this._buttons(); this._message("Wyznaczanie trasy i orientacyjnych godzin punktów…");
    try {
      const response = await callWS(this._context.getState().hass, { type: "manual_location/route_import", token: request.token,
        points: this._stops.map((point, index) => ({ latitude: point.latitude, longitude: point.longitude,
          stop: validStop(point.label) ? point.label : index === 0 ? "Początek" : index === this._stops.length - 1 ? "Cel" : `Przystanek ${index}` })),
        ...routing, duration_minutes: duration });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(false); return; }
      if (!Array.isArray(response?.points) || response.points.length < 2 || response.points.length > 500 || response.mode !== mode
        || !Number.isInteger(response.remaining) || !Number.isInteger(response.limit) || response.remaining < 0 || response.remaining > response.limit
        || typeof response.day !== "string"
        || !Number.isFinite(response.duration_seconds) || response.duration_seconds <= 0 || response.duration_seconds > 86400
        || response.points.some((point, index, points) => !coordinates(point) || !Number.isFinite(point.offset_seconds)
          || (point.stop !== undefined && !validStop(point.stop))
          || point.offset_seconds < 0 || (index === 0 ? point.offset_seconds !== 0 : point.offset_seconds <= points[index - 1].offset_seconds
            || new Date(settings.startedAt + point.offset_seconds * 1000).getTime() <= new Date(settings.startedAt + points[index - 1].offset_seconds * 1000).getTime()))
        || !validStop(response.points[0].stop) || !validStop(response.points.at(-1).stop)
        || response.points.filter((point) => point.stop !== undefined).length < this._stops.length
        || (random && (!Number.isInteger(response.duration_seconds) || response.points.some((point) => !Number.isInteger(point.offset_seconds))))
        || Math.abs(response.points.at(-1).offset_seconds - response.duration_seconds) > .001) throw { code: "route_invalid_response" };
      this._resolvedContext = null;
      // Names belong to the selected place object, which moves with reorder and
      // is replaced when the user chooses a different place. Measurements never
      // inherit a stop name merely because they happen to have its coordinates.
      const namedStops = this._stops.some((point) => point.name);
      if (namedStops) {
        const stops = response.points.filter((point) => point.stop !== undefined);
        if (stops.length !== this._stops.length) throw { code: "route_invalid_response" };
        stops.forEach((point, index) => {
          const name = this._stops[index].name;
          if (name) point.name = name;
        });
      }
      try { this._context.onImport(response, settings); }
      catch (error) { this._message(error.message || "Nie można utworzyć szkicu dla wybranego dnia i godziny."); return; }
      this.resetRoute();
      const seconds = Math.round(response.duration_seconds);
      this._message(`${replan ? "Przebudowano dalszą trasę w szkicu" : "Utworzono szkic"}: ${response.points.length} punktów, około ${Math.floor(seconds / 60)} min ${seconds % 60} s · ${MODES[mode]}.${duration !== null ? " Użyto wpisanego czasu podróży." : " Czas oszacowało Geoapify."} ${replan ? "Sprawdź podgląd. Zapisz plan zatwierdzi przebudowę; Start pozostaje osobnym przyciskiem" : "Sprawdź podgląd i kliknij Zapisz plan; Start uruchamia trasę osobno"}. Pozostało ${response.remaining} z ${response.limit} jednostek limitu (${response.day}, UTC).`);
    } catch (error) { if (this._pending === request && this._matches(request)) this._failure(error); }
    finally { if (this._pending === request) this._pending = null; this._buttons(); }
  }
}

if (!customElements.get("manual-location-route-import")) customElements.define("manual-location-route-import", ManualLocationRouteImport);
