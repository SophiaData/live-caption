# 质量门：format + lint + 类型检查 + 测试，一条命令全跑
.PHONY: check run

check:
	.venv/bin/ruff format --check app tests
	.venv/bin/ruff check app tests
	.venv/bin/mypy app
	.venv/bin/pytest -q

run:
	.venv/bin/python -m app.main
