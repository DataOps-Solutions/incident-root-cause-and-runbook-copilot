PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: setup test demo lint clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@test -f .env || cp .env.example .env
	@echo "Setup complete. Run 'make test' or 'make demo'."

test:
	$(PY) -m pytest

demo:
	LLM_PROVIDER=mock $(PY) -m src.main

lint:
	$(PY) -m ruff check src tests
	$(PY) -m mypy src

clean:
	rm -rf output/*.json output/*.md output/*.db .pytest_cache .mypy_cache
