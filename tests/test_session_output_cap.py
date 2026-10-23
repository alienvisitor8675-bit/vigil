"""The session output cap in configs/budgets.yaml (the arithmetic is in
tests/gateway/test_first_call.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vigild.config import Budgets, Config


def test_the_cap_is_configured_and_validated(cfg: Config) -> None:
    assert cfg.budgets.session_max_output_tokens == 16384
    raw = cfg.budgets.model_dump()
    for bad in (0, 1023, -1):
        with pytest.raises(ValidationError):
            Budgets(**{**raw, "session_max_output_tokens": bad})
    assert Budgets(**{**raw, "session_max_output_tokens": 1024}).session_max_output_tokens == 1024
