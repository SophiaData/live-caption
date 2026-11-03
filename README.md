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

## 打包发布（macOS）

**系统要求：需要 Apple 芯片（M1 及以上），macOS 13+；Intel Mac 不支持。**

本项目只做 Apple Silicon（不做 Intel、不做 universal2），并且「包是不是 arm64」由两道机器门保证，不靠构建机的型号碰运气：

```bash
make check-arch    # 打包前：构建用的 Python 必须是 arm64（非 macOS 自动跳过）
make verify-arch   # 打包后：逐个校验 .app 里的二进制架构，不合格即失败
```

产物名带版本号与**实测出来的**架构，例如 `live-caption-0.1.0-macos-arm64.dmg`。

包只做免费的 **ad-hoc 签名**，没有苹果签名与公证（那需要 $99/年 的 Apple Developer 账号）。所以用户首次打开时，如果提示「无法验证开发者」，到「系统设置 → 隐私与安全性」点「仍要打开」即可。

确需在 Intel/Rosetta 解释器上打一个只能本地调试的包，才用 `ALLOW_INTEL=1 make setup`——它会明确打印「已绕过」，这样打出来的包不能对外发布。

## 开发

项目仅支持 Python 3.12 及以上版本；Python 3.9 不在支持范围内。

首次安装依赖（默认使用 Python 3.12；如果 `python3.12` 不在 PATH 上，用 `make setup PYTHON=/path/to/python3.12` 指定解释器）：

```bash
make setup
```

运行质量门：

```bash
make check   # 格式 + lint + 类型 + 测试
```

查看覆盖率（包含分支覆盖率，不设阈值门禁）：

```bash
make coverage
```

该命令运行完整测试并输出 `app/` 的覆盖率报告，用于发现测试未触达的装配和业务分支。

## 文档

- 产品需求：`docs/PRD.md`
- 技术架构：`docs/architecture.md`
- 功能台账：`docs/tasks.md`
