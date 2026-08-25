from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def page1_statuses() -> list[dict]:
    return json.loads((FIXTURES_DIR / "statuses_page1.json").read_text())


@pytest.fixture
def page2_statuses() -> list[dict]:
    return json.loads((FIXTURES_DIR / "statuses_page2.json").read_text())


@pytest.fixture
def all_statuses(page1_statuses: list[dict], page2_statuses: list[dict]) -> list[dict]:
    return page1_statuses + page2_statuses
