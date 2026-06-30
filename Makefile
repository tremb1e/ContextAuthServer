.PHONY: build build-server-image up down restart logs shell health ready sample-ingest backup prune-data clean-data test test-server test-e2e test-docker test-load test-all server-test e2e docker-test

build:
	docker compose build

build-server-image:
	docker build -t contextauthlab/server:$${VERSION:-latest} .

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart contextauthlab-server

logs:
	docker compose logs -f contextauthlab-server

shell:
	docker compose exec contextauthlab-server sh

health:
	curl -fsS http://127.0.0.1:8000/health

ready:
	curl -fsS http://127.0.0.1:8000/ready

sample-ingest:
	python tools/send_sample_batch.py --server http://127.0.0.1:8000

backup:
	mkdir -p backups
	tar -czf backups/contextauthlab-$$(date +%Y%m%d-%H%M%S).tar.gz data/paper logs

prune-data:
	@echo "Manual retention review only; prune tool is not shipped in this prototype."

clean-data:
	rm -rf data/paper logs

test:
	PYTHONPATH=. pytest -q tests

test-server: test

test-e2e:
	bash tools/test_e2e.sh

test-docker:
	bash tools/test_docker_deployment.sh

test-load:
	python tools/test_load.py --iterations 60 --interval 5 --devices 50

test-all: test-server test-e2e test-docker

server-test: test-server
e2e: test-e2e
docker-test: test-docker
