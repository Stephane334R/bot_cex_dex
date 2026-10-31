PY ?= .venv/bin/python
HOURS ?= 3

.PHONY: setup collect backfill app test test-network fixture

setup:            ## create the venv and install dependencies
	python3 -m venv .venv && $(PY) -m pip install -q -r requirements.txt
	@test -f .env || cp .env.example .env

collect:          ## live shadow collector (Ctrl-C to stop)
	$(PY) -m collector.main

backfill:         ## replay the last $(HOURS) hours (make backfill HOURS=6)
	$(PY) -m collector.backfill --hours $(HOURS)

app:              ## Streamlit dashboard (reads SQLite only)
	$(PY) -m streamlit run app/dashboard.py

test:             ## offline unit tests (AMM math, QuoterV2 parity fixture, strategy, replay)
	$(PY) -m pytest

test-network:     ## live QuoterV2 parity against the RPC
	$(PY) -m pytest -m network

fixture:          ## re-record the QuoterV2 parity fixture at the current block
	$(PY) -m scripts.make_quoter_fixture
