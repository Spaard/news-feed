"""Relève des flux RSS/Atom et enrichissement des articles (stories, tags, traductions)."""

import asyncio
import html
import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx2
import openai

from news_feed import ai, db, stories
from news_feed.config import Catalog, Feed, Tag

log = logging.getLogger(__name__)

USER_AGENT = "news-feed (+https://github.com/Spaard/news-feed)"
FETCH_TIMEOUT = 20.0
MAX_CONCURRENT_FETCHES = 10
SUMMARY_MAX_CHARS = 600
# Certains flux exposent toute leur archive : on ne garde que l'actualité récente.
MAX_ENTRY_AGE = timedelta(days=7)
TRACKING_PARAM = re.compile(r"^(utm_.*|xtor|at_medium|at_campaign)$")


@dataclass(frozen=True)
class FeedResult:
    feed: Feed
    new: int = 0
    not_modified: bool = False
    error: str | None = None


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_url(url: str) -> str:
    """Retire les paramètres de suivi et le fragment, pour qu'une même page ait une seule URL."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAM.match(k)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def clean_text(value: str, max_chars: int | None = None) -> str:
    """Texte brut à partir d'un fragment HTML, tronqué proprement si besoin."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


def parse_feed(content: bytes, feed: Feed, fetched_at: str) -> list[db.NewArticle]:
    parsed = feedparser.parse(content)
    if not parsed.entries:
        raise ValueError(f"aucun article lisible ({parsed.get('bozo_exception') or 'flux vide'})")
    oldest = (datetime.fromisoformat(fetched_at) - MAX_ENTRY_AGE).isoformat(timespec="seconds")
    articles = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link")
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime(*stamp[:6], tzinfo=UTC).isoformat(timespec="seconds") if stamp else ""
        if not title or not link or (published and published < oldest):
            continue
        articles.append(
            db.NewArticle(
                url=clean_url(link),
                source=feed.source,
                lang=feed.lang,
                title=title,
                summary=clean_text(entry.get("summary", ""), SUMMARY_MAX_CHARS),
                # Sans date, ou avec une date dans le futur : on retient l'heure de la relève.
                published_at=min(published, fetched_at) if published else fetched_at,
                une=feed.une,
                paywall=feed.paywall,
                focus=feed.focus,
            )
        )
    return articles


async def fetch_feed(
    client: httpx2.AsyncClient, conn: sqlite3.Connection, feed: Feed
) -> FeedResult:
    state = db.feed_state(conn, feed.url)
    etag = state["etag"] if state else None
    last_modified = state["last_modified"] if state else None
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    fetched_at = now()
    try:
        response = await client.get(feed.url, headers=headers)
        if response.status_code == 304:
            db.save_feed_state(
                conn,
                feed.url,
                etag=etag,
                last_modified=last_modified,
                fetched_at=fetched_at,
                error=None,
            )
            return FeedResult(feed, not_modified=True)
        response.raise_for_status()
        articles = parse_feed(response.content, feed, fetched_at)
    except (httpx2.HTTPError, ValueError) as exc:
        error = str(exc) or type(exc).__name__
        log.warning("%s (%s) : %s", feed.source, feed.url, error)
        db.save_feed_state(
            conn,
            feed.url,
            etag=etag,
            last_modified=last_modified,
            fetched_at=fetched_at,
            error=error,
        )
        return FeedResult(feed, error=error)
    new = db.insert_articles(conn, articles, fetched_at)
    db.save_feed_state(
        conn,
        feed.url,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        fetched_at=fetched_at,
        error=None,
    )
    return FeedResult(feed, new=new)


async def refresh(
    conn: sqlite3.Connection,
    feeds: Iterable[Feed],
    transport: httpx2.AsyncBaseTransport | None = None,
) -> list[FeedResult]:
    """Relève tous les flux en parallèle (les écritures SQLite restent dans ce thread)."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    async with httpx2.AsyncClient(
        transport=transport,
        timeout=FETCH_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:

        async def fetch(feed: Feed) -> FeedResult:
            async with semaphore:
                return await fetch_feed(client, conn, feed)

        return await asyncio.gather(*(fetch(feed) for feed in feeds))


async def enrich(conn: sqlite3.Connection, tags: Sequence[Tag], client: openai.AsyncOpenAI) -> None:
    """Rattache les nouveaux articles aux stories, les classe et traduit les titres manquants.

    Chaque étape reprend ce qui reste à faire en base : une étape interrompue est reprise à la
    relève suivante.
    """
    pending = conn.execute(
        "SELECT id, title, summary, published_at, focus FROM articles WHERE story_id IS NULL"
    ).fetchall()
    if pending:
        vectors = await ai.embed(client, [f"{a['title']}\n{a['summary']}" for a in pending])
        stories.assign(conn, pending, vectors)
    untagged = conn.execute(
        "SELECT id, title, summary FROM articles WHERE tagged = 0 AND story_id IS NOT NULL"
    ).fetchall()
    if untagged:
        stories.save_tags(conn, await ai.tag_articles(client, [tuple(a) for a in untagged], tags))
    for lang in ai.LANGUAGES:
        missing = stories.untranslated(conn, lang)
        if missing:
            stories.save_translations(conn, lang, await ai.translate_titles(client, missing, lang))


async def run_cycle(
    conn: sqlite3.Connection,
    catalog: Catalog,
    client: openai.AsyncOpenAI | None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> list[FeedResult]:
    """Une relève complète : flux, enrichissement IA (si configurée), rétention."""
    results = await refresh(conn, catalog.feeds, transport)
    if client is None:
        log.warning(
            "IA non configurée (OPENAI_API_KEY absent) : ni stories, ni tags, ni traductions"
        )
    else:
        try:
            await enrich(conn, catalog.tags, client)
        except openai.OpenAIError as exc:
            log.error("enrichissement IA interrompu (repris à la prochaine relève) : %s", exc)
    stories.purge(conn, now())
    return results
