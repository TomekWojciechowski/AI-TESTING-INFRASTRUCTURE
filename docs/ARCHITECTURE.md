# Architektura

## Autoryzacja i wykonanie

1. Koordynator trzyma wyłączną blokadę `flock`. Przed przygotowaniem worktree zapisuje także `active-run.json`. Pozostawienie tego pliku po przerwaniu lub niepotwierdzonym zatrzymaniu blokuje wszystkie nowe uruchomienia z `--run`, również dla innego PR-a lub SHA.
2. Otwarty PR musi mieć etykietę `local-tests`, gałąź `claude/*`, head z oczekiwanego repozytorium i konta właściciela oraz 40 znaków SHA w zapisie szesnastkowym.
3. Przed pobraniem agent ponownie odczytuje PR z GitHuba. Porównuje `FETCH_HEAD` z autoryzowanym SHA i tworzy odłączony worktree dopiero po zgodnym porównaniu.
4. Agent wykonuje `npm ci --ignore-scripts`, odbudowę zależności, ESLint, testy jednostkowe Node, build i Playwright. Kończy po pierwszym niezaliczonym etapie. Kod testów może korzystać z sieci podczas instalacji zależności. Kolejne kontenery mają `--network none`.
5. Podman pracuje jako `ci-runner` z limitami pamięci, CPU i PID, bez capabilities, z `no-new-privileges` i `--userns=keep-id`. Koordynator uruchamia polecenia robocze bezpośrednio z UID, GID i grupami konta `ci-runner` oraz jawnie ustawia `PATH`, `HOME`, `LC_ALL`, `USER`, `LOGNAME` i `XDG_RUNTIME_DIR`. Ustawienia SSH trafiają do poleceń Git. Dodatkowe wpisy przekazywane do kontenera to `HOME=/tmp` oraz trzy flagi Vite. Automatyczne przekazywanie zmiennych proxy jest wyłączone.
6. Polecenia zewnętrzne startują we własnej sesji. Przy przekroczeniu limitu czasu lub przerwaniu koordynator zatrzymuje grupę procesu i sprawdza jej stan w systemie. Przed startem etapu zapisuje losową nazwę i etykietę kontenera. Po zakończeniu polecenia potwierdza brak kontenera lub jego stan końcowy. Po przerwaniu polecenia sam brak kontenera przy pierwszej inspekcji pozostawia blokadę. W razie potrzeby agent zatrzymuje kontener po pełnym ID, po sprawdzeniu nazwy i etykiety. Podman pracuje lokalnie z własnym limitem czasu etapu i wyłączonym restartem.

## Stan lokalny

Wynik identyfikuje PR, pełny SHA i zakres (`full`, `spec-<plik>` lub `visual`). Katalog stanu i poświadczenia należą do root. Agent zapisuje JSON do pliku tymczasowego obok docelowego, synchronizuje dane, podmienia plik przez `os.replace` i synchronizuje katalog. Wynik zawiera wersję schematu, identyfikator repozytorium, czasy UTC, statusy etapów, nazwę logu i stan publikacji.

- `RUNNING` zabezpiecza przed powtórnym uruchomieniem po przerwaniu procesu. Rekord pozostający w tym stanie po zwolnieniu blokady wymaga ręcznego sprawdzenia.
- `PASS` oznacza zaliczenie wszystkich etapów. `FAIL` oznacza, że co najmniej jeden etap zakończył się błędem albo przekroczył limit czasu.
- `BASELINES_GENERATED` oznacza ukończenie lokalnego generowania wzorców do oceny.
- Historyczne `.done` są odczytywane jako znaczniki pominięcia w dotychczasowych zakresach. Ich zawartość pozostaje bez zmian. Gdy historyczny `-full.done` nie ma rekordu pełnego przebiegu 2.0, agent pomija też `e2e/full.spec.js`, ponieważ obie operacje mogły utworzyć taki znacznik.

Logi starsze niż 14 dni są sprzątane przy przygotowaniu nowego przebiegu. JSON i znaczniki pozostają na dysku, ponieważ służą do deduplikacji. Worktree i artefakty są obsługiwane jako `ci-runner`.

## Publikacja

Wynik testów jest zapisywany przed sprzątaniem i przed publikacją. Zaległą publikację można obsłużyć bez wolnej przestrzeni wymaganej do rozpoczęcia nowego przebiegu. `--run --no-publish` zapisuje trwały status `disabled` dla GitHuba i pomija ntfy.

Agent wykonuje do trzech cykli publikacji na wynik. Trwały licznik i czas następnej próby obejmują ponowienie odczytu oraz potwierdzone odrzucenia przez limit GitHuba. Przed kolejnym `POST` agent ponownie weryfikuje PR i SHA. Zmiana oznacza `superseded`. Przy odroczeniu z powodu limitu agent uwzględnia `Retry-After` i `X-RateLimit-Reset`. [Limity GitHub REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

Przed `POST` agent utrwala stan `uncertain` i używa losowego znacznika w treści. Po utracie odpowiedzi sprawdza do 10 stron komentarzy na cykl. Potwierdzenie wymaga dokładnie tej samej treści oraz identyfikatora komentarza i skutkuje zapisaniem `.done`. Wyczerpanie cykli bez potwierdzenia daje `unconfirmed`. Jawny błąd uprawnień lub walidacji kończy próbę jako `rejected`. Wyczerpanie prób po potwierdzonych odmowach lub problemach przed wysłaniem daje `exhausted`. [Komentarze w GitHub REST API](https://docs.github.com/en/rest/issues/comments)

GitHub otrzymuje identyfikator przebiegu i stałe nazwy ze statusami etapów. Surowe logi, dynamiczne nazwy testów i artefakty pozostają lokalnie. Opcjonalne ntfy wysyła raz po nowym przebiegu wyłącznie numer PR-a, SHA i wynik. Status dostawy jest niezależny od komentarza.
