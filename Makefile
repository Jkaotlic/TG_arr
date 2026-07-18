.PHONY: help install dev test lint format typecheck run docker-build docker-up docker-down docker-logs docker-restart deploy rollback check-base-image clean

# Default target
help:
	@echo "TG_arr Bot - Available commands:"
	@echo ""
	@echo "  make install      - Install production dependencies"
	@echo "  make dev          - Install development dependencies"
	@echo "  make test         - Run tests"
	@echo "  make test-cov     - Run tests with coverage"
	@echo "  make lint         - Run linter (ruff)"
	@echo "  make format       - Format code (ruff)"
	@echo "  make typecheck    - Run mypy on bot/ (not wired into lint)"
	@echo "  make run          - Run bot locally"
	@echo "  make docker-build - Build Docker image"
	@echo "  make docker-up    - Start with Docker Compose"
	@echo "  make docker-down  - Stop Docker Compose"
	@echo "  make docker-logs  - Show Docker logs"
	@echo "  make docker-restart - Restart the bot container"
	@echo "  make deploy       - Build, tag previous image as :prev, up -d, ps"
	@echo "  make rollback     - Retag :prev back to :latest and up -d"
	@echo "  make check-base-image - Inspect current python:3.12-slim digest"
	@echo "  make clean        - Clean up generated files"

# Install dependencies
install:
	pip install -r requirements.txt

# Install development dependencies
# DEP-06: install both the editable package (for `bot` on the path) and the
# exact dev pins from requirements-dev.txt, instead of resolving `[dev]`
# ranges fresh each time (which can silently drift from the pinned toolchain
# as new ruff/pytest releases land inside the declared range).
dev:
	pip install -e . -r requirements-dev.txt

# Run tests
test:
	pytest tests/ -v

# Run tests with coverage
test-cov:
	pytest tests/ -v --cov=bot --cov-report=html --cov-report=term-missing

# Run linter
lint:
	ruff check bot/ tests/

# Format code
format:
	ruff format bot/ tests/
	ruff check --fix bot/ tests/

# DEP-03: mypy is a declared dev dependency but was never wired into any
# make target. Deliberately NOT part of `make lint` — bot/ isn't fully
# clean under mypy yet and fixing that is out of scope here.
typecheck:
	mypy bot/

# Run bot locally
run:
	python -m bot.main

# Docker commands
docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f tg-arr-bot

docker-restart:
	docker compose restart tg-arr-bot

# DEPLOY-02: codified deploy path for the Pi (previously done by hand:
# ssh -> git pull -> build -> up). Build happens BEFORE tagging :prev and
# before up -d, so a broken build fails fast without touching the running
# container; the previous :latest is preserved as :prev for `make rollback`.
# IMAGE must match the `image:` set in docker-compose.yml.
IMAGE := tg-arr-bot:latest
IMAGE_PREV := tg-arr-bot:prev

deploy:
	docker compose build
	-docker tag $(IMAGE) $(IMAGE_PREV)
	docker compose up -d --wait --wait-timeout 180 || { docker compose logs --tail 100 tg-arr-bot; exit 1; }
	docker compose ps

rollback:
	docker tag $(IMAGE_PREV) $(IMAGE)
	docker compose up -d --wait --wait-timeout 180 || { docker compose logs --tail 100 tg-arr-bot; exit 1; }
	docker compose ps

# DEP-08: the base image is pinned by digest (see Dockerfile) for
# reproducible builds, which means Debian/OS security patches only land on a
# manual refresh — there's no renovate/dependabot in this repo. Run monthly
# (or when a base-image CVE is announced), compare the digest to the one
# pinned in Dockerfile, and bump both `FROM` lines if it changed.
check-base-image:
	docker buildx imagetools inspect python:3.12-slim

# Clean up
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info/ 2>/dev/null || true
