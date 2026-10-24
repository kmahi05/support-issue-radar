# Support Issue Radar

Reads every incoming support ticket and groups tickets about the same underlying problem into one *issue*.
Issues are ranked by business impact (customers, ARR), spikes raise alerts, and spikes are linked to the release that likely caused them.
Full product spec: [PRD.md](PRD.md). Build plan: [TASKS.md](TASKS.md). Agent conventions: [CLAUDE.md](CLAUDE.md).

## Prerequisites
- PostgreSQL 16 on `localhost:5432` (any install method), superuser `postgres` / `postgres` for dev.
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for the API on first `uv sync`).
- Node.js 20+ and npm.
- A Gemini API key (`GOOGLE_API_KEY`) once the LLM tasks land; the boilerplate and tests do not need one.

## Setup (once, in order)
```
cp .env.example api/.env          # then fill APP_ENCRYPTION_KEY (command in the file)
cp web/.env.example web/.env.local
cd api
uv sync
uv run python ../scripts/db_setup.py      # roles sir_owner/sir_app/sir_worker + databases
uv run python ../scripts/migrate.py       # applies db/migrations/*.sql
cd ../web
npm install
```
`db_setup.py` and `migrate.py` read `PG_SUPERUSER_URL` / `MIGRATE_DATABASE_URL` from the environment and fall back to the dev defaults above.

## Run
- API (port 8003): `cd api; uv run uvicorn app.main:app --reload --port 8003`
- Web (port 3003): `cd web; npm run dev`, then open http://localhost:3003, register, and create an organization.

## Test and lint
```
cd api
uv run pytest -q                     # recreates support_radar_test; DB tests skip if Postgres is down
uv run ruff check . ../scripts
uv run ruff format --check . ../scripts
cd ../web
npm run lint
npm run typecheck
npm run build
```

## License
MIT, see [LICENSE](LICENSE).
