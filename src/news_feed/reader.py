"""Texte des articles en mode lecture : extrait de la page une fois, puis gardé en base."""

import asyncio
import logging
import sqlite3
from collections.abc import Sequence

import httpx2
import trafilatura

log = logging.getLogger(__name__)

MAX_CHARS = 12_000


async def contents(
    conn: sqlite3.Connection, http: httpx2.AsyncClient, articles: Sequence[sqlite3.Row]
) -> dict[int, str]:
    """Texte de chaque article ('' si la page est illisible : payante, bloquée, vide…)."""

    async def extract(article: sqlite3.Row) -> str | None:
        """Texte extrait, '' si la page n'en contient pas, None si l'échec est passager."""
        try:
            response = await http.get(article["url"])
        except httpx2.HTTPError as exc:
            log.warning("page injoignable (%s) : %s", article["url"], exc)
            return None
        if response.status_code >= 500:
            return None
        if response.status_code >= 400:
            return ""
        text = await asyncio.to_thread(
            trafilatura.extract, response.text, url=article["url"], favor_precision=True
        )
        return (text or "")[:MAX_CHARS]

    pending = [article for article in articles if article["content"] is None]
    extracted = await asyncio.gather(*(extract(article) for article in pending))
    results = list(zip(pending, extracted, strict=True))
    with conn:
        conn.executemany(
            "UPDATE articles SET content = ? WHERE id = ?",
            [(text, article["id"]) for article, text in results if text is not None],
        )
    found = {article["id"]: text or "" for article, text in results}
    return {a["id"]: found.get(a["id"], a["content"] or "") for a in articles}
