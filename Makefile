# 质量门：format + lint + 类型检查 + 测试，一条命令全跑
.PHONY: setup check run

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

run:
	.venv/bin/python -m app.main
