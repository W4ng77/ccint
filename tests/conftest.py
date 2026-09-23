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


@pytest.fixture
def seeded_db(clean_db):
    """播种一小组确定的帖子 + 标注，供 agent 工具层测试使用。

    工具层的测试必须在测试库上跑而不是生产库：``test_db`` 是 session 级且会把
    ``settings.db_name`` 改掉直到会话结束，所以任何依赖生产数据的断言都会随
    用例顺序时灵时不灵 —— 那是测试的缺陷，不是代码的。

    数据刻意构造成本项目的典型形态：一个高产零互动的 feed 账号 + 几个零散的
    个人账号，这样 broadcast 判据在测试里真的会命中，而不是恒为空。
    """
    import datetime as dt
    import hashlib
    from psycopg.types.json import Jsonb

    from ccint.portal import queries as pq

    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    # feed 账号：8 天各 2 帖，零互动 —— 满足 n_posts>=5, n_days>=4, zero_rate>=0.6
    for d in range(8):
        for k in range(2):
            rows.append(("feed.example", f"feed-{d}-{k}", "author:feed",
                         now - dt.timedelta(days=d + 1, hours=k),
                         f"Ransomware group listed a victim. CVE-2026-{1000+d:04d}.",
                         0, "ransomware"))
    # 个人账号：各 1 帖，有互动
    for i in range(5):
        rows.append(("bluesky", f"human-{i}", f"author:human{i}",
                     now - dt.timedelta(days=i + 1),
                     "Discussion of a data breach at a local organisation.",
                     7, "data_breach"))

    with clean_db.connect(autocommit=False) as conn, conn.cursor() as cur:
        for src, sid, aid, ts, text, likes, topic in rows:
            cur.execute("""
                INSERT INTO posts (source_key, source_post_id, author_id, author_handle,
                                   published_at, collected_at, text, text_hash,
                                   like_count, raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING post_id""",
                (src, sid, aid, aid.split(":")[1], ts, ts + dt.timedelta(days=3),
                 text, hashlib.sha1(text.encode()).hexdigest(), likes, Jsonb({})))
            pid = cur.fetchone()["post_id"]
            for ver, tk in ((pq.LABEL_VERSION, None), (pq.TOPIC_VERSION, topic)):
                cur.execute("""
                    INSERT INTO post_labels (post_id, label_version, is_cyber,
                        is_canada, is_relevant, topic_key, is_low_information,
                        matched_terms, confidence)
                    VALUES (%s,%s,true,true,true,%s,false,%s,null)""",
                    (pid, ver, tk, Jsonb({})))
        conn.commit()
    return clean_db
