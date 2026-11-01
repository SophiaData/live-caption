# live-caption

把电脑正在播放的声音实时变成「原文 + 译文」双语悬浮字幕。本地轻量软件，无账号、不上传音频。

## 特性

- **实时双语字幕**：电脑声音 → 自动断句 → 识别成文字 → 翻译 → 屏幕底部置顶字幕（原文小字 + 译文大字）
- **两种识别方式**：云端（任意 OpenAI 兼容语音识别服务，如硅基流动 / OpenAI / Groq）或本地（faster-whisper，免注册离线）
- **可配翻译**：任意 OpenAI 兼容大模型（DeepSeek / Kimi / 通义 / 本地 Ollama 等）
- **免驱动捕获**（macOS 13+）：用系统 ScreenCaptureKit，首次只授权一次屏幕录制；老系统可用 BlackHole 备用
- **好用的字幕窗**：可拖动、记住位置、字号/透明度可调
- **菜单栏常驻**：一键开始/停止

## 启动

```bash
make run
```

首次启动会引导配置「识别服务」和「翻译服务」（密钥存系统钥匙串）。

## 开发

项目仅支持 Python 3.12 及以上版本；Python 3.9 不在支持范围内。

首次安装依赖：

```bash
make setup
```

运行质量门：

```bash
make check   # 格式 + lint + 类型 + 测试
```

## 文档

- 产品需求：`docs/PRD.md`
- 技术架构：`docs/architecture.md`
- 功能台账：`docs/tasks.md`
