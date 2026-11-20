# Wdrożenie na Ubuntu LXC

Instalację i aktywację wykonuje się po zatwierdzeniu zmian. Najpierw sprawdź na odseparowanej instancji Ubuntu, czy pod użytkownikiem `ci-runner` działa rootless Podman z `--userns=keep-id`, dostępne są mapowania UID/GID, a `XDG_RUNTIME_DIR=/run/user/999` odpowiada działającej konfiguracji. Konto robocze powinno mieć wyłącznie grupy potrzebne do testów i nie mieć `sudo` ani dostępu do uprzywilejowanego gniazda silnika kontenerów. Zachowaj konfigurację SSH `ci-runner` z kluczem wdrożeniowym tylko do odczytu i zweryfikowanym `known_hosts`. Wymagane są Python 3.10+, Git, GitHub CLI i Podman. Sprawdź wersję i dostępność obrazu Playwright skonfigurowanego w agencie.

## Instalacja po akceptacji

1. Zabezpiecz kopię obecnego skryptu i katalogu `/var/lib/ai-test-runner`. Zachowaj wszystkie istniejące `.done`. Wstrzymaj poprzedni harmonogram, żeby wersje nie obsługiwały tej samej kolejki naprzemiennie.
2. Zainstaluj zweryfikowany `agent_testow_linux.py` w `/opt/ai-test-runner/`. Katalog ma mieć właściciela `root:root` i tryb `0755`, a skrypt `root:root` i `0644`. Katalogi nadrzędne muszą być zapisywalne wyłącznie przez root. Ustaw `root:root` i `0700` dla `/var/lib/ai-test-runner` oraz `/etc/ai-test-runner`. Pliki konfiguracyjne, poświadczenia i jednostki instaluj jako zwykłe pliki.
3. Umieść token typu fine-grained ograniczony do repozytorium Polska 2038 w `/etc/ai-test-runner/github-token`. Ustaw `root:root` i `0600` lub `0400`. Wystarczą uprawnienia repozytorium `Pull requests: read` i `Issues: write` oraz nadawane przez GitHub `Metadata: read`. [Uprawnienia do tworzenia komentarzy](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)
4. Sprawdź składnię jednostek i harmonogram bez ich uruchamiania.

```bash
systemd-analyze verify systemd/ai-test-runner.service systemd/ai-test-runner.timer
systemd-analyze calendar '*-*-* *:00/10:00'
```

5. Porównaj wynik dry-run z oczekiwanym PR-em i SHA. Przed aktywacją sprawdź na instancji testowej jeden uzgodniony przebieg z `--run --no-publish`.
6. Po osobnej zgodzie na harmonogram zainstaluj jednostki w `/etc/systemd/system/` jako `root:root` z trybem `0644`. Wykonaj `sudo systemctl daemon-reload` i `sudo systemctl enable --now ai-test-runner.timer`. Timer wywołuje usługę co 10 minut i pomija wywołanie, jeśli usługa nadal działa. [Dokumentacja timerów systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)

## Opcjonalne ntfy

Ustaw w `/etc/ai-test-runner/runner.conf` zmienną `AI_TEST_RUNNER_NTFY_URL` z pełnym adresem HTTPS prywatnego tematu. Plik ustawia środowisko usługi root i musi mieć właściciela `root:root` oraz tryb `0600` lub `0400`. Użyj certyfikatu zaufanego przez system i adresu bez danych logowania, query stringa oraz fragmentu. Umieść token ograniczony do publikacji w `/etc/ai-test-runner/ntfy-token` jako `root:root` z trybem `0600` lub `0400`. Pominięcie zmiennej wyłącza integrację. Agent nie podąża za przekierowaniami. [Publikowanie do ntfy](https://docs.ntfy.sh/publish/)

## Obsługa przerwania i wycofanie

- `KillMode=mixed` przekazuje `SIGTERM` koordynatorowi, a po jego wyjściu lub przekroczeniu 90 sekund systemd może użyć `SIGKILL` wobec pozostałych procesów cgroup usługi. Trwały plik `/var/lib/ai-test-runner/active-run.json` blokuje `--run` również po zwolnieniu `flock`. Dry-run pozostaje dostępny. [Dokumentacja zatrzymywania jednostek](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
- Jeśli pozostał `active-run.json`, wstrzymaj harmonogram i upewnij się, że nie działa usługa ani ręcznie uruchomiony koordynator. Sprawdź dziennik i procesy `ci-runner` związane z tym przebiegiem. Sam PID lub brak kontenera o danej nazwie nie wystarcza, jeśli poprzedni proces Podmana mógł jeszcze tworzyć kontener.
- Jeżeli zapis zawiera kontener, sprawdź go jako `ci-runner` w tej samej instancji rootless Podmana. Dopasuj dokładną nazwę i wartość etykiety `io.ai-test-runner.stage` do pól `name` i `token`. Zatrzymaj wyłącznie tak zweryfikowany kontener, używając pełnego ID z inspekcji, po czym sprawdź jego stan ponownie. Gdy tożsamość lub stan pozostają niepewne, pozostaw blokadę.
- Trzymaj wyłączną blokadę `flock` na istniejącym `/var/lib/ai-test-runner/agent.lock` podczas ręcznego porządkowania stanu. Dopiero po potwierdzeniu końca procesów i kontenera uporządkuj worktree i przenieś `active-run.json` poza katalog stanu. Rekord wyniku `RUNNING` nadal blokuje ten sam PR, SHA i zakres. Przenieś go wyłącznie po świadomej decyzji o ponownym wykonaniu.
- Dla `unconfirmed`, `exhausted` lub `rejected` sprawdź komentarze GitHuba i przyczynę awarii przed ręcznym raportowaniem. Zachowaj JSON jako dowód wykonania testów. Dla statusu `uncertain` lub `pending` z licznikiem 3 zastosuj tę samą procedurę, ponieważ proces mógł zostać przerwany w ostatnim cyklu.
- Wyłącz timer przez `sudo systemctl disable --now ai-test-runner.timer` i poczekaj na zakończenie aktywnej usługi. Przywróć poprzedni skrypt z kopii i zachowaj stan 2.0. Przed przywróceniem starego harmonogramu sprawdź przebiegi zapisane wyłącznie w JSON, ponieważ starsza wersja pomija pracę na podstawie `.done`. Używaj wyłącznie znaczników potwierdzonych publikacji.
