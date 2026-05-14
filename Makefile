# Strathon dev shortcuts.
# All targets are thin wrappers over the underlying tools; they exist so
# new contributors don't have to memorize flag combinations.

.PHONY: help up down logs reset test test-sdk test-receiver test-integration \
        demo-langgraph demo-crewai demo-openai-agents

help:
	@echo "Strathon dev commands:"
	@echo ""
	@echo "  make up                    Start Postgres + receiver (docker compose)"
	@echo "  make down                  Stop everything"
	@echo "  make logs                  Tail receiver logs"
	@echo "  make reset                 Stop, wipe Postgres volume, restart fresh"
	@echo ""
	@echo "  make test                  Run all tests (SDK + receiver + integration)"
	@echo "  make test-sdk              SDK unit tests only"
	@echo "  make test-receiver         Receiver tests only"
	@echo "  make test-integration      Cross-framework parity tests"
	@echo ""
	@echo "  make demo-langgraph        Run the LangGraph block demo"
	@echo "  make demo-crewai           Run the CrewAI block demo"
	@echo "  make demo-openai-agents    Run the OpenAI Agents SDK block demo"

# ---- docker compose lifecycle ----

up:
	docker compose up -d
	@echo ""
	@echo "Started. Watching receiver logs until the quickstart banner..."
	@docker compose logs -f receiver | sed '/Strathon receiver ready/q' || true
	@echo ""
	@echo "Receiver ready. Run 'make logs' to keep tailing, or 'make down' to stop."

down:
	docker compose down

logs:
	docker compose logs -f receiver

reset:
	docker compose down -v
	docker compose up -d

# ---- tests ----

test: test-sdk test-receiver test-integration

test-sdk:
	cd sdk && pytest tests/

test-receiver:
	cd receiver && pytest tests/

test-integration:
	pytest tests/

# ---- demos ----

demo-langgraph:
	python examples/intervention_demo.py

demo-crewai:
	python examples/crewai_intervention_demo.py

demo-openai-agents:
	python examples/openai_agents_intervention_demo.py
