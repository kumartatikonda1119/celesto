"""Grade the submission's evidence files with the same rules the validator uses.

The point of this test is that ``python scripts/validate_reports.py`` and
``pytest tests/test_reports.py`` cannot disagree: both call ``validate_all()``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_reports.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("saferepo_validator", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reports_meet_the_submission_contract():
    validator = _load_validator()
    errors = validator.validate_all()
    assert not errors, "submission problems:\n" + "\n".join(f"  - {error}" for error in errors)
