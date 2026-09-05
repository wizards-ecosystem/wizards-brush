SHELL := /bin/bash
PROJECT_ENV = source scripts/project-env.sh &&
.DEFAULT_GOAL := help

.PHONY: host-check install setup bootstrap install-nunchaku migrate dev start stop browser browser-dev build models models-status \
	benchmark-local-plan benchmark-local benchmark-a100-plan benchmark-a100 remote-gpu test clean-gens \
	clean clean-build clean-cache clean-runtime lint typecheck check release-check doctor offline-check \
	optional-check sbom-check release-layout-check release-bundle help

host-check:   ## verify required host commands without downloading anything
	bash scripts/check-host.sh

install:      ## install tools/dependencies and build; do not download model weights
	bash scripts/setup.sh --skip-models

setup:        ## install everything, including the enabled local model profile
	bash scripts/setup.sh

bootstrap:    ## install pinned uv + Node inside .runtime/tools
	bash scripts/bootstrap-tools.sh

install-nunchaku: ## install an operator-pinned Nunchaku wheel (advanced)
	bash scripts/install_nunchaku.sh

migrate:      ## move legacy model/cache directories into models/
	$(PROJECT_ENV) .venv/bin/python scripts/migrate_local_state.py

dev:          ## backend :8000 (reload) + Vite :5173
	bash scripts/dev.sh

start:        ## build frontend + serve on one LAN port (handles everything)
	bash scripts/start.sh

stop:         ## stop a running server
	@pkill -f "[u]vicorn backend.app.main" && echo "stopped" || echo "nothing running"

browser:      ## launch with browser profile/cache inside .runtime
	bash scripts/browser.sh

browser-dev:  ## launch the contained browser profile against Vite :5173
	WB_URL=http://localhost:5173 bash scripts/browser.sh

build:        ## build frontend only
	$(PROJECT_ENV) cd frontend && npm run build

models:       ## install the curated local model and LoRA profile
	$(PROJECT_ENV) .venv/bin/python scripts/download_models.py

models-status: ## audit configured/cached benchmark models and adapters
	$(PROJECT_ENV) .venv/bin/python scripts/model_suite_status.py

benchmark-local-plan: ## print the two-prompt local smoke matrix; queue nothing
	$(PROJECT_ENV) .venv/bin/python scripts/sampling_matrix.py --smoke --dry-run

benchmark-local: ## run the two-prompt local matrix and validate every result
	$(PROJECT_ENV) .venv/bin/python scripts/sampling_matrix.py --smoke --wait

benchmark-a100-plan: ## print the two-prompt Remote GPU matrix; no live worker needed
	$(PROJECT_ENV) .venv/bin/python scripts/a100_sampling_matrix.py --smoke --dry-run

benchmark-a100: ## run the two-prompt A100 matrix against a live Remote GPU worker
	$(PROJECT_ENV) .venv/bin/python scripts/a100_sampling_matrix.py --smoke --wait

remote-gpu:   ## write remote_gpu_filled.py with local settings injected
	$(PROJECT_ENV) .venv/bin/python scripts/make_remote_gpu.py

test:         ## run the backend test suite
	$(PROJECT_ENV) .venv/bin/python -m pytest tests -q

lint:         ## run ruff over backend, tests, scripts, and the remote runner
	$(PROJECT_ENV) .venv/bin/ruff check backend tests scripts remote_gpu.py

typecheck:    ## type-check backend, tests, scripts, and remote runner
	$(PROJECT_ENV) .venv/bin/python -m mypy

check:        ## everything CI runs: backend (ruff/mypy/pytest) + frontend (lint/format/test/build)
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test
	$(PROJECT_ENV) cd frontend && npm run lint && npm run format:check && npm run test && npm run build
	$(PROJECT_ENV) .venv/bin/python scripts/build_release.py --check

release-check: check doctor offline-check optional-check sbom-check ## run the complete pre-release verification gate

clean-gens:   ## delete ALL generated images/videos (keeps history + presets)
	$(PROJECT_ENV) .venv/bin/python scripts/clean.py

clean: clean-build ## remove rebuildable frontend output

clean-build:  ## remove frontend build and compiler cache
	rm -rf frontend/dist .runtime/cache/vite .runtime/cache/typescript \
		.runtime/cache/python-bytecode

clean-cache:  ## remove project package/compiler caches; keep models and data
	rm -rf .runtime/cache .pytest_cache .ruff_cache .mypy_cache \
		frontend/.vite frontend/tsconfig.tsbuildinfo

clean-runtime: ## remove local toolchain, venv, Node deps, caches, browser profile
	rm -rf .runtime .venv frontend/node_modules frontend/dist

doctor:       ## prove every active writable path stays inside this checkout
	$(PROJECT_ENV) .venv/bin/python scripts/doctor.py

offline-check: doctor ## verify installed Python/Node dependencies without downloads
	$(PROJECT_ENV) .venv/bin/python scripts/check_dependencies.py
	$(PROJECT_ENV) cd frontend && npm cache verify

optional-check: ## smoke-test MediaPipe and ControlNet imports from the full install
	$(PROJECT_ENV) .venv/bin/python scripts/check_optional_imports.py

sbom-check: ## validate release SBOM generation from both locked dependency graphs
	$(PROJECT_ENV) .venv/bin/python scripts/build_sbom.py --check

release-layout-check: ## validate bundle metadata and the explicit runtime-file allowlist
	$(PROJECT_ENV) .venv/bin/python scripts/build_release.py --check

release-bundle: build release-layout-check ## build the checksummed standalone Linux archive
	$(PROJECT_ENV) .venv/bin/python scripts/build_release.py
	$(PROJECT_ENV) .venv/bin/python scripts/build_sbom.py

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
