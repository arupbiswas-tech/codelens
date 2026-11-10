.PHONY: up down logs test lint-fix reset ingest eval eval-tune eval-no-rerank eval-review demo

up:        ## Build and start the app + database
	docker compose up --build -d

down:      ## Stop everything (keeps the database volume)
	docker compose down

logs:      ## Follow app logs
	docker compose logs -f app

test:      ## Run ruff + pytest inside Docker, with a real database for the SQL tests
	docker compose run --rm --build test

lint-fix:  ## Auto-fix lint issues inside Docker
	docker build --target test -t codelens-test .
	docker run --rm codelens-test ruff check . --fix

reset:     ## Stop and DELETE the database volume (re-runs db/init.sql)
	docker compose down -v

ingest:    ## Clone + chunk a repo and print a summary: make ingest URL=https://github.com/owner/repo
	@test -n "$(URL)" || (echo "Usage: make ingest URL=https://github.com/owner/repo" && exit 1)
	docker compose run --rm --no-deps app python -m codelens.ingestion $(URL)

eval:      ## Measure retrieval + answer quality (needs GROQ_API_KEY). Extra flags: make eval ARGS="--resume FILE"
	docker compose --profile eval run --rm --build eval $(ARGS)

eval-tune: ## Same, plus the chunk-size / candidate-count experiments (slower)
	docker compose --profile eval run --rm --build eval --tune $(ARGS)

eval-no-rerank: ## Answer from the merged search order, to A/B test the reranker
	docker compose --profile eval run --rm --build eval --no-rerank $(ARGS)

eval-review: ## Measure the PR reviewer on seeded-bug diffs (needs GROQ_API_KEY)
	docker compose --profile eval run --rm --build --entrypoint python eval -m codelens.evaluation.reviews $(ARGS)

demo:      ## Drive the web UI in a headless browser, check it, save screenshots. ARGS=--no-llm | --gif
	docker compose up -d --build
	docker run --rm --network codelens_default -v "$(CURDIR):/work" -w /work mcr.microsoft.com/playwright/python:v1.49.0-noble sh -c "pip install -q playwright==1.49.0 pillow && python tools/demo.py $(ARGS)"
