# Ręczny test timeoutu Podmana na Ubuntu LXC

Uruchom test z katalogu głównego repozytorium w sesji konta `ci-runner` na wydzielonym Ubuntu LXC. Potrzebujesz Pythona 3.10+, GNU `timeout`, lokalnego rootless Podmana z działającym `--userns=keep-id` oraz cgroups v2 z kontrolerami CPU, pamięci i PID. System musi udostępniać PID kontenera w `/proc`, jego limity w `/sys/fs/cgroup`, obsługiwać `pidfd_open` i mieć gotowy katalog uruchomieniowy sesji `/run/user/$(id -u)`.

W magazynie Podmana konta `ci-runner` musi już być zaufany obraz z wykonywalnym `/bin/sleep`. Poniższy przykład używa lokalnego BusyBox. Jeśli obraz ma inną lokalną nazwę, podstaw ją w poleceniu `image inspect`. Uruchomienie używa pełnego ID, `--pull=never` i `--network=none`. [Dokumentacja opcji Podmana](https://docs.podman.io/en/stable/markdown/podman-run.1.html)

```bash
image_id=$(timeout --kill-after=2s 5s podman --remote=false image inspect --format '{{.Id}}' docker.io/library/busybox:1.36) &&
  timeout --signal=TERM --kill-after=30s 90s \
    python3 -I -B tests/integration/podman_timeout.py --image-id "$image_id"
```

Skrypt jest osobnym poleceniem poza automatycznym wykrywaniem testów jednostkowych. Wywołuje `run_stage`, `launch`, `finish_container` i `active_run` z agenta. Adapter zastępuje `as_ci`, aby uruchomić lokalny Podman jako bieżący użytkownik i ustawić opcje testowe. Atrapa obejmuje wyłącznie wywołanie `stop` w scenariuszu awarii. Stan produkcyjny, wejście CLI koordynatora, GitHub i systemd są poza ścieżką wykonania. Blokadę trwałą `active-run.json` test sprawdza w prywatnym katalogu dla każdego scenariusza. Produkcyjny `flock` ma osobny test jednostkowy.

- `PASS confirmed-stop` oznacza, że timeout klienta nastąpił po 8 s, klient Podmana i jego grupa zakończyli działanie, agent potwierdził stan kontenera po rzeczywistym `stop`, proces `sleep` zakończył się i kolejny przebieg dostał dostęp do sekcji chronionej.
- `PASS failed-stop` oznacza, że po zasymulowanej odmowie zatrzymania kontener pozostał aktywny, agent ponownie sprawdził jego stan, a zapisany znacznik zablokował nowy przebieg. Kolejny rzeczywisty stop wykonuje już procedura sprzątania.
- Każdy kontener ma limit 64 MiB RAM, 0,25 CPU, 16 PID i awaryjny limit 40 s od uruchomienia. Test odczytuje rzeczywiste limity z cgroups v2. Powolny start Podmana może przekroczyć 8 s przed startem kontenera i spowodować błąd testu. Zewnętrzny `timeout` wysyła TERM po 90 s i daje do 30 s na sprzątanie przed SIGKILL.

Kod wyjścia `0` wymaga obu wyników `PASS` i zakończonego sprzątania. Przed zatrzymaniem i usunięciem test porównuje nazwę, unikalną etykietę `io.ai-test-runner.stage` i pełne ID zapisane przez `podman run --cidfile`. Usuwa wyłącznie potwierdzony zatrzymany kontener, po pełnym ID i bez wymuszania.

Po błędzie zachowaj katalog wypisany jako „Diagnostyka testu”. Zależnie od etapu zawiera on `preflight.log`, `podman.log`, `container.json` z nazwą i tokenem, `container.cid` oraz `active-run.json`. W razie błędu Podmana albo wymuszonego zakończenia może pozostać kontener testowy. Sprawdź go jako to samo konto `ci-runner` i porównaj pełne ID, nazwę oraz etykietę z diagnostyką przed ręcznym zatrzymaniem. Przy niepewnej tożsamości lub niepotwierdzonym stanie zachowaj diagnostykę.
