// Pure transformations. Never fall back to GPS without a validated view response.
export function coordinates(point) {
  if (!point || !Number.isFinite(point.latitude) || !Number.isFinite(point.longitude)
    || Math.abs(point.latitude) > 90 || Math.abs(point.longitude) > 180) return null;
  return { latitude: point.latitude, longitude: point.longitude, timestamp: point.timestamp };
}

export function windowHistory(points, start, end) {
  const sorted = (points || []).filter((p) => coordinates(p) && Number.isFinite(Date.parse(p.timestamp)))
    .map((p) => coordinates(p)).sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  const inside = sorted.filter((p) => Date.parse(p.timestamp) >= start && Date.parse(p.timestamp) <= end);
  const before = sorted.filter((p) => Date.parse(p.timestamp) < start).at(-1);
  // Match HA's state at the start of the window without changing stored timestamps.
  return before && Date.parse(inside[0]?.timestamp) !== start
    ? [{ ...before, timestamp: new Date(start).toISOString() }, ...inside] : inside;
}

export function historyOpacity(index, count) {
  // HA fades by point order. A single segment stays visible; the final dot is clamped.
  return count <= 2 ? 1 : Math.min(1, Math.max(0, .2 + index * .8 / (count - 2)));
}

export function historySegments(from, to) {
  if (Math.abs(from[1] - to[1]) <= 180) return [[from, to]];
  // Split only the rendered line at the antimeridian, as the native HA map does.
  const difference = ((to[1] - from[1] + 540) % 360) - 180;
  const edge = from[1] > 0 ? 180 : -180;
  const latitude = difference === 0 ? (from[0] + to[0]) / 2
    : from[0] + (to[0] - from[0]) * (edge - from[1]) / difference;
  return [[from, [latitude, edge]], [[latitude, -edge], to]];
}

export function validateView(response, entityIds) {
  if (!response || !Number.isInteger(response.revision) || !response.overrides
    || Array.isArray(response.overrides) || typeof response.overrides !== "object") throw new Error("invalid_view");
  for (const [id, value] of Object.entries(response.overrides)) {
    if (!entityIds.includes(id) || !coordinates(value?.current) || !Array.isArray(value.history)
      || value.history.some((p) => !coordinates(p) || !Number.isFinite(Date.parse(p.timestamp)))) throw new Error("invalid_view");
  }
  if (response.activity !== undefined) {
    if (!response.activity || typeof response.activity !== "object" || Array.isArray(response.activity)) throw new Error("invalid_view");
    for (const [id, value] of Object.entries(response.activity)) {
      if (!entityIds.includes(id) || !value || !["green", "red", "unknown", "hidden"].includes(value.status)
        || (["green", "red"].includes(value.status)
          ? typeof value.timestamp !== "string" || !Number.isFinite(Date.parse(value.timestamp)) : value.timestamp !== null)) throw new Error("invalid_view");
    }
  }
  return response;
}

export function originalIds(entityIds, view) {
  if (!view) return [];
  return entityIds.filter((id) => !Object.hasOwn(view.overrides, id));
}

export function historyPoints(states) {
  return (states || []).map((state) => {
    const attributes = state.attributes || state.a || {};
    const rawTime = state.last_updated || state.last_changed || state.lu || state.lc;
    const date = new Date(typeof rawTime === "number" ? rawTime * 1000 : rawTime);
    return Number.isFinite(date.getTime()) ? coordinates({ ...attributes, timestamp: date.toISOString() }) : null;
  }).filter(Boolean);
}

export function personModels(states, entityIds, view, originals, start, end) {
  if (!view) return [];
  return entityIds.flatMap((id) => {
    const state = states[id];
    if (!state) return [];
    const override = view.overrides[id];
    const current = override ? coordinates(override.current)
      : coordinates({ ...state.attributes, timestamp: state.last_updated || state.last_changed });
    if (!current) return [];
    const points = override ? override.history : (originals[id] || []);
    // Copy only identity fields. State/zone/gps_accuracy could disclose the original location.
    return [{ id, name: state.attributes.friendly_name || id, picture: state.attributes.entity_picture,
      current, history: windowHistory(points, start, end),
      ...(view.activity?.[id] ? { activity: { status: view.activity[id].status, timestamp: view.activity[id].timestamp } } : {}) }];
  });
}

export function geoModels(states, sources = []) {
  return Object.values(states).filter((s) => s.entity_id?.startsWith("geo_location.")
    && (sources.includes("all") || sources.includes(s.attributes.source)) && coordinates(s.attributes))
    .map((s) => ({ id: s.entity_id, name: s.attributes.friendly_name || s.entity_id, current: coordinates(s.attributes) }));
}

