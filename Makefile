.PHONY: setup lock dev build test doctor stop

PYTHON ?= python3
VENV_PYTHON := .venv/bin/python
PYTHON_BIN := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),$(PYTHON))

setup:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || { echo "Python 3.11 or later is required."; exit 1; }
	@command -v node >/dev/null || { echo "Node.js 20.19+ or 22.12+ is required."; exit 1; }
	@node -e 'const [a,b,c]=process.versions.node.split(".").map(Number); const ok=(a===20&&b>=19)||(a>=22&&(a>22||b>12||(b===12&&c>=0))); if(!ok) process.exit(1)' || { echo "Node.js 20.19+ or 22.12+ is required."; exit 1; }
	@command -v npm >/dev/null || { echo "npm is required."; exit 1; }
	@if [ ! -f requirements.lock ] || [ ! -f package-lock.json ]; then $(PYTHON) scripts/lock_dependencies.py; fi
	$(PYTHON) -m venv .venv
	.venv/bin/python scripts/dependency_tool.py python install --require-hashes -r requirements.lock
	./scripts/dependency_tool.py npm ci

lock:
	@command -v npm >/dev/null || { echo "npm is required to generate the frontend lockfile."; exit 1; }
	$(PYTHON) scripts/lock_dependencies.py

dev:
	@test -x $(VENV_PYTHON) || { echo "Run make setup first."; exit 1; }
	@test -d node_modules || { echo "Run make setup first."; exit 1; }
	./scripts/dev.sh

build:
	@test -d node_modules || { echo "Run make setup first."; exit 1; }
	npm run build
	PYTHONPATH=backend/src $(PYTHON_BIN) -m compileall -q backend/src

test:
	PYTHONPATH=backend/src $(PYTHON_BIN) -m unittest discover -s backend/tests -v
	node --test scripts/ui-contract.test.mjs

doctor:
	$(PYTHON_BIN) scripts/doctor.py $(if $(filter 1,$(NETWORK)),--network,)

stop:
	./scripts/stop.sh
