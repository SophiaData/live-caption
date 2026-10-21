"""功能 #4 测试：配置存储与 OpenAI 兼容客户端（网络层 mock，不打真请求）。"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config.store import load_config, save_config
from app.providers.openai_compatible import ModelConfig, OpenAICompatibleClient


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试用临时数据库路径，不污染用户目录。"""
    db = tmp_path / "test_config.db"
    monkeypatch.setattr("app.config.store.DB_PATH", db)
    # 模拟钥匙串（测试不真调系统命令）
    monkeypatch.setattr("app.config.store._save_key_to_keychain", lambda s, k: None)
    monkeypatch.setattr("app.config.store._read_key_from_keychain", lambda s: "test-key-" + s)
    # 隔离系统代理变量（某些环境的 NO_PROXY 含 [::1] 会触发 httpx 解析 bug）
    for var in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)


class TestConfigStore:
    def test_save_and_load_roundtrip(self) -> None:
        save_config("asr", "https://api.openai.com/v1", "whisper-1", "sk-test")
        cfg = load_config("asr")
        assert cfg is not None
        assert cfg.base_url == "https://api.openai.com/v1"
        assert cfg.model == "whisper-1"
        assert cfg.api_key == "test-key-asr"  # 从模拟钥匙串读出

    def test_load_missing_slot_returns_none(self) -> None:
        assert load_config("asr") is None

    def test_key_not_in_db(self) -> None:
        """密钥不落 SQLite 表（只存地址和模型名）。"""
        save_config("translate", "https://api.deepseek.com/v1", "deepseek-chat", "sk-secret")
        from app.config.store import DB_PATH

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT base_url, model FROM model_configs WHERE slot='translate'"
            ).fetchone()
        assert row is not None
        assert row[0] == "https://api.deepseek.com/v1"
        assert "sk-secret" not in str(row)  # 密钥不在数据库里

    def test_update_existing_slot(self) -> None:
        """同一槽位重复保存覆盖旧的。"""
        save_config("asr", "https://old.com/v1", "model-a", "sk-1")
        save_config("asr", "https://new.com/v1", "model-b", "sk-2")
        cfg = load_config("asr")
        assert cfg is not None
        assert cfg.base_url == "https://new.com/v1"
        assert cfg.model == "model-b"


class TestOpenAICompatibleClient:
    def test_chat_test_connection_success(self) -> None:
        """200 响应返回成功。"""
        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="sk-test", model="test")
        client = OpenAICompatibleClient(cfg)
        with patch.object(client._client, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            ok, msg = client.test_connection(mode="chat")
        assert ok is True
        assert "成功" in msg

    def test_chat_401_returns_key_error_hint(self) -> None:
        """401 响应返回密钥错误人话。"""
        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="wrong", model="test")
        client = OpenAICompatibleClient(cfg)
        with patch.object(client._client, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.text = "unauthorized"
            mock_post.return_value = mock_resp
            ok, msg = client.test_connection(mode="chat")
        assert ok is False
        assert "密钥" in msg or "401" in msg

    def test_chat_timeout_returns_network_hint(self) -> None:
        """超时返回网络人话。"""
        import httpx

        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="sk-test", model="test")
        client = OpenAICompatibleClient(cfg)
        with patch.object(client._client, "post", side_effect=httpx.TimeoutException("timeout")):
            ok, msg = client.test_connection(mode="chat")
        assert ok is False
        assert "超时" in msg or "网络" in msg

    def test_audio_test_connection_posts_wav(self) -> None:
        """audio 模式发 wav 文件到 /audio/transcriptions。"""
        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="sk-test", model="whisper-1")
        client = OpenAICompatibleClient(cfg)
        with patch.object(client._client, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp
            ok, msg = client.test_connection(mode="audio")
        assert ok is True
        # 验证确实发了 wav
        call = mock_post.call_args
        assert call[0][0].endswith("/audio/transcriptions")
        assert "file" in call[1]["files"]


class TestNoProxyBugFix:
    def test_client_constructs_with_bad_no_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归测试：NO_PROXY 含 [::1] 时构造客户端不能崩（曾导致测试连接永久卡住）。"""
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
        # 构造不应抛异常
        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="sk-x", model="m")
        client = OpenAICompatibleClient(cfg)
        assert client is not None
        # 坏条目应被清理
        import os

        assert "[::1]" not in os.environ.get("NO_PROXY", "")
