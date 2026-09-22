"""评估底座单测。"""
from pathlib import Path

import pytest

from ccint.evaluation import (STRATA, export_tsv, load_tsv, score, stratum_sizes,
                              wilson)


def test_wilson_bounds():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(9, 10)
    assert 0 < lo < 0.9 < hi <= 1.0
    # 样本越小区间越宽 —— 这正是它存在的理由
    w_small = wilson(1, 2); w_big = wilson(50, 100)
    assert (w_small[1] - w_small[0]) > (w_big[1] - w_big[0])


def test_strata_partition_the_corpus():
    """四象限必须互斥且穷尽，否则加权还原会错。"""
    assert set(STRATA) == {"relevant", "cyber_only", "canada_only", "neither"}


def test_export_import_roundtrip(tmp_path):
    from datetime import datetime, timezone
    rows = [{"post_id": 1, "stratum": "relevant", "is_cyber": True, "is_canada": True,
             "topic_key": "ransomware", "published_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
             "lang": "en", "text": "a\tb\nc"}]
    p = export_tsv(rows, tmp_path / "s.tsv")
    assert load_tsv(p) == [], "TRUE_* 未填写的行必须被跳过"

    txt = p.read_text(encoding="utf-8").replace("\t\t\t\t", "\ty\ty\t\t", 1)
    # 直接构造一份已标注的文件更可靠
    lines = p.read_text(encoding="utf-8").splitlines()
    hdr = lines[0].split("\t")
    cells = lines[1].split("\t")
    cells[hdr.index("TRUE_cyber")] = "y"
    cells[hdr.index("TRUE_canada")] = "n"
    p.write_text("\t".join(hdr) + "\n" + "\t".join(cells) + "\n", encoding="utf-8")

    got = load_tsv(p)
    assert len(got) == 1
    assert got[0]["true_cyber"] is True and got[0]["true_canada"] is False


def test_tab_and_newline_in_text_do_not_break_tsv(tmp_path):
    from datetime import datetime, timezone
    rows = [{"post_id": 7, "stratum": "neither", "is_cyber": False, "is_canada": False,
             "topic_key": None, "published_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
             "lang": None, "text": "line1\nline2\tcol2"}]
    p = export_tsv(rows, tmp_path / "s.tsv")
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2, "文本里的换行必须被折叠"


def test_score_requires_truth():
    with pytest.raises(ValueError, match="标注集为空"):
        score([], "rules_v1")


def test_stratum_sizes_sum_to_corpus(clean_db):
    with clean_db.connect(autocommit=True) as c:
        c.execute("""INSERT INTO posts (source_key, source_post_id, author_id,
                     published_at, text, text_hash, raw_payload)
                     SELECT 'test','p'||g,'did:'||g, now(), 'x','h','{}'
                     FROM generate_series(1,8) g""")
        c.execute("""INSERT INTO post_labels (post_id,label_version,is_cyber,is_canada,is_relevant)
                     SELECT post_id,'tv', post_id%2=0, post_id%3=0,
                            (post_id%2=0 AND post_id%3=0) FROM posts""")
    sizes = stratum_sizes("tv")
    assert sum(sizes.values()) == 8, "四象限必须穷尽，否则权重还原丢数据"
