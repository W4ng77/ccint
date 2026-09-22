import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TEST_DB = "ccint_test"


@pytest.fixture(scope="session")
def test_db():
    """独立测试库，与业务库 ccint 隔离。session 级建库 + migrate。"""
    from ccint import db
    from ccint.config import settings

    original = settings.db_name
    settings.db_name = TEST_DB
    db.ensure_database()
    db.migrate()
    yield db
    settings.db_name = original


@pytest.fixture
def clean_db(test_db):
    """每个用例前清表。不做破坏性删除原则针对生产数据，测试库不适用。"""
    with test_db.connect(autocommit=True) as conn:
        conn.execute(
            "TRUNCATE post_labels, posts, collection_runs, analysis_runs "
            "RESTART IDENTITY CASCADE"
        )
    return test_db