export function zoneModels(states, configuredIds) {
  return configuredIds.filter((id) => id.startsWith("zone.") && coordinates(states[id]?.attributes))
    .map((id) => ({ id, name: states[id].attributes.friendly_name || id,
      current: coordinates(states[id].attributes), radius: Math.max(0, Number(states[id].attributes.radius) || 0) }));
}

export function adminPersonOptions(states, persons) {
  const ids = new Set([...Object.keys(states).filter((id) => id.startsWith("person.")), ...Object.keys(persons)]);
  return [...ids].map((id) => ({ id, missing: !states[id],
    name: states[id]?.attributes.friendly_name || `${id}${states[id] ? "" : " (brak w HA)"}` }));
}

export function activitySettings(value = {}) {
  return { enabled: value.enabled ?? true, signals: [...(value.signals ?? ["unlock", "walking"])],
    match: value.match ?? "any", manual_since: value.manual_since ?? null };
}

export function activityPayload(config, now = Date.now()) {
  return Object.fromEntries(Object.entries(config).map(([id, value]) => {
    const settings = activitySettings(value);
    if (typeof settings.enabled !== "boolean" || !["any", "all"].includes(settings.match)
      || settings.signals.some(signal => !["unlock", "walking", "screen", "tilting"].includes(signal))
      || new Set(settings.signals).size !== settings.signals.length) throw new Error("Aktywność: sprawdź wybrane sygnały i sposób ich łączenia.");
    if (settings.manual_since !== null && (typeof settings.manual_since !== "string"
      || !Number.isFinite(Date.parse(settings.manual_since)) || Date.parse(settings.manual_since) > now))
      throw new Error("Aktywność: podaj poprawną datę i godzinę ostatniej aktywności, która nie leży w przyszłości.");
    if (settings.enabled && settings.manual_since === null && !settings.signals.length)
      throw new Error("Aktywność: wybierz przynajmniej jeden sygnał dla automatycznej zielonej kropki.");
    return [id, settings];
  }));
}

export function localDateInput(iso = new Date().toISOString()) {
  const date = new Date(iso);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 19);
}

export function readPoint(latitude, longitude, localTime) {
  if (latitude.trim() === "" || longitude.trim() === "") throw new Error("Wpisz obie współrzędne lub kliknij mapę.");
  const point = coordinates({ latitude: Number(latitude), longitude: Number(longitude) });
  if (!point) throw new Error("Szerokość musi mieścić się w −90…90, a długość w −180…180.");
  const date = localTime ? new Date(localTime) : new Date();
  if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now()) throw new Error("Wybierz poprawny czas, który nie leży w przyszłości.");
  return { ...point, timestamp: date.toISOString() };
}

export function editHistory(person, point, index = -1) {
  const history = person.history.filter((_, i) => i !== index);
  if (history.some((p) => Date.parse(p.timestamp) === Date.parse(point.timestamp))) throw new Error("Istnieje już punkt o tej dacie i godzinie.");
  if (history.length >= 5000) throw new Error("Osiągnięto limit 5000 punktów.");
  history.push(point);
  history.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  return { ...person, history };
}

export function removeHistory(person, index) {
  return { ...person, history: person.history.filter((_, i) => i !== index) };
}

