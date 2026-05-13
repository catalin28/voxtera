.PHONY: install run dev test lint format clean help

help:
	@echo "Voxtera — common commands"
	@echo ""
	@echo "  make install   Install dependencies and pre-commit hooks"
	@echo "  make run       Run the local voice loop (bot.py)"
	@echo "  make dev       Run with auto-reload on src/ changes"
	@echo "  make test      Run pytest"
	@echo "  make lint      Run ruff + mypy"
	@echo "  make format    Auto-format with ruff"
	@echo "  make clean     Remove caches and build artefacts"

install:
	uv sync
	uv run pre-commit install

run:
	uv run voxtera run

dev:
	uv run watchfiles --filter python "voxtera run" src/voxtera config/

test:
	uv run pytest -v

lint:
	uv run ruff check src tests
	uv run mypy src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
