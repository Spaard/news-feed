"""Interface web : liste des stories filtrable, story et ses questions de suivi, lecture des
articles, brief, bascule FR/EN, relève en fond."""

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Annotated
from urllib.parse import parse_qs, urlencode

import httpx2
import openai
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from news_feed import ai, db, followup, ingest, reader, stories
from news_feed.config import Catalog, Settings
from news_feed.i18n import LANGS, TEXTS

log = logging.getLogger(__name__)

PERIODS = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
# Filtre France / Monde : zone → tag de story exigé.
ZONES = {"france": "france", "monde": "international"}
PAGE_SIZE = 30
BRIEF_STORIES = 20
PACKAGE = files("news_feed")
# Lors d'un déploiement ACA, l'ancienne révision tourne encore quelques secondes à côté de la
# nouvelle : on attend qu'elle soit arrêtée avant d'écrire dans la base partagée (Azure Files).
FIRST_REFRESH_DELAY = 60
CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def lang_of(request: Request) -> str:
    lang = request.cookies.get("lang")
    return lang if lang in LANGS else LANGS[0]


def ago(iso: str, texts: Mapping[str, str]) -> str:
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds() // 60)
    if minutes < 60:
        return texts["ago_minutes"].format(n=max(minutes, 1))
    if minutes < 48 * 60:
        return texts["ago_hours"].format(n=minutes // 60)
    return texts["ago_days"].format(n=minutes // (24 * 60))


def card(story: sqlite3.Row, articles: Sequence[sqlite3.Row], tags: list[str], lang: str) -> dict:
    """Ce qu'il faut pour afficher une story dans `lang`."""
    title, summary, translated = stories.display(story, articles, lang)
    outlets = list(dict.fromkeys(article["source"] for article in articles))
    texts = TEXTS[lang]
    count = (
        texts["sources_one"] if len(outlets) == 1 else texts["sources_many"].format(n=len(outlets))
    )
    return {
        "id": story["id"],
        "title": title,
        "summary": summary,
        "translated": translated,
        "outlets": outlets,
        "outlets_label": count,
        "tags": tags,
        "last_at": max(article["published_at"] for article in articles),
    }


def with_query(request: Request, **changes: object) -> str:
    """Query string relative de la page courante, avec `changes` (None retire le paramètre)."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in changes]
    params += [(k, str(v)) for k, v in changes.items() if v is not None]
    return "?" + urlencode(params)


def rich_text(text: str, sources: Mapping[int, tuple[str, str]]) -> Markup:
    """Texte d'une réponse IA en HTML : paragraphes, listes à tirets, citations [n] en liens."""

    def cite(line: str) -> str:
        def link(match: re.Match) -> str:
            numbers = [int(n) for n in match[1].split(",")]
            if not all(n in sources for n in numbers):
                return match[0]
            return "".join(
                f'<a class="cite" href="{escape(sources[n][1])}" title="{escape(sources[n][0])}">'
                f"[{n}]</a>"
                for n in numbers
            )

        return CITATION.sub(link, str(escape(line)))

    blocks = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if lines and all(line.startswith("- ") for line in lines):
            blocks.append(
                "<ul>" + "".join(f"<li>{cite(line[2:])}</li>" for line in lines) + "</ul>"
            )
        elif lines:
            blocks.append("<p>" + "<br>".join(cite(line) for line in lines) + "</p>")
    return Markup("".join(blocks))


def local_path(target: str) -> str:
    """`target` s'il désigne une page de l'app, sinon l'accueil : « //site » et « /\\site »
    mèneraient vers un autre site."""
    return target if target.startswith("/") and not target.startswith(("//", "/\\")) else "/"


def page_numbers(page: int, pages: int) -> list[int | None]:
    """Numéros de page à afficher : la première, la dernière et les voisines de `page` ;
    None marque un saut."""
    shown = sorted({1, pages, *range(page - 2, page + 3)} & set(range(1, pages + 1)))
    numbers: list[int | None] = []
    for number in shown:
        if numbers and number - numbers[-1] > 1:
            numbers.append(None)
        numbers.append(number)
    return numbers


def last_answer(thread: Sequence[Mapping], question: str) -> str | None:
    """Réponse du fil à la dernière occurrence de `question`, posée mot pour mot (presets)."""
    answer, asked = None, False
    for message in thread:
        if message["role"] == "user":
            asked = message["content"] == question
        elif asked:
            answer = message["content"]
            asked = False
    return answer


def selection(
    period: str, sort: str, q: str, tags: Sequence[str], zone: str, known: Mapping
) -> dict:
    """Filtres de la liste, nettoyés, et le début de la période."""
    period = period if period in PERIODS else "day"
    return {
        "period": period,
        "sort": sort if sort in db.SORTS else "top",
        "q": q,
        "tags": [slug for slug in tags if slug in known],
        "zone": zone if zone in ZONES else "",
        "since": (datetime.now(UTC) - PERIODS[period]).isoformat(timespec="seconds"),
    }


def create_app(
    settings: Settings,
    catalog: Catalog,
    client: openai.AsyncOpenAI | None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    conn = db.connect(settings.db_path)
    http = httpx2.AsyncClient(
        transport=transport,
        timeout=ingest.FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": ingest.USER_AGENT},
    )
    tags = {tag.slug: tag for tag in catalog.tags}
    templates = Jinja2Templates(directory=str(PACKAGE / "templates"))
    templates.env.globals.update(ago=ago, with_query=with_query, periods=PERIODS)
    templates.env.filters["rich"] = rich_text

    cycle = asyncio.Lock()  # une seule relève à la fois, périodique ou manuelle
    manual: set[asyncio.Task] = set()  # tâches des relèves manuelles, gardées jusqu'à leur fin

    async def refresh_once() -> None:
        async with cycle:
            try:
                await ingest.run_cycle(conn, catalog, client)
            except Exception:
                log.exception("relève en échec")

    async def refresh_forever() -> None:
        await asyncio.sleep(FIRST_REFRESH_DELAY)
        while True:
            await refresh_once()
            await asyncio.sleep(settings.refresh_minutes * 60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(refresh_forever()) if settings.refresh_minutes > 0 else None
        yield
        if task:
            task.cancel()
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(PACKAGE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Derrière Easy Auth (ACA), n'importe quel compte GitHub peut se connecter : on ne laisse
        # passer que le propriétaire (login GitHub, insensible à la casse). Les sondes de santé
        # arrivent sans authentification.
        user = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "")
        if (
            settings.allowed_user
            and request.url.path != "/healthz"
            and user.lower() != settings.allowed_user.lower()
        ):
            return PlainTextResponse("Accès refusé", status_code=403)
        # Les actions (payantes) ne partent que des pages de l'app, pas d'un autre site.
        if request.method == "POST" and request.headers.get("Sec-Fetch-Site") not in (
            None,
            "same-origin",
            "none",
        ):
            return PlainTextResponse("Requête d'un autre site refusée", status_code=403)
        return await call_next(request)

    def render(request: Request, template: str, **context: object):
        lang = lang_of(request)
        context |= {
            "lang": lang,
            "t": TEXTS[lang],
            "tags": tags,
            "zones": ZONES,
            "zone_of": {tag: zone for zone, tag in ZONES.items()},
            "ai_enabled": client is not None,
        }
        return templates.TemplateResponse(request, template, context)

    async def form(request: Request) -> dict[str, list[str]]:
        return parse_qs((await request.body()).decode(), keep_blank_values=True)

    def article_or_404(article_id: int) -> sqlite3.Row:
        article = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if article is None:
            raise HTTPException(status_code=404)
        return article

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/lang/{lang}")
    async def switch_language(lang: str, next: str = "/"):
        response = RedirectResponse(local_path(next), status_code=303)
        if lang in LANGS:
            response.set_cookie("lang", lang, max_age=365 * 24 * 3600, samesite="lax")
        return response

    @app.get("/")
    async def index(
        request: Request,
        period: str = "day",
        sort: str = "top",
        q: str = "",
        tag: Annotated[list[str], Query()] = [],  # noqa: B006 (FastAPI copie la valeur par défaut)
        zone: str = "",
        page: int = 1,
        brief: str = "",
        error: int = 0,
    ):
        chosen = selection(period, sort, q, tag, zone, tags)
        page = max(page, 1)
        rows = db.list_stories(
            conn,
            since=chosen["since"],
            tags=chosen["tags"],
            zone=ZONES.get(chosen["zone"]),
            sort=chosen["sort"],
            query=q,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        total = rows[0]["total"] if rows else 0
        loaded = db.load_stories(conn, [row["id"] for row in rows])
        updated = conn.execute("SELECT max(fetched_at) FROM feeds").fetchone()[0]
        return render(
            request,
            "index.html",
            stories=[card(*story, lang_of(request)) for story in loaded],
            period=chosen["period"],
            sort=chosen["sort"],
            q=q,
            selected=chosen["tags"],
            zone=chosen["zone"],
            page=page,
            total=total,
            page_numbers=page_numbers(page, -(-total // PAGE_SIZE)),
            updated=updated,
            refreshing=cycle.locked(),
            brief=load_brief(brief),
            error=error,
        )

    @app.post("/refresh")
    async def refresh(request: Request):
        if not cycle.locked():
            task = asyncio.create_task(refresh_once())
            manual.add(task)
            task.add_done_callback(manual.discard)
        back = (await form(request)).get("next", ["/"])[0]
        return RedirectResponse(local_path(back), status_code=303)

    def load_brief(key: str) -> dict | None:
        """Brief mis en cache sous `key` : son texte et ses sources (numéro → titre, lien)."""
        try:
            brief = json.loads(db.cache_get(conn, key) or "null") if key else None
        except json.JSONDecodeError:  # clé d'une autre sortie IA
            return None
        if not isinstance(brief, dict):
            return None
        brief["sources"] = {i: tuple(s) for i, s in enumerate(brief["sources"], 1)}
        return brief

    @app.post("/brief")
    async def make_brief(request: Request):
        fields = await form(request)
        chosen = selection(
            fields.get("period", ["day"])[0],
            fields.get("sort", ["top"])[0],
            fields.get("q", [""])[0],
            fields.get("tag", []),
            fields.get("zone", [""])[0],
            tags,
        )
        lang = lang_of(request)
        filters = [(k, chosen[k]) for k in ("period", "sort", "q", "zone") if chosen[k]]
        filters += [("tag", slug) for slug in chosen["tags"]]
        rows = db.list_stories(
            conn,
            since=chosen["since"],
            tags=chosen["tags"],
            zone=ZONES.get(chosen["zone"]),
            sort=chosen["sort"],
            query=chosen["q"],
            limit=BRIEF_STORIES,
        )
        cards = [card(*story, lang) for story in db.load_stories(conn, [r["id"] for r in rows])]
        numbered = "\n\n".join(
            f"[{i}] {c['title']} ({c['outlets_label']})\n{c['summary']}"
            for i, c in enumerate(cards, 1)
        )
        key = ai.cache_key("brief", lang, numbered)
        if db.cache_get(conn, key) is None:
            try:
                if client is None or not cards:
                    raise ai.Unavailable("IA non configurée" if client is None else "aucune story")
                text = await ai.brief(client, numbered, lang)
            except (openai.OpenAIError, ai.Unavailable) as exc:
                log.warning("brief indisponible : %s", exc)
                return RedirectResponse("/?" + urlencode([*filters, ("error", 1)]), status_code=303)
            sources = [(c["title"], f"/stories/{c['id']}") for c in cards]
            db.cache_put(conn, key, json.dumps({"text": text, "sources": sources}), ingest.now())
        return RedirectResponse("/?" + urlencode([*filters, ("brief", key)]), status_code=303)

    @app.get("/stories/{story_id}")
    async def story_page(request: Request, story_id: int, error: int = 0):
        loaded = db.load_stories(conn, [story_id])
        if not loaded:
            raise HTTPException(status_code=404)
        story, articles, story_tags = loaded[0]
        lang = lang_of(request)
        thread = followup.thread(conn, story_id, lang)
        return render(
            request,
            "story.html",
            articles=articles,
            story=card(story, articles, story_tags, lang),
            thread=thread,
            synthesis=last_answer(thread, TEXTS[lang]["preset_sources"]),
            sources=followup.sources(conn, story_id, lang),
            error=error,
        )

    @app.post("/stories/{story_id}/ask")
    async def ask(request: Request, story_id: int):
        if not db.load_stories(conn, [story_id]):
            raise HTTPException(status_code=404)
        question = (await form(request)).get("question", [""])[0].strip()
        if question:
            try:
                if client is None:
                    raise ai.Unavailable("IA non configurée")
                await followup.ask(conn, client, http, story_id, lang_of(request), question)
            except (openai.OpenAIError, ai.Unavailable) as exc:
                log.warning("question sans réponse : %s", exc)
                return RedirectResponse(f"/stories/{story_id}?error=1#chat", status_code=303)
        return RedirectResponse(f"/stories/{story_id}#chat", status_code=303)

    @app.get("/articles/{article_id}")
    async def article_page(request: Request, article_id: int, error: int = 0):
        article = article_or_404(article_id)
        text = (await reader.contents(conn, http, [article]))[article_id]
        lang = lang_of(request)
        cached = {
            kind: db.cache_get(conn, ai.cache_key(kind, lang, text)) if text else None
            for kind in ("summary", "translation")
        }
        return render(
            request,
            "article.html",
            article=article,
            text=text,
            summary=cached["summary"],
            translation=cached["translation"] if article["lang"] != lang else None,
            error=error,
        )

    @app.post("/articles/{article_id}/{kind}")
    async def article_ai(request: Request, article_id: int, kind: str):
        if kind not in ("summary", "translation"):
            raise HTTPException(status_code=404)
        article = article_or_404(article_id)
        text = (await reader.contents(conn, http, [article]))[article_id]
        lang = lang_of(request)
        key = ai.cache_key(kind, lang, text)
        if db.cache_get(conn, key) is None:
            try:
                if client is None or not text:
                    raise ai.Unavailable(
                        "IA non configurée" if client is None else "texte illisible"
                    )
                value = await (
                    ai.summarize_article(client, article["title"], text, lang)
                    if kind == "summary"
                    else ai.translate_article(client, text, lang)
                )
            except (openai.OpenAIError, ai.Unavailable) as exc:
                log.warning("%s indisponible : %s", kind, exc)
                return RedirectResponse(f"/articles/{article_id}?error=1", status_code=303)
            db.cache_put(conn, key, value, ingest.now())
        return RedirectResponse(f"/articles/{article_id}", status_code=303)

    return app
