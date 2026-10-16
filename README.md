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

## Mapa na telefonie i aktualizacje interfejsu

W widoku panelowym mapa zajmuje pełną dostępną wysokość, także po obrocie telefonu. Ręczne przybliżenie lub przesunięcie zatrzymuje automatyczne dopasowywanie; „Dopasuj mapę” przywraca widok wszystkich punktów. Aktualizacja danych nadal działa w tle. Zmiana konta lub listy encji resetuje zapamiętany widok.

Po zainstalowaniu nowszej wersji integracji i restarcie HA otwarty interfejs wykrywa wydanie co około 30 sekund oraz po powrocie do widocznej aplikacji. Wczytuje wtedy świeże pliki automatycznie. Przeładowanie jest wstrzymane podczas edycji panelu; może jednokrotnie przywrócić początkowy widok mapy po zmianie wydania. Nie jest to automatyczna instalacja aktualizacji HACS.

Klient z wersją starszą niż 0.1.4 nie ma jeszcze tego mechanizmu: przy pierwszym przejściu może być konieczne zwykłe odświeżenie lub ponowne otwarcie widoku. Zamknięta lub uśpiona aplikacja sprawdza wersję po wznowieniu, gdy odzyska połączenie.
## Podgląd niezapisanych punktów

Każde kliknięcie **Ustaw bieżącą pozycję** z innymi współrzędnymi dodaje osobny fioletowy szkic z datą i godziną. Możesz przygotować kilka szkiców, zmienić ich czas i współrzędne przyciskiem **Edytuj** albo usunąć je ikoną kosza. Lista jest uporządkowana od najnowszych do najstarszych; punkt z wcześniejszą godziną trafia między odpowiednie wpisy. **Zapisz wszystkie zmiany** znajduje się pod mapą, bezpośrednio nad listą. Zapis zachowuje przygotowane czasy bez dodatkowej kopii bieżącego punktu.

Bieżącą pozycją po zapisie będzie najnowszy szkic dodany przyciskiem **Ustaw bieżącą pozycję** (także po edycji jego czasu). Usunięcie wszystkich takich szkiców istniejącej osoby przywraca jej zapisaną pozycję, zachowując inne zmiany historii. Dla nowej osoby pozostawione punkty są zachowane, a najnowszy staje się bieżącym; usunięcie ostatniego punktu usuwa jej roboczą konfigurację. Edycja samej zapisanej historii nadal nie przesuwa bieżącej pozycji. Oznaczenia znikają dopiero po potwierdzonym zapisie lub wczytaniu danych; inne karty otrzymują zmiany po zapisie.
## Historia jak w natywnej mapie

Długość widocznej trasy określa `hours_to_show` w każdej karcie. Wartość `24` oznacza przesuwające się ostatnie 24 godziny, a `0` wyłącza trasę. Starsze punkty i odcinki są bledsze, nowsze wyraźniejsze: stopniowanie zależy od ich kolejności, tak jak w natywnym rendererze Home Assistant. Początkowy stan sprzed okna jest pokazywany na jego granicy, jeśli w tym momencie nie ma już punktu. Po wskazaniu punktu można odczytać czas.

Okno mapy nie usuwa zapisanej historii z panelu administratora. Bieżący marker nadal pokazuje osobno zapisaną pozycję, również gdy historia jest pusta lub zawiera tylko starsze wpisy. Zachowane są zdjęcia oraz ręcznie wybrany zoom.

