# AI Test Runner 2.0

Lokalny runner CI dla projektu Polska 2038. Koordynator sprawdza autoryzację PR-a i dokładny SHA, a etapy projektu uruchamia jako `ci-runner` w rootless Podmanie. Wynik trafia do lokalnego JSON przed próbą wysłania komentarza.

Na skonfigurowanym Linuksie polecenie bez flag wykonuje dry-run. Odczytuje listę PR-ów z GitHuba oraz lokalny stan i wyświetla pierwszego kandydata.

```bash
sudo python3 -I agent_testow_linux.py
```

| Flaga | Działanie |
| --- | --- |
| `--run` | Obsługuje jedną zaległą publikację albo uruchamia jeden nowy przebieg z raportem. |
| `--run --no-publish` | Uruchamia jeden przebieg i zachowuje wynik lokalnie. |
| `--pr-number 123` | Ogranicza wybór PR-a i obsługę publikacji do podanego numeru. |
| `--run --e2e-file e2e/nazwa.spec.js` | Zawęża etap Playwright do wskazanego pliku. |
| `--run --no-publish --capture-visual-baselines` | Generuje wzorce wizualne do ręcznej oceny. |

Wyniki i stan publikacji są w `/var/lib/ai-test-runner/result-pr-*.json`. Zwykły przebieg, wskazany plik E2E i generowanie wzorców mają odrębne klucze deduplikacji. ntfy jest opcjonalne i domyślnie wyłączone.

Testy jednostkowe można uruchomić w Pythonie 3.10 lub nowszym. Używają mocków GitHuba, Git i Podmana oraz katalogów tymczasowych.

```bash
python3 -m unittest discover -s tests -v
```

Ręczny test timeoutu i zatrzymywania rzeczywistego kontenera opisano w [instrukcji dla Ubuntu LXC](docs/PODMAN_TIMEOUT_INTEGRATION.md).

Zobacz [architekturę](docs/ARCHITECTURE.md) i [procedurę wdrożenia](docs/DEPLOYMENT.md). Jednostki w katalogu `systemd/` są gotowe do ręcznej instalacji po akceptacji.
