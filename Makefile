PREDATOR ?= predator
DIR ?= ~/assistant

dev:
	.venv/bin/uvicorn web.app:app --reload

deploy:
	ssh $(PREDATOR) 'cd $(DIR) && git pull && docker compose up -d --build && sleep 5 && curl -fs localhost/api/personas > /dev/null && echo deployed'

verify:
	ssh $(PREDATOR) 'cd $(DIR) && ./scripts/verify-isolation.sh'

logs:
	ssh $(PREDATOR) 'cd $(DIR) && docker compose logs -f --tail 100'
