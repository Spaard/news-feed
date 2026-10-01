import asyncio

import httpx2

from news_feed import db, reader
from tests.helpers import add_story, article, fixture


def stored(conn):
    return conn.execute("SELECT * FROM articles").fetchone()


def test_contents_extracts_the_article_once(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    add_story(conn, article("https://site.example/offensive"))
    calls = []

    def page(request):
        calls.append(request.url)
        return httpx2.Response(200, content=fixture("article.html"))

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(page))
    [text] = asyncio.run(reader.contents(conn, http, [stored(conn)])).values()
    [again] = asyncio.run(reader.contents(conn, http, [stored(conn)])).values()

    assert "premier paragraphe" in text
    assert "troisième paragraphe" in text
    assert "Menu Monde" not in text
    assert again == text
    assert len(calls) == 1


def test_contents_remembers_unreadable_pages_but_retries_outages(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    add_story(conn, article("https://site.example/offensive"))
    responses = iter([httpx2.Response(503), httpx2.Response(403)])

    def page(request):
        return next(responses)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(page))

    assert asyncio.run(reader.contents(conn, http, [stored(conn)])) == {1: ""}
    assert stored(conn)["content"] is None  # panne passagère : on réessaiera
    assert asyncio.run(reader.contents(conn, http, [stored(conn)])) == {1: ""}
    assert stored(conn)["content"] == ""  # page refusée : on ne réessaie plus
