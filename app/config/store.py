"""配置存储：SQLite 存模型配置，密钥走系统钥匙串。

- base_url / model / 语言对等非敏感配置 → SQLite（用户目录）
- api_key → 系统钥匙串（macOS Keychain / Windows Credential Manager）
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".live-caption" / "config.db"
KEYCHAIN_SERVICE = "live-caption"  # 钥匙串服务名


@dataclass(frozen=True)
class StoredConfig:
    """一个模型槽位的完整配置（含从钥匙串读出的密钥）。"""

    base_url: str
    model: str
    api_key: str  # 从钥匙串读出的，不落盘


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_configs (
                slot TEXT PRIMARY KEY,  -- "asr" | "translate"
                base_url TEXT NOT NULL,
                model TEXT NOT NULL
            )
        """)


def save_config(slot: str, base_url: str, model: str, api_key: str) -> None:
    """存配置（密钥进钥匙串，其余进 SQLite）。"""
    _init_db()
    _save_key_to_keychain(slot, api_key)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO model_configs (slot, base_url, model) VALUES (?, ?, ?)",
            (slot, base_url, model),
        )
    logger.info("配置已保存：slot=%s", slot)


def load_config(slot: str) -> StoredConfig | None:
    """读配置（含钥匙串密钥）；没有则返回 None。"""
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT base_url, model FROM model_configs WHERE slot = ?", (slot,)
        ).fetchone()
    if row is None:
        return None
    key = _read_key_from_keychain(slot)
    if key is None:
        logger.warning("槽位 %s 有配置但钥匙串密钥缺失", slot)
        return None
    return StoredConfig(base_url=row[0], model=row[1], api_key=key)


def _save_key_to_keychain(slot: str, api_key: str) -> None:
    """密钥进系统钥匙串。"""
    import platform

    if platform.system() == "Darwin":
        _mac_keychain_save(slot, api_key)
    elif platform.system() == "Windows":
        _win_credential_save(slot, api_key)
    else:
        raise RuntimeError("不支持的平台")


def _read_key_from_keychain(slot: str) -> str | None:
    """从系统钥匙串读密钥。"""
    import platform

    if platform.system() == "Darwin":
        return _mac_keychain_read(slot)
    if platform.system() == "Windows":
        return _win_credential_read(slot)
    return None


def _mac_keychain_save(slot: str, api_key: str) -> None:
    """macOS Keychain：security add-generic-password。"""
    import subprocess

    # 先删旧项（存在则覆盖）
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", slot],
        capture_output=True,
    )
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            slot,
            "-w",
            api_key,
            "-U",  # 更新已有项
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"保存密钥到钥匙串失败：{result.stderr}")


def _mac_keychain_read(slot: str) -> str | None:
    import subprocess

    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", slot, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _win_credential_save(slot: str, api_key: str) -> None:
    """Windows Credential Manager：cmdkey + 注册表（简化版用 Windows 凭据管理器）。"""
    import subprocess

    # 用 Windows 的 cmdkey（系统凭据管理器）
    subprocess.run(
        ["cmdkey", "/add", f"{KEYCHAIN_SERVICE}:{slot}", "/user", slot, "/pass", api_key],
        capture_output=True,
    )


def _win_credential_read(slot: str) -> str | None:

    # Windows 凭据管理器没有简单命令行读回，这里用占位——实际应用需 win32cred 库
    # MVP 阶段：Windows 用户密钥暂存环境变量（启动时读）
    return os.environ.get(f"LIVE_CAPTION_KEY_{slot.upper()}")
