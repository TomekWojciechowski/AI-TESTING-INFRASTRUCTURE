"""Ręczny test cyklu życia kontenera w lokalnym rootless Podmanie."""

import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import agent_testow_linux as runner


CLIENT_TIMEOUT = 8
CONTAINER_TIMEOUT = 40
SLEEP_SECONDS = "60"


class PodmanLifecycle:
    def __init__(self, state, log, env, image_id, *, fail_stop):
        self.state = state
        self.log = log
        self.env = env
        self.image_id = image_id
        self.fail_stop = fail_stop
        self.identity = None
        self.client = None
        self.sleeper = None
        self.client_timed_out = False
        self.stop_attempted = False
        self.inspected_after_stop = False
        self.check = unittest.TestCase()

    @property
    def guard_path(self):
        return self.state / runner.ACTIVE_RUN

    def podman(self, *args, timeout=5, log=None):
        argv = ["podman", "--remote=false", *args]
        print(argv, file=self.log, flush=True)
        result = runner.launch(argv, env=self.env, cwd=self.state, timeout=timeout, log=log)
        self.log.write(f"exit={result.returncode}\n{result.stdout or ''}{result.stderr or ''}\n")
        self.log.flush()
        return result

    def read_identity(self):
        return json.loads(self.guard_path.read_text(encoding="utf-8"))["container"]

    def assert_blocked(self):
        self.check.assertEqual(self.read_identity(), self.identity)
        with self.check.assertRaisesRegex(runner.InterventionRequired, "Nowe przebiegi zablokowane"):
            with runner.active_run(2, "2" * 40):
                self.check.fail("Drugi przebieg wszedł do sekcji chronionej")

    def container_info(self):
        return runner.container_info(self.identity["name"], self.identity["token"])

    def assert_created_container(self, info):
        self.check.assertIsNotNone(info, "Brak kontenera o oczekiwanej nazwie i etykiecie")
        cid = (self.state / "container.cid").read_text(encoding="ascii").strip()
        self.check.assertEqual(info["Id"], cid, "ID różni się od zapisanego przez podman run")

    def check_running_sleeper(self):
        info = self.container_info()
        self.assert_created_container(info)
        self.check.assertTrue(info["State"]["Running"], "Kontener zakończył się przed próbą stop")
        pid = info["State"]["Pid"]
        self.check.assertGreater(pid, 0)
        self.sleeper = os.pidfd_open(pid)
        self.check.assertEqual(select.select([self.sleeper], [], [], 0)[0], [])
        cgroup_line = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").strip()
        self.check.assertTrue(cgroup_line.startswith("0::/"), cgroup_line)
        cgroup = Path("/sys/fs/cgroup") / cgroup_line.removeprefix("0::/")
        quota, period = (cgroup / "cpu.max").read_text(encoding="ascii").split()
        self.check.assertNotEqual(quota, "max", "Brak limitu CPU")
        self.check.assertLessEqual(int(quota) * 4, int(period))
        self.check.assertLessEqual(int((cgroup / "memory.max").read_text()), 64 * 1024**2)
        self.check.assertLessEqual(int((cgroup / "pids.max").read_text()), 16)
        return info

    def as_ci(self, args, *, timeout=120, cwd=None, log=None):
        self.check.assertEqual(args[0], "podman")
        if args[1] == "run":
            self.identity = self.read_identity()
            name, token = self.identity["name"], self.identity["token"]
            self.check.assertRegex(token, r"^[0-9a-f]{32}$")
            self.check.assertEqual(name, f"ai-test-runner-{token}")
            self.check.assertEqual(args[args.index("--name") + 1], name)
            self.check.assertEqual(args[args.index("--label") + 1], f"{runner.CONTAINER_LABEL}={token}")
            self.assert_blocked()
            self.check.assertEqual(timeout, CLIENT_TIMEOUT)
            runner.save_result(self.state / "container.json", self.identity)
            return self.run_container(timeout, log)
        self.check.assertEqual(args[1], "container")
        action = args[2]
        if action == "stop":
            self.assert_blocked()
            self.check.assertIsNotNone(self.client)
            self.check.assertTrue(self.client_timed_out)
            self.check.assertEqual(self.client.returncode, -signal.SIGKILL)
            with self.check.assertRaises(ProcessLookupError):
                os.killpg(self.client.pid, 0)
            info = self.check_running_sleeper()
            self.check.assertEqual(args, ["podman", "container", "stop", "--time", "5", info["Id"]])
            self.stop_attempted = True
            if self.fail_stop:
                print("Symulacja stop: pozostawiam działający kontener", file=self.log, flush=True)
                return subprocess.CompletedProcess(args, 125)
            result = self.podman(*args[1:], timeout=timeout)
            self.assert_blocked()
            return result
        self.check.assertIn(action, ("exists", "inspect"))
        self.check.assertEqual(args[3:], [self.identity["name"]])
        self.assert_blocked()
        result = self.podman(*args[1:])
        if action == "inspect" and self.stop_attempted and result.returncode == 0:
            self.inspected_after_stop = True
        return result

    def run_container(self, timeout, log):
        popen = subprocess.Popen

        def observe_child(*args, **kwargs):
            self.client = popen(*args, **kwargs)
            return self.client

        # Zachowujemy nazwę i etykietę agenta. Kontener zachowuje rekord po wyjściu,
        # a własny limit Podmana jest dłuższy niż timeout klienta.
        with patch.object(runner.subprocess, "Popen", side_effect=observe_child):
            try:
                return self.podman(
                    "run", "--name", self.identity["name"],
                    "--label", f"{runner.CONTAINER_LABEL}={self.identity['token']}",
                    "--cidfile", str(self.state / "container.cid"),
                    "--pull=never", "--network=none", "--restart=no", "--read-only",
                    "--image-volume=ignore", "--no-healthcheck", "--http-proxy=false",
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--userns=keep-id",
                    "--pid=private", "--ipc=private", "--cgroups=enabled",
                    "--memory=64m", "--cpus=0.25", "--pids-limit=16",
                    f"--timeout={CONTAINER_TIMEOUT}", "--entrypoint=/bin/sleep",
                    self.image_id, SLEEP_SECONDS, timeout=timeout, log=log,
                )
            except subprocess.TimeoutExpired:
                self.client_timed_out = True
                raise

    def run(self):
        def stage():
            with runner.active_run(1, "1" * 40) as guard:
                return runner.run_stage("podman-timeout", self.state, [SLEEP_SECONDS],
                                        online=False, timeout=CLIENT_TIMEOUT, log=self.log, guard=guard)

        with patch.object(runner, "STATE", self.state), patch.object(runner, "as_ci", self.as_ci):
            if self.fail_stop:
                with self.check.assertRaisesRegex(runner.InterventionRequired, "potwierdził zatrzymania"):
                    stage()
                self.check.assertEqual(select.select([self.sleeper], [], [], 0)[0], [])
                self.assert_blocked()
            else:
                self.check.assertIs(stage(), False)
                self.check.assertFalse(self.guard_path.exists())
                self.check.assertEqual(select.select([self.sleeper], [], [], 2)[0], [self.sleeper])
                with runner.active_run(2, "2" * 40):
                    pass
            self.check.assertTrue(self.inspected_after_stop, "Brak inspekcji agenta po próbie stop")

    def cleanup(self):
        if self.client is None:
            return

        def local_podman(args, **kwargs):
            self.check.assertEqual(args[:2], ["podman", "container"])
            self.check.assertIn(args[2], ("exists", "inspect"))
            return self.podman(*args[1:], **kwargs)

        with patch.object(runner, "as_ci", local_podman):
            info = self.container_info()
            self.assert_created_container(info)
            container_id = info["Id"]
            if not runner.container_stopped(info):
                self.podman("container", "stop", "--time", "2", container_id)
                info = self.container_info()
            self.check.assertTrue(runner.container_stopped(info), "Sprzątanie: kontener nadal aktywny")
            self.assert_created_container(info)
            self.check.assertIsNotNone(self.client.returncode, "Sprzątanie: klient nie został zebrany")
            with self.check.assertRaises(ProcessLookupError):
                os.killpg(self.client.pid, 0)
            if self.sleeper is not None:
                self.check.assertEqual(select.select([self.sleeper], [], [], 2)[0], [self.sleeper])
            result = self.podman("container", "rm", container_id)
            self.check.assertEqual(result.returncode, 0, "Sprzątanie: nie można usunąć kontenera testu")
            self.check.assertIsNone(self.container_info(), "Sprzątanie: kontener nadal istnieje")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True, help="Pełne ID zaufanego obrazu dostępnego lokalnie")
    args = parser.parse_args()
    image_id = args.image_id.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        parser.error("--image-id wymaga pełnego ID obrazu")
    if sys.platform != "linux":
        parser.error("Test wymaga Linuksa")
    import pwd

    account = pwd.getpwuid(os.geteuid())
    if account.pw_uid == 0 or account.pw_name != runner.USER:
        parser.error("Wymagany Linux i nieuprzywilejowane konto ci-runner")
    if not hasattr(os, "pidfd_open") or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        parser.error("Wymagany Linux z pidfd i cgroups v2")
    env = {"PATH": runner.TRUSTED_PATH, "HOME": account.pw_dir, "LC_ALL": "C.UTF-8",
           "USER": account.pw_name, "LOGNAME": account.pw_name,
           "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"}
    root = Path(tempfile.mkdtemp(prefix="ai-test-podman-"))
    print(f"Diagnostyka testu: {root}", flush=True)
    signal.signal(signal.SIGTERM, runner.request_stop)
    with (root / "preflight.log").open("w", encoding="utf-8") as log:
        probe = PodmanLifecycle(root, log, env, image_id, fail_stop=False)
        runner.require_success(probe.podman("image", "exists", image_id), "Lokalny obraz Podmana")
    for fail_stop in (False, True):
        state = root / ("failed-stop" if fail_stop else "confirmed-stop")
        state.mkdir(mode=0o700)
        with (state / "podman.log").open("w", encoding="utf-8") as log:
            scenario = PodmanLifecycle(state, log, env, image_id, fail_stop=fail_stop)
            try:
                scenario.run()
            finally:
                try:
                    scenario.cleanup()
                finally:
                    if scenario.sleeper is not None:
                        os.close(scenario.sleeper)
        print(f"PASS {state.name}", flush=True)
    shutil.rmtree(root)


if __name__ == "__main__":
    main()
