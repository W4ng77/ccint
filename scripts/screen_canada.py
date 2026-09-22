"""加拿大实体筛:对 rules_v2 判为 irrelevant 的帖子逐条问一个问题——有没有加拿大实体。

这个脚本回答的是一个召回问题:**AND 过滤器丢掉的那 9 万条里,有多少是真的
该留下的?** 它不是 labeler,不写 post_labels,不产生 label_version。它的输出
是一份供人工审核的 TSV,因为「筛子说通过」和「这条确实相关」之间还隔着一次
人工确认,而整个项目的前提就是不把模型的判断当成 ground truth。

[MUST] 只问加拿大,不问 cyber。语料里每一条都是 34 个安全词搜来的,cyber 那一侧
由采集边界保证。实测过合取版本:4B 模型会把 "cyber ∧ Canada" 坍缩成 "cyber",
通过率 6.8% 且绝大多数是与加拿大无关的全球 CTI 噪声。拆成单问后降到 1.1%,
且通过的样本里开始出现真正该抓到的帖(加拿大公司出现在勒索组织受害者名单上)。
"""
from __future__ import annotations

import argparse, csv, hashlib, json, sys, time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ccint import db

PROMPT = (
    "Does this post mention a CANADIAN entity? That means: Canada or "
    "Canadian; a Canadian province, territory or city; a Canadian "
    "government body, agency, police force, university, hospital, bank, "
    "telecom or company; a .ca or .gc.ca domain; or a named Canadian "
    "person in an official role. France, Belgium, Switzerland, or the "
    "French language alone do NOT count. Answer ONLY compact JSON "
    '{"c":true|false}. No other text.'
)
SCHEMA = {"type": "object", "properties": {"c": {"type": "boolean"}},
          "required": ["c"], "additionalProperties": False}

SELECT = """
SELECT p.post_id, p.author_handle, p.published_at, p.text
FROM posts p JOIN post_labels l USING (post_id)
WHERE l.label_version = %(base)s AND NOT l.is_relevant
  AND length(p.text) > %(minlen)s
ORDER BY p.post_id
"""


def screen(cl: httpx.Client, base_url: str, model: str, text: str) -> bool | None:
    for attempt in range(3):
        try:
            r = cl.post(f"{base_url}/chat/completions", json={
                "model": model, "temperature": 0, "max_tokens": 12, "seed": 0,
                "messages": [{"role": "system", "content": PROMPT},
                             {"role": "user", "content": text[:1200]}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "screen", "schema": SCHEMA}},
            }, timeout=120)
            r.raise_for_status()
            return json.loads(r.json()["choices"][0]["message"]["content"])["c"]
        except Exception:
            if attempt == 2:
                return None            # None ≠ False:失败和判否必须可区分
            time.sleep(1.5 * (attempt + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", default="user")
    ap.add_argument("--base-version", default="rules_v2")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--min-len", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="eval/canada_screen.tsv")
    a = ap.parse_args()

    with db.connect(autocommit=True) as conn:
        rows = [dict(r) for r in conn.execute(
            SELECT, {"base": a.base_version, "minlen": a.min_len}).fetchall()]
    if a.limit:
        rows = rows[:a.limit]
    print(f"待筛 {len(rows):,} 条(base={a.base_version}, min_len={a.min_len})")

    lim = httpx.Limits(max_connections=a.workers + 4,
                       max_keepalive_connections=a.workers + 4)
    t0 = time.perf_counter()
    n_pass = n_err = 0
    with httpx.Client(limits=lim) as cl, \
            ThreadPoolExecutor(a.workers) as pool, \
            open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
        w.writerow(["post_id", "verdict", "author_handle", "published_at",
                    "text", "human"])
        fn = lambda r: (r, screen(cl, a.base_url, a.model, r["text"] or ""))
        for i, (r, v) in enumerate(pool.map(fn, rows), 1):
            if v is None:
                n_err += 1
            elif v:
                n_pass += 1
                w.writerow([r["post_id"], "PASS", r["author_handle"],
                            r["published_at"].isoformat(),
                            " ".join((r["text"] or "").split()), ""])
            if i % 10000 == 0:
                print(f"  {i:,}/{len(rows):,}  pass={n_pass:,}  err={n_err}",
                      flush=True)

    wall = time.perf_counter() - t0
    ph = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]
    print(f"\n{'='*58}\nmodel={a.model}  prompt_sha256[:12]={ph}")
    print(f"screened {len(rows):,}  pass {n_pass:,} "
          f"({n_pass/max(1,len(rows)-n_err):.2%})  error {n_err}")
    print(f"wall {wall/60:.1f} min  ({len(rows)/wall:.0f} posts/s)")
    print(f"→ {a.out}  ({n_pass:,} 行待人工审)")
    print("\n[MUST] PASS 不等于 relevant。下一步是人工审这份 TSV 的 human 列,"
          "\n未经人工确认前不要把任何数字写进报告。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
