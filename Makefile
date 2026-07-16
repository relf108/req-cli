.PHONY: install qa lint format typecheck test run

install:
	uv sync

qa: lint typecheck

lint:
	ruff check src tests
	ruff format --check src tests

format:
	ruff format src tests
	ruff check --fix src tests

typecheck:
	mypy

test:
	pytest

run:
	nix build . && req
