"""数据库层：用户态 PostgreSQL 生命周期、连接、migration runner。

[决策记录] HANDOFF §2 要求用 docker-compose 起 Postgres，但目标机器无 docker/podman
且无 sudo。改用 pgserver（PyPI，自带 PostgreSQL 16.2，走 unix socket 不占端口，
与宿主已有的 5432 实例零冲突）。docker-compose.yml 仍保留在仓库中供其它机器使用。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from .config import REPO_ROOT, settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = REPO_ROOT / "migrations"

# [MUST] 所有连接强制 UTC。见 001_init.sql 的说明——这不只是显示问题，
# date_trunc 对 timestamptz 的分桶直接取决于 session TimeZone。
_CONN_OPTIONS = "-c TimeZone=UTC"


def get_server():
    """启动（或复用）用户态 Postgres 实例。幂等。"""
    import pgserver

    settings.pgdata.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(str(settings.pgdata))


def _admin_dsn() -> str:
    return get_server().get_uri()


def dsn() -> str:
    """ccint 业务库的连接串。"""
    return get_server().get_uri(database=settings.db_name)


def ensure_database() -> bool:
    """确保业务库存在。返回 True 表示本次新建。幂等。"""
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (settings.db_name,)
        ).fetchone()
        if exists:
            return False
        # 库名来自配置而非用户输入，仍用 Identifier 正确转义
        conn.execute(
            pgsql.SQL("CREATE DATABASE {}").format(
                pgsql.Identifier(settings.db_name)
            )
        )
        log.info("created database %s", settings.db_name)
        return True


@contextmanager
def connect(*, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """业务库连接。row_factory=dict_row，TimeZone 强制 UTC。"""
    ensure_database()
    with psycopg.connect(
        dsn(), autocommit=autocommit, row_factory=dict_row, options=_CONN_OPTIONS
    ) as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """显式事务；异常回滚。"""
    with connect(autocommit=False) as conn:
        with conn.transaction():
            yield conn


def assert_utc(conn: psycopg.Connection) -> None:
    """[MUST] 断言 session 时区为 UTC。被 migrate() 和测试调用。"""
    tz = conn.execute("SHOW TimeZone").fetchone()["TimeZone"]
    if tz != "UTC":
        raise RuntimeError(
            f"session TimeZone is {tz!r}, expected 'UTC'. "
            "按天分桶会被错误切分 —— 见 migrations/001_init.sql 说明。"
        )


# --------------------------------------------------------------------------
# Migration runner：编号 .sql 顺序执行，不引入 Alembic（HANDOFF §2）
# --------------------------------------------------------------------------

_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migrate(migrations_dir: Path | None = None) -> list[str]:
    """按文件名顺序执行未应用的 migration。每个文件单事务。幂等。

    返回本次实际应用的文件名列表（重复执行时为空）。
    """
    d = migrations_dir or MIGRATIONS_DIR
    files = sorted(p for p in d.glob("*.sql") if p.is_file())
    applied: list[str] = []

    ensure_database()
    with psycopg.connect(dsn(), autocommit=True, row_factory=dict_row,
                         options=_CONN_OPTIONS) as conn:
        conn.execute(_SCHEMA_MIGRATIONS)
        done = {
            r["filename"]
            for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        for f in files:
            if f.name in done:
                log.debug("migration already applied: %s", f.name)
                continue
            log.info("applying migration %s", f.name)
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (f.name,)
                )
            applied.append(f.name)

    # ALTER DATABASE SET timezone 只对新连接生效，故在新连接上断言
    with connect(autocommit=True) as conn:
        assert_utc(conn)
    return applied


def table_names() -> list[str]:
    with connect(autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
    return [r["tablename"] for r in rows]


def stop_server() -> None:
    try:
        get_server().cleanup()
    except Exception as e:  # noqa: BLE001
        log.warning("stop_server: %s", e)
