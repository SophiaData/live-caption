# 质量门：format + lint + 类型检查 + 测试，一条命令全跑
.PHONY: setup check coverage run latency latency-live

PYTHON ?= python3.12

setup:
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
