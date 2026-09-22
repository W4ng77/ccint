"""配置加载。.env + pydantic-settings，密钥永不入库。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Bluesky ---
    # 实测：*.bsky.app 在本机被出网策略拦截（HTML 403），bsky.social 可用且功能等价。
    bluesky_base_url: str = "https://bsky.social"
    bluesky_handle: str = ""
    bluesky_app_password: str = ""
    # [MUST] createSession 实测限额 10 次/天（RateLimit-Policy: 10;w=86400）。
    # session 必须持久化复用，否则定时任务会在一小时内锁死账号。
    bluesky_session_path: Path = REPO_ROOT / ".bsky_session.json"

    # --- Database ---
    pgdata: Path = REPO_ROOT / "pgdata"
    db_name: str = "ccint"

    # --- Collection ---
    query_version: str = "cyber_v1"
    # incremental 无历史 checkpoint 时的回退起点
    default_since_days: int = 7
    # checkpoint 向前回拨，覆盖延迟入索引的帖子；去重保证重叠无害
    incremental_lookback_minutes: int = 15

    # --- Agent (M6) ---
    anthropic_api_key: str = ""
    agent_model: str = "claude-opus-5"
    prompt_version: str = "analyst_v1"

    # --- Report ---
    report_dir: Path = REPO_ROOT / "reports"

    @property
    def field_prefix(self) -> str:
        return "CCINT_"


class _PrefixedSettings(Settings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="CCINT_",
        extra="ignore",
    )


def load_settings() -> Settings:
    """CCINT_ 前缀优先；ANTHROPIC_API_KEY 无前缀（沿用官方 SDK 惯例）。"""
    import os

    s = _PrefixedSettings()
    if not s.anthropic_api_key:
        s.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "") or _read_env_raw(
            "ANTHROPIC_API_KEY"
        )
    return s


def _read_env_raw(key: str) -> str:
    p = REPO_ROOT / ".env"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return ""


settings = load_settings()
