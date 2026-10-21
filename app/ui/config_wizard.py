"""UI：模型配置向导（首次启动用）。

填「识别服务」和「翻译服务」两槽位：地址 + 密钥 + 模型名，
各带「测试连接」按钮（绿色对勾/红色错误人话），通过后保存。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config.store import load_config, save_config
from ..providers.openai_compatible import ModelConfig, OpenAICompatibleClient


class TestConnectionWorker(QThread):
    """后台线程跑连接测试，避免卡 UI。"""

    done = Signal(bool, str)  # (成功否, 人话消息)

    def __init__(self, config: ModelConfig, mode: str) -> None:
        super().__init__()
        self._config = config
        self._mode = mode

    def run(self) -> None:
        client = OpenAICompatibleClient(self._config)
        ok, msg = client.test_connection(mode=self._mode)
        self.done.emit(ok, msg)


class ConfigWizard(QDialog):
    """模型配置向导对话框。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("live-caption · 模型配置")
        self.setModal(True)
        self.resize(520, 380)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "配置两个模型服务（都是 OpenAI 兼容地址）：\n"
            "1. 识别服务：把声音变成文字（如 Whisper API）\n"
            "2. 翻译服务：把原文翻译成译文（如 DeepSeek）\n"
            "密钥只存系统钥匙串，不落配置文件。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # 识别服务槽位
        layout.addWidget(QLabel("<b>识别服务</b>（声音 → 文字）"))
        asr_form = QFormLayout()
        self._asr_url = QLineEdit()
        self._asr_url.setPlaceholderText("如 https://api.openai.com/v1")
        asr_form.addRow("地址:", self._asr_url)
        self._asr_key = QLineEdit()
        self._asr_key.setPlaceholderText("sk-... 密钥（存系统钥匙串）")
        self._asr_key.setEchoMode(QLineEdit.EchoMode.Password)
        asr_form.addRow("密钥:", self._asr_key)
        self._asr_model = QLineEdit()
        self._asr_model.setPlaceholderText("如 whisper-1")
        asr_form.addRow("模型名:", self._asr_model)
        layout.addLayout(asr_form)
        self._asr_test_btn = QPushButton("测试连接")
        self._asr_status = QLabel("未测试")
        asr_row = QHBoxLayout()
        asr_row.addWidget(self._asr_test_btn)
        asr_row.addWidget(self._asr_status)
        layout.addLayout(asr_row)

        layout.addSpacing(10)

        # 翻译服务槽位
        layout.addWidget(QLabel("<b>翻译服务</b>（原文 → 译文）"))
        trans_form = QFormLayout()
        self._trans_url = QLineEdit()
        self._trans_url.setPlaceholderText("如 https://api.deepseek.com/v1")
        trans_form.addRow("地址:", self._trans_url)
        self._trans_key = QLineEdit()
        self._trans_key.setPlaceholderText("sk-... 密钥（存系统钥匙串）")
        self._trans_key.setEchoMode(QLineEdit.EchoMode.Password)
        trans_form.addRow("密钥:", self._trans_key)
        self._trans_model = QLineEdit()
        self._trans_model.setPlaceholderText("如 deepseek-chat")
        trans_form.addRow("模型名:", self._trans_model)
        layout.addLayout(trans_form)
        self._trans_test_btn = QPushButton("测试连接")
        self._trans_status = QLabel("未测试")
        trans_row = QHBoxLayout()
        trans_row.addWidget(self._trans_test_btn)
        trans_row.addWidget(self._trans_status)
        layout.addLayout(trans_row)

        layout.addSpacing(10)

        # 保存按钮
        self._save_btn = QPushButton("保存配置")
        self._save_btn.setEnabled(False)
        layout.addWidget(self._save_btn)

        self._asr_test_btn.clicked.connect(
            lambda: self._test_slot(
                "asr", self._asr_url, self._asr_key, self._asr_model, self._asr_status
            )
        )
        self._trans_test_btn.clicked.connect(
            lambda: self._test_slot(
                "translate", self._trans_url, self._trans_key, self._trans_model, self._trans_status
            )
        )
        self._save_btn.clicked.connect(self._save)

        self._load_existing()

    def _load_existing(self) -> None:
        """已有配置则预填（密钥不回显，提示已保存）。"""
        asr = load_config("asr")
        if asr:
            self._asr_url.setText(asr.base_url)
            self._asr_model.setText(asr.model)
            self._asr_key.setPlaceholderText("密钥已保存（改的话重新填）")
        trans = load_config("translate")
        if trans:
            self._trans_url.setText(trans.base_url)
            self._trans_model.setText(trans.model)
            self._trans_key.setPlaceholderText("密钥已保存（改的话重新填）")

    def _test_slot(
        self,
        slot: str,
        url_edit: QLineEdit,
        key_edit: QLineEdit,
        model_edit: QLineEdit,
        status_label: QLabel,
    ) -> None:
        """后台测试连接，不卡 UI。"""
        url = url_edit.text().strip()
        key = key_edit.text().strip()
        model = model_edit.text().strip()
        if not all([url, key, model]):
            status_label.setText("❌ 请先填完地址、密钥、模型名")
            status_label.setStyleSheet("color: red")
            return

        status_label.setText("测试中…")
        status_label.setStyleSheet("color: gray")
        mode = "audio" if slot == "asr" else "chat"
        config = ModelConfig(base_url=url, api_key=key, model=model)
        worker = TestConnectionWorker(config, mode)
        worker.done.connect(lambda ok, msg: self._on_test_done(ok, msg, status_label))
        worker.start()
        # 存 worker 引用防被 GC
        self._workers = getattr(self, "_workers", [])
        self._workers.append(worker)

    def _on_test_done(self, ok: bool, msg: str, status_label: QLabel) -> None:
        if ok:
            status_label.setText(f"✅ {msg}")
            status_label.setStyleSheet("color: green")
        else:
            status_label.setText(f"❌ {msg}")
            status_label.setStyleSheet("color: red")
        self._check_save_enabled()

    def _check_save_enabled(self) -> None:
        """两个槽位都测试通过才允许保存。"""
        asr_ok = "✅" in self._asr_status.text()
        trans_ok = "✅" in self._trans_status.text()
        self._save_btn.setEnabled(asr_ok and trans_ok)

    def _save(self) -> None:
        """保存配置（密钥进钥匙串）。"""
        save_config(
            "asr",
            self._asr_url.text().strip(),
            self._asr_model.text().strip(),
            self._asr_key.text().strip(),
        )
        save_config(
            "translate",
            self._trans_url.text().strip(),
            self._trans_model.text().strip(),
            self._trans_key.text().strip(),
        )
        self.accept()  # 关闭对话框
