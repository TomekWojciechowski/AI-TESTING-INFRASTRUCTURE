import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import agent_testow_linux as runner


SHA = "1" * 40
NEW_SHA = "2" * 40


def pull(sha=SHA, *, draft=False):
    return {
        "number": 7, "state": "open", "draft": draft, "labels": [{"name": runner.LABEL}],
        "head": {"sha": sha, "repo": {"full_name": runner.REPO},
                 "user": {"login": runner.OWNER}, "ref": "claude/test"},
    }


class RunnerTests(unittest.TestCase):
    def enter(self, manager):
        return self.stack.enter_context(manager)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.enter(tempfile.TemporaryDirectory())
        self.state = Path(temp)
        self.enter(patch.object(runner, "STATE", self.state))
        self.enter(patch.object(runner, "WORK", self.state / "work"))
        self.enter(patch.object(runner, "agent_lock", side_effect=lambda: contextlib.nullcontext(True)))
        self.enter(patch.object(runner, "github_env", return_value={"GH_TOKEN": "mock-github"}))
        self.enter(patch.object(runner.shutil, "disk_usage", return_value=Mock(free=16 * 1024**3)))
        self.enter(patch.dict(os.environ, {}, clear=True))
        self.stdout = self.enter(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enter(contextlib.redirect_stderr(io.StringIO()))
        self.api = self.enter(patch.object(runner, "api", side_effect=self.github))
        self.enter(patch.object(runner, "ci_identity", return_value=(999, 999, [999])))
        self.git = Mock(side_effect=self.git_command)
        self.podman = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.podman_control = Mock(return_value=subprocess.CompletedProcess([], 1))
        self.enter(patch.object(runner, "launch", side_effect=self.command))
        self.head = pull()
        self.post = Mock(side_effect=lambda body: {"id": 42, "body": body})
        self.comments = Mock(return_value=[])

    def github(self, endpoint, *, env, post=None):
        if post is not None:
            return self.post(post)
        if endpoint == "pulls/7":
            return self.head
        if endpoint.startswith("pulls?"):
            return [self.head]
        if endpoint.startswith("issues/7/comments?"):
            return self.comments(endpoint)
        raise AssertionError(endpoint)

    def git_command(self, argv, **kwargs):
        output = SHA + "\n" if "rev-parse" in argv else ""
        return subprocess.CompletedProcess(argv, 0, output, "")

    def command(self, argv, **kwargs):
        if argv[:3] == ["podman", "--remote=false", "run"]:
            return self.podman(argv, **kwargs)
        if argv[:3] == ["podman", "--remote=false", "container"]:
            return self.podman_control(argv, **kwargs)
        return self.git(argv, **kwargs)

    def invoke(self, *args):
        with patch.object(runner.sys, "argv", ["agent", *args]):
            return runner.main()

    def path(self, *, sha=SHA, spec=None, visual=False):
        return runner.result_path(7, sha, spec, visual)

    def result(self, **kwargs):
        return runner.read_result(self.path(**kwargs))

    def advance(self, seconds=3600):
        return patch.object(runner.time, "time", return_value=runner.time.time() + seconds)

    def test_default_dry_run(self):
        self.assertEqual(self.invoke(), 0)
        self.assertIn("Dry-run", self.stdout.getvalue())
        self.git.assert_not_called()
        self.podman.assert_not_called()
        self.post.assert_not_called()
        self.assertFalse(self.path().exists())

    def test_automatic_run_skips_draft_and_selects_ready_pr(self):
        draft = dict(pull(draft=True), number=6)
        self.api.side_effect = lambda endpoint, **kwargs: (
            [draft, self.head] if endpoint.startswith("pulls?") else self.github(endpoint, **kwargs)
        )
        self.assertEqual(self.invoke("--run", "--no-publish"), 0)
        fetches = [call.args[0][-1] for call in self.git.call_args_list if "fetch" in call.args[0]]
        self.assertEqual(fetches, ["refs/pull/7/head"])
        self.assertEqual(self.result()["outcome"], "PASS")

    def test_automatic_dry_run_and_execution_skip_draft(self):
        self.head = pull(draft=True)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.invoke("--run"), 0)
        self.assertNotIn("PLAN:", self.stdout.getvalue())
        self.git.assert_not_called()
        self.podman.assert_not_called()
        self.post.assert_not_called()

    def test_automatic_run_requires_local_tests_label(self):
        self.head["labels"] = []
        self.assertEqual(self.invoke("--run"), 0)
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_manual_draft_requires_explicit_flag(self):
        self.head = pull(draft=True)
        with self.assertRaisesRegex(RuntimeError, "Wymagane --allow-draft"):
            self.invoke("--run", "--pr-number", "7")
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_allow_draft_requires_manual_number(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke("--run", "--allow-draft")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--allow-draft wymaga --pr-number", self.stderr.getvalue())
        self.api.assert_not_called()
        self.git.assert_not_called()

    def test_manually_authorized_draft_runs_and_is_deduplicated(self):
        self.head = pull(draft=True)
        args = ("--run", "--pr-number", "7", "--allow-draft")
        self.assertEqual(self.invoke(*args), 0)
        self.assertEqual(self.invoke(*args), 0)
        self.head = pull()
        self.assertEqual(self.invoke("--run"), 0)
        self.assertEqual(self.result()["github"]["status"], "published")
        self.assertEqual(self.podman.call_count, 6)
        self.post.assert_called_once()

    def test_automatic_retry_publishes_saved_draft_result_without_running_tests(self):
        self.head = pull(draft=True)
        self.post.side_effect = runner.GitHubError("issues/7/comments", 429)
        self.invoke("--run", "--pr-number", "7", "--allow-draft")
        self.assertEqual(self.result()["github"]["status"], "pending")

        self.post.side_effect = lambda body: {"id": 42, "body": body}
        self.git.reset_mock()
        self.podman.reset_mock()
        with self.advance():
            self.assertEqual(self.invoke("--run"), 0)
        self.assertEqual(self.result()["github"]["status"], "published")
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_authorization_fields(self):
        self.assertTrue(runner.eligible(pull()))
        edits = [
            {"state": "closed"}, {"labels": []}, {"number": True},
            {"head": {"sha": "malformed"}}, {"head": {"ref": "other/topic"}},
            {"head": {"user": {"login": "other"}}},
            {"head": {"repo": {"full_name": "other/fork"}}},
        ]
        for edit in edits:
            with self.subTest(edit=edit):
                candidate = pull()
                head_edit = edit.get("head")
                if head_edit:
                    candidate["head"].update(head_edit)
                else:
                    candidate.update(edit)
                self.assertFalse(runner.eligible(candidate))
                candidate["draft"] = True
                self.assertFalse(runner.eligible(candidate, allow_draft=True))
                self.head = candidate
                with self.assertRaisesRegex(RuntimeError, "nie spełnia zasad autoryzacji"):
                    self.invoke("--run", "--pr-number", "7", "--allow-draft")
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_missing_or_invalid_draft_status_fails_closed(self):
        candidate = pull()
        del candidate["draft"]
        for invalid in (candidate, dict(candidate, draft=0), dict(candidate, draft="false")):
            self.assertFalse(runner.eligible(invalid))
            self.assertFalse(runner.eligible(invalid, allow_draft=True))

    def test_revalidation_blocks_fetch_after_sha_status_or_authorization_changes(self):
        for selected, current, allow_draft in (
            (pull(), pull(NEW_SHA), False),
            (pull(), pull(draft=True), False),
            (pull(), pull(draft=True), True),
            (pull(), dict(pull(), state="closed"), False),
            (pull(), dict(pull(), labels=[]), False),
            (pull(draft=True), pull(), True),
        ):
            with self.subTest(selected=selected, current=current):
                def prepare():
                    self.head = current

                self.head = copy.deepcopy(selected)
                with patch.object(runner, "prepare_workspace", side_effect=prepare):
                    with self.assertRaisesRegex(RuntimeError, "zmienił się SHA, status lub autoryzacja"):
                        runner.execute(selected, {}, publish=True, allow_draft=allow_draft)
                self.assertFalse((self.state / runner.ACTIVE_RUN).exists())
        self.git.assert_not_called()
        self.podman.assert_not_called()
        self.assertFalse(self.path().exists())

    def test_github_list_error_never_fetches_or_tests(self):
        self.api.side_effect = runner.GitHubError("pulls?state=open&per_page=100", 429)
        with self.assertRaises(runner.GitHubError):
            self.invoke("--run")
        self.git.assert_not_called()
        self.podman.assert_not_called()
        self.post.assert_not_called()

    def test_github_revalidation_error_never_fetches_or_tests(self):
        self.api.side_effect = [[self.head], runner.GitHubError("pulls/7", 503)]
        with patch.object(runner, "prepare_workspace"):
            with self.assertRaises(runner.GitHubError):
                self.invoke("--run")
        self.git.assert_not_called()
        self.podman.assert_not_called()
        self.post.assert_not_called()
        self.assertFalse(self.path().exists())
        self.assertFalse((self.state / runner.ACTIVE_RUN).exists())

    def test_malformed_github_metadata_fails_closed(self):
        self.api.side_effect = None
        for malformed in (None, [], dict(pull(), head="invalid"), dict(pull(), labels=None)):
            with self.subTest(malformed=malformed):
                self.api.return_value = [malformed]
                self.assertEqual(self.invoke("--run"), 0)
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_wrong_fetched_sha_prevents_worktree_and_tests(self):
        self.head = pull(NEW_SHA)
        with self.assertRaisesRegex(RuntimeError, "Pobrany SHA"):
            self.invoke("--run")
        self.assertFalse(any("worktree" in call.args[0] for call in self.git.call_args_list))
        self.podman.assert_not_called()
        self.assertFalse(self.path(sha=NEW_SHA).exists())

    def test_result_precedes_post_and_success_is_deduplicated(self):
        def confirm(body):
            result = self.result()
            self.assertEqual(result["sha"], SHA)
            self.assertEqual(result["outcome"], "PASS")
            self.assertEqual(len(result["checks"]), 6)
            self.assertEqual(result["github"]["status"], "uncertain")
            return {"body": body, "id": 42}

        self.post.side_effect = confirm
        self.assertEqual(self.invoke("--run"), 0)
        self.assertEqual(self.invoke("--run"), 0)
        self.assertEqual(self.podman.call_count, 6)
        self.post.assert_called_once()
        self.assertEqual(self.result()["github"]["comment_id"], 42)
        self.assertTrue(runner.report_stamp(7, SHA, None).exists())

    def test_lost_post_is_reconciled_on_second_page_without_reexecution(self):
        saved = {}

        def lose_response(body):
            saved.update(body=body, id=314)
            raise runner.GitHubError("issues/7/comments")

        self.post.side_effect = lose_response
        self.invoke("--run")
        self.assertEqual(self.result()["github"]["status"], "uncertain")
        self.comments.side_effect = [[{"id": 12, "body": "other"}] * 100, [saved]]
        with self.advance():
            self.invoke("--run")
        self.assertEqual(self.result()["github"]["comment_id"], 314)
        self.assertEqual(self.comments.call_count, 2)
        self.post.assert_called_once()
        self.assertEqual(self.podman.call_count, 6)

    def test_unconfirmed_post_never_reposted_even_if_marker_not_found(self):
        self.post.side_effect = runner.GitHubError("issues/7/comments", 502)
        self.invoke("--run")
        for seconds in (3600, 7200, 10800, 14400):
            with self.advance(seconds):
                self.invoke("--run")
        self.assertEqual(self.result()["github"]["status"], "unconfirmed")
        self.assertEqual(self.result()["github"]["attempts"], 3)
        self.assertEqual(self.comments.call_count, 2)
        self.post.assert_called_once()
        self.assertEqual(self.podman.call_count, 6)

    def test_explicit_rate_limit_retries_are_bounded_and_deferred(self):
        self.post.side_effect = runner.GitHubError("issues/7/comments", 429, {"retry-after": "1800"})
        with self.advance(0):
            self.invoke("--run")
        with self.advance(900):
            self.invoke("--run")
        self.post.assert_called_once()
        for seconds in (2700, 5400, 8100):
            with self.advance(seconds):
                self.invoke("--run")
        self.assertEqual(self.result()["github"]["status"], "exhausted")
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.podman.call_count, 6)

    def test_closed_or_changed_pr_prevents_new_post(self):
        self.post.side_effect = runner.GitHubError("issues/7/comments", 429)
        self.invoke("--run")
        self.head = pull(NEW_SHA)
        with self.advance():
            self.invoke("--run")
        self.assertEqual(self.result()["github"]["status"], "superseded")
        self.post.assert_called_once()
        self.assertEqual(self.podman.call_count, 6)

    def test_legacy_stamps_skip_without_creating_a_pass_record(self):
        legacy = self.state / f"pr-7-{SHA}.done"
        legacy.write_text("legacy", encoding="utf-8")
        runner.report_stamp(7, SHA, "e2e/existing.spec.js").touch()
        self.invoke("--run")
        self.invoke("--run", "--e2e-file", "e2e/existing.spec.js")
        self.assertFalse(list(self.state.glob("*.json")))
        self.assertEqual(legacy.read_text(encoding="utf-8"), "legacy")
        self.git.assert_not_called()
        self.podman.assert_not_called()

    def test_new_full_stamp_does_not_block_e2e_file_named_full(self):
        self.invoke("--run")
        self.invoke("--run", "--e2e-file", "e2e/full.spec.js")
        self.assertEqual(self.result()["github"]["status"], "published")
        self.assertEqual(self.result(spec="e2e/full.spec.js")["github"]["status"], "published")
        self.assertEqual(self.podman.call_count, 12)

    def test_local_only_runs_remain_local_and_scopes_are_distinct(self):
        args = ("--run", "--no-publish")
        self.invoke(*args)
        self.invoke(*args, "--e2e-file", "e2e/full.spec.js")
        self.invoke(*args, "--e2e-file", "e2e/wizualne.spec.js")
        self.invoke(*args, "--capture-visual-baselines")
        self.invoke("--run")
        self.assertEqual(self.podman.call_count, 24)
        self.assertEqual(len(list(self.state.glob("*.json"))), 4)
        self.assertEqual(self.result(spec="e2e/wizualne.spec.js", visual=True)["outcome"],
                         "BASELINES_GENERATED")
        self.assertEqual(self.result()["github"]["status"], "disabled")
        self.post.assert_not_called()

    def test_failure_persists_and_reports_no_untrusted_log_output(self):
        def run(argv, *, log, **kwargs):
            if "--workers=1" in argv:
                log.write("mock-private-data\n")
                return subprocess.CompletedProcess(argv, 1)
            return subprocess.CompletedProcess(argv, 0)

        self.podman.side_effect = run
        self.invoke("--run")
        result = self.result()
        self.assertEqual(result["outcome"], "FAIL")
        self.assertEqual(result["checks"][-1]["outcome"], "FAIL")
        self.assertIn("mock-private-data", (self.state / result["log"]).read_text(encoding="utf-8"))
        self.assertNotIn("mock-private-data", self.post.call_args.args[0])
        self.assertNotIn("mock-private-data", json.dumps(result))

    def test_interrupted_run_remains_guarded(self):
        with patch.object(runner, "run_stage", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke("--run")
        self.assertEqual(self.result()["outcome"], "RUNNING")
        with self.assertRaises(runner.InterventionRequired):
            self.invoke("--run")
        self.podman.assert_not_called()
        self.post.assert_not_called()

    def test_successful_podman_stop_is_confirmed_before_releasing_guard(self):
        self.podman.side_effect = subprocess.TimeoutExpired(["podman"], 1)
        self.podman_control.side_effect = self.container_control(stop_works=True)
        self.invoke("--run", "--no-publish")
        self.assertEqual(self.result()["outcome"], "FAIL")
        self.assertFalse((self.state / runner.ACTIVE_RUN).exists())
        self.assertEqual([call.args[0][3] for call in self.podman_control.call_args_list],
                         ["exists", "inspect", "stop", "exists", "inspect"])

    def test_failed_podman_stop_blocks_new_pr_and_preserves_worktree(self):
        self.podman.side_effect = subprocess.TimeoutExpired(["podman"], 1)
        self.podman_control.side_effect = self.container_control(stop_works=False)
        with self.assertRaisesRegex(runner.InterventionRequired, "potwierdził zatrzymania"):
            self.invoke("--run")
        record = json.loads((self.state / runner.ACTIVE_RUN).read_text(encoding="utf-8"))
        self.assertIsNotNone(record["container"])
        self.assertEqual(self.result()["outcome"], "RUNNING")
        self.assertFalse(any("remove" in call.args[0] for call in self.git.call_args_list))
        self.assertEqual(self.api.call_count, 2)
        self.head = pull(NEW_SHA)
        with self.assertRaisesRegex(runner.InterventionRequired, "Nowe przebiegi zablokowane"):
            self.invoke("--run")
        self.assertEqual(self.api.call_count, 2)
        self.podman.assert_called_once()
        self.post.assert_not_called()
        self.invoke()
        self.podman.assert_called_once()

    def test_timeout_with_missing_container_keeps_guard(self):
        self.podman.side_effect = subprocess.TimeoutExpired(["podman"], 1)
        with self.assertRaisesRegex(runner.InterventionRequired, "po przerwaniu startu"):
            self.invoke("--run")
        self.assertTrue((self.state / runner.ACTIVE_RUN).exists())
        with self.assertRaises(runner.InterventionRequired):
            self.invoke("--run")
        self.podman.assert_called_once()

    def test_inconsistent_container_identity_never_calls_stop(self):
        self.podman_control.side_effect = self.container_control(stop_works=True, token_matches=False)
        with self.assertRaisesRegex(runner.InterventionRequired, "tożsamości"):
            self.invoke("--run")
        self.assertEqual([call.args[0][3] for call in self.podman_control.call_args_list],
                         ["exists", "inspect"])
        self.assertTrue((self.state / runner.ACTIVE_RUN).exists())

    def test_sigterm_stops_container_and_leaves_interruption_guard(self):
        self.podman.side_effect = runner.StopRequested
        self.podman_control.side_effect = self.container_control(stop_works=True)
        with self.assertRaises(runner.StopRequested):
            self.invoke("--run")
        self.assertIsNone(json.loads((self.state / runner.ACTIVE_RUN).read_text())["container"])
        self.assertEqual(self.result()["outcome"], "RUNNING")
        self.assertEqual([call.args[0][3] for call in self.podman_control.call_args_list],
                         ["exists", "inspect", "stop", "exists", "inspect"])

    def container_control(self, *, stop_works, token_matches=True):
        running = True
        container_id = "a" * 64

        def control(argv, **kwargs):
            nonlocal running
            guard = json.loads((self.state / runner.ACTIVE_RUN).read_text(encoding="utf-8"))
            name, token = guard["container"]["name"], guard["container"]["token"]
            action = argv[3]
            if action == "stop":
                self.assertEqual(argv[4:], ["--time", "5", container_id])
                running = not stop_works
                return subprocess.CompletedProcess(argv, int(running))
            self.assertEqual(argv[4:], [name])
            info = {"Id": container_id, "Name": name,
                    "Config": {"Labels": {runner.CONTAINER_LABEL: token if token_matches else "foreign"}},
                    "State": {"Status": "running" if running else "exited", "Running": running}}
            output = json.dumps([info]) if action == "inspect" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        return control

    def test_podman_isolation_and_environment_allowlist(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-private",
                                     "OTHER_SECRET": "mock-secret", "HTTPS_PROXY": "mock-proxy",
                                     "LD_PRELOAD": "mock-injected"}):
            self.invoke("--run", "--no-publish")
        for index, call in enumerate(self.podman.call_args_list):
            argv = call.args[0]
            self.assertEqual(argv[0:3], ["podman", "--remote=false", "run"])
            self.assertEqual(call.kwargs["identity"], (999, 999, [999]))
            self.assertEqual(set(call.kwargs["env"]),
                             {"PATH", "HOME", "LC_ALL", "USER", "LOGNAME", "XDG_RUNTIME_DIR"})
            self.assertNotIn("GIT_SSH_COMMAND", str(argv))
            self.assertNotIn("mock-", str(call))
            for option in ("--cap-drop", "no-new-privileges", "--userns=keep-id", "--http-proxy=false",
                           "--restart=no", "--timeout"):
                self.assertIn(option, argv)
            if index == 0:
                self.assertNotIn("--network", argv)
                self.assertIn("--ignore-scripts", argv)
            else:
                self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_atomic_write_failure_keeps_old_value(self):
        path = self.state / "record.json"
        runner.atomic_write(path, '{"previous": true}')
        with patch.object(runner.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                runner.atomic_write(path, '{"new": true}')
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"previous": True})
        self.assertFalse(list(self.state.glob(".runner-*")))

    def test_ntfy_is_off_by_default_and_failure_is_not_replayed(self):
        with patch.object(runner, "build_opener") as opener:
            self.invoke("--run")
            opener.assert_not_called()
            opener.return_value.open.side_effect = URLError("mock-private-address")
            with patch.dict(os.environ, {"AI_TEST_RUNNER_NTFY_URL": "https://example.invalid/topic"}):
                with patch.object(runner, "read_secret", return_value="mock-ntfy"):
                    runner.notify_ntfy(self.path())
                    runner.notify_ntfy(self.path())
        self.assertEqual(self.result()["ntfy"], "unconfirmed")
        opener.return_value.open.assert_called_once()
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer mock-ntfy")
        self.assertIn(SHA, request.data.decode("utf-8"))
        self.assertNotIn("mock-private-address", self.stderr.getvalue())
        self.assertIsNone(runner.NoRedirect().redirect_request(None, None, 307, "", {}, ""))


