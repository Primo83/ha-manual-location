# Manual Location Gateway

App towarzyszy integracji Manual Location 2.0. Panel i przełącznik pozostają w dotychczasowej wtyczce. Normalny GPS wymaga świadomego zapisu właściciela i 5 minut karencji. Restart App lub HA nie przywraca normalnego GPS.

Wersja 2.0.1 przekazuje potwierdzone zdarzenia normalnej lokalizacji do indeksu historii w integracji HA. Prawdziwe współrzędne telefonu nadal nie są zapisywane na dysku App. Wspólna historia i kasowanie wymagają również integracji 2.0.1; aktualizuj najpierw integrację, potem App, przy potwierdzonym custom. Aktualizacja nie uruchamia GPS.

Wersja App 2.0.2 rozszerza istniejący watchdog o żywotność zadań odzyskiwania, obsługi instalatora i certyfikatów. Po zakończeniu krytycznego zadania kontrola zdrowia zwraca błąd, aby Supervisor mógł wznowić App. W działającej instalacji włącz „Uruchamianie przy starcie” i „Watchdog”. Awaria procesu, restart App lub HA pozostawiają custom; nieznany dziennik wymaga naprawy przy zamkniętym GPS. Celowy STOP administratora pozostaje respektowany.

Instalacja wymaga skoordynowanego przeniesienia wejścia Core na prywatny loopback HTTPS z obowiązkowym certyfikatem klienta oraz sprawdzenia konfiguracji HTTP i kopii danych. Samo zainstalowanie App nie ustanawia ochrony. Nie zmieniaj ręcznie trybu staging/production, identyfikatorów, tokenu ani checkpointu w działającym systemie.

Docelowe środowisko: HAOS 18.3, Supervisor 2026.10.2, Core 2026.9.4; odbiór App na urządzeniu pozostaje niepotwierdzony do zakończenia wdrożenia. Nowsze wersje wymagają ponownej weryfikacji. Wymaga istniejącej rejestracji Companion bez szyfrowania/cloudhook/remote UI. Certyfikaty są odczytywane z /ssl, konfiguracja HA wyłącznie do odczytu, stan bramki z trwałego /data. Po awarii dane mogą pozostawać zablokowane do naprawy. Przy wyłączonej App publiczny panel także może być niedostępny; nie omijaj tego przez ponowne otwarcie Core.

App instaluje się z ręcznym rozruchem; nie należy jej uruchamiać przed konfiguracją i przygotowaniem instalatora. Instalator używa katalogu konfiguracji App: odczytuje status.json z nowym boot_nonce, a następnie atomowo zapisuje request.json z tym nonce, unikalnym id, action i payload. Dopuszczone są tylko initialize z payload {"core_stopped":true}, po świeżym potwierdzeniu zatrzymania Core i zakończeniu starych handlerów, oraz attach z pustym payload. Wynik result.json zawiera wyłącznie id, success i error_code. Każde żądanie zostaje usunięte przed wykonaniem i nie jest odtwarzane po restarcie. Attach konfiguruje istniejącą integrację przez natywny OptionsFlow; lokalne, uwierzytelnione odpowiedniki /control/initialize i /control/attach są dostępne wyłącznie na loopback App. Nie ma operacji czyszczenia niepewnego checkpointu. Kluczy i tokenów nie należy publikować ani kopiować do zgłoszeń.

Koła zależności i obraz są przypięte. Pliki package-manifest.json wiążą opublikowane bajty ze źródłem. To repo nie zawiera konfiguracji domowej, haseł ani współrzędnych.
