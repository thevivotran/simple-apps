.PHONY: all lint fmt test run image

all: lint test

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .
	ruff check --fix .

test:
	python3 -m unittest discover -s tests -t . -v

# Local run with a throwaway data dir and a dev-only token.
run:
	mkdir -p data
	DATA_DIR=./data PORT=8080 ADMIN_TOKEN=dev-token-dev-token-dev-token python3 -m simple_apps

image:
	podman build -t simple-apps:dev .
