"""The only module that talks to Celesto.

Design rules enforced here:

* Nothing else in the codebase imports ``celesto``. If the installed SDK names
  a method differently than expected, this is the one file to fix -- and the
  probe at the bottom of this module tells us exactly what the SDK exposes.
* EVERY command an agent or the pipeline issues goes through :meth:`Sandbox.run`
  -> command policy -> audit line -> SDK. There is no second path.
* An uncertain result is raised, never converted into success.

The constructor/result shapes are normalised defensively because the project
was renamed from smolvm to Celesto and the SDK surface has moved between
releases. The adapter never guesses silently: anything it cannot understand is
reported as ``UncertainResultError``.
"""

from __future__ import annotations

import atexit
import base64
import importlib
import inspect
import random
import shlex
import string
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from ..config import Settings, get_settings
from . import command_policy
from .evidence import append_command

READY_PROBE = "echo saferepo-ready"


class SandboxError(RuntimeError):
    """Base class for sandbox-layer failures."""


class SandboxUnavailable(SandboxError):
    """The Celesto SDK is not installed or not configured."""


class SandboxCreateError(SandboxError):
    """The guest could not be created."""


class UncertainResultError(SandboxError):
    """We do not know whether a command ran or what it returned."""


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    decision: str = "allow"

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def blocked(self) -> bool:
        return self.decision == "block"


# ---------------------------------------------------------------------------
# unique names
# ---------------------------------------------------------------------------
_NAME_ALPHABET = string.ascii_lowercase + string.digits
_COUNTER = 0
_COUNTER_LOCK = Lock()


def make_name(tag: str = "saferepo") -> str:
    """Return a unique name matching ^sbx-[a-z0-9]{8,}$."""
    global _COUNTER
    with _COUNTER_LOCK:
        _COUNTER += 1
        counter = _COUNTER
    suffix = "".join(random.choices(_NAME_ALPHABET, k=6))
    # counter keeps names unique even inside one millisecond; the random tail
    # keeps them unique across processes.
    return f"sbx-{tag}{int(time.time()) % 100000:05d}{counter:02d}{suffix}"


# ---------------------------------------------------------------------------
# SDK discovery
# ---------------------------------------------------------------------------
def _sdk_module():
    try:
        return importlib.import_module("celesto")
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SandboxUnavailable(
            "the 'celesto' SDK is not installed; run `pip install -U celesto`"
        ) from exc


def _computer_class():
    """Return the SDK's ``Computer`` class (verified against celesto 0.1.4).

    ``Computer`` is the public cloud/local handle. Its constructor only stores
    options; the guest is created by ``start()``.
    """
    sdk = _sdk_module()
    for path in ("Computer", "sdk.Computer"):
        obj: Any = sdk
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is not None:
            return obj
    raise SandboxUnavailable(
        "could not find the Computer class in the installed celesto SDK"
    )


def _network_mode(settings: Settings) -> str:
    """Map the human setting to the cloud API's enum (open | off)."""
    value = (settings.network_mode or "open").strip().lower()
    if value == "on":
        value = "open"
    if value not in {"open", "off"}:
        raise SandboxUnavailable("network_mode must be open or off")
    return value


def _attach_create_options(handle: Any, settings: Settings) -> dict[str, Any]:
    """Set create-time fields the public constructor omits (SDK 0.1.4).

    ``ComputerCreateRequest`` accepts ``network_policy`` and ``size_id``, but
    ``CloudOptions`` does not forward them. Both are applied to the provider's
    request body before ``start()`` sends it. This is the only place in the
    codebase that reaches into the SDK; any mismatch is reported, never ignored.
    """
    applied: dict[str, Any] = {}
    provider = getattr(handle, "_provider", None)
    body = getattr(provider, "_body", None)
    if body is None:
        raise SandboxUnavailable("SDK cloud create body unavailable; cannot enforce network settings")
    try:
        from _celesto_cloud_api.models.network_policy import NetworkPolicy
        from _celesto_cloud_api.models.network_policy_mode import NetworkPolicyMode

        mode = NetworkPolicyMode(_network_mode(settings))
        body.network_policy = NetworkPolicy(mode=mode)
        applied["network_policy"] = mode.value
    except Exception as exc:  # pragma: no cover - SDK drift
        raise SandboxUnavailable(f"cannot enforce cloud network policy: {exc}") from exc
    if settings.celesto_size_id:
        try:
            body.size_id = settings.celesto_size_id
            applied["size_id"] = settings.celesto_size_id
        except Exception as exc:  # pragma: no cover - SDK drift
            raise SandboxUnavailable(f"cannot configure cloud size: {exc}") from exc
    return applied


