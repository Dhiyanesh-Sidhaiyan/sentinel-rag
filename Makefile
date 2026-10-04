.PHONY: install dev-key run test lint eval demo demo-remote up down logs image k8s-up k8s-forward k8s-status k8s-logs k8s-down k8s-prod

PY ?= .venv/bin/python

install:            ## create venv + install deps
	uv venv --python 3.12 .venv && uv pip install --python $(PY) -e ".[dev]"

dev-key:            ## generate an API key (hash goes to .env) + local postgres password
	$(PY) scripts/gen_api_key.py --tenant orbitly

run:                ## run API locally with in-memory backends (hot reload)
	$(PY) -m uvicorn --factory app.main:create_app --reload --port 8000

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check app tests scripts && $(PY) -m mypy app

eval:               ## RAG quality gate (fails on regression)
	$(PY) scripts/evaluate.py

demo:               ## ingest sample docs + run sample queries in-process -> data/sample_outputs
	$(PY) scripts/demo.py

demo-remote:        ## same, against the docker compose stack (export API_KEY first)
	$(PY) scripts/demo.py --base-url http://localhost:8000

up:
	docker compose up --build -d
down:
	docker compose down -v
logs:
	docker compose logs -f api

image:
	docker build -t sentinel-rag:1.0.0 .

k8s-up:             ## build + deploy full stack to local Kubernetes and wait until healthy
	scripts/k8s_local.sh up
k8s-forward:        ## expose the API on http://localhost:8000
	scripts/k8s_local.sh forward
k8s-status:
	scripts/k8s_local.sh status
k8s-logs:
	scripts/k8s_local.sh logs
k8s-down:           ## delete the local deployment (namespace + volumes)
	scripts/k8s_local.sh down
k8s-prod:
	kubectl apply -k k8s/overlays/prod
