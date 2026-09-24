PYTHON := $(firstword $(wildcard .venv/bin/python venv/bin/python) python3)

.PHONY: login up down session tick brief status itinerary \
	sync freeze resume reset test

# Sign in to Airbnb by hand, once. The profile lands in ./data, which `up` mounts.
login:
	@$(PYTHON) manage.py login

# Build the image and run office, courier, and the dashboard.
up:
	docker compose up -d --build

down:
	docker compose down

session:
	@$(PYTHON) manage.py session

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

# Wipe all pipeline data and start fresh (keeps your Airbnb login + guardrails).
reset:
	@$(PYTHON) manage.py reset --yes

test:
	@$(PYTHON) -m pytest tests/ -q
