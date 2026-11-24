# AI Test Runner 2.0

A self-hosted CI runner written in Python. It picks up pull requests from GitHub, verifies that they are authorised and that the exact commit has not changed, and runs the project's test stages (ESLint, Node unit tests, build, Playwright E2E) in **rootless Podman** containers. The result is stored locally before it is posted back to the pull request as a comment.

I built it to run the full test suite of my [Polska 2038](https://polska2038.pl) web project on my own Linux server, because the end-to-end and visual regression tests need more time and a more stable environment than free hosted runners give.

*Polish version: [README.pl.md](README.pl.md). Detailed design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (Polish).*

## What it does

| Step | Detail |
| --- | --- |
| Select a pull request | Open, ready for review, labelled `local-tests`, from the expected repository and owner, on a `claude/*` branch, with a full 40-character SHA |
| Re-check before checkout | Reads the PR again right before `git fetch` and compares `FETCH_HEAD` with the authorised SHA; any mismatch stops the run |
| Run stages | `npm ci --ignore-scripts`, ESLint, Node unit tests, build, Playwright; stops at the first failing stage |
| Isolate | Podman runs as an unprivileged `ci-runner` user with memory, CPU and PID limits, no capabilities, `no-new-privileges`, and no network after dependency installation |
| Handle timeouts | Each stage has a time limit; on timeout or interruption the runner stops the process group and confirms the container is gone before releasing the lock |
| Store the result | Atomic JSON write (`os.replace` + `fsync`) with PR, SHA, scope, per-stage status and timestamps; one result per PR, SHA and scope prevents duplicate runs |
| Publish | Posts one comment with stage statuses; retries respect GitHub rate-limit headers, and a lost response is reconciled by searching for the comment's random marker instead of posting twice |
| Schedule | A `systemd` timer starts the runner; a file lock and an `active-run.json` marker prevent overlapping runs |

## Tests

38 unit tests (Python `unittest`) cover PR authorisation rules, SHA re-checks, stage failures and timeouts, result deduplication, atomic state files and every publication outcome (confirmed, rate-limited, rejected, superseded, unconfirmed). GitHub, Git and Podman are replaced with mocks and temporary directories, so the suite runs anywhere with Python 3.10+:

```bash
python3 -m unittest discover -s tests -v
```

A manual integration test for timeouts against a real Podman container is described in [docs/PODMAN_TIMEOUT_INTEGRATION.md](docs/PODMAN_TIMEOUT_INTEGRATION.md).

## Usage

On a configured Linux host, the command without flags is a dry run: it reads open pull requests and local state and prints the first candidate.

```bash
sudo python3 -I agent_testow_linux.py
```

| Flag | Effect |
| --- | --- |
| `--run` | Publishes one pending result, or runs one new test pass and reports it |
| `--run --no-publish` | Runs one pass and keeps the result local |
| `--pr-number 123` | Limits selection and publication to one pull request |
| `--pr-number 123 --allow-draft` | Allows a draft PR after the full authorisation check |
| `--run --e2e-file e2e/name.spec.js` | Runs only one Playwright spec |
| `--run --no-publish --capture-visual-baselines` | Generates visual baselines for manual review |

Deployment steps and the `systemd` units are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [systemd/](systemd/).

## Stack

Python 3 (standard library only), Podman (rootless), systemd, GitHub REST API, Git worktrees, Playwright, Proxmox LXC (Ubuntu)
