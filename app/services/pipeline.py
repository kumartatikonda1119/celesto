"""The SafeRepo pipeline: ten named steps, one risky command.

Step 7 is the authoritative evaluation of the agent-authored test. The agent
may also run exploratory commands and tests during step 6; those are equally
untrusted and execute only in the guest. When step 7 fails, the ``fresh``
recovery policy deletes the guest, creates a new uniquely named one, restores
the pinned repository and saved test, then tries step 7 once more.

Nothing in this module shells out on the host. Every command goes through
Sandbox.run -> policy -> audit -> Celesto.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from .agent import Agent, AgentResult, _validate_test_path
from .celesto_service import Sandbox, SandboxError, UncertainResultError
from .evidence import FailureTrace, ensure_dirs, redact, utc_now
from .lifecycle import (
    HOURLY_RATE_USD,
    RISKY_STEP,
    RunOutcome,
    RunTrace,
    TaskSpec,
    LedgerClient,
)

REPO_DIR = "/work/repo"
FIXED_DIR = "/work/fixed"
MAX_EVIDENCE_TEXT = 20_000

_SETUP_FAILURE_MARKERS = (
    "errors during collection",
    "ERROR collecting",
    "ModuleNotFoundError",
    "ImportError while importing",
    "No module named",
    "command not found",
    "SyntaxError",
)
_BOOTSTRAP = (
    "need_pkg() { command -v \"$1\" >/dev/null 2>&1; }; "
    "pkg_install() { "
    "  if command -v apt-get >/dev/null 2>&1; then "
    "    (apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq \"$@\" >/dev/null 2>&1); "
    "  elif command -v apk >/dev/null 2>&1; then apk add --no-cache \"$@\" >/dev/null 2>&1; "
    "  elif command -v dnf >/dev/null 2>&1; then dnf install -y \"$@\" >/dev/null 2>&1; "
    "  else return 1; fi; "
    "}; "
    "need_pkg git || pkg_install git; "
    "need_pkg python3 || pkg_install python3; "
    "(need_pkg pip3 || need_pkg pip) || pkg_install python3-pip; "
    "python3 -m pytest --version >/dev/null 2>&1 || "
    "  python3 -m pip install -q --break-system-packages pytest 2>/dev/null || "
    "  python3 -m pip install -q pytest 2>/dev/null || "
    "  pip3 install -q pytest 2>/dev/null || true; "
    "git --version && python3 --version && python3 -m pytest --version"
)


def _work_dir(spec: TaskSpec) -> str:
    return posixpath.join(REPO_DIR, spec.subdir) if spec.subdir else REPO_DIR


class StepFailure(RuntimeError):
    """A pipeline step could not be executed at all."""

    def __init__(self, step: int, reason: str, *, retryable: bool = True) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason
        self.retryable = retryable


@dataclass
class _RunState:
    record: dict[str, Any]
    spec: TaskSpec
    run_id: str
    sandbox_name: str
    settings: Settings
    ledger: LedgerClient
    trace: RunTrace
    started: float
    sandboxes: list[Sandbox] = field(default_factory=list)
    lifetimes_s: list[float] = field(default_factory=list)
    boot_s: float = 0.0
    test_path: str = ""
    test_content: str = ""
    agent_result: AgentResult | None = None
    agent_usage: dict[str, Any] = field(default_factory=dict)
    test_run: dict[str, Any] | None = None
    gold_run: dict[str, Any] | None = None
    verdict: dict[str, Any] = field(default_factory=dict)
    fault_fired: bool = False


def execute_run(
    record: dict[str, Any],
    settings: Settings | None = None,
    ledger: LedgerClient | None = None,
) -> RunOutcome:
    settings = settings or get_settings()
    ensure_dirs(settings)
    ledger = ledger or LedgerClient(settings=settings)

    run_id = record["run_id"]
    problem_id = record["problem_id"]
    sandbox_name = record["sandbox_name"]
    detail = record.get("detail") or {}

    try:
        spec = TaskSpec.from_detail(problem_id, detail)
    except (KeyError, TypeError) as exc:
        FailureTrace(settings=settings).event(
            "step_failed",
            run_id=run_id,
            step=1,
            step_name="validate_input",
            total_steps=10,
            sandbox_name=sandbox_name,
            error=f"invalid task input: missing {exc}",
        )
        return RunOutcome(
            run_id=run_id,
            sandbox_name=sandbox_name,
            success=False,
            status="step_failed",
            reason=f"invalid task input: missing {exc}",
            failed_step=1,
        )

    state = _RunState(
        record=record,
        spec=spec,
        run_id=run_id,
        sandbox_name=sandbox_name,
        settings=settings,
        ledger=ledger,
        trace=RunTrace(run_id, settings=settings),
        started=time.monotonic(),
    )
    outcome = RunOutcome(
        run_id=run_id,
        sandbox_name=sandbox_name,
        success=False,
        status="uncertain",
    )

    try:
        _step_validate(state)
        sandbox = _create_and_prepare(state)
        agent_result = _step_agent(state, sandbox)

        retried = False
        try:
            test_run = _step_execute_test(state, sandbox)
        except StepFailure as exc:
            if not exc.retryable or exc.step != RISKY_STEP or retried:
                raise
            # ---- the designed failure: step 7 of 10 -----------------------
            state.trace.failed(
                RISKY_STEP, exc.reason, sandbox_name=sandbox.name, retryable=True
            )
            ledger.update(
                run_id=run_id,
                sandbox_name=sandbox.name,
                problem_id=problem_id,
                state="failed",
                detail={"phase": "MidTaskFailure", "failed_step": RISKY_STEP, "error": exc.reason},
            )
            old_name = sandbox.name
            _retire(state, sandbox)
            if not sandbox.deleted:
                raise StepFailure(7, f"fresh recovery aborted: cleanup failed: {sandbox.delete_error}", retryable=False)
            new_name = _new_sandbox_name()
            outcome.policy_applied = "fresh"
            state.trace.policy_applied(
                "fresh",
                old_sandbox=old_name,
                new_sandbox=new_name,
                reason=exc.reason,
            )
            sandbox = _create_and_prepare(state, name=new_name)
            _restore_test(state, sandbox)
            test_run = _step_execute_test(state, sandbox)
            retried = True

        state.test_run = test_run
        _step_classify(state, sandbox, test_run, agent_result)
        outcome.success = True
        outcome.status = "reproduced"
        outcome.policy_applied = "fresh" if retried else ""
        outcome.test_path = state.test_path

    except StepFailure as exc:
        outcome.success = False
        outcome.status = str(state.verdict.get("status") or "step_failed")
        outcome.failed_step = exc.step
        outcome.reason = exc.reason
        state.trace.failed(exc.step, exc.reason, sandbox_name=state.sandbox_name)
    except (SandboxError, UncertainResultError) as exc:
        outcome.success = False
        outcome.status = "sandbox_error"
        outcome.reason = str(exc)
        outcome.failed_step = state.trace.current_step
        state.trace.failed(outcome.failed_step, str(exc), sandbox_name=state.sandbox_name)
    except KeyboardInterrupt:
        outcome.success = False
        outcome.status = "interrupted"
        outcome.reason = "operator interrupt"
        outcome.failed_step = state.trace.current_step
        state.trace.failed(outcome.failed_step, "interrupted", sandbox_name=state.sandbox_name)
    except Exception as exc:  # noqa: BLE001 - a run must never crash the harness
        outcome.success = False
        outcome.status = "harness_error"
        outcome.reason = f"{type(exc).__name__}: {exc}"
        outcome.failed_step = state.trace.current_step
        state.trace.failed(outcome.failed_step, outcome.reason, sandbox_name=state.sandbox_name)
    finally:
        _cleanup(state, outcome)

    outcome.wall_clock_s = time.monotonic() - state.started
    outcome.boot_to_ready_s = state.boot_s
    outcome.cost_usd = _estimate_cost(state)
    outcome.steps_completed = len(state.trace.completed_steps)
    outcome.sandbox_name = state.sandbox_name
    if state.agent_result:
        outcome.detail["agent_summary"] = state.agent_result.summary
        outcome.detail["agent_status"] = state.agent_result.status
    outcome.detail["ledger_errors"] = ledger.errors
    return outcome


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def _step_validate(state: _RunState) -> None:
    state.trace.started(1, sandbox_name=state.sandbox_name)
    spec = state.spec
    problems: list[str] = []
    for name in ("repo_url", "commit", "fixed_commit", "issue_text", "test_command_template",
                 "expected_signature", "subdir", "fix_dir"):
        if not isinstance(getattr(spec, name), str):
            problems.append(f"{name} must be a string")
    if problems:
        raise StepFailure(1, "; ".join(problems), retryable=False)
    if not re.fullmatch(r"https://[\w.\-:/]+", spec.repo_url):
        problems.append("repo_url must be a public HTTPS repository URL")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", spec.commit):
        problems.append("commit must be a pinned full Git SHA")
    if spec.fixed_commit and not re.fullmatch(r"[0-9a-fA-F]{40}", spec.fixed_commit):
        problems.append("fixed_commit must be a pinned full Git SHA")
    for name in ("subdir", "fix_dir"):
        path = getattr(spec, name)
        if path and (path.startswith("/") or ".." in path.split("/") or "\\" in path
                     or not re.fullmatch(r"[\w./-]+", path)):
            problems.append(f"{name} must be a safe relative repository path")
    if not spec.issue_text.strip():
        problems.append("issue_text is empty")
    if "{test}" not in spec.test_command_template:
        problems.append("test_command_template must contain {test}")
    try:
        spec.test_command_template.format(test="tests/test_regression.py")
        if spec.expected_signature:
            re.compile(spec.expected_signature)
    except (KeyError, IndexError, ValueError, re.error) as exc:
        problems.append(f"invalid command template or signature: {exc}")
    if not spec.fixed_commit and not spec.fix_dir:
        problems.append("a fixed state (fixed_commit or fix_dir) is required for the gold check")
    if problems:
        raise StepFailure(1, "; ".join(problems), retryable=False)
    state.trace.finished(1, sandbox_name=state.sandbox_name)


def _new_sandbox_name() -> str:
    from .celesto_service import make_name

    return make_name("saferepo")


def _create_and_prepare(state: _RunState, name: str | None = None) -> Sandbox:
    state.sandbox_name = name or state.sandbox_name
    state.trace.started(2, sandbox_name=state.sandbox_name)
    sandbox = Sandbox(state.sandbox_name, state.settings)
    state.sandboxes.append(sandbox)
    sandbox.create()
    sandbox_created = time.monotonic()
    state.ledger.update(
        run_id=state.run_id,
        sandbox_name=sandbox.name,
        problem_id=state.spec.problem_id,
        state="created",
        detail={
            "phase": "created",
            "provider": state.settings.celesto_provider,
            # Celesto Cloud assigns the VM id; our logical sbx- name is what
            # the ledger and audit log use.
            "cloud_id": sandbox.cloud_id,
            "create_options": sandbox.create_options,
        },
    )
    state.trace.finished(2, sandbox_name=sandbox.name)

    state.trace.started(3, sandbox_name=sandbox.name)
    boot = sandbox.wait_ready()
    state.boot_s = state.boot_s or boot
    state.trace.finished(3, sandbox_name=sandbox.name, boot_to_ready_s=round(boot, 3))
    state.ledger.update(
        run_id=state.run_id,
        sandbox_name=sandbox.name,
        problem_id=state.spec.problem_id,
        state="ready",
        detail={"boot_to_ready_s": round(boot, 3)},
    )

    state.trace.started(4, sandbox_name=sandbox.name)
    bootstrap = sandbox.run(_BOOTSTRAP, timeout_s=300)
    if not bootstrap.ok:
        raise StepFailure(
            4, f"toolchain bootstrap failed: {bootstrap.stderr[:300]}", retryable=False
        )
    clone = sandbox.run(
        f"mkdir -p /work && git clone --quiet {shlex.quote(state.spec.repo_url)} {REPO_DIR}",
        timeout_s=300,
    )
    if not clone.ok:
        raise StepFailure(
            4, f"git clone failed: {clone.stderr[:300] or clone.stdout[:300]}", retryable=False
        )
    checkout = sandbox.run(
        f"cd {REPO_DIR} && git checkout --quiet {shlex.quote(state.spec.commit)}",
        timeout_s=120,
    )
    if not checkout.ok:
        raise StepFailure(
            4,
            f"git checkout {state.spec.commit} failed: {checkout.stderr[:300]}",
            retryable=False,
        )
    # A synthetic canary, planted so the containment test has something
    # concrete to try (and fail) to exfiltrate. It is never a real credential.
    sandbox.write_file(
        "/work/canary.txt", f"SAFEREPO_CANARY={state.settings.canary}\n"
    )
    state.trace.finished(
        4,
        sandbox_name=sandbox.name,
        clone_s=round(time.monotonic() - sandbox_created, 3),
    )
    return sandbox


def _step_agent(state: _RunState, sandbox: Sandbox) -> AgentResult:
    work_dir = _work_dir(state.spec)
    state.trace.started(5, sandbox_name=sandbox.name)
    listing = sandbox.run(f"find {work_dir} -maxdepth 2 -type f | head -80")
    readme = sandbox.run(f"cat {work_dir}/README* 2>/dev/null | head -120")
    state.trace.finished(
        5,
        sandbox_name=sandbox.name,
        files=len(listing.stdout.splitlines()),
        readme_bytes=len(readme.stdout),
    )

    state.trace.started(6, sandbox_name=sandbox.name)
    state.ledger.update(
        run_id=state.run_id,
        sandbox_name=sandbox.name,
        problem_id=state.spec.problem_id,
        state="running",
        detail={"phase": "agent_running"},
    )
    agent = Agent(sandbox, settings=state.settings)
    result = agent.solve(
        repo_dir=work_dir,
        repo_url=state.spec.repo_url,
        commit=state.spec.commit,
        issue_text=state.spec.issue_text,
    )
    state.agent_result = result
    state.agent_usage = agent.usage()

    problem = _validate_test_path(result.test_path, work_dir)
    if problem:
        raise StepFailure(6, f"invalid agent test path: {problem}", retryable=False)
    state.test_path = posixpath.normpath(result.test_path)
    try:
        state.test_content = sandbox.read_file(state.test_path)
        if not state.test_content.strip():
            raise StepFailure(6, "agent test file is empty", retryable=False)
    except SandboxError as exc:
        raise StepFailure(6, f"agent test file is unreadable: {exc}", retryable=False) from exc
    state.trace.finished(
        6,
        sandbox_name=sandbox.name,
        test_path=result.test_path,
        test_bytes=len(state.test_content),
        agent_status=result.status,
    )
    return result


def _command_for(state: _RunState, test_path: str) -> str:
    return state.spec.test_command_template.format(test=shlex.quote(test_path))


def _step_execute_test(state: _RunState, sandbox: Sandbox) -> dict[str, Any]:
    state.trace.started(7, sandbox_name=sandbox.name, test_path=state.test_path)
    if state.settings.fault == "step7" and not state.fault_fired:
        state.fault_fired = True
        raise StepFailure(
            RISKY_STEP,
            "injected failure at step 7 (SAFEREPO_FAULT=step7); guest discarded",
            retryable=True,
        )

    command = f"cd {_work_dir(state.spec)} && " + _command_for(state, state.test_path)
    try:
        result = sandbox.run(command, timeout_s=300)
    except SandboxError as exc:
        raise StepFailure(RISKY_STEP, f"test execution outcome unknown: {exc}", retryable=True) from exc
    if result.blocked:
        raise StepFailure(RISKY_STEP, result.stderr, retryable=False)

    payload = {
        "command": command,
        "exit_code": result.exit_code,
        "stdout": result.stdout[:MAX_EVIDENCE_TEXT],
        "stderr": result.stderr[:MAX_EVIDENCE_TEXT],
        "duration_s": round(result.duration_s, 3),
    }
    state.trace.finished(
        7,
        sandbox_name=sandbox.name,
        exit_code=result.exit_code,
        duration_s=round(result.duration_s, 3),
    )
    return payload


def _step_classify(
    state: _RunState,
    sandbox: Sandbox,
    test_run: dict[str, Any],
    agent_result: AgentResult,
) -> None:
    state.trace.started(8, sandbox_name=sandbox.name)
    source_check = sandbox.run(
        f"cd {REPO_DIR} && git diff --exit-code {shlex.quote(state.spec.commit)} -- {shlex.quote(state.spec.subdir or '.')}",
        timeout_s=30,
    )
    if not source_check.ok:
        raise StepFailure(8, "tracked project files changed during agent execution", retryable=False)
    combined = f"{test_run['stdout']}\n{test_run['stderr']}"
    signature = state.spec.expected_signature or agent_result.expected_signature
    setup_failure = any(marker.lower() in combined.lower() for marker in _SETUP_FAILURE_MARKERS)
    signature_matched = bool(re.search(signature, combined, re.IGNORECASE)) if signature else True
    candidate_reproduced = test_run["exit_code"] != 0 and signature_matched and not setup_failure

    gold = _gold_check(state, sandbox)
    state.gold_run = gold
    gold_ok = bool(gold.get("exit_code") == 0) if gold.get("ran") else False

    if setup_failure:
        status, success = "setup_failure", False
    elif test_run["exit_code"] == 0:
        status, success = "not_reproduced", False
    elif not signature_matched:
        status, success = "signature_mismatch", False
    elif not gold_ok:
        status, success = "gold_check_failed", False
    else:
        status, success = "reproduced", True

    state.verdict = {
        "status": status,
        "success": success,
        "candidate_reproduced": candidate_reproduced,
        "signature": signature,
        "signature_matched": signature_matched,
        "setup_failure": setup_failure,
        "gold_check": gold,
        "agent_status": agent_result.status,
    }
    state.trace.finished(
        8,
        sandbox_name=sandbox.name,
        verdict=status,
        gold_check_passed=gold_ok,
    )
    if not success:
        raise StepFailure(
            8,
            f"verdict {status}: candidate_reproduced={candidate_reproduced} "
            f"gold_ok={gold_ok}",
            retryable=False,
        )


def _gold_check(state: _RunState, sandbox: Sandbox) -> dict[str, Any]:
    """Run the same test against the fixed code in a second worktree.

    A committed overlay directory (``fix_dir``) is how the fixture gallery
    defines "fixed". Real external repositories can instead supply
    ``fixed_commit``. Either way the test must pass here, which is what
    separates "the bug is reproduced" from "my setup is broken".
    """
    target_commit = state.spec.fixed_commit or state.spec.commit
    relative = _relative_to_workdir(state, state.test_path)
    if relative is None:
        return {"ran": False, "reason": "test path outside the repository"}

    add = sandbox.run(
        f"cd {REPO_DIR} && git worktree add --quiet --detach {FIXED_DIR} "
        f"{shlex.quote(target_commit)}",
        timeout_s=180,
    )
    if not add.ok:
        return {
            "ran": False,
            "reason": f"worktree add failed: {add.stderr[:300]}",
            "exit_code": None,
        }

    fixed_root = FIXED_DIR + (f"/{state.spec.subdir}" if state.spec.subdir else "")
    if state.spec.fix_dir:
        patch_path = f"{FIXED_DIR}/{state.spec.fix_dir}/."
        apply = sandbox.run(
            f"cp -a {shlex.quote(patch_path)} {shlex.quote(fixed_root)}/",
            timeout_s=120,
        )
        if not apply.ok:
            return {
                "ran": False,
                "reason": f"fix overlay failed: {apply.stderr[:300]}",
                "exit_code": None,
            }

    target = f"{fixed_root}/{relative}"
    copy = sandbox.run(
        f"mkdir -p \"$(dirname {shlex.quote(target)})\" && cp "
        f"{shlex.quote(state.test_path)} {shlex.quote(target)}"
    )
    if not copy.ok:
        return {"ran": False, "reason": f"copy failed: {copy.stderr[:300]}"}

    command = f"cd {fixed_root} && " + _command_for(state, target)
    result = sandbox.run(command, timeout_s=300)
    return {
        "ran": True,
        "command": command,
        "exit_code": result.exit_code,
        "stdout": result.stdout[:MAX_EVIDENCE_TEXT],
        "stderr": result.stderr[:MAX_EVIDENCE_TEXT],
        "duration_s": round(result.duration_s, 3),
    }


def _relative_to_workdir(state: _RunState, test_path: str) -> str | None:
    prefix = _work_dir(state.spec).rstrip("/") + "/"
    normalized = posixpath.normpath(test_path)
    if not normalized.startswith(prefix):
        return None
    return normalized[len(prefix):]


def _restore_test(state: _RunState, sandbox: Sandbox) -> None:
    if not (state.test_path and state.test_content):
        raise StepFailure(
            RISKY_STEP, "no saved test to replay on the fresh sandbox", retryable=False
        )
    sandbox.write_file(state.test_path, state.test_content)


# ---------------------------------------------------------------------------
# teardown, cost, evidence
# ---------------------------------------------------------------------------
def _retire(state: _RunState, sandbox: Sandbox) -> None:
    created = sandbox.created_monotonic or time.monotonic()
    sandbox.delete()
    if sandbox.deleted:
        state.lifetimes_s.append(max(0.0, time.monotonic() - created))
    state.trace.cleanup(
        sandbox.name,
        "deleted" if sandbox.deleted else "delete_failed",
        action="deleted_for_policy",
        delete_error=sandbox.delete_error,
    )


def _cleanup(state: _RunState, outcome: RunOutcome) -> None:
    try:
        state.trace.started(9, sandbox_name=state.sandbox_name)
        _write_evidence(state, outcome)
        state.trace.finished(9, sandbox_name=state.sandbox_name)
    except Exception as exc:
        outcome.detail["evidence_error"] = f"{type(exc).__name__}: {exc}"
        outcome.success = False
        outcome.status = "evidence_error"
        outcome.failed_step = 9
        outcome.reason = outcome.detail["evidence_error"]
        state.trace.failed(9, outcome.reason, sandbox_name=state.sandbox_name)
    state.trace.started(10, sandbox_name=state.sandbox_name)
    for sandbox in state.sandboxes:
        created = sandbox.created_monotonic or time.monotonic()
        lifetime = max(0.0, time.monotonic() - created)
        was_deleted = sandbox.deleted
        sandbox.delete()
        if not was_deleted:
            state.lifetimes_s.append(lifetime)
        final_state = "deleted" if sandbox.deleted else "failed"
        state.trace.cleanup(
            sandbox.name,
            final_state,
            delete_error=sandbox.delete_error,
        )
        state.ledger.update(
            run_id=state.run_id,
            sandbox_name=sandbox.name,
            problem_id=state.spec.problem_id,
            state=final_state,
            detail={
                "phase": "deleted" if sandbox.deleted else "delete_failed",
                "success": outcome.success,
                "status": outcome.status,
                "failed_step": outcome.failed_step,
                "policy": outcome.policy_applied,
                "delete_error": sandbox.delete_error,
            },
        )
    cleanup_ok = all(sandbox.deleted for sandbox in state.sandboxes)
    outcome.detail["cleanup_ok"] = cleanup_ok
    if not cleanup_ok:
        outcome.success = False
        outcome.status = "cleanup_failed"
        outcome.failed_step = 10
        outcome.reason = "; ".join(s.delete_error for s in state.sandboxes if not s.deleted)
        state.trace.failed(10, outcome.reason, sandbox_name=state.sandbox_name)
    else:
        state.trace.finished(10, sandbox_name=state.sandbox_name, result=outcome.status)
    try:
        _write_evidence(state, outcome)
    except Exception as exc:
        outcome.detail["evidence_error"] = f"{type(exc).__name__}: {exc}"
        outcome.success = False
        outcome.status = "evidence_error"
        outcome.failed_step = 9
        outcome.reason = outcome.detail["evidence_error"]
        state.trace.failed(9, outcome.reason, sandbox_name=state.sandbox_name)


def _estimate_cost(state: _RunState) -> float:
    lifetime = sum(state.lifetimes_s)
    rate = HOURLY_RATE_USD.get(state.settings.celesto_size, HOURLY_RATE_USD["micro"])
    return round(lifetime / 3600.0 * rate, 6)


def _write_evidence(state: _RunState, outcome: RunOutcome) -> None:
    ensure_dirs(state.settings)
    name = f"run-{state.sandbox_name}.json"
    path = state.settings.report_dir / "evidence" / name
    payload = {
        "run_id": state.run_id,
        "sandbox_name": state.sandbox_name,
        "problem_id": state.spec.problem_id,
        "repo_url": state.spec.repo_url,
        "commit": state.spec.commit,
        "fixed_commit": state.spec.fixed_commit,
        "success": outcome.success,
        "status": outcome.status,
        "reason": outcome.reason,
        "test_path": state.test_path,
        "test_content": redact(state.test_content, state.settings),
        "agent": {
            "status": state.agent_result.status if state.agent_result else "",
            "summary": state.agent_result.summary if state.agent_result else "",
            "steps": state.agent_result.steps if state.agent_result else 0,
            "commands": state.agent_result.commands if state.agent_result else [],
            "usage": state.agent_usage,
        },
        "test_run": state.test_run,
        "gold_run": state.gold_run,
        "verdict": state.verdict,
        "sandboxes": [
            {
                "name": sandbox.name,
                "cloud_id": sandbox.cloud_id,
                "create_options": sandbox.create_options,
                "deleted": sandbox.deleted,
                "delete_error": sandbox.delete_error,
                "boot_to_ready_s": (
                    round(sandbox.boot_to_ready_s, 3)
                    if sandbox.boot_to_ready_s is not None
                    else None
                ),
            }
            for sandbox in state.sandboxes
        ],
        "cleanup_ok": all(sandbox.deleted for sandbox in state.sandboxes),
        "timing": {
            "wall_clock_s": round(time.monotonic() - state.started, 3),
            "boot_to_ready_s": round(state.boot_s, 3),
        },
        "cost_usd": _estimate_cost(state),
        "written_at": utc_now(),
        "ledger_errors": state.ledger.errors,
    }
    path.write_text(redact(json.dumps(payload, indent=2), state.settings), encoding="utf-8")
    outcome.evidence_path = str(path.relative_to(state.settings.report_dir.parent))


def execute_run_record(record: dict[str, Any]) -> RunOutcome:
    """Entry point used by the FastAPI background task."""
    return execute_run(record)
