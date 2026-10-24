"""Mocked SDK and pipeline tests; no command is executed on the host or cloud."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services import celesto_service as sdk
from app.services import pipeline
from app.services.agent import AgentResult, _validate_test_path


class Ledger:
    def __init__(self):
        self.errors = []
        self.rows = []

    def update(self, **row):
        self.rows.append(row)
        return True


@pytest.fixture
def settings(tmp_path):
    return replace(get_settings(), celesto_api_key="synthetic-celesto-test-key",
                   llm_api_key="synthetic-llm-test-key", llm_model="test-model",
                   report_dir=tmp_path / "reports", log_dir=tmp_path / "logs", fault="")


@pytest.fixture
def record():
    return {
        "run_id": "unit-test-run", "sandbox_name": "sbx-testing123", "problem_id": "test-bug",
        "detail": {"repo_url": "https://github.com/example/repo", "commit": "a" * 40,
                   "subdir": "project", "fix_dir": "fixed", "issue_text": "Expected one, got zero.",
                   "expected_signature": "assert 0 == 1"},
    }


@pytest.fixture
def fake_runtime(monkeypatch):
    class FakeSandbox:
        instances = []
        fail_create = False
        fail_ready = False
        fail_delete = False
        uncertain_test_once = False
        changed_source = False

        def __init__(self, name, settings):
            self.name, self.settings = name, settings
            self.deleted = False
            self.delete_error = ""
            self.cloud_id = "synthetic-cloud-id"
            self.create_options = {}
            self.created_monotonic = time.monotonic()
            self.boot_to_ready_s = 0.25
            self.commands = []
            self.files = {}
            self.__class__.instances.append(self)

        def create(self):
            if self.fail_create:
                raise sdk.SandboxCreateError("synthetic create failure")
            return self

        def wait_ready(self):
            if self.fail_ready:
                raise sdk.SandboxError("synthetic readiness failure")
            return 0.25

        def run(self, command, timeout_s=None):
            self.commands.append(command)
            if "git diff" in command and self.changed_source:
                return sdk.ExecResult(command, 1, stdout="tracked implementation changed")
            if "python3 -m pytest -q" in command:
                if self.__class__.uncertain_test_once:
                    self.__class__.uncertain_test_once = False
                    raise sdk.UncertainResultError("synthetic transport loss")
                if "cd /work/fixed" in command:
                    return sdk.ExecResult(command, 0, stdout="1 passed")
                return sdk.ExecResult(command, 1, stdout="assert 0 == 1")
            return sdk.ExecResult(command, 0)

        def read_file(self, path):
            return "def test_regression():\n    assert observed() == 1\n"

        def write_file(self, path, content):
            self.files[path] = content

        def delete(self):
            if self.fail_delete:
                self.delete_error = "synthetic deletion failure"
            else:
                self.deleted = True

    class FakeAgent:
        calls = 0

        def __init__(self, sandbox, settings):
            pass

        def solve(self, **kwargs):
            self.__class__.calls += 1
            return AgentResult(status="reproduced", test_path="/work/repo/project/tests/test_regression.py")

        def usage(self):
            return {}

    monkeypatch.setattr(pipeline, "Sandbox", FakeSandbox)
    monkeypatch.setattr(pipeline, "Agent", FakeAgent)
    return FakeSandbox, FakeAgent


def events(settings):
    return [json.loads(line) for line in (settings.report_dir / "failure_trace.jsonl").read_text().splitlines()]


def test_sdk_omitted_timeout_keeps_sdk_default():
    def run(command, timeout=30):
        assert timeout is not None
        return command, timeout
    assert sdk._invoke_exec(run, "example", None) == ("example", 30)
    assert sdk._invoke_exec(run, "example", 60) == ("example", 60)


@pytest.mark.parametrize("raw", [None, "1 passed", b"output", {"stdout": "output"}])
def test_sdk_output_without_status_is_uncertain(raw):
    with pytest.raises(sdk.UncertainResultError):
        sdk._normalise(raw, "example", 0.1)


def test_sdk_delete_failure_retains_handle_for_retry(settings):
    class Handle:
        calls = 0
        def delete(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic delete failure")
    sandbox = sdk.Sandbox("sbx-testing123", settings)
    sandbox.handle = Handle()
    sdk._ACTIVE.add(sandbox)
    sandbox.delete()
    assert not sandbox.deleted and sandbox in sdk._ACTIVE
    sandbox.delete()
    assert sandbox.deleted and sandbox not in sdk._ACTIVE
    assert not sandbox.delete_error


def test_cloud_only_guard_fails_before_sdk_import(settings, monkeypatch):
    monkeypatch.setattr(sdk, "_computer_class", lambda: SimpleNamespace)
    sandbox = sdk.Sandbox(settings=replace(settings, celesto_provider="local"))
    with pytest.raises(sdk.SandboxUnavailable, match="Cloud only"):
        sandbox.create()


def test_success_completes_all_ten_steps(settings, record, fake_runtime):
    sandbox_cls, _ = fake_runtime
    ledger = Ledger()
    outcome = pipeline.execute_run(record, settings, ledger)
    assert outcome.success and outcome.steps_completed == 10
    assert outcome.detail["cleanup_ok"]
    assert all(s.deleted for s in sandbox_cls.instances)
    assert {r["step"] for r in events(settings) if r["event"] == "step_finished"} == set(range(1, 11))
    evidence = json.loads((settings.report_dir / "evidence" / f"run-{outcome.sandbox_name}.json").read_text())
    assert evidence["cleanup_ok"] and evidence["gold_run"]["exit_code"] == 0
    assert ledger.rows[-1]["state"] == "deleted"
    assert any("/work/fixed/fixed/." in c for c in sandbox_cls.instances[0].commands)


@pytest.mark.parametrize("fault", ["injected", "transport"])
def test_step7_fresh_recovery_uses_saved_test_not_second_llm(settings, record, fake_runtime, fault):
    sandbox_cls, agent_cls = fake_runtime
    if fault == "injected":
        settings = replace(settings, fault="step7")
    else:
        sandbox_cls.uncertain_test_once = True
    outcome = pipeline.execute_run(record, settings, Ledger())
    assert outcome.success and outcome.policy_applied == "fresh"
    assert len(sandbox_cls.instances) == 2 and agent_cls.calls == 1
    assert all(s.deleted for s in sandbox_cls.instances)
    assert sandbox_cls.instances[0].name != sandbox_cls.instances[1].name
    policy = next(r for r in events(settings) if r["event"] == "policy_applied")
    assert policy["policy"] == "fresh" and policy["failed_step"] == 7


def test_cleanup_failure_is_not_success(settings, record, fake_runtime):
    fake_runtime[0].fail_delete = True
    outcome = pipeline.execute_run(record, settings, Ledger())
    assert not outcome.success and outcome.status == "cleanup_failed"
    assert not outcome.detail["cleanup_ok"]


def test_no_fresh_guest_when_retiring_old_guest_fails(settings, record, fake_runtime):
    fake_runtime[0].fail_delete = True
    outcome = pipeline.execute_run(record, replace(settings, fault="step7"), Ledger())
    assert not outcome.success
    assert len(fake_runtime[0].instances) == 1
    assert not any(r["event"] == "policy_applied" for r in events(settings))


@pytest.mark.parametrize("flag,step", [("fail_create", 2), ("fail_ready", 3)])
def test_startup_failure_has_correct_step_and_cleanup(settings, record, fake_runtime, flag, step):
    setattr(fake_runtime[0], flag, True)
    outcome = pipeline.execute_run(record, settings, Ledger())
    assert not outcome.success and outcome.failed_step == step
    assert fake_runtime[0].instances[0].deleted


def test_gold_rejects_modified_implementation(settings, record, fake_runtime):
    fake_runtime[0].changed_source = True
    outcome = pipeline.execute_run(record, settings, Ledger())
    assert not outcome.success and outcome.failed_step == 8
    assert "tracked project files changed" in outcome.reason


@pytest.mark.parametrize("field,value", [("subdir", "../../etc"), ("subdir", "/etc"),
    ("fix_dir", "x; touch /tmp/unwanted"), ("commit", "HEAD"), ("expected_signature", "[")])
def test_invalid_inputs_rejected_before_create(settings, record, fake_runtime, field, value):
    record["detail"][field] = value
    outcome = pipeline.execute_run(record, settings, Ledger())
    assert not outcome.success and outcome.failed_step == 1
    assert not fake_runtime[0].instances


@pytest.mark.parametrize("path", ["/work/repo/conftest.py", "/work/repo/latest.py",
    "/work/repo/tests/../../test_escape.py", "/etc/test_any.py"])
def test_agent_write_path_must_stay_in_tests(path):
    assert _validate_test_path(path, "/work/repo")


def test_bootstrap_has_real_shell_braces_and_requires_pytest():
    assert "{{" not in pipeline._BOOTSTRAP and "}}" not in pipeline._BOOTSTRAP
    assert pipeline._BOOTSTRAP.endswith("git --version && python3 --version && python3 -m pytest --version")


def test_execute_route_schedules_existing_runner(settings, record, monkeypatch):
    from app.api import routes
    monkeypatch.setattr(routes, "get_run", lambda run_id: record)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    background = BackgroundTasks()
    assert routes.api_execute_run(record["run_id"], background)["status"] == "scheduled"
    assert background.tasks[0].func is pipeline.execute_run_record
    assert background.tasks[0].args == (record,)