Wzorzec zgodności: [renderer HA frontend 20260826.7](https://github.com/home-assistant/frontend/blob/20260826.7/src/components/map/ha-map.ts) oraz [karta mapy](https://github.com/home-assistant/frontend/blob/20260826.7/src/panels/lovelace/cards/hui-map-card.ts). Uwzględniono prawidłową, skończoną przezroczystość także dla pojedynczego punktu i trasy z dwoma punktami.

## Scenariusz trasy na dzień

Przy planie w **Bibliotece tras** kliknij **Duplikuj**, aby użyć go jako szablonu — także gdy jest już ukończony. Otworzy się niezależny szkic z dopiskiem „(kopia)”, ze wszystkimi zastosowanymi punktami i godzinami, bez postępu wykonania. Zmień **Dzień trasy**, dostosuj punkty i kliknij **Zapisz plan**. Godziny pozostają zgodne ze strefą HA; data ze zmianą czasu może wymagać wybrania innej, jednoznacznej godziny. Oryginał pozostaje zachowany. Kopia zacznie wykonanie dopiero po osobnym **Start**; niezastosowany formularz punktu źródłowego pozostaje w źródle.

Na mapie edytora trasy celownik **Użyj lokalizacji tego urządzenia** jednorazowo pobiera pozycję telefonu lub komputera z otwartym panelem. Po zgodzie przeglądarki pokazuje punkt i obszar dokładności oraz uzupełnia współrzędne lokalnego edytora. Dzień i godzina pozostają zachowane; punkt dodajesz lub zatwierdzasz osobno, a plan zapisujesz przez **Zapisz plan**.

Przy godzinie wybierz odstęp **5, 10, 15, 20, 30, 45, 60, 90 lub 120 minut** (domyślnie 10), a następnie **Uzupełnij godzinę**. Podstawą jest ostatni chronologicznie punkt trasy. Pierwszy punkt ustawiasz ręcznie. Przycisk służy nowemu punktowi; podczas edycji istniejącego jest wyłączony. Nie dodaje ani nie zapisuje punktu automatycznie, a ponowne kliknięcie bez dodania punktu daje tę samą godzinę. Przekroczenie dnia trasy zgłasza komunikat, zamiast przenosić punkt na kolejny dzień.

W panelu administratora otwórz **Plany i szkice**. Biblioteka pokazuje wszystkie zapisane trasy z osobą, dniem, godzinami, statusem i postępem; możesz filtrować ją według osoby i statusu. Przy każdym planie są osobne przyciski otwarcia, uruchomienia, zatrzymania i usunięcia. **Nowy plan** tworzy kolejny szkic dla wybranej osoby, bez zastępowania poprzedniego.

Jedna osoba może mieć wiele planów. Możesz zapisać nazwany szkic bez punktów i wrócić do niego po odświeżeniu lub restarcie HA. Niezapisane zmiany pozostają tylko w tej karcie i mają osobne oznaczenie. Maksymalnie 100 zapisanych planów łącznie, każdy na jeden dzień w strefie HA i do 500 punktów o różnych godzinach. Oczekujące punkty można edytować i usuwać; wykonane pozostają częścią historii.

**Zapisz scenariusz** przechowuje plan. Dopiero **Uruchom** włącza wykonanie. O zaplanowanej porze HA ustawia kolejny punkt jako bieżącą pozycję i dopisuje go do ręcznej historii, widocznej w kartach mapy. Przyszłe punkty widać wyłącznie w podglądzie planu administratora. Telefon ani otwarta przeglądarka nie są wymagane; HA musi działać.

Po przerwie HA uruchomiony plan uzupełnia zaległe punkty z ich oryginalnymi godzinami i ustawia ostatni należny jako bieżący. **Zatrzymaj** zachowuje wykonaną historię i nie wznawia planu samoczynnie. Ręczny zapis zmienionej pozycji lub historii danej osoby oraz wyłączenie ręcznych lokalizacji zatrzymują jej aktywny scenariusz. Usunięcie planu zachowuje już wykonaną historię.

Zarządzanie scenariuszem wymaga odblokowanego panelu administratora. Po świadomym uruchomieniu wygaśnięcie sesji PIN nie przerywa trasy. Konflikt godzin z zapisaną historią, limit historii, brak osoby lub jej wyłączenie zatrzymują wykonanie z informacją o przyczynie. Plan dotyczy map Manual Location; dane encji person/GPS i Recorder pozostają oddzielne.

Plany różnych osób mogą działać równocześnie. Dla tej samej osoby możesz wcześniej uruchomić kilka tras o rozłącznych godzinach. **Uruchom** odrzuci plan, jeśli jego przedział od pierwszego do ostatniego punktu przecina się z inną uruchomioną trasą tej osoby; także wspólna godzina graniczna jest konfliktem. Szkice i zatrzymane plany mogą się pokrywać jako alternatywy. Sam zapis nigdy nie uruchamia trasy.

Przed aktualizacją wykonaj kopię konfiguracji HA. Wersja 0.1.11 automatycznie przenosi dotychczasowy plan każdej osoby do biblioteki, zachowując punkty i postęp. Starszy otwarty panel wymaga odświeżenia przed zmianą planów. Powrót do 0.1.10 lub starszej wersji wymaga także przywrócenia danych integracji z kopii sprzed aktualizacji.

## Edycja punktu na mapie

Kliknij **Edytuj** przy zapisanym punkcie lub szkicu: mapa pokaże wyróżniony pomarańczowy znacznik z datą i godziną. Kliknięcie nowego miejsca zmienia jego podgląd i współrzędne w formularzu. Ręczne wpisanie współrzędnych również przesuwa znacznik. Godzina pozostaje zachowana, dopóki nie zmienisz jej w polu czasu. Niepoprawne współrzędne lub czas ukrywają podgląd do poprawienia formularza.

Przy mapie kliknij **Zastosuj edycję**, aby przygotować zmianę, albo **Anuluj edycję**, aby ją odrzucić. Dane zostaną utrwalone po **Zapisz wszystkie zmiany**; podczas trwającej edycji należy ją najpierw zastosować albo anulować. Inne przygotowane szkice pozostają w formularzu.
