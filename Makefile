PYTHON := $(firstword $(wildcard .venv/bin/python venv/bin/python) python3)

.PHONY: start login session api worker tick brief status itinerary sync freeze resume test

# The usual way to run it: API + worker in one process, dashboard opens.
start:
	@$(PYTHON) manage.py start

# Sign in to Airbnb by hand, once. Nothing can do this for you.
login:
	@$(PYTHON) manage.py login

session:
	@$(PYTHON) manage.py session

# Run the halves separately — useful when the worker lives somewhere else.
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

itinerary:
	@$(PYTHON) manage.py itinerary

sync:
	@$(PYTHON) manage.py sync

freeze:
	@$(PYTHON) manage.py freeze --reason "manual"

resume:
	@$(PYTHON) manage.py resume

test:
	@$(PYTHON) -m pytest tests/ -q
