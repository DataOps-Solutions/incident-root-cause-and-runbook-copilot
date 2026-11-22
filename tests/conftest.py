"""Shared fixtures: sample dataset, runbook index and an in-memory database."""

import sqlite3
from pathlib import Path

import pytest

from src.correlator import connect, ingest
from src.models import Dataset
from src.runbook_retriever import RunbookIndex

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "data" / "sample_signals.json"
RUNBOOKS_DIR = ROOT / "data" / "runbooks"


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return Dataset.model_validate_json(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def db(tmp_path: Path, dataset: Dataset) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    ingest(conn, dataset)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def runbook_index() -> RunbookIndex:
    return RunbookIndex.from_directory(RUNBOOKS_DIR)
