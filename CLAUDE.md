# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Personal news aggregator meant to be the user's only news source: RSS feeds (FR + EN, every topic, deep dives on AI and cybersecurity) grouped into multi-source stories, tagged, readable in FR or EN, with AI context and follow-up questions (GPT models on Microsoft Foundry, `openai` SDK). Hosted on Azure Container Apps first, later on a Synology NAS, with the same Docker image. The roadmap is in the README; the user communicates in French, and user-facing text (UI, README, log/CLI messages) is French.

## Commands

Always go through `uv`; never call `pip`, `python -m venv`, or edit `.venv` by hand.

```bash
uv sync                                             # install/refresh env from uv.lock
uv run news-feed fetch                              # one refresh cycle into $NEWS_FEED_DATA_DIR (default ./data)
uv run --env-file .env news-feed fetch              # same, with AI enrichment (.env from .env.example)
uv run --env-file .env news-feed serve              # web app on http://127.0.0.1:8000 + background refresh
uv run pytest                                       # full suite (no network: fixtures + httpx2.MockTransport)
uv run pytest tests/test_ingest.py::test_parse_atom # single test
uv run ruff check --fix . && uv run ruff format .   # lint + format
git config core.hooksPath .githooks                 # once per clone: pre-push runs the CI "check" job
```

Image smoke test (same as the CI `image` job), required when the Dockerfile or dependencies change:

```bash
docker build -t news-feed . && docker run -d --name smoke -p 8000:8000 -e NEWS_FEED_REFRESH_MINUTES=0 news-feed
curl -f http://localhost:8000/healthz; docker rm -f smoke
```

Commit `uv.lock` alongside any `pyproject.toml` dependency change.

## Architecture

- `src/news_feed/sources.toml` is the catalog: feeds (`source` = outlet name, so several rubric feeds of one outlet count as one source; flags `une` / `paywall` / `focus`) and the fixed tag taxonomy. `config.load_catalog()` parses it into frozen dataclasses (unknown keys fail); `tests/test_config.py` validates it. Verify a new feed with a real `uv run news-feed fetch` before adding it.
- `ingest.run_cycle()` is one full refresh, used by `news-feed fetch` and by the web app's background loop: `refresh()` → `enrich()` (only if `ai.client()` is configured; `openai.OpenAIError` is logged, not raised) → `stories.purge()`.
  - `refresh()` fetches all feeds concurrently (conditional GET with ETag/Last-Modified kept in the `feeds` table). `parse_feed()` normalizes entries: tracking params stripped from URLs, plain-text summaries, UTC dates, entries older than 7 days dropped. `db.insert_articles()` dedupes by URL; a known URL inherits the `une`/`focus` flags.
  - `enrich()` is resumable: each stage selects its pending work from the DB (`story_id IS NULL` → embeddings + `stories.assign()`; `tagged = 0` → `ai.tag_articles()` + `stories.save_tags()`; `stories.untranslated()` → `ai.translate_titles()`), so an interrupted stage simply runs again next cycle.
