# FlowOps — developer entry points.
#
# Every target works with a stock Python 3.11+ and Node 20+; there is nothing to
# install. `make verify` is the gate: it runs lint, the Python suite, the console
# suite, and a live end-to-end smoke test against a real server.

PY ?= python
NODE ?= node
BACKEND := backend
FRONTEND := frontend
PORT ?= 8787
BASE ?= http://127.0.0.1:$(PORT)

.DEFAULT_GOAL := help
.PHONY: help demo serve seed export engine test test-py test-js lint lint-py check check-imports render smoke verify clean dist clean-dist publish publish-dry

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

demo: ## run the built-in policy walkthrough in the terminal
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli demo

serve: ## start the API + console on $(BASE) with demo data
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli serve --port $(PORT) --seed

serve-memory: ## start without touching disk (in-memory store)
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli serve --store memory --port $(PORT)

seed: ## insert the demo dataset into SQLite
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli seed --reset

export: ## regenerate the offline console snapshot and the Pyodide engine bundle
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli export --store memory --out ../$(FRONTEND)/data/snapshot.json
	cd $(BACKEND) && $(PY) scripts/build_frontend_engine.py

engine: ## rebuild only the in-browser Python engine bundle
	cd $(BACKEND) && $(PY) scripts/build_frontend_engine.py

test: test-py test-js ## run both test suites

test-py: ## run the Python suite (236+ tests, no dependencies)
	cd $(BACKEND) && $(PY) -m unittest discover -s tests -t . -v

test-js: ## run the console unit tests
	cd $(FRONTEND) && $(NODE) --test tests/lib.test.js

check-imports: ## verify every frontend import resolves (blank-page guard)
	cd $(FRONTEND) && $(NODE) scripts/check-imports.js

render: ## boot the console headlessly against $(BASE) and assert it renders
	cd $(FRONTEND) && $(NODE) --test tests/render.test.js $(BASE)

lint: lint-py check-imports ## static checks

lint-py: ## policy lint + compile check (no third-party linter required)
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli lint
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli check > /dev/null

ruff: ## run ruff if it is installed (optional)
	cd $(BACKEND) && $(PY) -m ruff check app tests && $(PY) -m ruff format --check app tests

smoke: ## end-to-end smoke test against a running server ($(BASE))
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) scripts/smoke.py --base $(BASE)

verify: lint test ## full verification except the live smoke test

ci: ## what CI runs, ending with a live smoke test
	cd $(BACKEND) && PYTHONIOENCODING=utf-8 $(PY) -m app.cli lint
	cd $(BACKEND) && $(PY) -m unittest discover -s tests -t .
	cd $(FRONTEND) && $(NODE) --test tests/lib.test.js
	cd $(BACKEND) && $(PY) scripts/ci_smoke.py --port 8799

dist: ## build a self-contained demo bundle under dist/
	cd $(BACKEND) && $(PY) scripts/build_dist.py

publish: ## publish this working copy to GitHub through the REST API (needs GITHUB_TOKEN)
	cd $(BACKEND) && $(PY) scripts/publish_github.py --repo $(REPO) --message-file $(MSG)

publish-dry: ## show exactly which files would be published
	cd $(BACKEND) && $(PY) scripts/publish_github.py --dry-run

clean-dist: ## remove dist/
	rm -rf dist

clean: clean-dist ## remove generated data and caches
	rm -rf data/*.db data/*.db-wal data/*.db-shm
	find $(BACKEND) -name '__pycache__' -type d -prune -exec rm -rf {} +
