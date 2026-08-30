PYTHON := $(firstword $(wildcard .venv/bin/python venv/bin/python) python3)

.PHONY: up down

up:
	@$(PYTHON) -m app.devctl up

down:
	@$(PYTHON) -m app.devctl down
