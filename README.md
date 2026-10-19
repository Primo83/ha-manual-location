# Manual Location for Home Assistant

Ręczne pozycje i historia osób na wybranych kartach mapy Home Assistanta. Administrator zarządza nimi we wspólnym panelu **Ustawienia mapy**, chronionym uprawnieniami administratora HA i dodatkowym PIN-em.

Bez skonfigurowanej niezależnej bramki integracja zachowuje działanie 1.x: zmienia prezentację w kartach `custom:manual-location-map`, a encje `person`, GPS z telefonów, automatyzacje i Recorder pozostają niezależne. Osobne przytrzymanie systemowej ikony przez administratora może poprosić przypisany telefon Companion o nową lokalizację. Inne ekrany i API mogą wtedy pokazywać oryginalne dane. PIN nie chroni przed właścicielem serwera z dostępem do plików lub kodu.

## Opcjonalna bramka 2.0

Samo pobranie aktualizacji HACS **nie blokuje raportów telefonu**. Wariant 2.0 wymaga niezależnej App z repozytorium `https://github.com/Primo83/ha-manual-location#codex/gateway-apps`, prywatnego wejścia Core dostępnego wyłącznie na loopbacku oraz podłączenia adaptera w opcjach integracji. Własny bieżący punkt trafia wtedy do natywnych encji tracker i person i może uruchamiać automatyzacje oraz zapisy Recorder. Edytowana historia w panelu nadal pozostaje osobna od Recorder.

Obsługa pozostaje w **Ustawieniach mapy**: wybierz źródło i kliknij **Zapisz wszystkie zmiany**. Świadomy wybór Home Assistant rozpoczyna 300 sekund karencji; przez ten czas nadal obowiązuje własny punkt. Po karencji dopuszczane są raporty Companion, również potencjalnie znacznie opóźnione — czas oczekiwania nie dowodzi świeżości pomiaru. Restart, awaria, Stop i koniec własnej trasy nie włączają GPS. Po powrocie działania zapisana trasa nadrabia punkty jak 1.x.

Awaria App zamyka drogę do panelu i raportowania; nie uruchamia bezpośredniego publicznego Core. Zachowaj niezależny dostęp serwisowy. Przy wycofywaniu tej konfiguracji pozostaw Core prywatny i bramkę zamkniętą; przywrócenie publicznego Core usuwa ochronę. Nie edytuj ręcznie `.storage`. Dalsza instrukcja HACS opisuje instalację samej integracji i jej podstawowe działanie bez bramki.

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
- Bieżąca pozycja jest przechowywana osobno, ale zawsze ma widoczny wiersz na liście. Usunięcie innego punktu historii jej nie przesuwa; usunięcie punktu oznaczonego „Bieżąca pozycja” wybiera najnowszy pozostały punkt z jego oryginalną godziną. Zmiana bieżącej pozycji dopisuje punkt historyczny.
- Aktywna osoba korzysta wyłącznie z ręcznej historii. Przy błędzie odczytu lub ponownym łączeniu karta ukrywa lokalizacje do czasu poprawnego odczytu ustawień.
- Zwykły użytkownik nie ma dostępu do panelu administracyjnego. PIN ma limit prób i czasową sesję.

Po instalacji sprawdź zapis pozycji, dodawanie i usuwanie historii, przełącznik włączenia oraz zachowanie ustawień po restarcie HA. Sprawdź także widok na zwykłym koncie. Lokalne testy projektu nie zastępują sprawdzenia w konkretnej instalacji.

Kafelki OpenStreetMap są pobierane przez oficjalny mechanizm Map tiles Home Assistanta, bez dodatkowego klucza API do ustawienia. Dostawca otrzymuje żądania dotyczące oglądanego obszaru. Biblioteka Leaflet jest dołączona lokalnie, a mapa wymaga dostępu do kafelków.

## Źródło lokalizacji każdej osoby

