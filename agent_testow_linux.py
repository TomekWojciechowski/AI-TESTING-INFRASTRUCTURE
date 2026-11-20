#!/usr/bin/env python3
"""Opt-in PR CI polling on a dedicated Linux VM.

The coordinator runs as root solely to read a repo-scoped GitHub token.
All repository operations and untrusted build steps run as ci-runner.
Never execute this coordinator from an unreviewed PR checkout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

REPO = "TomekWojciechowski/projekt-polska-2038-pro"
OWNER = "TomekWojciechowski"
LABEL = "local-tests"
USER = "ci-runner"
HOME = Path("/home/ci-runner")
SOURCE = HOME / "projects/polska2038"
WORK = HOME / "ci-jobs"
STATE = Path("/var/lib/ai-test-runner")
SECRET = Path("/etc/ai-test-runner/github-token")
NTFY_SECRET = Path("/etc/ai-test-runner/ntfy-token")
IMAGE = "mcr.microsoft.com/playwright:v1.63.0-noble"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
E2E_RE = re.compile(r"e2e/[A-Za-z0-9_.-]+\.spec\.js")
EP_FLAGS = ["VITE_EP_NAWIGACJA=true", "VITE_EP_STRONA_GLOWNA=true", "VITE_EP_PODSTRONY=true"]
TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAX_PUBLICATION_ATTEMPTS = 3
ACTIVE_RUN = "active-run.json"
CONTAINER_LABEL = "io.ai-test-runner.stage"
GROUP_STOP_TIMEOUT = 5


class InterventionRequired(RuntimeError):
    """An external process or container could not be confirmed stopped."""


class StopRequested(KeyboardInterrupt):
    pass


def request_stop(signum, _frame) -> None:
    signal.signal(signum, signal.SIG_IGN)
    raise StopRequested


class GitHubError(RuntimeError):
    def __init__(self, path: str, status: int = 0, headers: dict | None = None):
        super().__init__(f"GitHub API {path}: HTTP {status}" if status else f"GitHub API {path}: brak odpowiedzi")
        self.status = status
        self.headers = headers or {}

    def rate_limited(self) -> bool:
        return self.status == 429 or (self.status == 403 and (
            "retry-after" in self.headers or self.headers.get("x-ratelimit-remaining") == "0"
        ))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def authorized(pr: object) -> bool:
    if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
        return False
    head = pr["head"]
    repo, user = head.get("repo"), head.get("user")
    if not isinstance(repo, dict) or not isinstance(user, dict):
        return False
    sha, ref = head.get("sha"), head.get("ref")
    repo_name, login = repo.get("full_name"), user.get("login")
    labels = pr.get("labels")
    return (
        pr.get("state") == "open"
        and type(pr.get("number")) is int and pr["number"] > 0
        and isinstance(sha, str) and SHA_RE.fullmatch(sha) is not None
        and isinstance(repo_name, str) and repo_name.lower() == REPO.lower()
        and isinstance(login, str) and login.lower() == OWNER.lower()
        and isinstance(ref, str) and ref.startswith("claude/")
        and isinstance(labels, list)
        and any(isinstance(item, dict) and item.get("name") == LABEL for item in labels)
    )


def eligible(pr: object, *, allow_draft: bool = False) -> bool:
    return authorized(pr) and (pr.get("draft") is False or (allow_draft and pr.get("draft") is True))


def wait_for_process_group(group: int) -> None:
    deadline = time.monotonic() + GROUP_STOP_TIMEOUT
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        except OSError:
            raise InterventionRequired("Nie można sprawdzić grupy procesów.") from None
        if time.monotonic() >= deadline:
            raise InterventionRequired("Grupa procesów nadal istnieje. Wymagana interwencja.")
        time.sleep(0.05)


def launch(argv: list[str], *, timeout: int = 60, env: dict | None = None,
           cwd: Path | None = None, log=None, identity=None) -> subprocess.CompletedProcess:
    credentials = {}
    if identity is not None:
        uid, gid, groups = identity
        credentials = {"user": uid, "group": gid, "extra_groups": groups}
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if log is None else log,
        stderr=subprocess.PIPE if log is None else subprocess.STDOUT,
        encoding="utf-8", errors="replace", env=env, cwd=cwd, start_new_session=True,
        **credentials,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        if process.returncode is None:
            # Signal only our own new group, while its leader has not been reaped.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                raise InterventionRequired("Nie można zatrzymać grupy procesów.") from None
            try:
                process.wait(timeout=GROUP_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise InterventionRequired("Proces nie zakończył się po SIGKILL.") from None
        wait_for_process_group(process.pid)
        raise
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    wait_for_process_group(process.pid)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def read_secret(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise RuntimeError(f"Wymagany zwykły plik root z uprawnieniami 0600 lub 0400: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value or not value.isascii() or any(char.isspace() for char in value):
        raise RuntimeError(f"Nieprawidłowa zawartość pliku poświadczeń: {path}")
    return value


def github_env() -> dict[str, str]:
    if os.geteuid() != 0:
        raise RuntimeError("Uruchom koordynator przez sudo (token tylko dla root).")
    return {"PATH": TRUSTED_PATH, "HOME": "/root", "LC_ALL": "C.UTF-8",
            "GH_TOKEN": read_secret(SECRET), "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1"}


def api(path: str, *, env: dict, post: str | None = None) -> dict | list:
    cmd = ["gh", "api", "--hostname", "github.com", "--include", f"repos/{REPO}/{path}"]
    if post is not None:
        cmd += ["--method", "POST", "-f", f"body={post}"]
    try:
        result = launch(cmd, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError(path) from None
    header, _, payload = result.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = header.splitlines()
    status_line = re.fullmatch(r"HTTP/\S+ (\d{3})(?: .*)?", lines[0]) if lines else None
    status = int(status_line[1]) if status_line else 0
    if result.returncode:
        headers = {}
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            if separator:
                headers[key.lower()] = value.strip()
        raise GitHubError(path, status, headers)
    if not 200 <= status < 300:
        raise GitHubError(path, status)
    try:
        return json.loads(payload)
    except ValueError:
        raise GitHubError(path, status) from None


def ci_identity() -> tuple[int, int, list[int]]:
    import pwd

    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        raise RuntimeError(f"Brakuje konta {USER}.") from None
    groups = os.getgrouplist(USER, account.pw_gid)
    if account.pw_uid == 0 or account.pw_gid == 0 or 0 in groups:
        raise RuntimeError(f"Konto {USER} musi mieć nieuprzywilejowany UID i grupy.")
    return account.pw_uid, account.pw_gid, groups


def as_ci(args: list[str], *, timeout: int = 120, cwd: Path = HOME,
          log=None) -> subprocess.CompletedProcess:
    env = {"PATH": TRUSTED_PATH, "HOME": str(HOME), "LC_ALL": "C.UTF-8",
           "USER": USER, "LOGNAME": USER, "XDG_RUNTIME_DIR": "/run/user/999"}
    if args[0] == "git":
        env["GIT_SSH_COMMAND"] = (
            "ssh -i /home/ci-runner/.ssh/polska2038_deploy "
            "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes "
            "-o ControlMaster=no -o ControlPath=none"
        )
    if args[0] == "podman":
        args = ["podman", "--remote=false", *args[1:]]
    return launch(args, timeout=timeout, env=env, cwd=cwd, log=log, identity=ci_identity())


def require_success(result: subprocess.CompletedProcess, label: str) -> None:
    if result.returncode:
        raise RuntimeError(f"{label}: kod zakończenia {result.returncode}")


def container_info(name: str, token: str) -> dict | None:
    found = as_ci(["podman", "container", "exists", name], timeout=10)
    if found.returncode == 1:
        return None
    if found.returncode != 0:
        raise InterventionRequired("Nie można sprawdzić magazynu kontenerów Podmana.")
    result = as_ci(["podman", "container", "inspect", name], timeout=10)
    if result.returncode != 0:
        # --rm may have removed it in between the two calls.
        if as_ci(["podman", "container", "exists", name], timeout=10).returncode == 1:
            return None
        raise InterventionRequired("Podman nie potwierdził tożsamości kontenera.")
    try:
        items = json.loads(result.stdout)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError
        info = items[0]
        if (info["Name"] != name or info["Config"]["Labels"][CONTAINER_LABEL] != token
                or not re.fullmatch(r"[0-9a-f]{64}", info["Id"])):
            raise ValueError
        state = info["State"]
        if not isinstance(state["Status"], str) or type(state["Running"]) is not bool:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise InterventionRequired("Podman nie potwierdził tożsamości lub stanu kontenera.") from None
    return info


def container_stopped(info: dict | None) -> bool:
    return info is None or (info["State"]["Status"] in {"exited", "stopped"}
                            and info["State"]["Running"] is False)


def finish_container(guard: dict, *, interrupted: bool = False) -> bool:
    name, token = guard["container"]["name"], guard["container"]["token"]
    try:
        info = container_info(name, token)
        if info is None and interrupted:
            # Absence alone cannot exclude a partially started Podman operation.
            raise InterventionRequired("Nie potwierdzono stanu kontenera po przerwaniu startu Podmana.")
        stop_needed = not container_stopped(info)
        if stop_needed:
            container_id = info["Id"]
            try:
                as_ci(["podman", "container", "stop", "--time", "5", container_id], timeout=12)
            except subprocess.TimeoutExpired:
                # The command group has ended. Inspect the outcome before deciding.
                pass
            after = container_info(name, token)
            if not container_stopped(after) or (after is not None and after["Id"] != container_id):
                raise InterventionRequired("Podman nie potwierdził zatrzymania kontenera.")
    except (OSError, subprocess.TimeoutExpired):
        raise InterventionRequired("Nie można potwierdzić zatrzymania kontenera Podmana.") from None
    guard["container"] = None
    save_result(STATE / ACTIVE_RUN, guard)
    return stop_needed


def run_stage(name: str, worktree: Path, command: list[str], *, online: bool,
              timeout: int, log, guard: dict) -> bool:
    token = uuid4().hex
    container_name = f"ai-test-runner-{token}"
    container = ["podman", "run", "--rm", "--name", container_name,
                 "--label", f"{CONTAINER_LABEL}={token}", "--restart=no", "--timeout", str(timeout),
                 "--memory", "2300m", "--cpus", "1",
                 "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt",
                 "no-new-privileges", "--userns=keep-id", "--http-proxy=false",
                 "--env", "HOME=/tmp", "--volume",
                 f"{worktree}:/work:rw", "--workdir", "/work"]
    if not online:
        container += ["--network", "none"]
    for flag in EP_FLAGS:
        container += ["--env", flag]
    container += [IMAGE, *command]
    started = time.monotonic()
    log.write(f"\n===== START {name} =====\n")
    log.flush()
    guard["container"] = {"name": container_name, "token": token}
    save_result(STATE / ACTIVE_RUN, guard)
    interrupted = False
    try:
        try:
            result = as_ci(container, timeout=timeout, log=log)
            status = result.returncode == 0
        except subprocess.TimeoutExpired:
            interrupted = True
            status = False
            log.write(f"TIMEOUT po {timeout}s\n")
    except InterventionRequired:
        # The client may still be starting the container. Keep the durable guard.
        raise
    except BaseException:
        finish_container(guard, interrupted=True)
        raise
    else:
        if finish_container(guard, interrupted=interrupted):
            status = False
            log.write("Agent musiał zatrzymać kontener po zakończeniu polecenia.\n")
    log.write(f"\n===== {name}: {'PASS' if status else 'FAIL'} ({time.monotonic()-started:.1f}s) =====\n")
    log.flush()
    return status



def report_stamp(number: int, sha: str, e2e_file: str | None) -> Path:
    """Scope comments by commit and test file so different suites can report once."""
    if e2e_file == "e2e/full.spec.js":
        return result_path(number, sha, e2e_file, False).with_suffix(".done")
    scope = "full" if e2e_file is None else e2e_file.removeprefix("e2e/").removesuffix(".spec.js")
    return STATE / f"pr-{number}-{sha}-{scope}.done"


def result_path(number: int, sha: str, e2e_file: str | None, capture_visual: bool) -> Path:
    if type(number) is not int or number < 1 or not SHA_RE.fullmatch(sha):
        raise ValueError("Nieprawidłowa tożsamość wyniku.")
    if e2e_file is not None and not E2E_RE.fullmatch(e2e_file):
        raise ValueError("Nieprawidłowy zakres wyniku.")
    scope = ("visual" if capture_visual else
             ("full" if e2e_file is None else "spec-" + Path(e2e_file).name))
    return STATE / f"result-pr-{number}-{sha}-{scope}.json"


def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".runner-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def save_result(path: Path, record: dict) -> None:
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def require_no_active_run() -> None:
    try:
        (STATE / ACTIVE_RUN).lstat()
    except FileNotFoundError:
        return
    raise InterventionRequired(f"Pozostał {STATE / ACTIVE_RUN}. Nowe przebiegi zablokowane. Wymagana interwencja.")


@contextmanager
def active_run(number: int, sha: str):
    require_no_active_run()
    path = STATE / ACTIVE_RUN
    guard = {"pr": number, "sha": sha, "started_at": utc_now(), "container": None}
    save_result(path, guard)
    try:
        yield guard
    except (InterventionRequired, KeyboardInterrupt):
        raise
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        if guard["container"] is None:
            path.unlink()
            sync_directory(path.parent)
        raise
    else:
        if guard["container"] is not None:
            raise InterventionRequired("Nie potwierdzono końca etapu Podmana.")
        path.unlink()
        sync_directory(path.parent)


def render_report(record: dict) -> str:
    capture_visual = record["capture_visual"]
    title = "GENEROWANIE WZORCÓW (NIEZATWIERDZONE)" if capture_visual else "NUC Linux CI"
    status = "ZAKOŃCZONE" if record["outcome"] == "BASELINES_GENERATED" else record["outcome"]
    summary = "\n".join(f"- {check['outcome']}: {check['name']}" for check in record["checks"])
    report = (
        f"**{title}: {status}**\n\n"
        f"PR #{record['pr']}, commit `{record['sha']}`\n\n{summary}\n\n"
        "Uruchomiono w rootless Podman (2.3 GiB RAM, 1 CPU, 1 worker Playwright). "
        "Pełny log dostępny lokalnie na ai-test-runner. "
        "Wynik nie oznacza zatwierdzenia do scalenia."
    )
    if capture_visual:
        report += "\n\nObrazy wymagają ręcznej recenzji."
    return report


def current_head_matches(number: int, sha: str, env: dict, *, draft: bool | None = None) -> bool:
    # Publication checks the existing result's authorization and SHA independently of draft status.
    current = api(f"pulls/{number}", env=env)
    return (authorized(current)
            and current["number"] == number and current["head"]["sha"] == sha
            and (draft is None or current.get("draft") is draft))


def execute(pr: dict, env: dict, *, publish: bool, e2e_file: str | None = None,
            capture_visual: bool = False, allow_draft: bool = False) -> Path:
    if not eligible(pr, allow_draft=allow_draft):
        raise RuntimeError("PR nie spełnia zasad autoryzacji do testów.")
    number, sha, draft = pr["number"], pr["head"]["sha"], pr["draft"]
    path = result_path(number, sha, e2e_file, capture_visual)
    if path.exists() or previously_reported(number, sha, e2e_file, capture_visual):
        raise RuntimeError(f"PR #{number}: wynik lub znacznik już istnieje.")
    with active_run(number, sha) as guard:
        prepare_workspace()
        if not current_head_matches(number, sha, env, draft=draft):
            raise RuntimeError(f"PR #{number}: zmienił się SHA, status lub autoryzacja.")
        return execute_job(number, sha, path, guard=guard, publish=publish,
                           e2e_file=e2e_file, capture_visual=capture_visual)


def execute_job(number: int, sha: str, path: Path, *, guard: dict, publish: bool,
                e2e_file: str | None, capture_visual: bool) -> Path:
    fetch = as_ci(["git", "-C", str(SOURCE), "fetch", "--no-tags", "origin",
                   f"refs/pull/{number}/head"], timeout=180)
    require_success(fetch, "git fetch")
    actual = as_ci(["git", "-C", str(SOURCE), "rev-parse", "FETCH_HEAD"])
    require_success(actual, "rev-parse FETCH_HEAD")
    if actual.stdout.strip() != sha:
        raise RuntimeError("Pobrany SHA nie odpowiada autoryzowanemu PR.")

    worktree = WORK / f"pr-{number}-{sha[:12]}"
    if worktree.exists():
        raise RuntimeError(f"Katalog roboczy już istnieje: {worktree}. Sprawdź ręcznie.")
    create = as_ci(["git", "-C", str(SOURCE), "worktree", "add", "--detach",
                    str(worktree), sha], timeout=120)
    require_success(create, "git worktree add")
    logfile = STATE / f"run-{number}-{sha}-{time.time_ns()}.log"
    record = {
        "schema_version": 1, "repo": REPO, "pr": number, "sha": sha,
        "e2e_file": e2e_file, "capture_visual": capture_visual,
        "started_at": utc_now(), "finished_at": None, "outcome": "RUNNING",
        "checks": [], "log": logfile.name,
        "github": {"status": "pending" if publish else "disabled", "attempts": 0,
                   "next_attempt_at": 0, "marker": uuid4().hex if publish else None},
    }
    cleanup = True
    try:
        with logfile.open("x", encoding="utf-8") as log:
            log.write(f"PR #{number} SHA {sha} UTC {record['started_at']}\n")
            # Fail closed if interrupted after untrusted code has started.
            save_result(path, record)
            steps = [
                # Network access is permitted only while downloading packages;
                # lifecycle scripts are disabled during this step.
                ("npm ci (no scripts)", ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], True, 600),
                # Rebuild native dependencies with lifecycle scripts, without any network.
                ("npm rebuild (offline)", ["npm", "rebuild", "--offline"], False, 300),
                ("ESLint", ["npm", "run", "lint"], False, 300),
                # package.json currently uses `node --test src`, incompatible with Node 24.
                ("Unit (Node 24 discovery)", ["node", "--test"], False, 300),
                ("Build", ["npm", "run", "build"], False, 900),
                ("Generowanie wzorców (DO OCENY)" if capture_visual else
                 ("Playwright E2E" if not e2e_file else f"Playwright E2E (tylko {e2e_file})"),
                 ["npx", "playwright", "test", *([e2e_file] if e2e_file else []),
                  "--workers=1", *(["--project=komputer", "--update-snapshots"] if capture_visual else [])],
                 False, 2400),
            ]
            for name, cmd, online, timeout in steps:
                passed = run_stage(name, worktree, cmd, online=online, timeout=timeout, log=log, guard=guard)
                record["checks"].append({"name": name, "outcome": "PASS" if passed else "FAIL"})
                if not passed:
                    break
            record["outcome"] = ("BASELINES_GENERATED" if capture_visual else "PASS") if passed else "FAIL"
            record["finished_at"] = utc_now()
            save_result(path, record)
    except (InterventionRequired, KeyboardInterrupt):
        # Preserve diagnostics and leave the durable guard for operator review.
        cleanup = False
        raise
    finally:
        if cleanup and guard["container"] is None:
            cleanup_worktree(worktree, number, sha, capture_visual)

    print(render_report(record))
    return path


def cleanup_worktree(worktree: Path, number: int, sha: str, capture_visual: bool) -> None:
    try:
        # Copy PR artifacts as ci-runner.
        result_dir = worktree / "test-results"
        if result_dir.is_dir() and not result_dir.is_symlink():
            artifact_base = HOME / "ci-artifacts"
            destination = artifact_base / f"pr-{number}-{sha[:12]}-{time.time_ns()}"
            prepared = as_ci(["mkdir", "-p", str(artifact_base)])
            if prepared.returncode == 0:
                saved = as_ci(["cp", "-a", str(result_dir), str(destination)], timeout=300)
                if saved.returncode == 0:
                    print(f"Zachowano artefakty E2E: {destination}")
                else:
                    print("UWAGA: nie udało się zachować artefaktów.", file=sys.stderr)
        if capture_visual:
            # Snapshot files are untrusted PR output: copy them as ci-runner,
            # never read or follow them as root, and do not commit them.
            snapshots = worktree / "e2e/wizualne.spec.js-snapshots"
            if snapshots.is_dir() and not snapshots.is_symlink():
                target = HOME / "ci-artifacts" / f"visual-pr-{number}-{sha[:12]}-{time.time_ns()}"
                created = as_ci(["mkdir", "-p", str(target)], timeout=120)
                if created.returncode == 0:
                    copied = as_ci(["cp", "-a", str(snapshots), str(target / "snapshots")], timeout=300)
                    if copied.returncode == 0:
                        print(f"Zachowano NIEZATWIERDZONE wzorce: {target / 'snapshots'}")
                    else:
                        print("UWAGA: kopiowanie wzorców nieudane.", file=sys.stderr)
            else:
                print("UWAGA: brak katalogu wygenerowanych wzorców.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        print("UWAGA: nie udało się zachować artefaktów.", file=sys.stderr)
    try:
        removed = as_ci(["git", "-C", str(SOURCE), "worktree", "remove", "--force", str(worktree)],
                        timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        print(f"UWAGA: worktree wymaga ręcznej kontroli: {worktree}", file=sys.stderr)
    else:
        if removed.returncode:
            print(f"UWAGA: worktree wymaga ręcznej kontroli: {worktree}", file=sys.stderr)


def previously_reported(number: int, sha: str, e2e_file: str | None, capture_visual: bool) -> bool:
    # Visual generation is a distinct, local-only operation.
    if capture_visual:
        return False
    if report_stamp(number, sha, e2e_file).exists():
        return True
    if e2e_file is None:
        return (STATE / f"pr-{number}-{sha}.done").exists()
    # An old "-full.done" may belong to either scope. A v2 full record resolves it.
    return (e2e_file == "e2e/full.spec.js" and report_stamp(number, sha, None).exists()
            and not result_path(number, sha, None, False).exists())


def read_result(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    if (record["schema_version"] != 1 or record["repo"] != REPO
            or path != result_path(record["pr"], record["sha"], record["e2e_file"],
                                   record["capture_visual"])):
        raise ValueError("Niespójny plik wyniku.")
    return record


def comment_id(comment: object, body: str) -> int | None:
    if (isinstance(comment, dict) and comment.get("body") == body
            and type(comment.get("id")) is int and comment["id"] > 0):
        return comment["id"]
    return None


def reconcile_comment(endpoint: str, body: str, env: dict) -> int | None:
    for page in range(1, 11):
        comments = api(f"{endpoint}?per_page=100&page={page}", env=env)
        if not isinstance(comments, list):
            raise GitHubError(endpoint)
        for comment in comments:
            existing = comment_id(comment, body)
            if existing is not None:
                return existing
        if len(comments) < 100:
            break
    return None


def next_attempt_at(attempts: int, headers: dict) -> float:
    now = time.time()
    delay = 60 * 2 ** (attempts - 1)
    retry_after = headers.get("retry-after", "")
    reset = headers.get("x-ratelimit-reset", "")
    if re.fullmatch(r"[0-9]{1,12}", retry_after):
        delay = max(delay, int(retry_after))
    if headers.get("x-ratelimit-remaining") == "0" and re.fullmatch(r"[0-9]{1,12}", reset):
        delay = max(delay, int(reset) - now)
    return now + delay


def publish_result(path: Path, record: dict, env: dict) -> None:
    publication = record["github"]
    number, sha = record["pr"], record["sha"]
    marker = publication["marker"]
    if not re.fullmatch(r"[0-9a-f]{32}", marker):
        raise ValueError("Nieprawidłowy identyfikator publikacji.")
    body = render_report(record) + f"\n\n<!-- ai-test-runner:{marker} -->"
    endpoint = f"issues/{number}/comments"
    publication["attempts"] += 1
    publication["next_attempt_at"] = next_attempt_at(publication["attempts"], {})
    save_result(path, record)
    existing = None
    try:
        if publication["status"] == "uncertain":
            existing = reconcile_comment(endpoint, body, env)
        elif not current_head_matches(number, sha, env):
            publication["status"] = "superseded"
        else:
            # Commit the intent before POST. A lost response permits only GET reconciliation.
            publication["status"] = "uncertain"
            save_result(path, record)
            try:
                existing = comment_id(api(endpoint, env=env, post=body), body)
            except GitHubError as error:
                if error.rate_limited():
                    publication["status"] = "pending"
                elif error.status in {400, 401, 403, 404, 410, 422}:
                    publication["status"] = "rejected"
                raise
    except GitHubError as error:
        publication["next_attempt_at"] = next_attempt_at(publication["attempts"], error.headers)
        print(f"UWAGA: PR #{number}, {error}", file=sys.stderr)

    if existing is not None:
        atomic_write(report_stamp(number, sha, record["e2e_file"]), utc_now() + "\n")
        publication.update(status="published", comment_id=existing, published_at=utc_now())
    elif publication["attempts"] >= MAX_PUBLICATION_ATTEMPTS:
        if publication["status"] in {"pending", "uncertain"}:
            publication["status"] = ("unconfirmed" if publication["status"] == "uncertain"
                                     else "exhausted")
    save_result(path, record)
    print(f"GitHub PR #{number} SHA {sha}: {publication['status']}.")


def publish_pending(env: dict, pr_number: int | None) -> bool:
    for path in sorted(STATE.glob("result-pr-*.json")):
        try:
            record = read_result(path)
            if pr_number is not None and pr_number != record["pr"]:
                continue
            publication = record["github"]
            ready = (
                record["outcome"] in {"PASS", "FAIL"} and not record["capture_visual"]
                and publication["status"] in {"pending", "uncertain"}
                and publication["attempts"] < MAX_PUBLICATION_ATTEMPTS
                and publication["next_attempt_at"] <= time.time()
            )
        except (ValueError, KeyError, TypeError) as error:
            print(f"UWAGA: uszkodzony stan {path.name} ({type(error).__name__}).", file=sys.stderr)
            continue
        if ready:
            publish_result(path, record, env)
            return True
    return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notify_ntfy(path: Path) -> None:
    url = os.environ.get("AI_TEST_RUNNER_NTFY_URL", "")
    if not url:
        return
    record = read_result(path)
    if "ntfy" in record or record["github"]["status"] == "disabled":
        return
    try:
        endpoint = urlsplit(url)
        if (endpoint.scheme != "https" or not endpoint.hostname or not endpoint.path.strip("/")
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
                or endpoint.username is not None or endpoint.query or endpoint.fragment):
            raise ValueError("Nieprawidłowy adres ntfy.")
        message = f"AI Test Runner: PR #{record['pr']}, SHA {record['sha']}, {record['outcome']}"
        request = Request(url, data=message.encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {read_secret(NTFY_SECRET)}",
            "Content-Type": "text/plain; charset=utf-8",
        })
    except (OSError, RuntimeError, ValueError):
        record["ntfy"] = "configuration_error"
        save_result(path, record)
        print("UWAGA: nieprawidłowa konfiguracja ntfy.", file=sys.stderr)
        return
    record["ntfy"] = "attempted"
    save_result(path, record)
    try:
        with build_opener(NoRedirect()).open(request, timeout=10):
            record["ntfy"] = "sent"
    except (HTTPException, OSError, ValueError):
        record["ntfy"] = "unconfirmed"
        print("UWAGA: niepotwierdzona dostawa ntfy.", file=sys.stderr)
    save_result(path, record)


@contextmanager
def agent_lock():
    import fcntl

    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("Katalog stanu musi należeć do root i mieć uprawnienia 0700.")
    with (STATE / "agent.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def prepare_workspace() -> None:
    if shutil.disk_usage(HOME).free < 8 * 1024**3:
        raise RuntimeError("Mniej niż 8 GiB wolnego miejsca: nie uruchamiam testów.")
    cutoff = time.time() - 14 * 86400
    for path in STATE.glob("run-*.log"):
        if not path.is_symlink() and path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
    require_success(as_ci(["mkdir", "-p", str(WORK)]), "mkdir CI workspace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Polska 2038 Linux PR runner (domyślnie dry-run)")
    parser.add_argument("--run", action="store_true", help="Uruchom jeden PR i opublikuj raport")
    parser.add_argument("--no-publish", action="store_true", help="Z --run: zapisz wynik tylko lokalnie")
    parser.add_argument("--e2e-file", default=None,
                        help="Z --run: jeden plik e2e/*.spec.js (wynik publikowany, chyba że --no-publish)")
    parser.add_argument("--capture-visual-baselines", action="store_true",
                        help="Tylko --run --no-publish: wygeneruj wzorce EP do ręcznej oceny")
    parser.add_argument("--pr-number", type=int, default=None,
                        help="Testuj wskazany autoryzowany PR zamiast pierwszego oczekującego")
    parser.add_argument("--allow-draft", action="store_true",
                        help="Z --pr-number: dopuść autoryzowany PR w statusie Draft")
    args = parser.parse_args()
    if args.pr_number is not None and args.pr_number < 1:
        parser.error("--pr-number musi być dodatnią liczbą całkowitą")
    if args.allow_draft and args.pr_number is None:
        parser.error("--allow-draft wymaga --pr-number")
    if args.no_publish and not args.run:
        parser.error("--no-publish wymaga --run")
    if args.capture_visual_baselines:
        if not args.run or not args.no_publish or args.e2e_file:
            parser.error("--capture-visual-baselines wymaga --run --no-publish, bez --e2e-file")
        args.e2e_file = "e2e/wizualne.spec.js"
    if args.e2e_file and not args.run:
        parser.error("--e2e-file wymaga --run")
    if args.e2e_file is not None and not E2E_RE.fullmatch(args.e2e_file):
        parser.error("--e2e-file musi wskazywać e2e/nazwa.spec.js")
    with agent_lock() as locked:
        if not locked:
            print("Agent już działa. Pomijam przebieg.")
            return 0
        if args.run:
            require_no_active_run()
        env = github_env()
        publish = args.run and not args.no_publish
        if args.pr_number is not None:
            pr = api(f"pulls/{args.pr_number}", env=env)
            if not authorized(pr) or pr["number"] != args.pr_number:
                raise RuntimeError(f"PR #{args.pr_number} nie jest otwarty lub nie spełnia zasad autoryzacji.")
            if not eligible(pr, allow_draft=args.allow_draft):
                if pr.get("draft") is True:
                    raise RuntimeError(f"PR #{args.pr_number} jest Draft. Wymagane --allow-draft.")
                raise RuntimeError(f"PR #{args.pr_number} ma nieznany status gotowości.")
            candidates = [pr]
        if publish and publish_pending(env, args.pr_number):
            return 0
        if args.pr_number is None:
            prs = api("pulls?state=open&per_page=100", env=env)
            if not isinstance(prs, list):
                raise RuntimeError("Nieprawidłowa odpowiedź listy PR-ów.")
            candidates = sorted((pr for pr in prs if eligible(pr)), key=lambda pr: pr["number"])
        for pr in candidates:
            number, sha = pr["number"], pr["head"]["sha"]
            path = result_path(number, sha, args.e2e_file, args.capture_visual_baselines)
            if path.exists() or previously_reported(number, sha, args.e2e_file,
                                                   args.capture_visual_baselines):
                continue
            print(f"PLAN: PR #{number} SHA {sha}")
            if args.run:
                path = execute(pr, env, publish=publish, e2e_file=args.e2e_file,
                               capture_visual=args.capture_visual_baselines, allow_draft=args.allow_draft)
                if publish:
                    notify_ntfy(path)
                    publish_result(path, read_result(path), env)
            else:
                print("Dry-run: bez pobierania kodu, testów i publikacji.")
            return 0
        print("Brak nowych autoryzowanych commitów PR z etykietą local-tests.")
        return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, request_stop)
    try:
        sys.exit(main())
    except StopRequested:
        print("AGENT STOP: SIGTERM. Sprawdź ewentualny zapis aktywnego przebiegu przed wznowieniem.", file=sys.stderr)
        sys.exit(128 + signal.SIGTERM)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"AGENT FAIL: {error}", file=sys.stderr)
        sys.exit(1)
