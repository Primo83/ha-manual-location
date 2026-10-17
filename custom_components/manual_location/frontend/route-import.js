import { callWS, coordinates, errorText } from "./data.js?v=0.1.21";
import "./place-search.js?v=0.1.21";

const MODES = { walk: "Pieszo", drive: "Samochód", bicycle: "Rower", transit: "Komunikacja publiczna · szacunek" };
const INTERVALS = [1, 5, 10, 15, 20, 30, 45, 60, 90, 120];
const FIELDS = ["source", "url", "mode", "day", "start", "spacing", "interval", "min", "max", "duration"];

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
    this._context = context;
    if (this.isConnected && !this._rendered) this._render();
    else this.sync();
  }
  connectedCallback() { if (this._context && !this._rendered) this._render(); }
  disconnectedCallback() { this.cancel(true); }
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
  _pickOnMap(index) {
    if (this._pending || !this._capture() || this.shadowRoot.querySelector("#import-source").value !== "direct" || ![0, 1].includes(index)) return;
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
    this._stops[pick.index] = { latitude: selected.latitude, longitude: selected.longitude, label: pick.index === 0 ? "Początek z mapy" : "Cel z mapy" };
    this._dirty = true; this.cancel(false);
    this._message("Miejsce wskazane na mapie. Wybierz drugi koniec lub kliknij Wyznacz trasę i utwórz szkic.");
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
    this.shadowRoot.querySelector("#import-build").disabled = !allowed || busy || this.picking || this._stops.length < 2 || this._stops.some((point) => !coordinates(point));
    this.shadowRoot.querySelector("#import-resolve").setAttribute("aria-busy", String(busy));
    this._changeButtons?.forEach((button) => { button.disabled = !allowed || busy; });
    this._pickButtons?.forEach((button, index) => {
      button.disabled = !allowed || busy;
      button.setAttribute("aria-pressed", String(this._mapPick?.index === index));
    });
    this._searches?.forEach((search) => search.sync?.());
    this._context.onMapStateChange?.({ stops: allowed && direct ? this._stops.filter((point) => coordinates(point)).map((point) => ({ ...point, index: this._stops.indexOf(point) })) : [],
      picking: allowed ? this._mapPick?.index ?? null : null });
  }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;border-bottom:1px solid var(--divider-color,#d9e2e0);font:inherit;color:var(--primary-text-color,#20343b)}*{box-sizing:border-box}details{padding:18px 22px}summary{font-weight:700;font-size:14px;cursor:pointer;min-height:24px}.body{margin-top:16px;max-width:900px}.link{display:flex;gap:9px;align-items:end}.link>div{flex:1;min-width:0}label{display:block;font-size:13px;font-weight:600;margin:0 0 7px}input,select,button{font:inherit;color:inherit;min-width:0;min-height:42px;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px;background:var(--input-fill-color,var(--card-background-color,#fff))}input,select{width:100%;padding:9px 11px}button{padding:9px 13px;font-size:13px;font-weight:600;cursor:pointer}button.primary{background:#176e82;border-color:#176e82;color:#fff}button:disabled,input:disabled,select:disabled{opacity:.5;cursor:default}input:focus,select:focus,button:focus-visible{outline:2px solid #338b9c;outline-offset:2px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:16px 0}.grid>div{min-width:0}p{font-size:12px;line-height:1.6;margin:9px 0;color:var(--secondary-text-color,#657a82)}a{color:var(--primary-color,#176e82)}ol{list-style:none;margin:12px 0;padding:0}li{border-top:1px solid var(--divider-color,#d9e2e0);padding:12px 0;font-size:13px;overflow-wrap:anywhere}li strong,li small{display:block}li small{font-size:12px;line-height:1.5;margin-top:5px;color:var(--secondary-text-color,#657a82)}manual-location-place-search{display:block;margin-top:10px;border:1px solid var(--divider-color,#d9e2e0);border-radius:7px}#import-status{border-left:3px solid #176e82;padding:8px 12px;overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:600px){details{padding:16px 18px}.grid{grid-template-columns:1fr 1fr}.link{flex-wrap:wrap}.link>div{flex-basis:100%}.link>button{width:100%}}@media(max-width:360px){.grid{grid-template-columns:1fr}}
      li>button{margin:8px 8px 0 0}button[aria-pressed=true]{background:#176e82;color:#fff;border-color:#176e82}
      </style><details><summary>Wyznacz lub importuj trasę</summary><div class="body">
      <label for="import-source">Źródło trasy</label><select id="import-source"><option value="direct">Wyznacz we wtyczce</option><option value="google">Link Google Maps</option></select>
      <div class="link" id="import-link"><div><label for="import-url">Link do trasy Google Maps</label><input id="import-url" type="url" maxlength="8192" autocomplete="off" placeholder="https://maps.app.goo.gl/…"></div><button id="import-resolve" type="button">Odczytaj link</button></div>
      <p>Wybierz początek i cel z wyników wyszukiwania lub wskaż je na mapie — możesz łączyć obie metody. Możesz też odczytać miejsca z linku Google Maps. Wyznaczenie utworzy nowy szkic; zapis i Start zatwierdzasz osobno. Wyszukiwanie i wyznaczanie używają wspólnego limitu Geoapify.</p>
      <ol id="import-stops" aria-label="Miejsca trasy"></ol>
      <div class="grid"><div><label for="import-mode">Sposób podróży</label><select id="import-mode"><option value="">Wybierz sposób podróży</option>${Object.entries(MODES).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></div><div><label for="import-day">Dzień trasy</label><input id="import-day" type="date"></div><div><label for="import-start">Godzina początku</label><input id="import-start" type="time" step="1"></div><div><label for="import-spacing">Odstępy punktów</label><select id="import-spacing"><option value="random">Losowe w przedziale</option><option value="fixed">Stały odstęp</option></select></div><div id="import-range-min"><label for="import-min">Od (minuty)</label><input id="import-min" type="number" min="1" max="1440" step="1" value="9"></div><div id="import-range-max"><label for="import-max">Do (minuty)</label><input id="import-max" type="number" min="1" max="1440" step="1" value="15"></div><div id="import-fixed"><label for="import-interval">Punkt co</label><select id="import-interval">${INTERVALS.map((value) => `<option value="${value}"${value === 15 ? " selected" : ""}>${value} min</option>`).join("")}</select></div><div><label for="import-duration">Własny czas podróży (min)</label><input id="import-duration" type="number" min="1" max="1440" step="1" placeholder="np. 70 · opcjonalnie"></div></div>
      <p>Losowe odstępy uwzględniają sekundy, np. 9 min 37 s. Ostatni odcinek do celu może być krótszy od minimum. Godziny losowane są raz przy wyznaczaniu szkicu; późniejsza edycja nie losuje ich ponownie.</p>
      <p>Wpisany czas, np. z Google Maps, zastąpi szacunek Geoapify i proporcjonalnie dostosuje godziny punktów. Puste pole: czas według wybranego transportu w Geoapify. Dla pociągów i autobusów to przybliżenie, bez rozkładu, konkretnych kursów i gwarancji czasu przesiadek.</p>
      <p>Godziny według strefy Home Assistanta. Trasa musi zmieścić się w jednym dniu. Droga może różnić się od wybranego wariantu Google.</p>
      <button id="import-build" type="button" class="primary">Wyznacz trasę i utwórz szkic</button><p id="import-status" role="status" aria-live="polite" hidden></p>
      <p><a href="https://www.geoapify.com/" target="_blank" rel="noopener noreferrer">Powered by Geoapify</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a> · wspólny limit zużycia z wyszukiwarką</p>
      </div></details>`;
    this._rendered = true;
    this.shadowRoot.querySelector("#import-day").value = this._context.getState().day;
    for (const [id, value] of [["source", "direct"], ["spacing", "random"], ["min", "9"], ["max", "15"], ["interval", "15"]]) this.shadowRoot.querySelector("#import-" + id).value = value;
    for (const id of FIELDS) {
      this.shadowRoot.querySelector("#import-" + id).addEventListener("input", () => {
        this._dirty = true; this.cancel(id === "url" || id === "source");
      });
    }
    this.shadowRoot.querySelector("#import-resolve").addEventListener("click", () => this._resolve());
    this.shadowRoot.querySelector("#import-build").addEventListener("click", () => this._build());
    this.cancel(true);
  }
  _showStops() {
    const list = this.shadowRoot.querySelector("#import-stops"); list.replaceChildren(); this._searches = []; this._changeButtons = []; this._pickButtons = [];
    this._stops.forEach((point, index) => {
      const row = document.createElement("li"), label = document.createElement("strong"), detail = document.createElement("small");
      label.textContent = `${index + 1}. ${point.label || point.query || (index === 0 ? "Początek" : index === this._stops.length - 1 ? "Cel" : "Przystanek")}`;
      detail.textContent = coordinates(point) ? `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)} · sprawdź na podglądzie trasy` : "Wybierz dokładne miejsce z wyników wyszukiwania lub wskaż je na mapie.";
      row.append(label, detail); list.append(row);
      if (this.shadowRoot.querySelector("#import-source").value === "direct") {
        const pick = document.createElement("button"); pick.type = "button";
        pick.textContent = index === 0 ? "Wskaż początek" : "Wskaż cel";
        pick.addEventListener("click", () => this._pickOnMap(index)); row.append(pick); this._pickButtons.push(pick);
      }
      if (coordinates(point)) {
        const change = document.createElement("button"); change.type = "button"; change.textContent = "Zmień miejsce";
        change.setAttribute("aria-label", "Zmień miejsce " + (index + 1));
        change.addEventListener("click", () => this._changeStop(index)); row.append(change); this._changeButtons.push(change);
      } else {
        const search = document.createElement("manual-location-place-search"); search._stopIndex = index; row.append(search);
        const rawQuery = point.query || "";
        const generation = this._generation;
        search.context = {
          getState: () => { const state = this._context.getState(); return { ...state, allowed: !!this._capture() && !this._pending && generation === this._generation,
            stamp: JSON.stringify([state.stamp, generation, index, point.query]) }; },
          onSelect: (selected) => {
            if (generation !== this._generation || !this._capture()) return false;
            if (!coordinates(selected)) return false;
            this._rememberQueries();
            this._stops[index] = { latitude: selected.latitude, longitude: selected.longitude, label: String(selected.label || "").slice(0, 300) };
            this._dirty = true; this.cancel(false); this._context.onShowStops?.(); return true;
          }, onExpired: (message) => this._context.onExpired?.(message),
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
    });
  }
  _changeStop(index) {
    if (this._pending || !this._capture() || !this._stops[index]) return;
    this._rememberQueries();
    this._stops[index] = { query: this._stops[index].label || this._stops[index].query || "" };
    this._dirty = true; this.cancel(false);
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
    if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._context.onExpired?.(errorText(error));
  }
  async _build() {
    this.sync(); if (this._pending || !this._capture()) return;
    const value = (id) => this.shadowRoot.querySelector(`#import-${id}`).value;
    const mode = value("mode"), random = value("spacing") === "random";
    const interval = Number(value(random ? "min" : "interval")), maximum = random ? Number(value("max")) : null;
    const duration = value("duration") === "" ? null : Number(value("duration"));
    if (!["direct", "google"].includes(value("source")) || !["random", "fixed"].includes(value("spacing"))
      || !Object.hasOwn(MODES, mode) || (random ? !Number.isInteger(interval) || interval < 1 || interval > 1440 || !Number.isInteger(maximum) || maximum < interval || maximum > 1440 : !INTERVALS.includes(interval))
      || !/^\d{4}-\d\d-\d\d$/.test(value("day")) || !/^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(value("start"))
      || (duration !== null && (!Number.isInteger(duration) || duration < 1 || duration > 1440))) { this._message("Wybierz transport, dzień i godzinę startu. Losowy przedział: całe minuty 1–1440, Do nie mniejsze od Od. Czas podróży: puste pole albo 1–1440 minut."); return; }
    if (this.picking) { this._message("Wskaż miejsce na mapie albo anuluj wskazywanie przed wyznaczeniem trasy."); return; }
    if (this._stops.length < 2 || this._stops.some((point) => !coordinates(point))) { this._message("Wybierz dokładny początek i cel z wyników wyszukiwania, wskaż je na mapie lub odczytaj miejsca z linku."); return; }
    const request = this._capture(), settings = { day: value("day"), startTime: value("start") };
    this._pending = request; this._buttons(); this._message("Wyznaczanie trasy i orientacyjnych godzin punktów…");
    try {
      const response = await callWS(this._context.getState().hass, { type: "manual_location/route_import", token: request.token,
        points: this._stops.map(({ latitude, longitude }) => ({ latitude, longitude })), mode, interval_minutes: interval, ...(random ? { interval_max_minutes: maximum } : {}), duration_minutes: duration });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(false); return; }
      if (!Array.isArray(response?.points) || response.points.length < 2 || response.points.length > 500 || response.mode !== mode
        || !Number.isInteger(response.remaining) || !Number.isInteger(response.limit) || response.remaining < 0 || response.remaining > response.limit
        || typeof response.day !== "string"
        || !Number.isFinite(response.duration_seconds) || response.duration_seconds <= 0 || response.duration_seconds > 86400
        || response.points.some((point, index, points) => !coordinates(point) || !Number.isFinite(point.offset_seconds)
          || point.offset_seconds < 0 || (index === 0 ? point.offset_seconds !== 0 : point.offset_seconds <= points[index - 1].offset_seconds))
        || (random && (!Number.isInteger(response.duration_seconds) || response.points.some((point) => !Number.isInteger(point.offset_seconds))))
        || Math.abs(response.points.at(-1).offset_seconds - response.duration_seconds) > .001) throw { code: "route_invalid_response" };
      this._resolvedContext = null;
      try { this._context.onImport(response, settings); }
      catch (error) { this._message(error.message || "Nie można utworzyć szkicu dla wybranego dnia i godziny."); return; }
      this.shadowRoot.querySelector("#import-url").value = ""; this._dirty = false; this.cancel(true);
      const seconds = Math.round(response.duration_seconds);
      this._message(`Utworzono szkic: ${response.points.length} punktów, około ${Math.floor(seconds / 60)} min ${seconds % 60} s · ${MODES[mode]}.${duration !== null ? " Użyto wpisanego czasu podróży." : " Czas oszacowało Geoapify."} Sprawdź podgląd i kliknij Zapisz plan; Start uruchamia trasę osobno. Pozostało ${response.remaining} z ${response.limit} jednostek limitu (${response.day}, UTC).`);
    } catch (error) { if (this._pending === request && this._matches(request)) this._failure(error); }
    finally { if (this._pending === request) this._pending = null; this._buttons(); }
  }
}

if (!customElements.get("manual-location-route-import")) customElements.define("manual-location-route-import", ManualLocationRouteImport);
