import { callWS, coordinates, errorText } from "./data.js?v=2.0.2";

const TYPES = { amenity: "Obiekt / miejsce", building: "Adres / budynek", street: "Ulica", suburb: "Dzielnica",
  district: "Dzielnica", postcode: "Kod pocztowy", city: "Miejscowość", county: "Powiat", state: "Region", country: "Kraj" };

export class ManualLocationPlaceSearch extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this._generation = 0; this._results = []; }
  set context(context) {
    this._context = context;
    if (this.isConnected && !this._rendered) this._render();
    else this.sync();
  }
  connectedCallback() { if (this._context && !this._rendered) this._render(); }
  disconnectedCallback() { this.cancel(); }
  _capture() {
    const state = this._context?.getState();
    if (!this.isConnected || !state?.allowed || !state.token || !state.hass.user?.is_admin || state.hass.connected === false) return null;
    return { generation: this._generation, token: state.token, user: state.hass.user.id, connection: state.hass.connection,
      stamp: state.stamp, query: this.shadowRoot.querySelector("#place-query")?.value || "" };
  }
  _matches(stamp) {
    const current = this._capture();
    return !!current && Object.keys(current).every((key) => current[key] === stamp[key]);
  }
  sync() {
    const stamp = this._pending || this._resultContext;
    if (stamp && !this._matches(stamp)) this.cancel();
    this._buttons();
  }
  cancel() {
    this._generation++; this._pending = null; this._resultContext = null; this._results = [];
    this.shadowRoot.querySelector("#place-results")?.replaceChildren();
    const attribution = this.shadowRoot.querySelector("#place-extra-attribution"); if (attribution) attribution.textContent = "";
    this._message(""); this._buttons();
  }
  _buttons() {
    const allowed = !!this._capture(), input = this.shadowRoot.querySelector("#place-query"), button = this.shadowRoot.querySelector("#place-search");
    if (input) input.disabled = !allowed;
    if (button) { button.disabled = !allowed || !!this._pending; button.setAttribute("aria-busy", String(!!this._pending)); }
  }
  _message(message) { const box = this.shadowRoot.querySelector("#place-status"); if (box) { box.textContent = message; box.hidden = !message; } }
  _render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;padding:16px 18px;border-bottom:1px solid var(--divider-color,#d9e2e0);font:inherit;color:var(--primary-text-color,#20343b)}*{box-sizing:border-box}label{display:block;font-size:13px;font-weight:600;margin-bottom:7px}form{display:flex;gap:8px;align-items:end}form>div{min-width:0;flex:1}input,button{font:inherit;color:inherit;min-height:42px;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px;background:var(--input-fill-color,var(--card-background-color,#fff))}input{width:100%;min-width:0;padding:9px 11px}button{white-space:nowrap;padding:9px 13px;font-size:13px;font-weight:600;cursor:pointer}button:disabled,input:disabled{opacity:.5;cursor:default}button:hover:not(:disabled){background:#63889a16}input:focus,button:focus-visible{outline:2px solid #338b9c;outline-offset:2px}p{font-size:12px;line-height:1.5;margin:9px 0 0;color:var(--secondary-text-color,#657a82)}a{color:var(--primary-color,#176e82)}ol{padding:0;margin:10px 0 0;list-style:none}li+li{margin-top:7px}li button{width:100%;text-align:left;white-space:normal;overflow-wrap:anywhere}strong,small{display:block}strong{font-size:13px;line-height:1.5}small{font-size:12px;font-weight:400;line-height:1.5;margin-top:4px;color:var(--secondary-text-color,#657a82)}#place-status{overflow-wrap:anywhere}#place-extra-attribution{overflow-wrap:anywhere}[hidden]{display:none!important}@media(max-width:400px){:host{padding:14px}form{flex-wrap:wrap}form>div{flex-basis:200px}form>button{flex:1}}
      </style><form id="place-form"><div><label for="place-query">Nazwa miejsca lub pełny adres</label><input id="place-query" type="search" minlength="3" maxlength="200" autocomplete="off" placeholder="np. Kraków, Rynek Główny 1"></div><button id="place-search" type="submit">Szukaj</button></form><p>Tekst zapytania jest wysyłany do Geoapify dopiero po kliknięciu Szukaj lub Enter. Wybór miejsca uzupełnia współrzędne; zapis zatwierdzasz osobno.</p><p id="place-status" role="status" aria-live="polite" hidden></p><ol id="place-results" aria-label="Wyniki wyszukiwania miejsc"></ol><p><a href="https://www.geoapify.com/" target="_blank" rel="noopener noreferrer">Powered by Geoapify</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a> · <a href="/config/integrations/integration/manual_location">Konfiguracja Geoapify w HA</a></p><p id="place-extra-attribution"></p>`;
    this._rendered = true;
    this.shadowRoot.querySelector("#place-query").addEventListener("input", () => this.cancel());
    this.shadowRoot.querySelector("#place-form").addEventListener("submit", (event) => { event.preventDefault(); return this._search(); });
    this._buttons();
  }
  async _search() {
    this.sync();
    if (this._pending || !this._capture()) return;
    const raw = this.shadowRoot.querySelector("#place-query").value, query = raw.trim().replace(/\s+/g, " ");
    if (query.length < 3 || query.length > 200 || /[\u0000-\u001f\u007f]/.test(raw)) { this._message("Wpisz nazwę miejsca albo adres: od 3 do 200 znaków."); return; }
    this.cancel(); this._context.onBeforeSearch?.();
    const request = this._capture(); if (!request) return;
    this._pending = request; this._buttons(); this._message("Wyszukiwanie miejsc…");
    try {
      const state = this._context.getState();
      const response = await callWS(state.hass, { type: "manual_location/geocode", token: request.token, query });
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(); return; }
      if (!Array.isArray(response?.results) || !Number.isInteger(response.remaining) || !Number.isInteger(response.limit)
        || response.remaining < 0 || response.limit < response.remaining || typeof response.day !== "string") throw { code: "geocoder_unavailable" };
      this._results = response.results.slice(0, 5).filter((result) => coordinates(result) && typeof result.label === "string" && result.label.trim())
        .map((result) => ({ latitude: result.latitude, longitude: result.longitude, label: result.label.slice(0, 300),
          result_type: result.result_type, housenumber: typeof result.housenumber === "string" ? result.housenumber.slice(0, 80) : "",
          attribution: typeof result.attribution === "string" ? result.attribution.slice(0, 200) : "" }));
      this._pending = null; this._resultContext = request;
      const list = this.shadowRoot.querySelector("#place-results"); list.replaceChildren();
      this._results.forEach((result, index) => {
        const row = document.createElement("li"), button = document.createElement("button"), label = document.createElement("strong"), detail = document.createElement("small");
        button.type = "button"; label.textContent = result.label;
        detail.textContent = result.result_type === "street" ? "Ulica · numer budynku niepotwierdzony"
          : `${TYPES[result.result_type] || "Miejsce"}${result.housenumber ? ` · nr ${result.housenumber}` : result.result_type === "building" ? " · numer budynku niepotwierdzony" : ""}`;
        button.append(label, detail); button.addEventListener("click", () => this._select(index, request)); row.append(button); list.append(row);
      });
      this.shadowRoot.querySelector("#place-extra-attribution").textContent = [...new Set(this._results.map((result) => result.attribution).filter(Boolean))].join(" · ");
      this._message(`${this._results.length ? "Wybierz miejsce z listy." : "Nie znaleziono miejsca. Doprecyzuj adres lub wybierz punkt na mapie."} Pozostało ${response.remaining} z ${response.limit} jednostek wspólnego limitu integracji (${response.day}, UTC).`);
    } catch (error) {
      if (this._pending !== request) return;
      if (!this._matches(request)) { this.cancel(); return; }
      this._message(errorText(error));
      if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._context.onExpired?.(errorText(error), error.code);
    } finally { if (this._pending === request) this._pending = null; this._buttons(); }
  }
  _select(index, request) {
    if (this._resultContext !== request || !this._matches(request)) { this.cancel(); return; }
    const result = this._results[index]; if (!result) return;
    this.cancel();
    if (this._context.onSelect(result) !== false) {
      this.shadowRoot.querySelector("#place-extra-attribution").textContent = result.attribution;
      this._message(`Wybrano: ${result.label}. Sprawdź punkt na mapie i zatwierdź go w formularzu.`);
    }
  }
}

if (!customElements.get("manual-location-place-search")) customElements.define("manual-location-place-search", ManualLocationPlaceSearch);
