"""Probe regression tests: all guests are mocked; evidence is temporary only."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.celesto_service import ExecResult


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(tmp_path):
    return replace(get_settings(), celesto_api_key="synthetic-probe-key",
                   report_dir=tmp_path / "reports", log_dir=tmp_path / "logs")


@pytest.fixture
def guest():
    class MockGuest:
        fail_delete = False
        fail_create = False
        curl_missing = False
        network_ignores_off = False
        loses_marker = False
        instances = []

        def __init__(self, name, settings, external_volume=False):
            self.name, self.settings = name, settings
            self.cloud_id = "synthetic-cloud-id"
            self.deleted = False
            self.delete_error = ""
            self.create_options = {"network_policy": settings.network_mode}
            self.external_volume = external_volume
            self.files = {}
            self.__class__.instances.append(self)

        def create(self):
            if self.fail_create:
                raise RuntimeError("synthetic startup failure")
            return self

        def wait_ready(self):
            return 0.1

        def write_file(self, path, content):
            self.files[path] = content

        def read_file(self, path):
            return "lost" if self.loses_marker else self.files[path]

        def stop(self):
            return True

        def start(self):
            return True

        def delete(self):
            self.deleted = not self.fail_delete
            self.delete_error = "synthetic delete failure" if self.fail_delete else ""

        def run(self, command, timeout_s=None):
            if command.startswith("command -v curl"):
                return ExecResult(command, 1 if self.curl_missing else 0,
                                  stdout="" if self.curl_missing else "/usr/bin/curl")
            if command.startswith("curl"):
                code = 7 if self.settings.network_mode == "off" and not self.network_ignores_off else 0
                return ExecResult(command, code, stdout="HTTP 200" if code == 0 else "")
            if command.startswith("rm -rf"):
                return ExecResult(command, 126, decision="block", stderr="synthetic policy block")
            return ExecResult(command, 0, stdout="job-1-ok")

    return MockGuest


def test_persistence_claim_requires_surviving_marker(settings, guest, monkeypatch):
    module = load_script("run_capabilities")
    monkeypatch.setattr(module, "Sandbox", guest)
    passed, path = module.persistence(settings)
    assert passed and guest.instances[0].external_volume
    assert json.loads(path.read_text())["marker_survived"]
    guest.loses_marker = True
    passed, path = module.persistence(settings)
    assert not passed and not json.loads(path.read_text())["passed"]


@pytest.mark.parametrize("flag", [None, "curl_missing", "network_ignores_off", "fail_delete"])
def test_network_policy_requires_working_control_and_cleanup(settings, guest, monkeypatch, flag):
    module = load_script("run_capabilities")
    monkeypatch.setattr(module, "Sandbox", guest)
    if flag:
        setattr(guest, flag, True)
    passed, path = module.network_policy(settings)
    assert passed is (flag is None)
    payload = json.loads(path.read_text())
    assert len(payload["evidence_paths"]) == 2
    assert payload["passed"] is passed


@pytest.mark.parametrize("scenario", ["success", "failure", "interrupt"])
def test_lifecycle_cleanup_handles_all_controlled_paths(settings, guest, monkeypatch, scenario):
    module = load_script("run_lifecycle")
    monkeypatch.setattr(module, "Sandbox", guest)
    name, passed, path = module.cleanup_probe(settings, scenario)
    assert passed and guest.instances[0].deleted
    assert json.loads(path.read_text())["scenario"] == scenario


def test_startup_failure_is_not_lifecycle_proof(settings, guest, monkeypatch):
    module = load_script("run_lifecycle")
    monkeypatch.setattr(module, "Sandbox", guest)
    guest.fail_create = True
    _, passed, _ = module.cleanup_probe(settings, "failure")
    assert not passed


def test_containment_requires_block_and_surviving_guest_marker(settings, guest, monkeypatch):
    module = load_script("run_containment")
    monkeypatch.setattr(module, "Sandbox", guest)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    assert module.main() == 0
    payload = json.loads((settings.report_dir / "containment.json").read_text())
    assert payload["blocked"] and payload["host_untouched"] and payload["cleanup_ok"]
    guest.fail_delete = True
    assert module.main() == 1
    assert not json.loads((settings.report_dir / "containment.json").read_text())["passed"]


def test_concurrency_records_real_cleanup_failure(settings, guest, monkeypatch):
    module = load_script("run_concurrency")
    monkeypatch.setattr(module, "Sandbox", guest)
    guest.fail_delete = True
    row = module._job(1, settings, Barrier(1), Barrier(1))
    assert row["ok"] and not row["cleanup_ok"]
    assert row["cloud_id"] == "synthetic-cloud-id"
    assert row["delete_error"]


def test_owned_sweep_selects_cloud_ids_not_logical_names(tmp_path):
    module = load_script("sweep_orphans")
    payload = {"sandboxes": [
        {"name": "sbx-saferepo12345678", "cloud_id": "owned-cloud-id"},
        {"name": "sbx-anotherproject123", "cloud_id": "other-cloud-id"},
    ]}
    (tmp_path / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    assert module.owned_ids(tmp_path) == {"owned-cloud-id": "sbx-saferepo12345678"}


def test_example_placeholders_do_not_enable_billable_calls(settings):
    assert not replace(settings, celesto_api_key="replace-me").has_celesto
    assert not replace(settings, llm_api_key="replace-me", llm_model="model").has_llm
