import { adminPersonOptions, callWS, editHistory, errorText, localDateInput, readPoint, removeHistory } from "./data.js";
import { L, makeMap, mapStyles, popup } from "./map-common.js";

export class ManualLocationAdmin extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._epoch = 0;
    this._selected = "";
    this._editIndex = -1;
    this._onDisconnect = () => this._clearSession("Połączenie zmieniło się. Odblokuj panel ponownie.");
    this._onReady = () => this._clearSession("Odblokuj panel po ponownym połączeniu.");
  }

  set hass(hass) {
    const changed = this._hass?.connection !== hass.connection || this._hass?.user?.id !== hass.user?.id;
    const disconnected = hass.connected === false && this._hass?.connected !== false;
    this._hass = hass;
    if (changed) {
      this._unbind();
      this._connection = hass.connection;
      if (this.isConnected) this._bind();
      this._clearSession();
    } else if (disconnected || !hass.user?.is_admin) this._clearSession();
    if (this.isConnected && !this.shadowRoot.querySelector("main")) this._render();
  }

  connectedCallback() { this._bind(); this._render(); }
  disconnectedCallback() { this._unbind(); this._clearSession(); this._destroyMap(); }

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
  _clearSession(message = "") {
    this._epoch++;
    this._token = null;
    this._snapshot = null;
    this._busy = false;
    this._dirty = false;
    clearTimeout(this._expiry);
    if (this.isConnected) { this._render(); this._message(message); }
  }
  _destroyMap() { this._resize?.disconnect(); this._map?.remove(); this._map = null; }
  _message(text, good = false) {
    const box = this.shadowRoot.querySelector("#message");
    if (box) { box.textContent = text; box.classList.toggle("good", good); box.hidden = !text; }
  }
  _changed() { this._dirty = true; this._saveState(); }
  _saveState() {
    const status = this.shadowRoot.querySelector("#save-state");
    if (status) status.textContent = this._dirty ? "Masz niezapisane zmiany" : `Zapisane · rewizja ${this._snapshot?.revision}`;
    const save = this.shadowRoot.querySelector("#save");
    if (save) save.disabled = this._busy || !this._dirty;
    const layout = this.shadowRoot.querySelector(".layout");
    if (layout) layout.inert = this._busy;
    const reload = this.shadowRoot.querySelector("#reload");
    if (reload) reload.disabled = this._busy;
    const pinChange = this.shadowRoot.querySelector(".pin-change");
    if (pinChange) pinChange.inert = this._busy;
  }

  _render() {
    this._destroyMap();
    const admin = this._hass?.user?.is_admin;
    const unlocked = admin && this._token && this._snapshot;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;color:var(--primary-text-color,#20343b);background:var(--primary-background-color,#f2f5f4);font-family:var(--paper-font-body1_-_font-family,"Segoe UI",sans-serif)}*{box-sizing:border-box}main{max-width:1220px;margin:auto;padding:32px 28px 56px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:700;color:var(--secondary-text-color,#657a82);margin-bottom:8px}h1{font-family:Georgia,serif;font-size:32px;font-weight:400;margin:0}h2{font-size:17px;margin:0 0 18px}.sub{color:var(--secondary-text-color,#657a82);line-height:1.6;font-size:14px;margin:10px 0 0}.box{border:1px solid var(--divider-color,#d9e2e0);border-radius:12px;background:var(--card-background-color,#fff);padding:22px}.lockbox{max-width:480px;margin:58px auto}.layout{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}.sidebar{display:flex;flex-direction:column;gap:18px}.mapbox{padding:0;overflow:hidden}.map{height:380px}.caption{padding:12px 18px;border-top:1px solid var(--divider-color,#d9e2e0);font-size:13px;color:var(--secondary-text-color,#657a82)}label{display:block;font-size:13px;font-weight:600;margin-bottom:7px}input,select,button{font:inherit}input,select{width:100%;min-height:42px;padding:9px 11px;background:var(--input-fill-color,var(--card-background-color,#fff));color:inherit;border:1px solid var(--divider-color,#c8d6d4);border-radius:7px}input:focus,select:focus{outline:2px solid #338b9c;outline-offset:1px}.field{margin-bottom:16px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}.toggle{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:500;cursor:pointer}.toggle input{width:20px;min-height:20px;accent-color:#176e82}.toggle small{display:block;font-weight:400;color:var(--secondary-text-color,#657a82);margin-top:3px}button{min-height:40px;border-radius:7px;padding:9px 14px;cursor:pointer;background:transparent;color:inherit;border:1px solid var(--divider-color,#c8d6d4);font-size:13px;font-weight:600}button:hover:not(:disabled){background:#63889a16}button:focus-visible{outline:3px solid #338b9c;outline-offset:2px}button.primary{background:#176e82;color:white;border-color:#176e82}button.primary:hover:not(:disabled){background:#105769}button:disabled{opacity:.48;cursor:default}.wide{width:100%}.actions{display:flex;gap:8px;flex-wrap:wrap}.history{margin-top:18px}.history-top{display:flex;align-items:center;justify-content:space-between;gap:16px}.history-top h2{margin:0}.history-editor{display:grid;grid-template-columns:1fr 1fr 1.5fr auto;gap:10px;align-items:end;padding:20px 0}.history-editor .field{margin:0}.scroll{max-height:340px;overflow:auto;border-top:1px solid var(--divider-color,#d9e2e0)}.row{display:flex;align-items:center;justify-content:space-between;padding:13px 0;gap:12px;border-bottom:1px solid var(--divider-color,#e3e9e7)}.row time{display:block;font-size:13px;font-weight:600}.row small{display:block;margin-top:4px;color:var(--secondary-text-color,#657a82)}.row button{min-height:34px;padding:5px 10px}.danger{color:#b74435}.empty{padding:22px 0;color:var(--secondary-text-color,#657a82);font-size:14px;line-height:1.6}.footer{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-top:20px;font-size:13px;color:var(--secondary-text-color,#657a82)}#message{margin:0 0 20px;padding:13px 16px;border-radius:8px;background:#a8402715;border:1px solid #a8402750;font-size:14px;line-height:1.5}#message.good{background:#34815e13;border-color:#34815e50}[hidden]{display:none!important}.current-summary{font-size:13px;line-height:1.6;color:var(--secondary-text-color,#657a82);margin:0 0 18px}.pin-change{margin-top:18px}.pin-change summary{cursor:pointer;font-size:13px}.pin-change form{margin-top:16px;max-width:400px}.notice{border-left:3px solid #176e82;padding-left:12px;font-size:13px;color:var(--secondary-text-color,#657a82);line-height:1.6}
      @media(max-width:900px){.layout{grid-template-columns:1fr}.sidebar{display:grid;grid-template-columns:1fr 1fr}.history-editor{grid-template-columns:1fr 1fr}.map{height:340px}}@media(max-width:580px){main{padding:22px 14px 36px}header{align-items:flex-start}h1{font-size:27px}.sidebar{display:flex}.box{padding:18px}.mapbox{padding:0}.history-editor{grid-template-columns:1fr 1fr}.history-editor .timefield{grid-column:1/-1}.footer{align-items:flex-start;flex-direction:column}.row{align-items:flex-start}.map{height:290px}}
      </style><main><header><div><div class="eyebrow">Manual Location</div><h1>Pozycje i historia</h1><p class="sub">Jeden zapis, wspólny dla wszystkich kart mapy.</p></div>${unlocked ? '<button id="lock">Zablokuj panel</button>' : ""}</header><div id="message" role="status" hidden></div>
      ${!admin ? '<section class="box lockbox"><h2>Panel administratora</h2><p class="sub">Zaloguj się na konto administratora Home Assistant, aby zarządzać lokalizacjami.</p></section>' : !unlocked ? '<section class="box lockbox"><h2>Odblokuj ustawienia</h2><p class="sub">Wpisz PIN ustalony podczas konfiguracji integracji. Sesja trwa do 15 minut.</p><form id="unlock-form"><div class="field" style="margin-top:22px"><label for="pin">PIN administratora (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="off" required></div><button class="primary wide" type="submit">Odblokuj</button></form></section>' : `
      <div class="layout"><aside class="sidebar"><section class="box"><h2>Ustawienia map</h2><label class="toggle"><input type="checkbox" id="global-enabled"><span>Włącz ręczne lokalizacje<small>Wyłączenie przywraca dane HA</small></span></label><div class="field" style="margin-top:24px"><label for="person">Osoba</label><select id="person"></select></div><label class="toggle"><input type="checkbox" id="person-enabled"><span>Włącz dla tej osoby</span></label><p class="notice">Nowa osoba jest włączona po ustawieniu pierwszej pozycji i zapisaniu zmian.</p><p class="notice" id="missing-person-notice" hidden>Tej osoby nie ma już w Home Assistant. Usuń jej ustawienia poniżej, a następnie zapisz wszystkie zmiany. Pozostałe osoby zachowają swoje ustawienia.</p><button class="wide danger" id="remove-person">Usuń ustawienia tej osoby</button></section>
      <section class="box"><h2>Bieżąca pozycja</h2><p class="current-summary" id="current-summary"></p><div class="pair"><div class="field"><label for="current-lat">Szerokość</label><input id="current-lat" type="number" step="any" min="-90" max="90"></div><div class="field"><label for="current-lon">Długość</label><input id="current-lon" type="number" step="any" min="-180" max="180"></div></div><button class="wide" id="set-current">Ustaw bieżącą pozycję</button><p class="sub">Przy zapisie serwer dopisze tę pozycję do historii z aktualnym czasem.</p></section></aside>
      <section><div class="box mapbox"><div class="map" id="map" aria-label="Mapa wyboru pozycji"></div><div class="caption">Kliknij mapę, aby uzupełnić współrzędne. Następnie ustaw pozycję lub dodaj punkt historii.</div></div><section class="box history"><div class="history-top"><h2>Historia</h2><span class="eyebrow" id="count"></span></div><p class="sub">Edycja lub usunięcie punktu nie zmienia bieżącej pozycji.</p><form id="history-form" class="history-editor"><div class="field"><label for="history-lat">Szerokość</label><input id="history-lat" type="number" step="any" min="-90" max="90" required></div><div class="field"><label for="history-lon">Długość</label><input id="history-lon" type="number" step="any" min="-180" max="180" required></div><div class="field timefield"><label for="history-time">Lokalna data i godzina</label><input id="history-time" type="datetime-local" step="1" required></div><button id="add-history" type="submit">Dodaj punkt</button></form><button id="cancel-edit" hidden>Anuluj edycję punktu</button><div class="scroll" id="history-list"></div></section></section></div>
      <footer class="footer"><span id="save-state"></span><div class="actions"><button id="reload">Wczytaj zapisane dane</button><button class="primary" id="save">Zapisz wszystkie zmiany</button></div></footer><details class="pin-change"><summary>Zmień PIN administratora</summary><form id="pin-form"><div class="field"><label for="new-pin">Nowy PIN (4–12 znaków: litery A–Z, a–z lub cyfry)</label><input id="new-pin" type="password" pattern="[A-Za-z0-9]{4,12}" minlength="4" maxlength="12" autocomplete="off" required></div><button type="submit">Zmień PIN i zablokuj sesje</button></form></details>`}</main>`;
    if (!admin) return;
    if (!unlocked) {
      this.shadowRoot.querySelector("#unlock-form").addEventListener("submit", (e) => { e.preventDefault(); this._unlock(); });
      return;
    }
    mapStyles(this.shadowRoot);
    this._map = makeMap(this.shadowRoot.querySelector("#map"), this._hass);
    this._layers = L.layerGroup().addTo(this._map);
    this._map.on("click", ({ latlng }) => {
      if (!this._hass.states[this._selected]) return;
      for (const prefix of ["current", "history"]) {
        this.shadowRoot.querySelector(`#${prefix}-lat`).value = latlng.lat.toFixed(6);
        this.shadowRoot.querySelector(`#${prefix}-lon`).value = latlng.lng.toFixed(6);
      }
      this._message("Współrzędne są gotowe. Ustaw bieżącą pozycję albo dodaj punkt historii.", true);
    });
    this._resize = new ResizeObserver(() => this._map?.invalidateSize());
    this._resize.observe(this.shadowRoot.querySelector("#map"));
    const select = this.shadowRoot.querySelector("#person");
    const persons = adminPersonOptions(this._hass.states, this._snapshot.persons);
    for (const person of persons) {
      const option = document.createElement("option");
      option.value = person.id;
      option.textContent = person.name;
      select.append(option);
    }
    if (!persons.some((p) => p.id === this._selected)) this._selected = persons[0]?.id || "";
    select.value = this._selected;
    select.addEventListener("change", () => { this._selected = select.value; this._editIndex = -1; this._refreshPerson(); });
    const global = this.shadowRoot.querySelector("#global-enabled");
    global.checked = this._snapshot.enabled;
    global.addEventListener("change", () => { this._snapshot.enabled = global.checked; this._changed(); });
    this.shadowRoot.querySelector("#person-enabled").addEventListener("change", (e) => {
      if (!this._hass.states[this._selected]) return;
      const person = this._person();
      if (person) { person.enabled = e.target.checked; this._changed(); }
    });
    this.shadowRoot.querySelector("#set-current").addEventListener("click", () => this._setCurrent());
    this.shadowRoot.querySelector("#remove-person").addEventListener("click", () => this._removePerson());
    this.shadowRoot.querySelector("#history-form").addEventListener("submit", (e) => { e.preventDefault(); this._addHistory(); });
    this.shadowRoot.querySelector("#cancel-edit").addEventListener("click", () => { this._editIndex = -1; this._refreshHistory(); });
    this.shadowRoot.querySelector("#save").addEventListener("click", () => this._save());
    this.shadowRoot.querySelector("#reload").addEventListener("click", () => this._reload());
    this.shadowRoot.querySelector("#lock").addEventListener("click", () => this._lock());
    this.shadowRoot.querySelector("#pin-form").addEventListener("submit", (e) => { e.preventDefault(); this._changePin(); });
    this._refreshPerson();
    this._saveState();
  }

  _person() { return this._snapshot?.persons[this._selected]; }
  _refreshPerson() {
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
      ? `${person.current.latitude.toFixed(5)}, ${person.current.longitude.toFixed(5)} · ${new Date(person.current.timestamp).toLocaleString()}`
      : "Wybierz punkt na mapie, aby ustawić pierwszą pozycję.";
    this.shadowRoot.querySelector("#history-time").value = localDateInput();
    this._refreshHistory();
    this._drawPerson(true);
  }
  _refreshHistory() {
    const history = this._person()?.history || [];
    this.shadowRoot.querySelector("#count").textContent = `${history.length} punktów`;
    this.shadowRoot.querySelector("#add-history").textContent = this._editIndex < 0 ? "Dodaj punkt" : "Zastosuj edycję";
    this.shadowRoot.querySelector("#cancel-edit").hidden = this._editIndex < 0;
    const list = this.shadowRoot.querySelector("#history-list");
    list.replaceChildren();
    if (!history.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Brak punktów historii. Bieżąca pozycja pozostaje niezależna od trasy.";
      list.append(empty);
    }
    history.forEach((point, index) => {
      const row = document.createElement("div");
      row.className = "row";
      const info = document.createElement("div");
      const time = document.createElement("time");
      time.dateTime = point.timestamp;
      time.textContent = new Date(point.timestamp).toLocaleString();
      const coords = document.createElement("small");
      coords.textContent = `${point.latitude.toFixed(5)}, ${point.longitude.toFixed(5)}`;
      info.append(time, coords);
      const actions = document.createElement("div");
      actions.className = "actions";
      const edit = document.createElement("button");
      edit.disabled = !this._hass.states[this._selected];
      edit.textContent = "Edytuj";
      edit.setAttribute("aria-label", `Edytuj punkt ${time.textContent}`);
      edit.addEventListener("click", () => {
        this._editIndex = index;
        this.shadowRoot.querySelector("#history-lat").value = point.latitude;
        this.shadowRoot.querySelector("#history-lon").value = point.longitude;
        this.shadowRoot.querySelector("#history-time").value = localDateInput(point.timestamp);
        this._refreshHistory();
      });
      const remove = document.createElement("button");
      remove.disabled = !this._hass.states[this._selected];
      remove.className = "danger";
      remove.textContent = "Usuń";
      remove.setAttribute("aria-label", `Usuń punkt ${time.textContent}`);
      remove.addEventListener("click", () => {
        this._snapshot.persons[this._selected] = removeHistory(this._person(), index);
        this._editIndex = -1;
        this._changed(); this._refreshHistory(); this._drawPerson();
      });
      actions.append(edit, remove); row.append(info, actions); list.append(row);
    });
  }
  _drawPerson(fit = false) {
    this._layers.clearLayers();
    const person = this._person();
    if (!person) { this._map.setView([25, 0], 2); return; }
    const current = [person.current.latitude, person.current.longitude];
    const name = this._hass.states[this._selected]?.attributes.friendly_name || this._selected;
    L.circleMarker(current, { color: "#176e82", radius: 9, fillOpacity: 1 }).bindPopup(popup(name, person.current)).addTo(this._layers);
    const path = person.history.map((p) => [p.latitude, p.longitude]);
    if (path.length > 1) L.polyline(path, { color: "#176e82", weight: 3 }).addTo(this._layers);
    for (const point of person.history) L.circleMarker([point.latitude, point.longitude], { color: "#ad6c34", radius: 4, fillOpacity: 1 }).bindPopup(popup("Punkt historii", point)).addTo(this._layers);
    if (fit) this._map.fitBounds([current, ...path], { padding: [35, 35], maxZoom: 14 });
  }
  _setCurrent() {
    if (this._busy) return;
    try {
      if (!this._hass.states[this._selected]) throw new Error("Najpierw dodaj osobę w Home Assistant.");
      const point = readPoint(this.shadowRoot.querySelector("#current-lat").value, this.shadowRoot.querySelector("#current-lon").value);
      const old = this._person();
      if (!old && Object.keys(this._snapshot.persons).length >= 20) throw new Error("Osiągnięto limit 20 osób.");
      this._snapshot.persons[this._selected] = { enabled: this.shadowRoot.querySelector("#person-enabled").checked, current: point, history: old?.history || [] };
      this._changed(); this._refreshPerson();
      this._message("Pozycja przygotowana. Zapisz wszystkie zmiany, aby pokazać ją na kartach.", true);
    } catch (error) { this._message(error.message); }
  }
  _addHistory() {
    if (this._busy) return;
    try {
      if (!this._hass.states[this._selected]) throw new Error("Tej osoby nie ma w Home Assistant. Usuń jej ustawienia przed zapisem.");
      if (!this._person()) throw new Error("Najpierw ustaw bieżącą pozycję tej osoby.");
      const point = readPoint(this.shadowRoot.querySelector("#history-lat").value, this.shadowRoot.querySelector("#history-lon").value, this.shadowRoot.querySelector("#history-time").value);
      this._snapshot.persons[this._selected] = editHistory(this._person(), point, this._editIndex);
      this._editIndex = -1;
      this._changed(); this._refreshHistory(); this._drawPerson();
      this._message("Punkt przygotowany. Zapisz wszystkie zmiany, aby zaktualizować karty.", true);
    } catch (error) { this._message(error.message); }
  }

  _removePerson() {
    if (this._busy || !this._person()) return;
    if (!window.confirm("Usunąć ręczną pozycję i całą historię tej osoby? Po zapisaniu wszystkich zmian karty wrócą do danych HA, jeśli osoba nadal istnieje.")) return;
    delete this._snapshot.persons[this._selected];
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
      this._expiry = setTimeout(() => this._clearSession("Sesja wygasła. Odblokuj panel ponownie."), Math.max(1, session.expires_in - 2) * 1000);
      this._render();
    } catch (error) { if (epoch === this._epoch) this._message(errorText(error)); }
    finally { if (epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _admin(type, extra = {}) {
    if (this._busy || !this._token) return null;
    const epoch = this._epoch;
    this._busy = true;
    this._saveState();
    try {
      const data = await callWS(this._hass, { type: `manual_location/${type}`, token: this._token, ...extra });
      return epoch === this._epoch ? { data } : null;
    } catch (error) {
      if (epoch === this._epoch) {
        if (["invalid_token", "pin_required", "unauthorized", "forbidden"].includes(error?.code)) this._clearSession(errorText(error));
        else this._message(errorText(error));
      }
      return null;
    } finally { if (epoch === this._epoch) { this._busy = false; this._saveState(); } }
  }
  async _save() {
    const result = await this._admin("admin_save", structuredClone(this._snapshot));
    if (!result) return;
    this._snapshot = result.data;
    this._dirty = false;
    this._editIndex = -1;
    this._refreshPerson(); this._saveState();
    this._message("Zapisano. Wszystkie karty odczytają zmiany w ciągu 5 sekund.", true);
  }
  async _reload() {
    if (this._dirty && !window.confirm("Wczytać zapisane dane? Niezapisane zmiany w tym formularzu zostaną odrzucone.")) return;
    const result = await this._admin("admin_get");
    if (!result) return;
    this._snapshot = result.data;
    this._dirty = false; this._editIndex = -1;
    this._render();
    this._message("Wczytano zapisane dane.", true);
  }
  async _lock() {
    const token = this._token;
    this._clearSession("Panel zablokowany.");
    if (token) { try { await callWS(this._hass, { type: "manual_location/lock", token }); } catch { /* The local token is already forgotten. */ } }
  }
  async _changePin() {
    const input = this.shadowRoot.querySelector("#new-pin");
    const new_pin = input.value;
    input.value = "";
    const result = await this._admin("change_pin", { new_pin });
    if (result) this._clearSession("PIN zmieniony. Wszystkie sesje zostały zablokowane.");
  }
}

if (!customElements.get("manual-location-admin")) customElements.define("manual-location-admin", ManualLocationAdmin);
