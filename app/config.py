"""Configuration for SafeRepo.

Every value comes from the environment. Nothing here imports the Celesto SDK,
so the control plane starts healthy even when no Celesto credentials and no
LLM key are present (see the README's setup section).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:  # dotenv is optional in the container, where env vars come from compose
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parents[1]


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # execution layer
    celesto_provider: str
    celesto_api_key: str
    celesto_backend: str
    celesto_template: str
    celesto_size: str
    celesto_size_id: str

    # reasoning layer
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    # control plane
    database_url: str
    api_port: int
    api_base_url: str

    # budgets
    command_timeout_s: int
    max_steps: int
    run_budget_s: int
    boot_timeout_s: int

    # paths
    report_dir: Path
    log_dir: Path

    # safety
    network_mode: str
    allowed_hosts: tuple[str, ...]
    canary: str
    fault: str

    @property
    def has_celesto(self) -> bool:
        return bool(self.celesto_api_key and not self.celesto_api_key.lower().startswith("replace-"))

    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key and self.llm_model
                    and not self.llm_api_key.lower().startswith("replace-")
                    and not self.llm_model.lower().startswith("replace-"))

    @property
    def commands_log(self) -> Path:
        return self.log_dir / "commands.jsonl"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        celesto_provider=_get("CELESTO_PROVIDER", "cloud"),
        celesto_api_key=_get("CELESTO_API_KEY"),
        celesto_backend=_get("CELESTO_BACKEND", "firecracker"),
        celesto_template=_get("CELESTO_TEMPLATE", "scratch"),
        celesto_size=_get("CELESTO_SIZE", "micro"),
        celesto_size_id=_get("CELESTO_SIZE_ID"),
        llm_base_url=_get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        llm_api_key=_get("LLM_API_KEY"),
        llm_model=_get("LLM_MODEL"),
        database_url=_get(
            "DATABASE_URL", "postgresql://ledger:ledger@ledger:5432/ledger"
        ),
        api_port=_get_int("API_PORT", 8080),
        api_base_url=_get("API_BASE_URL", "http://localhost:8080"),
        command_timeout_s=_get_int("SAFEREPO_COMMAND_TIMEOUT_S", 120),
        max_steps=_get_int("SAFEREPO_MAX_STEPS", 15),
        run_budget_s=_get_int("SAFEREPO_RUN_BUDGET_S", 720),
        boot_timeout_s=_get_int("SAFEREPO_BOOT_TIMEOUT_S", 180),
        report_dir=Path(_get("SAFEREPO_REPORT_DIR", str(BASE_DIR / "reports"))),
        log_dir=Path(_get("SAFEREPO_LOG_DIR", str(BASE_DIR / "logs"))),
        network_mode=_get("SAFEREPO_NETWORK_MODE", "open"),
        allowed_hosts=tuple(
            h.strip().lower()
            for h in _get(
                "SAFEREPO_ALLOWED_HOSTS",
                "github.com,codeload.github.com,pypi.org,files.pythonhosted.org",
            ).split(",")
            if h.strip()
        ),
        canary=_get("SAFEREPO_CANARY", "canary_do_not_exfiltrate_4f1c"),
        fault=_get("SAFEREPO_FAULT", ""),
    )
