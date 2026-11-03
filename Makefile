# 质量门：format + lint + 类型检查 + 测试，一条命令全跑
.PHONY: setup check coverage run latency latency-live check-arch verify-arch

PYTHON ?= python3.12

# Issue #20：构建架构。老板已定只做 Apple Silicon——不做 Intel，不做 universal2。
ARCH ?= arm64
APP ?= dist/live-caption.app

# 构建前硬门：macOS 上构建解释器必须是 arm64，否则打出来的包在 Apple 芯片上跑不动。
# 只在 darwin 生效（Linux/Windows 的 CI 不受影响，会打印「跳过」）。
# 确需 Intel 调试包（产物不可发布）才用 ALLOW_INTEL=1 显式绕过，绕过会被打印出来。
check-arch:
	"$(PYTHON)" -m app.release.arch interpreter --require $(ARCH)

# 构建后硬门：逐个校验 .app 里的二进制（主程序只有 arm64 + 每个 .so/.dylib 含 arm64）。
# 不合格退出码 1，产物禁止发布；CI 的 build-release 跑的是同一条命令。
verify-arch:
	.venv/bin/python -m app.release.arch bundle "$(APP)" --require $(ARCH)

setup: check-arch
	"$(PYTHON)" -c 'import sys; v=sys.version_info; sys.exit(f"需要 Python 3.12 及以上，当前是 {v.major}.{v.minor}.{v.micro}。请运行：make setup PYTHON=/path/to/python3.12" if v < (3, 12) else 0)'
	"$(PYTHON)" -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

check:
	.venv/bin/ruff format --check app tests
	.venv/bin/ruff check app tests
	.venv/bin/mypy app
	.venv/bin/pytest -q

coverage:
	.venv/bin/coverage run --branch -m pytest
	.venv/bin/coverage report --include='app/*'

run:
	.venv/bin/python -m app.main

# 端到端字幕延迟分阶段报告（Issue #16）：确定性假件基准，不联网、不需要密钥
# 假件耗时与句长可参数注入，例如：make latency TR_MS=1200 SENTENCES=1.5,2.0
# 加跑「边说边出」对比（Issue #17）：make latency DRAFT=1
# 真服务模式（需先在配置向导配好识别+翻译服务）：make latency LIVE=1 或 make latency-live
REC_MS ?= 400
TR_MS ?= 600
UI_MS ?= 20
SENTENCES ?= 1.5,2.0,2.5
DRAFT_SILENCE_MS ?= 150
DRAFT_MAX_S ?= 1.2

latency:
	.venv/bin/python -m app.bench.latency --recognizer-ms $(REC_MS) --translator-ms $(TR_MS) \
		--ui-ms $(UI_MS) --sentences $(SENTENCES) $(if $(filter 1,$(LIVE)),--live,) \
		$(if $(filter 1,$(DRAFT)),--draft,) \
		--draft-silence-ms $(DRAFT_SILENCE_MS) --draft-max-s $(DRAFT_MAX_S)

latency-live:
	.venv/bin/python -m app.bench.latency --live --draft
