UV ?= uv
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python

.PHONY: install run test lint format typecheck check

install:
	$(UV) sync --python 3.12 --extra dev

run:
	$(PYTHON) -m uvicorn computegrid_api.main:app --app-dir apps/api --reload

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m mypy

check: lint typecheck test

