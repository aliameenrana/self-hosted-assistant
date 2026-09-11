# Self-hosted assistant

A chatbot with five personas, tool use, and a hard rule against claiming a tool
result it never received. Runs on a GTX 1060 6GB.

Design docs live in `docs/`. Start with `docs/architecture.md`.

## Layout

```
core/        harness: tool loop, validation, personas. No web dependency.
web/         FastAPI app, SSE streaming, static frontend
scripts/     setup, model download, isolation verification
docs/        design
```

## Quick start (Mac, development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./scripts/download-model.sh
brew install llama.cpp && llama-server -m models/*.gguf --port 8080 &
cp .env.example .env
uvicorn web.app:app --reload
```

## Predator (production)

See `docs/deploy.md`. Order matters: isolation verification passes before the
tunnel goes public.
