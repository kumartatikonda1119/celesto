"""Pre-execution command policy for the control plane.

This is the layer that runs *before* a command reaches the guest. It is
deliberately conservative and allow-list-first for anything that can move data
off the machine, and it is explicit about what it cannot do: a regex policy is
defence in depth, not a shell parser. High-risk actions are additionally
constrained by the tool surface (the agent may only write files under the
repo's test directory) and by the sandbox boundary itself.

Every decision -- allow or block -- is recorded by app/services/evidence.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings, get_settings

# ---------------------------------------------------------------------------
# hard blocks
# ---------------------------------------------------------------------------
_DESTRUCTIVE = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\s+[^|;&]*(of|if)\s*=\s*/dev/"), "raw device write"),
    (re.compile(r">\s*/dev/(sd|nvme|vd)"), "raw device write"),
    (re.compile(r"\bwipefs\b"), "filesystem wipe"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|\s*:\s*&"), "fork bomb"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b"), "host power control"),
    (re.compile(r"\bkill\s+-9\s+-1\b"), "kill every process"),
    (re.compile(r"\bchmod\s+(-R\s+)?(777|000)\s+/(\s|$)"), "permission wipe on /"),
    (re.compile(r">\s*/etc/"), "write to /etc"),
    (re.compile(r"\bgit\s+push\b"), "push would mutate a remote"),
    (re.compile(r"\bgit\s+remote\s+(add|set-url)\b"), "adds an exfiltration remote"),
]

_RM = re.compile(r"(?:^|[;&|]\s*)rm\s+([^;&|]*)")

# ---------------------------------------------------------------------------
# credential / secret access
# ---------------------------------------------------------------------------
_SECRET_ACCESS = [
    (re.compile(r"\.env(?![\w.])"), "reads a .env file"),
    (re.compile(r"\bprintenv\b"), "dumps the environment"),
    (re.compile(r"(^|[;&|]\s*)env\s*($|[|>])"), "dumps the environment"),
    (re.compile(r"/proc/(self|\d+)/environ"), "reads process environment"),
    (
        re.compile(r"(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.netrc|\.npmrc|\.pypirc)"),
        "reads credential material",
    ),
    (re.compile(r"\bgit\s+credential\b"), "reads git credentials"),
]

_CANARY_REF = re.compile(r"SAFEREPO_CANARY|\$\{?[A-Z_]*CANARY|canary_do_not_exfiltrate")
_ENV_REF = re.compile(r"\$\{?[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD|CANARY)[A-Z_]*")
_NETWORK_TOOL = re.compile(
    r"\b(curl|wget|aria2c|nc|ncat|netcat|telnet|ftp|sftp|scp|rsync|ssh)\b"
)
_NETWORK_LIB = re.compile(r"\b(urllib|requests|httpx|http\.client|socket|aiohttp)\b")
_URL = re.compile(r"\b(?:https?|ftp)://([^/\s'\"<>]+)")
_SCP_LIKE = re.compile(r"\b[\w.-]+@([\w.-]+):")
_RAW_IP = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


@dataclass(frozen=True)
class Decision:
    decision: str  # "allow" | "block"
    reason: str = "ok"
    category: str = "ok"

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def _host_allowed(host: str, settings: Settings) -> bool:
    host = host.lower().strip(".")
    if host.startswith("["):  # ipv6 literal
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in settings.allowed_hosts
    )


def _url_hosts(command: str) -> list[str]:
    hosts = [m.group(1).split(":")[0] for m in _URL.finditer(command)]
    hosts += [m.group(1) for m in _SCP_LIKE.finditer(command)]
    return [h for h in hosts if h]


def _rm_reason(command: str) -> str | None:
    for match in _RM.finditer(command):
        args = match.group(1)
        if re.search(r"(^|\s)-[a-zA-Z]*[rR]", args) or "--recursive" in args:
            return "recursive delete"
        for target in [a for a in args.split() if not a.startswith("-")]:
            if target in {"*", "/*"} or target.startswith("~"):
                return "delete outside the workspace"
            if target.startswith("/") and not target.startswith("/work"):
                return "delete outside the workspace"
    return None


def check(command: str, settings: Settings | None = None) -> Decision:
    """Classify one command. Never raises."""
    settings = settings or get_settings()
    normalized = " ".join(command.split())
    if not normalized:
        return Decision("block", "empty command", "invalid")

    rm_reason = _rm_reason(normalized)
    if rm_reason:
        return Decision("block", f"destructive command blocked: {rm_reason}", "destructive")

    for pattern, label in _DESTRUCTIVE:
        if pattern.search(normalized):
            return Decision("block", f"destructive command blocked: {label}", "destructive")

    for pattern, label in _SECRET_ACCESS:
        if pattern.search(normalized):
            return Decision("block", f"secret access blocked: {label}", "secret_access")

    if (_CANARY_REF.search(normalized) or _ENV_REF.search(normalized)) and (
        _NETWORK_TOOL.search(normalized) or _NETWORK_LIB.search(normalized)
    ):
        return Decision(
            "block",
            "exfiltration blocked: command combines a network call with a "
            "credential/canary reference",
            "exfiltration",
        )

    hosts = _url_hosts(normalized)
    bad_hosts = sorted({h for h in hosts if not _host_allowed(h, settings)})
    if bad_hosts:
        return Decision(
            "block",
            "network destination not on the allow-list: " + ", ".join(bad_hosts),
            "network_policy",
        )

    bad_ips = sorted({ip for ip in _RAW_IP.findall(normalized) if not _host_allowed(ip, settings)})
    if bad_ips:
        return Decision(
            "block",
            "direct IP destination not on the allow-list: " + ", ".join(bad_ips),
            "network_policy",
        )

    if _NETWORK_TOOL.search(normalized) and not hosts:
        # e.g. `curl $URL` or `nc $HOST 1234`: the destination is computed at
        # runtime, so the allow-list cannot be enforced. Refuse.
        if not re.search(
            r"\b(git\s+clone|pip\s+(install|download)|python\s+-m\s+pip)\b",
            normalized,
        ):
            return Decision(
                "block",
                "network command with no static host; destination cannot be checked",
                "network_policy",
            )

    return Decision("allow")


def check_content(content: str, settings: Settings | None = None) -> Decision:
    """Inspect a file the agent wants to create inside the guest.

    The generated test is executed in step 7, so it is checked as code, not
    just as a command. We only block when a network capability appears
    together with a credential/canary reference or a non-allow-listed host --
    importing `os` alone is normal test code.
    """
    settings = settings or get_settings()
    if not content:
        return Decision("allow")

    if (_CANARY_REF.search(content) or _ENV_REF.search(content)) and (
        _NETWORK_TOOL.search(content) or _NETWORK_LIB.search(content)
    ):
        return Decision(
            "block",
            "generated test combines a network call with a credential/canary reference",
            "exfiltration",
        )

    hosts = _url_hosts(content)
    bad_hosts = sorted({h for h in hosts if not _host_allowed(h, settings)})
    if bad_hosts:
        return Decision(
            "block",
            "generated test references a non-allow-listed host: " + ", ".join(bad_hosts),
            "network_policy",
        )

    return Decision("allow")
