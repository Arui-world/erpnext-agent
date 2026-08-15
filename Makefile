.PHONY: install check test run compose-config up rebuild restart logs ps down recreate eval eval-online

install:
	uv sync --frozen

check:
	uv run ruff check .
	uv run mypy src

test:
	uv run pytest

eval:
	uv run python -m erpnext_agent.evaluation.runner

# Runs the authenticated online suite inside the compose network (requires the stack
# to be up and EVAL_ONLINE_* variables set in .env, with both users logged in via OAuth).
eval-online:
	docker compose --profile tools run --rm eval

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
	
recreate:
	docker compose up -d --no-deps --force-recreate agent
