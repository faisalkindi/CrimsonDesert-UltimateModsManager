import pytest
from pathlib import Path

from cdumm.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Provide an initialized in-memory-like database for tests."""
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()