- `stories.py` owns the story model. Assignment is incremental cosine similarity against centroids of stories active in the last 48 h, with a stricter threshold for `focus` articles (specialized outlets all sound alike); both thresholds are calibrated on real FR + EN headlines for the `embed` model; article embeddings are never stored, only story centroids (cleared once inactive). A story's display text in a language is: native articles (title from `representative()`: une first, then earliest; overview from `overview()`: that article's summary, extended with other native articles' distinct summaries up to `OVERVIEW_MIN_CHARS`, no AI) > stored translation > original article. "Noteworthy" (≥ 2 outlets, or une, or focus) is the single SQL rule `db.NOTEWORTHY`, shared by listing, translation and retention.
- `ai.py` is the only module talking to Microsoft Foundry (OpenAI-compatible v1 endpoint, `openai` SDK configured by `OPENAI_BASE_URL` / `OPENAI_API_KEY`). Models are addressed by Foundry deployment name: `embed`, `fast` (bulk: tags, title translations, via `_in_batches()` with Pydantic structured outputs; a failed batch is logged and retried next cycle; a batch rejected by Azure's content filter is split in halves down to the offending article) and `main` (on demand: summaries, article translations, briefs, and the `chat()` tool-calling loop capped at `MAX_TOOL_ROUNDS`). A filtered or empty answer raises `ai.Unavailable`; the web layer turns it (and `openai.OpenAIError`) into an `?error=1` notice. On-demand outputs are cached in `ai_cache` under `ai.cache_key()`. The only automatic `main` calls are refreshes of syntheses the user asked for (see below).
- "Dig deeper" features: `reader.contents()` extracts article text with trafilatura once and stores it (`''` = unreadable, kept; transient failures are not stored). `wiki.search()` queries the public Wikipedia API. `followup._answer()` builds a story's context and runs the model with the `search_wikipedia` / `search_news` tools; sources get thread-stable numbers in `chat_sources` (story articles first, then pages found by the tools), and the model gets the free articles' text. `followup.ask()` stores a question and its answer in the thread (`chat_messages`). `followup.synthesize()` writes the detailed synthesis to `syntheses`, apart from the thread, with the story's article count. The server loop calls `followup.refresh_syntheses()` after each cycle, which redoes a synthesis once its story has grown by `REFRESH_GROWTH` (and at least `REFRESH_MIN_NEW` articles). Stories with a synthesis or a thread are never purged. In all cases, and citations `[n]` are rendered as links by `web.rich_text()` (plain text in, escaped HTML out; prompts ask for no Markdown).
- `db.py` uses plain `sqlite3` (no ORM). The schema is the `MIGRATIONS` list, applied through `PRAGMA user_version`. Production is deployed: only append migrations, never edit an existing one. Keep the default journal mode (no WAL): production data lives on Azure Files. Full-text search is an external-content FTS5 table kept in sync by triggers (cascade deletes included). `db.list_stories()` computes score/filters per period at query time.
- `web.create_app()` builds the FastAPI app around a single SQLite connection (`check_same_thread=False`): every route is `async def`, so the connection is only ever used from the event loop, never concurrently. The lifespan runs `run_cycle()` every `refresh_minutes` (the *Actualiser* button, `POST /refresh`, starts one now; both share one `asyncio.Lock`, so cycles never overlap), first after `FIRST_REFRESH_DELAY` (so the previous ACA revision has stopped before we write to the shared Azure Files DB). Pages are server-rendered Jinja templates (`templates/`, `static/`), no JS build, and must stay usable at phone width (the main use; check at 390 px); UI strings live in `i18n.TEXTS`, the language comes from the `lang` cookie. The `guard` middleware lets only `NEWS_FEED_ALLOWED_USER` through, except `PUBLIC_PATHS` (`/healthz`, `/static/`: a phone fetches the PWA manifest and icons without cookies, and Easy Auth's `excludedPaths` in the Bicep lets those files through too), and rejects cross-site POSTs (`Sec-Fetch-Site`). `github_login()` reads the GitHub login from Easy Auth's `X-MS-CLIENT-PRINCIPAL` claims (`urn:github:login`): on Container Apps, `X-MS-CLIENT-PRINCIPAL-NAME` is empty for the GitHub provider, confirmed live against a real deployment. Actions are plain HTML forms with POST-redirect-GET (form bodies parsed with `urllib.parse`, no `python-multipart`); a shared `httpx2.AsyncClient` (injectable transport for tests) serves the reader and Wikipedia.
- Deployment: `Dockerfile` (uv multi-stage, non-root uid 1000), `infra/main.bicep` (Container App with 1 fixed replica, Azure Files volume mounted `nobrl`, Easy Auth GitHub), `.github/workflows/ci.yml` (`check` → `image` build + smoke test, published to GHCR on `main` → `deploy` via OIDC when the repo's `AZURE_*` variables exist).
- The only HTTP library is `httpx2` (the `openai` SDK's own, also used by Starlette's `TestClient`); never add `httpx`.
- Timestamps are ISO 8601 UTC strings (`...+00:00`), compared lexicographically in SQL and Python.
- Tests: `tests/conftest.py` provides a `foundry` fixture, a fake Foundry behind the real `openai` SDK (via `httpx2.MockTransport`); `tests/helpers.py` builds articles and stories; `test_ingest.py` freezes `ingest.now` because fixtures have fixed dates, and web tests use timestamps relative to the real clock.

## Working rules

- **Minimal, no overbuilding.** Only write what the task needs. No speculative abstractions, config options, wrapper classes or "future-proof" layers.
- **Fix at the source.** When something is wrong, correct the original function directly. Do not add a helper/wrapper/post-processing function that patches the output of a broken one.
- **Tests every time.** For each change, write or update tests under `tests/` covering the new behavior. Tests never hit the network.
- **Local checks = CI.** Before declaring any change done, run `uv run ruff check . && uv run ruff format --check . && uv run pytest`: exactly what the pre-push hook and the CI `check` job run. If a change touches the container image (Dockerfile, dependencies, `uv.lock`), also build and smoke-test the image locally. If a change requires updating `.github/workflows/`, update it in the same change.
- **Docs always up to date.** User docs are French and split in three: `README.md` (what the app is, local quick start, configuration, development, roadmap: keep it short), `docs/architecture.md` (how it works end to end: request path, pipeline, AI, login, secrets, CI/CD, costs) and `docs/deploiement.md` (Azure setup step by step, day-to-day operations, troubleshooting). Any change to features, commands, configuration (env vars), the catalog format, CI/CD or deployment updates the relevant one in the same change, and this file too when commands or architecture change.
