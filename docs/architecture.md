# 架构与技术约束

> 维护人：技术合伙人。老板不需要读本文件。版本以 lockfile 为准，本表只记选型与理由。

## 技术栈

| 层 | 选型 | 一句话理由 |
|---|---|---|
| 语言/运行时 | Python 3.12（本机 ~/.local/bin/python3.12；打包时捆绑） | 音频/网络生态最全；GUI、捕获、协议层全用它 |
| GUI | PySide6 6.12（Qt6） | 成熟桌面 GUI，无边框置顶透明窗 + 菜单栏常驻一套搞定，双平台 |
| 音频捕获 | sounddevice 0.5.6（PortAudio） | 跨平台读虚拟声卡/回环设备；Mac 走 BlackHole 输入，Windows 用 WASAPI loopback |
| 网络 | httpx 0.28 | 同步/流式/可取消请求齐全，覆盖 OpenAI 兼容翻译 API 与分段识别 API |
| 配置存储 | SQLite（用户目录内单文件）+ 系统钥匙串存密钥 | 零服务、零依赖；密钥不落明文 |
| 测试 | pytest（GUI 逻辑与管道层可测，Qt 部分冒烟） | 主流保守 |
| 工具链 | ruff（format+lint）+ mypy + pytest 单命令串联 | agent 友好：类型检查强、报错可读 |

## 硬约束（动架构、加依赖前逐条核对）

- 依赖白名单制：新依赖必须在下表登记用途与理由；无理由不引入
- 黑名单：不引入本地 ASR/翻译模型运行库（faster-whisper、torch 等）——本产品定位「用户自配模型」，体积必须轻；不引入任何需要编译环境的重依赖（构建机之外）
- 模型接入只走「OpenAI 兼容协议」两种端点（chat/completions、audio/transcriptions）；厂商特有协议一律用适配层收编，不进核心管道
- 网络层：所有请求必须可取消、必须有超时；禁止无界重试（上限 3 次、指数退避）
- 管道层：各段用有界队列连接；UI 线程与音频线程、网络线程严格分离，UI 卡死 = P0 缺陷
- 日志：本地文件日志，禁止打印密钥；日志单文件上限 10MB 滚动
- 错误处理口径：用户可见错误一律给「人话 + 下一步怎么办」；技术细节进日志

## 依赖登记

| 依赖 | 用途 | 引入理由 | 引入于（功能#） |
|---|---|---|---|
| PySide6 | GUI（悬浮窗/配置页/菜单栏） | 置顶透明无边框窗与 tray 唯一成熟跨平台选择 | 立项 |
| sounddevice | 系统音频捕获 | PortAudio 封装，跨平台读输入设备 | #2 |
| httpx | 调识别/翻译 API | 流式响应 + 同步 + 取消全覆盖 | #3 |
| numpy | 音频块数值处理（sounddevice 回调伴生） | sounddevice 依赖，音频缓冲必要 | #2 |

## 决策记录（ADR-lite：只记「为什么」，防止日后翻案无据）

| # | 日期 | 决策 | 理由 | 被否的替代方案及原因 |
|---|---|---|---|---|
| 1 | 2026-10-21 | Python + PySide6 而非 Tauri/Electron | 音频捕获、协议适配生态全在 Python；Tauri 要 Rust sidecar 双技术栈，Electron 体积大 | Tauri（壳小但双栈维护贵）、Electron（体积违背「轻量」定位） |
| 2 | 2026-10-21 | 模型接入只认 OpenAI 兼容协议 | 一个协议覆盖 DeepSeek/Kimi/通义/OpenAI/中转站/Ollama，用户填 base_url 即用，厂商中立 | 各厂商私有 SDK（维护面爆炸、加引擎要改核心） |
| 3 | 2026-10-21 | Mac 音频捕获走 BlackHole 虚拟声卡 | 系统级捕获的唯一免编译稳定路径；ScreenCaptureKit 需录屏权限且实现重 | ScreenCaptureKit（权限重、复杂）、核心音频 HAL（要签名驱动） |
| 4 | 2026-10-21 | 识别走「VAD 断句→分段识别」而非流式识别协议 | 第一版延迟目标 2 秒内，分段方案延迟可达标且代码量减半；流式协议各家不兼容 | 各家流式 WS 协议（MVP 过重，记 tech-debt 待议） |
| 5 | 2026-10-21 | VAD 用能量阈值 + 静音时长断句，先不引 Silero | 零依赖即可达成 2 秒目标；观感不佳再升级（tech-debt 跟踪） | Silero VAD（引 ONNX 运行时，违背轻量，先不上） |
| 6 | 2026-10-21 | 配置存 SQLite + 钥匙串 | 本地软件定位，无账号无云；密钥安全是公开发布的基本要求 | 明文 JSON（不安全）、云同步（违背 PRD「不做账号」） |

## 目录结构约定

```
live-caption/
├── app/                  # 主包：入口 main.py 在此
│   ├── capture/          #   音频捕获（Mac/Win 设备抽象）
│   ├── pipeline/          #   断句→识别→翻译管道（纯逻辑，可测）
│   ├── providers/        #   OpenAI 兼容协议客户端（asr/translate 两槽位）
│   ├── ui/               #   PySide6 界面（悬浮窗/配置向导/托盘）
│   └── config/           #   SQLite 配置 + 钥匙串
├── tests/                #   pytest：管道与协议层为主
├── docs/                 #   PRD/架构/台账/协作手册（老板读物在 docs/）
├── backups/              #   改核心文件前的备份（不入 git）
└── dist/                 #   打包产物（不入 git）
```
