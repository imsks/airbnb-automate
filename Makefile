PYTHON := $(firstword $(wildcard .venv/bin/python venv/bin/python) python3)

.PHONY: login session api worker tick brief status itinerary sync freeze resume test

# Sign in to Airbnb by hand, once. The worker cannot do this for you.
login:
	@$(PYTHON) manage.py login

session:
	@$(PYTHON) manage.py session

# The API and the worker are separate processes. Only the worker
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
