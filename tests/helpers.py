"""Construction de données de test."""

import json
import sqlite3
from pathlib import Path

import httpx2

from news_feed import db

NOW = "2026-10-01T12:00:00+00:00"
FIXTURES = Path(__file__).parent / "fixtures"


def article(
    url: str,
    *,
    source: str = "Exemple",
    lang: str = "fr",
    title: str = "Titre",
    published_at: str = NOW,
    une: bool = False,
    focus: bool = False,
    paywall: bool = False,
) -> db.NewArticle:
    return db.NewArticle(
        url=url,
        source=source,
        lang=lang,
        title=title,
        summary="",
        published_at=published_at,
        une=une,
        paywall=paywall,
        focus=focus,
    )


def add_story(
    conn: sqlite3.Connection, *articles: db.NewArticle, tags: tuple[str, ...] = ()
) -> int:
    db.insert_articles(conn, articles, NOW)
    with conn:
        story_id = conn.execute(
            "INSERT INTO stories (size, last_seen_at) VALUES (?, ?)", (len(articles), NOW)
        ).lastrowid
        conn.executemany(
            "UPDATE articles SET story_id = ? WHERE url = ?", [(story_id, a.url) for a in articles]
        )
        conn.executemany(
            "INSERT INTO story_tags (story_id, tag) VALUES (?, ?)", [(story_id, t) for t in tags]
        )
    return story_id


def add_to_story(conn: sqlite3.Connection, story_id: int, *articles: db.NewArticle) -> None:
    """Rattache de nouveaux articles à une story existante, comme le ferait une relève."""
    db.insert_articles(conn, articles, NOW)
    with conn:
        conn.executemany(
            "UPDATE articles SET story_id = ? WHERE url = ?", [(story_id, a.url) for a in articles]
        )


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def tool_call(name: str, query: str, call_id: str = "call_1") -> dict:
    """Appel d'outil tel que le renvoie le modèle principal."""
    arguments = json.dumps({"query": query})
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


WIKIPEDIA_PAGE = {
    "batchcomplete": True,
    "query": {
        "pages": [
            {
                "pageid": 1,
                "ns": 0,
                "title": "Guerre russo-ukrainienne",
                "fullurl": "https://fr.wikipedia.org/wiki/Guerre_russo-ukrainienne",
                "extract": "La guerre russo-ukrainienne oppose la Russie et l'Ukraine depuis 2014.",
            }
        ]
    },
}


def web_pages(request):
    """Faux web : Wikipédia répond WIKIPEDIA_PAGE, tout autre site sert la page d'article."""
    if request.url.host.endswith("wikipedia.org"):
        return httpx2.Response(200, json=WIKIPEDIA_PAGE)
    return httpx2.Response(200, content=fixture("article.html"))
