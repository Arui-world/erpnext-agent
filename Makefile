.PHONY: install check test run compose-config up rebuild restart logs ps down

install:
	uv sync --frozen

check:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest

run:
	uv run uvicorn erpnext_agent.main:app --reload --host 0.0.0.0 --port 8001

compose-config:
	docker compose --env-file .env config --quiet

up:
	docker compose up -d

rebuild:
	docker compose up --build -d

restart:
	docker compose restart agent

logs:
	docker compose logs -f agent

ps:
	docker compose ps

down:
	docker compose down