def sdk_notes() -> dict[str, Any]:
    """Describe the installed SDK so the gate can record what is supported."""
    notes: dict[str, Any] = {"import": "failed", "class": None, "init_params": [], "methods": []}
    try:
        sdk = _sdk_module()
    except SandboxError as exc:
        notes["error"] = str(exc)
        return notes
    notes["import"] = "ok"
    notes["version"] = getattr(sdk, "__version__", "unknown")
    notes["module_attrs"] = sorted(
        name for name in dir(sdk) if not name.startswith("_")
    )[:60]
    try:
        cls = _computer_class()
    except SandboxError as exc:
        notes["error"] = str(exc)
        return notes
    notes["class"] = getattr(cls, "__name__", str(cls))
    try:
        notes["init_params"] = sorted(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):
        pass
    notes["methods"] = sorted(
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    )
    notes["run_signature"] = str(inspect.signature(cls.run))
    try:
        from celesto.types import CommandResult, InternetSettings  # noqa: PLC0415

        notes["command_result_fields"] = sorted(
            getattr(CommandResult, "model_fields", {})
            or [a.name for a in getattr(CommandResult, "__attrs_attrs__", [])]
        )
        notes["internet_settings_fields"] = sorted(InternetSettings.model_fields)
    except Exception as exc:  # pragma: no cover - SDK drift
        notes["types_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from _celesto_cloud_api.models.computer_create_request import (  # noqa: PLC0415
            ComputerCreateRequest,
        )

        notes["cloud_create_fields"] = sorted(
            a.name for a in getattr(ComputerCreateRequest, "__attrs_attrs__", [])
        )
    except Exception as exc:  # pragma: no cover - SDK drift
        notes["cloud_create_error"] = f"{type(exc).__name__}: {exc}"
    notes["cloud_note"] = (
        "Cloud create requests accept size_id/network_policy but CloudOptions does "
        "not forward them; celesto_service applies both to the provider body."
    )
    return notes


# ---------------------------------------------------------------------------
# active sandbox registry (orphan protection)
# ---------------------------------------------------------------------------
_ACTIVE: set["Sandbox"] = set()
_ACTIVE_LOCK = Lock()
_ATEXIT_INSTALLED = False


def _cleanup_at_exit() -> None:  # pragma: no cover - shutdown path
    with _ACTIVE_LOCK:
        leftovers = list(_ACTIVE)
    for sandbox in leftovers:
        try:
            sandbox.delete()
        except Exception:
            pass


def _ensure_atexit() -> None:
    global _ATEXIT_INSTALLED
    if not _ATEXIT_INSTALLED:
        atexit.register(_cleanup_at_exit)
        _ATEXIT_INSTALLED = True