export function errorText(error) {
  const code = error?.code || error?.message || "unknown_error";
  const messages = {
    revision_conflict: "Dane zmieniono w innej sesji. Twoje zmiany pozostały w formularzu. Wczytaj zapisane dane i wprowadź je ponownie.",
    invalid_pin: "Niepoprawny PIN.",
    rate_limited: "Za dużo prób. Odczekaj 5 minut przed kolejną próbą odblokowania.",
    invalid_token: "Sesja wygasła. Odblokuj panel ponownie.",
    pin_required: "Sesja wygasła lub połączenie zmieniło się. Odblokuj panel ponownie.",
    invalid_input: "Niepoprawne dane. Sprawdź współrzędne, daty i limity: 20 osób, 5000 punktów, PIN 4–12 znaków: litery A–Z, a–z lub cyfry.",
    not_loaded: "Integracja nie jest dostępna. Sprawdź jej konfigurację w Home Assistant.",
    storage_error: "Nie udało się zapisać danych na dysku Home Assistant. Zmiany są nadal w formularzu; sprawdź wolne miejsce i ponów zapis.",
    unauthorized: "Brak uprawnień administratora lub uprawnień do wybranej osoby.",
    forbidden: "Brak uprawnień do wykonania tej operacji.",
    invalid_view: "Niepoprawna odpowiedź integracji. Lokalizacje są ukryte.",
    timeout: "Serwer nie odpowiedział. Sprawdź połączenie i spróbuj ponownie.",
    geocoder_not_configured: "Wyszukiwanie nie jest skonfigurowane. Ustaw klucz Geoapify w konfiguracji integracji HA; nadal możesz używać mapy i ręcznych współrzędnych.",
    geocoder_invalid_key: "Geoapify odrzuciło klucz. Sprawdź konfigurację integracji HA; nie wpisuj klucza w wyszukiwarkę.",
    geocoder_rate_limited: "Wyszukiwarka chwilowo osiągnęła limit zapytań. Odczekaj chwilę i kliknij Szukaj ponownie.",
    geocoder_daily_limit: "Wykorzystano dzienny limit wyszukiwania tej integracji. Wróć po północy UTC lub wybierz punkt na mapie.",
    geocoder_unavailable: "Wyszukiwarka jest chwilowo niedostępna. Wybierz punkt na mapie lub spróbuj ponownie później.",
    geocoder_storage_error: "Nie można potwierdzić licznika wyszukiwań. Wyszukiwanie wstrzymano; mapa, ręczne punkty i zapis nadal działają.",
    scenario_running: "Najpierw zatrzymaj plan, aby go usunąć. Oczekujące punkty możesz edytować podczas działania trasy.",
    pending_in_past: "Oczekujące punkty uruchomionej trasy muszą mieć godziny w przyszłości. Przesuń je albo zatrzymaj plan przed edycją.",
    route_invalid_url: "Nie rozpoznano linku do trasy Google Maps. Wklej link z ustalonym początkiem i celem; także maps.app.goo.gl.",
    route_unavailable: "Nie udało się pobrać trasy. Sprawdź link, wybrane miejsca i sposób podróży, a następnie spróbuj ponownie.",
    route_invalid_response: "Usługa nie zwróciła poprawnego przebiegu i czasów trasy. Szkic nie został utworzony.",
    route_too_many_points: "Ta trasa ma za dużo miejsc lub punktów. Użyj najwyżej 12 miejsc i zwiększ odstęp między punktami (limit 500).",
    route_duration_invalid: "Czas podróży musi być dodatni i nie przekraczać 24 godzin. Sprawdź wpisany czas i wybrany transport.",
    scenario_missing: "Ten plan nie jest już zapisany w HA. Wczytaj bibliotekę i wybierz istniejący plan albo zapisz nowy szkic.",
    scenario_overlap: "Godziny tej trasy nakładają się na inny uruchomiony plan tej samej osoby. Zatrzymaj tamten plan albo zmień godziny i zapisz ten plan przed Start.",
    scenario_limit: "Osiągnięto limit 100 zapisanych planów i szkiców. Usuń niepotrzebny plan przed zapisaniem kolejnego.",
    client_outdated: "Panel ma starszą wersję. Zabezpiecz niezapisane zmiany i odśwież stronę przed pracą z biblioteką planów.",
    no_pending_points: "Wszystkie punkty tego planu zostały już wykonane. Dodaj kolejne przed Start.",
    executed_prefix: "Wykonane punkty planu muszą pozostać bez zmian. Odśwież plan i zmień tylko oczekujące punkty.",
    global_disabled: "Najpierw włącz ręczne lokalizacje i zapisz ustawienia powyżej.",
    person_missing: "Tej osoby nie ma w Home Assistant. Plan nie może zostać uruchomiony.",
    person_disabled: "Najpierw włącz lokalizacje tej osoby i zapisz ustawienia powyżej.",
    person_limit: "Osiągnięto limit 20 osób z ręczną lokalizacją. Usuń niepotrzebną konfigurację przed uruchomieniem planu nowej osoby.",
    history_limit: "Historia osiągnęła limit 5000 punktów. Zwolnij miejsce przed uruchomieniem planu.",
    history_conflict: "Godzina punktu planu koliduje z zapisaną historią. Zmień godzinę oczekującego punktu.",
  };
  return messages[code] || "Operacja nie powiodła się. Sprawdź dane i połączenie, a następnie spróbuj ponownie.";
}

export async function callWS(hass, request, timeoutMs = 12000) {
  let timeout;
  try {
    return await Promise.race([hass.callWS(request), new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error("timeout")), timeoutMs);
    })]);
  } finally { clearTimeout(timeout); }
}