class GitHubTransportTests(unittest.TestCase):
    def test_parses_headers_and_preserves_json_body(self):
        stdout = 'HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"id":42}'
        with patch.object(runner, "launch", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
            self.assertEqual(runner.api("issues/7/comments", env={}, post="report"), {"id": 42})

    def test_rate_limit_uses_headers_without_printing_github_stderr(self):
        stdout = "HTTP/2.0 429 Too Many Requests\nRetry-After: 300\n\n{}"
        with patch.object(runner, "launch", return_value=subprocess.CompletedProcess([], 1, stdout, "mock-secret")):
            with self.assertRaises(runner.GitHubError) as caught:
                runner.api("issues/7/comments", env={}, post="report")
        self.assertTrue(caught.exception.rate_limited())
        self.assertEqual(caught.exception.headers["retry-after"], "300")
        self.assertNotIn("mock-secret", str(caught.exception))


@unittest.skipUnless(os.name == "posix", "fcntl wymaga systemu POSIX")
class LockTests(unittest.TestCase):
    def test_second_lock_fails_until_first_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            info = Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
            with patch.object(runner, "STATE", Path(directory)), patch.object(Path, "lstat", return_value=info):
                with runner.agent_lock() as first, runner.agent_lock() as second:
                    self.assertTrue(first)
                    self.assertFalse(second)
                with runner.agent_lock() as next_run:
                    self.assertTrue(next_run)


@unittest.skipUnless(sys.platform.startswith("linux"), "Test grup procesów wymaga Linuksa")
class ProcessGroupTests(unittest.TestCase):
    command = [
        sys.executable, "-c",
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(20)\n",
    ]

    def live_process(self, pid):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
        except FileNotFoundError:
            return False
        return state not in {"Z", "X"}

    def read_child_pid(self, output):
        output.seek(0)
        return int(output.readline())

    def assert_process_ended(self, pid):
        deadline = time.monotonic() + 5
        while self.live_process(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.live_process(pid), "Proces testowy nadal działa")

    def test_timeout_stops_child_before_next_run_may_start(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile(mode="w+") as old_log:
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run(self.command, stdout=old_log, start_new_session=True, timeout=0.5)
            previous_child = self.read_child_pid(old_log)
            try:
                self.assertTrue(self.live_process(previous_child))
            finally:
                if self.live_process(previous_child):
                    os.kill(previous_child, signal.SIGKILL)
            self.assert_process_ended(previous_child)
            with patch.object(runner, "STATE", Path(directory)), tempfile.TemporaryFile(mode="w+") as log:
                try:
                    with runner.active_run(7, SHA):
                        with self.assertRaises(runner.InterventionRequired):
                            with runner.active_run(8, NEW_SHA):
                                self.fail("Drugi przebieg wystartował w trakcie pierwszego")
                        runner.launch(self.command, log=log, timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    self.fail("Oczekiwano przekroczenia limitu czasu")
                child = self.read_child_pid(log)
                self.assertFalse(self.live_process(child), "Potomek przetrwał timeout")
                with runner.active_run(8, NEW_SHA):
                    self.assertFalse(self.live_process(child))

    def test_sigkill_blocks_next_run_while_child_is_alive(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "STATE", Path(directory)):
            marker = Path(directory) / "test-child"
            pid = os.fork()
            if pid == 0:
                with runner.active_run(7, SHA):
                    sleeper = subprocess.Popen(["/bin/sleep", "20"], start_new_session=True)
                    marker.write_text(str(sleeper.pid), encoding="ascii")
                    os.kill(os.getpid(), signal.SIGKILL)
                os._exit(1)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
            child = int(marker.read_text(encoding="ascii"))
            try:
                self.assertTrue(self.live_process(child))
                with self.assertRaises(runner.InterventionRequired):
                    with runner.active_run(8, NEW_SHA):
                        self.fail("Drugi przebieg wystartował po awarii koordynatora")
            finally:
                if self.live_process(child) and os.getpgid(child) == child:
                    os.killpg(child, signal.SIGKILL)
            self.assert_process_ended(child)


if __name__ == "__main__":
    unittest.main()