Po odblokowaniu panelu podsumowanie pokazuje każdą osobę i jej **zapisane źródło lokalizacji**. Kliknięcie osoby wybiera jej ustawienia. Osobny opis **Po zapisie** pokazuje przygotowaną zmianę; mapy przełączą źródło dopiero po **Zapisz wszystkie zmiany**, dostępnym nad formularzem.

W ustawieniach wybranej osoby wybierz **Home Assistant** albo **Ręczna (custom)**. Home Assistant przywraca dane HA, zachowując ręczną pozycję i historię do ponownego użycia. Osoba bez ręcznej pozycji korzysta z HA; najpierw przygotuj pierwszy punkt, aby włączyć dla niej źródło ręczne. Jawne ustawienie pierwszego punktu tworzy włączony ręczny szkic, wymagający wspólnego zapisu.

Przełącznik globalny dotyczy **wszystkich osób**. Jego wyłączenie oznacza źródło HA na mapach, także gdy indywidualnie wybrano ręczną lokalizację. Podsumowanie uwzględnia tę zależność i odróżnia zapisany stan od szkicu. Sam wybór osoby nie tworzy ustawień ani niezapisanych zmian.

Sekcja **Aktywność telefonu** jest podpisana imieniem wybranej osoby i działa niezależnie od źródła lokalizacji. Jednakowe domyślne zaznaczenia sygnałów u dwóch osób są prawidłowe; późniejsze zmiany i przywrócenie domyślnych dotyczą tylko wybranej osoby. Szczegóły źródła są widoczne w panelu administratora, a obie karty mapy zachowują jednakowy neutralny wygląd ikon i dymków.

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

Bieżącą pozycją po zapisie będzie ostatnio ustawiony lub wybrany punkt. Przycisk **Ustaw bieżącą** przy **Edytuj** wybiera istniejący punkt bez kopiowania historii i zachowuje jego dokładną godzinę. Przy dodawaniu lub edycji zaznacz **Ustaw także jako bieżącą pozycję**, aby wykonać obie czynności razem; opcja jest domyślnie wyłączona. Edycja czasu wybranego szkicu zachowuje jego wybór, nawet gdy inny punkt ma późniejszą godzinę. Wszystkie te zmiany wymagają **Zapisz wszystkie zmiany**. Usunięcie wszystkich szkiców bieżącej pozycji istniejącej osoby przywraca jej zapisaną pozycję, o ile nie została wcześniej jawnie usunięta; zachowuje pozostałe zmiany historii. Dla nowej osoby najnowszy pozostały punkt staje się bieżącym, a usunięcie ostatniego usuwa jej roboczą konfigurację. Edycja zapisanej historii bez zaznaczenia nowej opcji pozostawia bieżącą pozycję bez zmian. Oznaczenia znikają po potwierdzonym zapisie lub wczytaniu danych.
## Historia jak w natywnej mapie

Bieżąca pozycja jest pokazana na liście również wtedy, gdy jej wpis został wcześniej usunięty z historii. Ma oznaczenie **Bieżąca pozycja** i kosz. Jej usunięcie wybiera najnowszy pozostały punkt; mapa panelu zmienia się od razu, a karty po **Zapisz wszystkie zmiany**. Usuwanie innych punktów historii zachowuje bieżący marker. Gdy usuwasz ostatnią ręczną pozycję, panel wymaga potwierdzenia usunięcia ustawień osoby i informuje o powrocie kart do danych HA po zapisie. Anulowanie niczego nie usuwa.

Długość widocznej trasy określa `hours_to_show` w każdej karcie. Wartość `24` oznacza przesuwające się ostatnie 24 godziny, a `0` wyłącza trasę. Starsze punkty i odcinki są bledsze, nowsze wyraźniejsze: stopniowanie zależy od ich kolejności, tak jak w natywnym rendererze Home Assistant. Początkowy stan sprzed okna jest pokazywany na jego granicy, jeśli w tym momencie nie ma już punktu. Po wskazaniu punktu można odczytać czas.

