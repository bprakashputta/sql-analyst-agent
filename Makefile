.PHONY: install lint fmt test data demo-data serve up down eval eval-dev report safety fidelity

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -q

demo-data:
	uv run sql-analyst demo-data

data: demo-data
	uv run python -m sql_analyst.evals download

serve: demo-data
	uv run sql-analyst serve

up:
	docker compose up --build -d --wait
	@echo "open http://localhost:8000"

down:
	docker compose down

# Reproduce the reported numbers on the 400-question test split (needs ANTHROPIC_API_KEY; ~$1).
eval: data
	for c in baseline full_schema_selfcorrect agent agent_forced_linking; do \
		LLM_CACHE=false uv run python -m sql_analyst.evals run --config $$c --split test; done
	LLM_CACHE=false uv run python -m sql_analyst.evals run --config agent_sonnet --split test --limit 100
	uv run python -m sql_analyst.evals report

eval-dev: data
	uv run python -m sql_analyst.evals run --config agent --split dev

report:
	uv run python -m sql_analyst.evals report

safety: demo-data
	uv run python -m sql_analyst.evals safety

fidelity: data
	uv run python -m sql_analyst.evals.fidelity
