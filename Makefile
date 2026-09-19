PYTHON := $(firstword $(wildcard .venv/bin/python venv/bin/python) python3)

.PHONY: up down api worker tick brief status freeze resume test

up:
	@$(PYTHON) -m app.devctl up

down:
	@$(PYTHON) -m app.devctl down

# v2: the API and the worker are separate processes. Only the worker
# touches the browser, so run it where you are logged in to Airbnb.
api:
	@$(PYTHON) manage.py api --reload

worker:
	@$(PYTHON) manage.py worker

tick:
	@$(PYTHON) manage.py tick

brief:
	@$(PYTHON) manage.py brief

status:
	@$(PYTHON) manage.py status

freeze:
	@$(PYTHON) manage.py freeze --reason "manual"

resume:
	@$(PYTHON) manage.py resume

test:
	@$(PYTHON) -m pytest tests/ -q