Okno mapy nie usuwa zapisanej historii z panelu administratora. Bieżący marker nadal pokazuje osobno zapisaną pozycję, również gdy historia jest pusta lub zawiera tylko starsze wpisy. Zachowane są zdjęcia oraz ręcznie wybrany zoom.

Wzorzec zgodności: [renderer HA frontend 20260826.7](https://github.com/home-assistant/frontend/blob/20260826.7/src/components/map/ha-map.ts) oraz [karta mapy](https://github.com/home-assistant/frontend/blob/20260826.7/src/panels/lovelace/cards/hui-map-card.ts). Uwzględniono prawidłową, skończoną przezroczystość także dla pojedynczego punktu i trasy z dwoma punktami.

## Wyszukiwanie miejsc i adresów — Geoapify Free

Na mapie wyboru pozycji i w edytorze trasy wpisz nazwę miejsca albo pełny adres, a następnie kliknij **Szukaj** lub Enter. Wybierz jeden z maksymalnie pięciu wyników. Wybór uzupełnia współrzędne i podgląd; dzień oraz godzina pozostają zachowane. Punkt dodajesz/zatwierdzasz i zapisujesz osobno, a **Start** planu nadal jest osobną czynnością.

Wymagane jest bezpłatne konto [Geoapify MyProjects](https://myprojects.geoapify.com/) i klucz projektu. Wpisz klucz osobiście w HA: **Ustawienia → Urządzenia i usługi → Manual Location → Konfiguruj**. Pole pozostaje puste przy kolejnym otwarciu; puste zatwierdzenie zachowuje dotychczasowy klucz, a osobna opcja pozwala go usunąć. Nie wklejaj klucza w zgłoszeniach lub czacie. Wyszukiwarka korzysta z klucza po stronie HA; odpowiedzi i pliki mapy go nie zawierają. Konfiguracja klucza nie wymaga następnego restartu.

Zapytanie tekstowe jest wysyłane do Geoapify dopiero po świadomym wyszukaniu. Bieżące współrzędne ani pozycja GPS nie są automatycznie przesyłane. Wyniki preferują Polskę, ale obejmują również inne kraje. Sprawdź typ wyniku, numer i podgląd: wynik ulicy nie potwierdza konkretnego numeru budynku. Widoczne odnośniki **Powered by Geoapify** i **© OpenStreetMap** oraz informacja o źródle danych stanowią wymaganą atrybucję.

Integracja stosuje wspólny trwały budżet **2500 jednostek na dzień UTC** i limit jednego nowego wywołania na sekundę. Wyszukiwanie rezerwuje 1 jednostkę; routing konserwatywnie 100 przed wysłaniem, a po poprawnym wyniku zwraca nadwyżkę według liczby odcinków i długości. Niepotwierdzona odpowiedź zachowuje rezerwację. To lokalna ochrona zużycia, nie gwarancja rozliczeń dostawcy; jedno żądanie routingu może zużywać więcej niż jeden kredyt. Ten limit obejmuje tylko tę integrację, nie inne zastosowania tego samego klucza. [Geoapify Free](https://www.geoapify.com/pricing/) udostępnia 3000 kredytów dziennie bez karty płatniczej. Wyszukiwarka nie przełącza konta na płatny plan i nie ponawia zapytań automatycznie. Brak wyników, limit lub awaria usługi daje komunikat; ręczny wybór na mapie nadal działa. Free nie obejmuje gwarancji SLA ani kompletności adresów. [Warunki Geoapify](https://www.geoapify.com/terms-and-conditions/).

## Scenariusz trasy na dzień

### Edycja godziny i oznaczanie usunięć

Kliknij **Edytuj** przy punkcie Historii lub planu. Formularz otworzy się bezpośrednio pod nim: zmienisz współrzędne oraz datę i godzinę w Historii lub godzinę w planie, także z sekundami. **Zastosuj edycję** przygotowuje zmianę lokalnie; dopiero **Zapisz wszystkie zmiany** lub **Zapisz plan** zatwierdza ją w HA. **Anuluj** odrzuca niezastosowany formularz. Zmiana samych współrzędnych zachowuje oryginalny czas i ułamki sekundy. Historia używa czasu urządzenia, a plan strefy HA wskazanej w edytorze.

Po kliknięciu kosza punkt pozostaje na liście na czerwono z opisem **Do usunięcia po zapisie**. Znika dopiero po udanym zapisie. Błąd zapisu zachowuje oznaczenie; **Wczytaj zapisane dane** lub **Odrzuć szkic planu** przywraca zapisany stan. Czerwone punkty są wyłącznie podglądem usunięć: nie są edytowalne ani wybieralne do szablonu i nie są częścią mapy lub wykonywanej trasy. Dotyczy to również nowych, jeszcze niezapisanych punktów. Wykonane punkty trasy pozostają chronione.

### Szablon z punktów historii

W sekcji **Historia** zaznacz checkboxami zapisane punkty jednej osoby z jednego dnia i kliknij **Utwórz szablon trasy**. W bibliotece otworzy się niezależny szkic **Szablon z historii**: punkty będą od najstarszego do najnowszego, z oryginalnymi godzinami i sekundami. Historia źródłowa pozostaje bez zmian; fioletowe niezapisane punkty trzeba najpierw zapisać. Zmień **Dzień trasy** i godzinę początku w **Czas trasy**, dostosuj punkty, następnie wybierz **Zapisz plan** i osobno **Start**. Limit to500 punktów z jednego dnia według strefy HA; błędny wybór pozostaje zaznaczony, a częściowy szablon nie powstaje.

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

## Wyznaczanie, import i zmiana czasu trasy

### Przystanki pośrednie i przebudowa dalszej trasy

W formularzu wyznaczania kliknij **Dodaj przystanek pośredni**. Wybierz każde miejsce z wyników wyszukiwania albo wskaż je na mapie. Środkowe miejsca możesz usuwać i przesuwać **W górę / W dół**; początek i cel pozostają końcami. Trasa prowadzi przez wszystkie przystanki w podanej kolejności. Każdy otrzymuje własny punkt z nazwą; pomiędzy nimi nadal powstają automatyczne pomiary, np. losowo9–15min. Obowiązkowy przystanek może skrócić odstęp pomiarów. Samo wskazanie miejsca nie dodaje czasu postoju; przerwę ustawisz przesunięciem dalszych godzin.

W istniejącym planie wybierz **Przelicz od teraz** (od ostatniej wykonanej pozycji) albo **Przelicz odtąd** przy przyszłym punkcie. Przy odtąd dotychczasowy fragment do tego punktu włącznie zostaje zachowany. Formularz załaduje dalsze przystanki; dodaj lub przestaw miejsca, sprawdź transport i wyznacz podgląd. Początek przebudowy, dzień i jego czas są zablokowane. W trybie teraz godzina jest odświeżana podczas wyznaczania. Jeśli plan nie wykonał jeszcze punktu, użyj odtąd.

Podgląd zmienia wyłącznie szkic tego samego planu. **Zapisz plan** zatwierdza dalszą trasę; działający scenariusz kontynuuje wykonanie, a wykonane punkty i historia pozostają bez zmian. Ukończony plan z dodaną dalszą częścią przechodzi do zatrzymanego i wymaga **Start**. **Anuluj przebudowę** odrzuca formularz miejsc, a **Odrzuć szkic planu** przywraca zapisany plan. Błąd wyznaczenia/zapisu zachowuje dotychczasową trasę. Jeśli w międzyczasie wykonano kolejny punkt, wczytaj aktualny plan i przelicz ponownie.

Własny czas przy przebudowie oznacza czas **pozostałej części**, a nie całej poprzedniej podróży. Stare plany bez zapisanych ustawień wymagają ponownego wyboru transportu. Limit to12miejsc jednego wyznaczenia i500punktów całego planu, razem z zachowanym fragmentem; trasa nadal musi zmieścić się w jednym dniu HA.

W **Plany i szkice → Wyznacz lub importuj trasę** wybierz **Wyznacz we wtyczce**. Wyszukaj początek i cel, a następnie jawnie wybierz właściwe miejsca z wyników. Wybrane miejsce możesz zmienić przed wyznaczeniem. Google Maps nie jest wymagane.

Alternatywnie wybierz **Link Google Maps**, wklej link (także maps.app.goo.gl) i kliknij **Odczytaj link**. Sprawdź początek, cel i przystanki; przy samych nazwach wybierz dokładne wyniki. W obu trybach wybierz transport: pieszo, samochód, rower albo komunikację publiczną.

Ustaw dzień i godzinę początku, także z sekundami. Domyślnie punkty powstają w **losowym przedziale 9–15 minut**. Możesz zmienić minimum i maksimum albo wybrać dotychczasowy stały odstęp. Losowana jest liczba pełnych sekund pomiędzy granicami, np. 9 min 37 s, a nie tylko całe minuty. Początek i koniec są zachowane; ostatni odcinek może być krótszy niż minimum. Losowanie odbywa się raz podczas wyznaczania: zapis, edycja, zmiana dnia i przesunięcie czasu zachowują uzyskane godziny oraz sekundy.

Pole **Własny czas podróży (min)** jest opcjonalne. Możesz podać np. 70 minut odczytanych z Google; to ręczny wpis, bez odczytu czasu ze strony Google i bez Google Cloud. Puste pole korzysta z szacunku Geoapify dla wybranego transportu. Wpisany czas proporcjonalnie skaluje etapy. W losowym trybie czas jest zaokrąglany do pełnej sekundy. **Wyznacz trasę i utwórz szkic** tworzy osobny lokalny plan. Sprawdź podgląd, nazwę i godziny, następnie **Zapisz plan**; **Start** uruchamia go osobno.

Geoapify wyznacza drogę i orientacyjne pozycje na niej; przebieg może różnić się od wariantu Google. Komunikacja publiczna jest przybliżeniem, bez gwarancji konkretnego pociągu/autobusu, rozkładu i czasów przesiadek. Brak obsługi połączenia zgłasza błąd zamiast zastępować je samochodem. Wyszukiwanie wysyła nazwę dopiero po kliknięciu Szukaj; routing wysyła zatwierdzone współrzędne po kliknięciu wyznaczania. Oba korzystają z obecnego wspólnego limitu Geoapify. Maksymalnie 12 miejsc i 500 punktów; trasa musi zmieścić się w jednym dniu HA.

**Czas trasy** pozwala ustawić godzinę pierwszego oczekującego punktu, przesuwając wszystkie dalsze o tę samą różnicę. Przy wybranym punkcie kliknij **Przesuń odtąd** i podaj przesunięcie w minutach (np. +40 na przerwę, −15 na wcześniejszy wyjazd) albo jego nową godzinę. Zastosowanie zmienia szkic; **Zapisz plan** zatwierdza go w HA.

Oczekujące punkty możesz zmieniać także podczas działania trasy, bez Stop i ponownego Start. Wykonane punkty oraz historia pozostają niezmienione; oczekujące godziny aktywnego planu muszą być w przyszłości. Jeśli w trakcie edycji HA wykona punkt, zapis szkicu zostanie wstrzymany: odrzuć szkic, wczytaj aktualny plan i ponów korektę. Chroni to postęp przed nadpisaniem. Kolizja z inną uruchomioną trasą tej samej osoby lub przekroczenie dnia również blokuje zapis.

Opcja **Pobierz GPS i zapisz pozycję** pod przyciskiem lokalizacji urządzenia wykonuje odczyt i zapis bieżącej pozycji oraz punktu historii jednym kliknięciem. Najpierw zapisz lub odrzuć inne zmiany. Dopiero potwierdzony zapis zmienia widok; chmurka wyniku jest widoczna bez przewijania. Przy braku potwierdzenia wczytaj zapisane dane przed ponowną próbą. Ręczna zmiana zatrzymuje aktywny plan tej osoby.

W ustawieniach osoby nadaj **Pseudonim na ekranie PIN** i zaznacz **Szybki zapis na ekranie PIN dla tej osoby**, a następnie zapisz zmiany. Przypisanie jest wspólne dla urządzeń. Na ekranie PIN pojawi się wyłącznie pseudonim oraz neutralny przycisk **Aktualizuj**. Wpisz PIN i kliknij Aktualizuj: po sprawdzeniu PIN-u potwierdź akcję; lokalizacja zapisze się dla przypisanej osoby bez otwierania dashboardu. Jednorazowa sesja zostaje zamknięta, a chmurka pokazuje neutralny wynik. Pseudonim zmienia podpis, nie uprawnienia administratora HA ani dostęp przez API.

## Sprawdzenie aktualizacji pozycji

Administrator może przytrzymać bieżącą ikonę osoby przez około 0,75 sekundy. Zwykły klik nadal pokazuje dymek z godziną. Wygląd i komunikaty ikon są jednakowe. Gdy osoba korzysta z systemowej lokalizacji, integracja prosi jej jednoznacznie przypisany telefon Companion o aktualizację. Przy aktywnej pozycji ręcznej sprawdza wyłącznie dane mapy, bez pobierania prawdziwego GPS ani zmiany zapisanej godziny.

Jedna prośba na osobę i telefon na minutę zapobiega wielokrotnemu wywołaniu z różnych kart. Oczekiwanie trwa maksymalnie **60 sekund**. Integracja rozpoznaje rzeczywisty raport przypisanego telefonu również wtedy, gdy współrzędne i czas zmiany encji `person` pozostały identyczne. „Odebrano nowsze dane” potwierdza nowy raport lub nowsze dane mapy, nie gwarantuje wykonania osobnego pomiaru GPS na żądanie: w tym oknie może dotrzeć także raport automatyczny. Bez potwierdzenia pojawia się „Nie potwierdzono aktualizacji pozycji”. Po utracie połączenia oczekiwanie jest kończone; spóźniona odpowiedź nie zatwierdza kolejnej próby.

Przytrzymanie nie zmienia sztucznie godziny `person` ani GPS i nie zapisuje historii, ustawień lub cache aktywności. Sam restart HA nie jest potwierdzeniem odebrania raportu. Telefon musi mieć dostęp do sieci i uprawnienia lokalizacji; opóźnienia dostarczania i pomiaru zależą od Androida oraz aplikacji. Funkcja nie wymaga włączania czujników aktywności i nie zmienia ich ustawień. [Dokumentacja Companion](https://companion.home-assistant.io/docs/notifications/notification-commands/#request-location-updates).

Dla diagnostyki dedykowany logger `custom_components.manual_location.coordinator.location_refresh` zapisuje etapy operacji na poziomie INFO: losowy identyfikator, etap oraz czas trwania. Nie zapisuje współrzędnych, nazw i identyfikatorów osób lub telefonów, PIN-u, tokenów, webhooków ani treści raportów i wyjątków. Pozostałe loggery zachowują swoje ustawienia.

## Aktywność telefonu

Dymek bieżącej pozycji na obu kartach może pokazywać kropkę i datę z godziną wykrytej aktywności telefonu. Wygląd i podpis są jednakowe dla wszystkich osób. Domyślnie wskaźnik jest włączony: zielona kropka oznacza odblokowanie telefonu **lub** rozpoznane chodzenie/bieg w ostatnich 60 minutach, a czerwona — starszą potwierdzoną aktywność. To wskazówka aktywności urządzenia, nie pewne rozpoznanie snu lub obecności człowieka.

W panelu **Ustawienia mapy**, po wpisaniu PIN-u i wybraniu osoby, sekcja **Aktywność telefonu** pozwala osobno włączyć wskaźnik i wybrać sygnały: **Odblokowanie**, **Chodzenie i bieg**, opcjonalnie **Włączony ekran** oraz **Poruszenie / przechylenie telefonu**. Ekran może obudzić również powiadomienie. Tryb **Dowolny** wymaga jednego zaznaczonego sygnału; **Wszystkie w ostatniej godzinie** wymaga każdego z nich w ostatnich 60 minutach, bez konieczności wystąpienia naraz. Godzina przy tym drugim trybie dotyczy najstarszego z wymaganych potwierdzeń. Zatwierdź ustawienia przyciskiem **Zapisz wszystkie zmiany**. Nie wymagają one ustawiania ręcznej pozycji osoby i nie zmieniają GPS, tras ani Recorder.

Checkbox **Poruszenie / przechylenie telefonu** jest domyślnie wyłączony; dotychczasowe odblokowanie lub chodzenie pozostają domyślne. Możesz wybrać go samodzielnie albo razem z innymi sygnałami w trybie Dowolny/Wszystkie. Korzysta z rozpoznanego stanu `tilting` tego samego czujnika **Detected activity**. Ruch w kieszeni lub aucie też może zadziałać: nie dowodzi rzeczywistego użycia telefonu i nie gwarantuje wykrycia każdego małego przechylenia.

Na swoim telefonie właściciel włącza udostępnianie potrzebnych czujników w **Companion → Ustawienia → Aplikacja towarzysząca → Zarządzaj czujnikami**: **Interactive** i **Keyguard locked** dla odblokowania, **Detected activity** dla chodzenia/biegu i poruszenia/przechylenia, samo **Interactive** dla ekranu. Nazwy menu mogą zależeć od języka aplikacji. Android może wymagać osobnego zezwolenia na rozpoznawanie aktywności. Przełączniki panelu wybierają warunki; nie włączają zdalnie czujników ani uprawnień telefonu. Źródło musi jednoznacznie należeć do przypisanego telefonu Companion.

Brak, wyłączony lub niedostępny czujnik, brak uprawnienia odczytu albo niepotwierdzona świeżość odczytu daje szarą kropkę **brak danych**, zamiast udawanej bezczynności. Odczyty starsze niż 30 minut tracą potwierdzenie świeżości; w trybie Dowolny może wystarczyć inny świeży dodatni sygnał. Po restarcie HA potrzebny jest nowy rzeczywisty raport telefonu — sam odtworzony stan nie tworzy zielonej kropki ani nowej godziny.

Opcja ręcznej czerwonej pozwala od razu ustawić wybraną przeszłą datę i godzinę. Po zapisie obowiązuje aż do jej odznaczenia i ponownego zapisu, także po restarcie; późniejsze raporty telefonu jej nie zmieniają. Przyszła lub niepełna data blokuje zapis. W zwykłym dymku pozostaje ten sam podpis i wygląd. Integracja przechowuje tylko ostatnie dodatnie czasy wybranych sygnałów w oddzielnym prywatnym cache, bez historii użycia aplikacji, powiadomień i surowych raportów.

Wersja 0.1.28 dodaje ustawienia aktywności do magazynu integracji. **Powrót do 0.1.27 lub starszej wersji wymaga przywrócenia także danych integracji z kopii sprzed aktualizacji.** Nie edytuj ręcznie plików `.storage`. [Dokumentacja czujników Companion](https://companion.home-assistant.io/docs/core/sensors/).

Po zapisaniu sygnału poruszenia/przechylenia w 0.1.29 powrót do 0.1.28 wymaga przywrócenia danych integracji i cache z kopii sprzed rozszerzenia: starsza wersja nie rozpoznaje nowego sygnału.
