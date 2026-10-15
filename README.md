# Manual Location for Home Assistant

Ręczne pozycje i historia osób na wybranych kartach mapy Home Assistanta. Administrator zarządza nimi we wspólnym panelu **Ustawienia mapy**, chronionym uprawnieniami administratora HA i dodatkowym PIN-em.

Integracja zmienia prezentację w kartach `custom:manual-location-map`. Nie zmienia encji `person`, GPS z telefonów, automatyzacji ani historii Recorder. Inne ekrany i API Home Assistanta mogą nadal pokazywać oryginalne dane. PIN nie chroni przed właścicielem serwera z dostępem do plików lub kodu.

## Instalacja przez HACS

Sprawdzona wersja Home Assistanta: **2026.9.4**. Zgodność z nowszymi wersjami wymaga osobnego sprawdzenia. HACS musi być już zainstalowany.

1. W HACS otwórz menu **⋮ → Custom repositories / Niestandardowe repozytoria**.
2. Wpisz `https://github.com/Primo83/ha-manual-location`, wybierz typ **Integration / Integracja** i dodaj repozytorium.
3. Wyszukaj **Manual Location**, otwórz je i wybierz pobranie z gałęzi **main**.
4. Po pobraniu samodzielnie uruchom ponownie Home Assistanta.
5. Wybierz **Ustawienia → Urządzenia i usługi → Dodaj integrację → Manual Location** i ustal PIN składający się z 4–12 znaków: litery A–Z, a–z lub cyfry bezpośrednio w HA. Nie umieszczaj PIN-u w zgłoszeniach ani w repozytorium.
6. Dodaj zasób dashboardu: URL `/manual_location_static/map.js`, typ **Moduł JavaScript**. Lista zasobów może wymagać trybu zaawansowanego w profilu użytkownika. Odśwież przeglądarkę z pominięciem pamięci podręcznej.
7. Otwórz panel **Ustawienia mapy**, odblokuj go PIN-em, wybierz osobę i zapisz jej pozycję.
8. W każdej wybranej karcie mapy zmień `type: map` na `type: custom:manual-location-map`, zachowując listę encji i pozostałe parametry. Integracja nie zmienia dashboardów automatycznie.

Przykład karty z przykładową encją, którą należy zastąpić własną:

```yaml
type: custom:manual-location-map
entities:
  - person.example
  - zone.home
hours_to_show: 24
auto_fit: true
```

Można zachować również `geo_location_sources`, np. źródło Blitzortung, jeśli odpowiednia integracja jest dostępna w danym HA.

## Działanie i sprawdzenie

- W panelu administratora przycisk **Użyj lokalizacji tego urządzenia** lub ikona celownika na mapie pobierają jednorazowo położenie telefonu albo komputera z otwartym panelem. Po zgodzie przeglądarki mapa pokazuje punkt i krąg dokładności, a pola bieżącej pozycji zostają uzupełnione. Następnie kliknij **Ustaw bieżącą pozycję** i **Zapisz wszystkie zmiany**. Sam odczyt nie zapisuje danych i nie uruchamia ciągłego śledzenia.
- Lokalizacja urządzenia wymaga HTTPS oraz uprawnień przeglądarki/aplikacji i systemu. Na Windows wynik może być przybliżony — sprawdź podaną dokładność. Funkcja dotyczy urządzenia z otwartym panelem, nie zdalnego pobrania pozycji innego telefonu. [Geolocation API](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation/getCurrentPosition).
- Dane ręczne są wspólne dla wszystkich kart i odświeżają się co około 5 sekund.
- Po skonfigurowaniu osoby funkcja jest włączona. Wyłączenie jej w panelu przywraca prezentację oryginalnych danych HA dla tej osoby.
- Bieżąca pozycja i historia są osobne. Zmiana bieżącej pozycji dopisuje punkt historyczny; edycja lub usunięcie historii nie przesuwają bieżącego markera.
- Aktywna osoba korzysta wyłącznie z ręcznej historii. Przy błędzie odczytu lub ponownym łączeniu karta ukrywa lokalizacje do czasu poprawnego odczytu ustawień.
- Zwykły użytkownik nie ma dostępu do panelu administracyjnego. PIN ma limit prób i czasową sesję.

Po instalacji sprawdź zapis pozycji, dodawanie i usuwanie historii, przełącznik włączenia oraz zachowanie ustawień po restarcie HA. Sprawdź także widok na zwykłym koncie. Lokalne testy projektu nie zastępują sprawdzenia w konkretnej instalacji.

Kafelki OpenStreetMap są pobierane przez oficjalny mechanizm Map tiles Home Assistanta, bez dodatkowego klucza API do ustawienia. Dostawca otrzymuje żądania dotyczące oglądanego obszaru. Biblioteka Leaflet jest dołączona lokalnie, a mapa wymaga dostępu do kafelków.

## Aktualizacje z main

To repozytorium dystrybucyjne nie publikuje GitHub Releases. HACS korzysta z domyślnej gałęzi **main** i identyfikuje aktualizacje po commitach. Nowe pliki trafiają tutaj po przejściu testów. [Zasady HACS](https://www.hacs.dev/docs/publish/integration/).

Publikacja nowego commita nie instaluje go automatycznie w HA i nie uruchamia HA ponownie. Gdy HACS pokaże aktualizację, wykonaj kopię zapasową HA, zainstaluj aktualizację w HACS, uruchom ponownie HA i odśwież przeglądarkę. Numer `version` integracji może pozostać taki sam dla kolejnych commitów dystrybucji.

## Wycofanie i usunięcie

Przywróć `type: map` w wybranych kartach, aby ponownie pokazywały oryginalne dane HA. Aby usunąć rozszerzenie, usuń jego zasób JavaScript, integrację w ustawieniach HA oraz pliki przez HACS, a następnie uruchom ponownie HA. **Usunięcie integracji kasuje zapisane w niej ręczne pozycje i historię.** Historia Recorder pozostaje niezależna.

Problemy zgłaszaj w [Issues](https://github.com/Primo83/ha-manual-location/issues), bez PIN-u, tokenów, współrzędnych ani prywatnej konfiguracji.

## Biblioteki zewnętrzne

Leaflet 1.9.4 jest udostępniony na licencji BSD-2-Clause; dołączono [oryginalną licencję](custom_components/manual_location/frontend/vendor/LICENSE-Leaflet) i [informację o źródle](custom_components/manual_location/frontend/vendor/SOURCE.txt). Ta licencja dotyczy biblioteki Leaflet.
