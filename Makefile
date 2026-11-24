.PHONY: all lint fmt test run image skill

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

# The app-authoring skill (skill/simple-apps-app): refresh its copies of the docs,
# install it for Claude Code and build the zip to upload in claude.ai chat.
SKILL_DIR := skill/simple-apps-app
skill:
	mkdir -p $(SKILL_DIR)/references dist
	cp APP_AUTHORING.md ARCHITECTURE.md $(SKILL_DIR)/references/
	rm -rf $(HOME)/.claude/skills/simple-apps-app
	mkdir -p $(HOME)/.claude/skills
	cp -r $(SKILL_DIR) $(HOME)/.claude/skills/
	find $(HOME)/.claude/skills/simple-apps-app -name __pycache__ -prune -exec rm -rf {} +
	rm -f dist/simple-apps-app.zip
	cd skill && zip -qr ../dist/simple-apps-app.zip simple-apps-app -x '*/__pycache__/*'
	@echo "Claude Code: installed to ~/.claude/skills/simple-apps-app"
	@echo "Chat: upload dist/simple-apps-app.zip in claude.ai > Settings > Capabilities > Skills"