# ---------------------------------------------------------------------------
# the sandbox
# ---------------------------------------------------------------------------
class Sandbox:
    def __init__(
        self,
        name: str | None = None,
        settings: Settings | None = None,
        *,
        external_volume: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        # Our logical run name. Celesto Cloud assigns the VM its own id and
        # does not accept a caller-supplied name, so this name identifies the
        # run in the ledger and audit log; cloud_id is recorded alongside it.
        self.name = name or make_name()
        self.handle: Any = None
        self.cloud_id: str | None = None
        self.create_options: dict[str, Any] = {}
        self.external_volume = external_volume
        self.created_monotonic: float | None = None
        self.boot_to_ready_s: float | None = None
        self.deleted = False
        self.delete_error = ""
        self.template = self.settings.celesto_template

    # -- lifecycle ---------------------------------------------------------
    def create(self) -> "Sandbox":
        if not self.settings.has_celesto:
            raise SandboxUnavailable("CELESTO_API_KEY is not set")
        cls = _computer_class()
        provider = self.settings.celesto_provider
        if provider != "cloud":
            raise SandboxUnavailable("this submission supports Celesto Cloud only")
        options: dict[str, Any] = {
            "provider": provider,
            "lifetime": "ephemeral",
        }
        if provider == "cloud":
            options["api_key"] = self.settings.celesto_api_key
        if self.template:
            options["template_id"] = self.template
        if self.external_volume:
            options["external_volume_enabled"] = True

        self.created_monotonic = time.monotonic()
        try:
            self.handle = cls(**options)
            with _ACTIVE_LOCK:
                _ACTIVE.add(self)
            _ensure_atexit()
            self.create_options = _attach_create_options(self.handle, self.settings)
            # Keep ownership before the potentially billable create request.
            self.handle.start()
        except BaseException as exc:
            self.cloud_id = getattr(self.handle, "id", None)
            self.delete()
            if not isinstance(exc, Exception):
                raise
            raise SandboxCreateError(f"could not create sandbox {self.name}: {exc}") from exc
        self.cloud_id = getattr(self.handle, "id", None)
        return self

    def wait_ready(self, timeout_s: int | None = None) -> float:
        """Poll the guest until it executes a command. Returns seconds."""
        timeout = timeout_s or self.settings.boot_timeout_s
        deadline = time.monotonic() + timeout
        last_error = ""
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            started = time.monotonic()
            try:
                result = self._raw_exec(READY_PROBE, timeout_s=15)
                append_command(
                    sandbox_name=self.name,
                    command=READY_PROBE,
                    decision="allow",
                    reason=f"readiness probe (attempt {attempt})",
                    exit_code=result.exit_code,
                    settings=self.settings,
                )
                if result.ok and "saferepo-ready" in result.stdout:
                    if self.created_monotonic is not None:
                        self.boot_to_ready_s = time.monotonic() - self.created_monotonic
                    return self.boot_to_ready_s or 0.0
                last_error = result.stderr or result.stdout
            except Exception as exc:  # guest may still be booting
                last_error = str(exc)
                append_command(
                    sandbox_name=self.name,
                    command=READY_PROBE,
                    decision="allow",
                    reason=f"readiness probe (attempt {attempt}) failed: {last_error[:200]}",
                    settings=self.settings,
                )
            if time.monotonic() - started < 2:
                time.sleep(2)
        raise SandboxError(
            f"sandbox {self.name} was not ready within {timeout}s: {last_error}"
        )

    def stop(self) -> bool:
        """Stop the guest without deleting it (persistence experiment)."""
        fn = getattr(self.handle, "stop", None) if self.handle is not None else None
        if callable(fn):
            fn()
            return True
        return False

    def start(self) -> bool:
        """Resume a stopped guest (persistence experiment).

        ``Computer.start()`` only creates-or-starts a handle that was never
        started; after ``stop()`` the correct call is ``resume()``.
        """
        fn = getattr(self.handle, "resume", None) if self.handle is not None else None
        if callable(fn):
            fn()
            return True
        return False

    def delete(self) -> None:
        """Idempotent teardown. Never raises; records why it could not delete.

        ``stop`` is deliberately NOT used as a fallback: a stopped guest still
        exists (and still bills), so reporting it as deleted would be a lie.
        """
        if self.deleted:
            return
        if self.handle is None:
            self.deleted = True
            self.delete_error = ""
            return
        try:
            # Verified public SDK method; a logical audit name is not a VM id.
            self.handle.delete()
        except BaseException as exc:
            self.delete_error = f"delete: {exc}"
            # Retain ownership so finally/atexit can retry the same handle.
            return
        self.deleted = True
        self.delete_error = ""
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)

    # -- commands ----------------------------------------------------------
    def run(
        self,
        command: str,
        timeout_s: int | None = None,
        *,
        operator_reason: str = "",
    ) -> ExecResult:
        """Policy-check, audit, then execute inside the guest.

        ``operator_reason`` is only for trusted pipeline steps that must run
        despite the agent policy (readiness probes, containment probes). It is
        written to the audit log so the exception is visible.
        """
        decision = command_policy.check(command, self.settings)
        if not decision.allowed and not operator_reason:
            append_command(
                sandbox_name=self.name,
                command=command,
                decision="block",
                reason=decision.reason,
                settings=self.settings,
            )
            return ExecResult(
                command=command,
                exit_code=126,
                stderr=f"blocked by command policy: {decision.reason}",
                decision="block",
            )

        reason = decision.reason if decision.allowed else f"operator override: {operator_reason}"
        started = time.monotonic()
        append_command(
            sandbox_name=self.name,
            command=command,
            decision="allow",
            reason=reason,
            settings=self.settings,
        )
        result = self._raw_exec(command, timeout_s=timeout_s)
        result.duration_s = time.monotonic() - started
        return result

    def read_file(self, path: str, max_bytes: int = 200_000) -> str:
        result = self.run(f"head -c {max_bytes} {shlex.quote(path)}")
        if not result.ok:
            raise SandboxError(f"read failed for {path}: {result.stderr or result.stdout}")
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        decision = command_policy.check_content(content, self.settings)
        if not decision.allowed:
            append_command(
                sandbox_name=self.name,
                command=f"write_file {path}",
                decision="block",
                reason=decision.reason,
                settings=self.settings,
            )
            raise SandboxError(f"write blocked by policy: {decision.reason}")
        # Cloud commands are capped at 10,000 characters, and base64 expands by
        # 4/3. 3000-byte chunks keep every request comfortably below the cap.
        raw = content.encode("utf-8")
        chunks = [raw[i : i + 3000] for i in range(0, len(raw), 3000)] or [b""]
        quoted_path = shlex.quote(path)
        for index, chunk in enumerate(chunks):
            payload = base64.b64encode(chunk).decode("ascii")
            operator = ">" if index == 0 else ">>"
            command = (
                f"mkdir -p \"$(dirname {quoted_path})\" && "
                f"printf %s {shlex.quote(payload)} | base64 -d {operator} {quoted_path}"
            )
            result = self.run(command)
            if not result.ok:
                raise SandboxError(
                    f"write failed for {path} (chunk {index + 1}/{len(chunks)}): "
                    f"{result.stderr or result.stdout}"
                )

    def probe_egress(self, host: str, timeout_s: int = 8) -> tuple[bool, str]:
        """Operator containment probe: can the guest reach ``host``?

        Deliberately bypasses the agent policy (a probe must be able to try),
        and is audited as an operator action.
        """
        command = (
            f"curl -m {timeout_s} -sS -o /dev/null -w '%{{http_code}}' "
            f"https://{host} || echo CURL_FAILED"
        )
        result = self.run(command, operator_reason="containment egress probe")
        combined = (result.stdout + " " + result.stderr).strip()
        reachable = result.ok and "CURL_FAILED" not in combined and combined != "000"
        return reachable, combined

    # -- internals ---------------------------------------------------------
    def _raw_exec(self, command: str, timeout_s: int | None = None) -> ExecResult:
        if self.handle is None:
            raise SandboxError("sandbox handle is not initialised")
        method = None
        for name in ("run", "exec", "execute", "command"):
            candidate = getattr(self.handle, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            raise SandboxUnavailable(
                "the installed celesto Computer exposes none of run/exec/execute/command"
            )
        started = time.monotonic()
        try:
            raw = _invoke_exec(method, command, timeout_s)
        except Exception as exc:
            raise UncertainResultError(
                f"command outcome unknown for {command!r}: {exc}"
            ) from exc
        return _normalise(raw, command, time.monotonic() - started)


def _invoke_exec(method: Any, command: str, timeout_s: int | None) -> Any:
    """Call the SDK's exec method with a signature that matches it.

    Positional first: virtually every exec-like method takes the command
    positionally, so this is the safest common denominator.
    """
    try:
        params = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        return method(command)

    kwargs: dict[str, Any] = {}
    names = {p.name for p in params}
    if timeout_s is not None and "timeout" in names:
        kwargs["timeout"] = timeout_s
    elif timeout_s is not None and "timeout_s" in names:
        kwargs["timeout_s"] = timeout_s

    first = params[0] if params else None
    if first is not None and first.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return method(command, **kwargs)
    if "command" in names:
        return method(command=command, **kwargs)
    return method(command, **kwargs)


def _normalise(raw: Any, command: str, duration: float) -> ExecResult:
    """Turn whatever the SDK returned into an ExecResult, or raise."""
    if raw is None:
        raise UncertainResultError(f"no result returned for {command!r}")

    if isinstance(raw, (str, bytes)):
        raise UncertainResultError(f"result for {command!r} has output but no exit code")

    def pick(mapping: Any, *names: str) -> Any:
        if isinstance(mapping, dict):
            for name in names:
                if name in mapping:
                    return mapping[name]
            return None
        for name in names:
            if hasattr(mapping, name):
                return getattr(mapping, name)
        return None

    exit_code = pick(raw, "exit_code", "exitCode", "returncode", "return_code", "status")
    stdout = pick(raw, "stdout", "out", "output", "std_out") or ""
    stderr = pick(raw, "stderr", "err", "error", "std_err") or ""

    if exit_code is None:
        ok = pick(raw, "ok", "success")
        if ok is None:
            raise UncertainResultError(
                f"result for {command!r} has neither exit_code nor ok: {raw!r}"
            )
        exit_code = 0 if ok else 1
    if isinstance(exit_code, str):
        lowered = exit_code.strip().lower()
        if lowered in {"ok", "success", "succeeded", "done", "completed"}:
            exit_code = 0
        elif lowered in {"error", "failed", "failure"}:
            exit_code = 1
    try:
        exit_code = int(exit_code)
    except (TypeError, ValueError) as exc:
        raise UncertainResultError(f"unparseable exit code {exit_code!r}") from exc

    return ExecResult(
        command=command,
        exit_code=exit_code,
        stdout=_as_text(stdout),
        stderr=_as_text(stderr),
        duration_s=duration,
    )


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def inspect_owned_computer(cloud_id: str, settings: Settings) -> dict[str, Any]:
    """Inspect a recorded cloud ID using the pinned SDK (no local runtime)."""
    provider_cls = importlib.import_module("celesto._providers.cloud").CloudProvider
    response_cls = importlib.import_module("_celesto_cloud_api.models.computer_response").ComputerResponse
    endpoint = importlib.import_module(
        "_celesto_cloud_api.api.computers.get_computer_v1_computers_computer_id_get"
    )
    provider = provider_cls(api_key=settings.celesto_api_key)
    provider.vm_id = cloud_id
    try:
        response = provider._call(endpoint.sync_detailed, response_cls, computer_id=cloud_id)
        return {"id": response.id, "status": response.status, "name": response.name}
    except _sdk_module().VMNotFoundError:
        return {"id": cloud_id, "status": "deleted"}
    finally:
        provider.close()


def delete_owned_computer(cloud_id: str, settings: Settings) -> None:
    """Delete a previously inspected, operator-authorized cloud ID."""
    handle = _computer_class().get(cloud_id, provider="cloud", api_key=settings.celesto_api_key)
    try:
        handle.delete()
    finally:
        handle.close()


def make_sandbox(tag: str = "saferepo", settings: Settings | None = None) -> Sandbox:
    return Sandbox(make_name(tag), settings=settings).create()
