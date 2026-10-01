import asyncio
from pathlib import Path

import httpx2
import pytest

from news_feed import db, ingest
from news_feed.config import Catalog, Feed, load_catalog

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = "2026-10-01T12:00:00+00:00"
FEED = Feed(url="https://example.com/rss.xml", source="Exemple", lang="fr", une=True)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Les fixtures ont des dates fixes : la relève doit se croire le jour de FETCHED_AT."""
    monkeypatch.setattr(ingest, "now", lambda: FETCHED_AT)


def test_parse_rss_normalizes_entries():
    articles = ingest.parse_feed(fixture("rss.xml"), FEED, FETCHED_AT)

    assert [a.title for a in articles] == [
        "Premier & titre",
        "Article sans date",
        "Article daté du futur",
    ]
    first = articles[0]
    assert first.url == "https://example.com/article-1?id=42"
    assert first.summary == "Texte avec du gras et des entités & co."
    assert first.published_at == "2026-10-01T08:30:00+00:00"
    assert (first.source, first.lang, first.une, first.paywall, first.focus) == (
        "Exemple",
        "fr",
        True,
        False,
        False,
    )


def test_parse_missing_or_future_date_uses_fetch_time():
    articles = ingest.parse_feed(fixture("rss.xml"), FEED, FETCHED_AT)

    assert articles[1].published_at == FETCHED_AT
    assert articles[2].published_at == FETCHED_AT


def test_parse_skips_entries_older_than_a_week():
    titles = [a.title for a in ingest.parse_feed(fixture("rss.xml"), FEED, FETCHED_AT)]

    assert "Article d'archive" not in titles


def test_parse_atom():
    [article] = ingest.parse_feed(fixture("atom.xml"), FEED, FETCHED_AT)

    assert article.url == "https://example.org/atom-1"
    assert article.summary == "Résumé simple."
    assert article.published_at == "2026-10-01T09:00:00+00:00"


def test_parse_rejects_non_feed_content():
    with pytest.raises(ValueError):
        ingest.parse_feed(b"<html><body>Pas un flux</body></html>", FEED, FETCHED_AT)


def test_clean_text_truncates_on_word_boundary():
    assert ingest.clean_text("un deux trois quatre", max_chars=10) == "un deux…"


def test_refresh_stores_articles_then_uses_conditional_get(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    requests = []

    def handler(request):
        requests.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx2.Response(304)
        return httpx2.Response(200, content=fixture("rss.xml"), headers={"ETag": '"v1"'})

    transport = httpx2.MockTransport(handler)
    [first] = asyncio.run(ingest.refresh(conn, [FEED], transport))
    [second] = asyncio.run(ingest.refresh(conn, [FEED], transport))

    assert first.new == 3
    assert second.not_modified
    assert requests[0].headers["User-Agent"] == ingest.USER_AGENT
    assert conn.execute("SELECT count(*) FROM articles").fetchone()[0] == 3


def test_refresh_error_is_recorded_and_keeps_cache_headers(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    responses = iter(
        [
            httpx2.Response(200, content=fixture("rss.xml"), headers={"ETag": '"v1"'}),
            httpx2.Response(503),
            httpx2.Response(304),
        ]
    )
    sent_etags = []

    def handler(request):
        sent_etags.append(request.headers.get("If-None-Match"))
        return next(responses)

    transport = httpx2.MockTransport(handler)
    ok, failed = (asyncio.run(ingest.refresh(conn, [FEED], transport))[0] for _ in range(2))
    assert ok.new == 3
    assert failed.error
    assert db.feed_state(conn, FEED.url)["error"] == failed.error

    [recovered] = asyncio.run(ingest.refresh(conn, [FEED], transport))
    assert recovered.not_modified
    assert db.feed_state(conn, FEED.url)["error"] is None
    assert sent_etags == [None, '"v1"', '"v1"']


def test_enrich_groups_tags_and_translates(tmp_path, foundry):
    conn = db.connect(tmp_path / "news.db")
    common = {"summary": "", "published_at": FETCHED_AT, "paywall": False}
    db.insert_articles(
        conn,
        [
            db.NewArticle(
                url="https://lemonde/seisme",
                source="Le Monde",
                lang="fr",
                title="Séisme au Japon",
                une=True,
                focus=False,
                **common,
            ),
            db.NewArticle(
                url="https://bbc/quake",
                source="BBC",
                lang="en",
                title="Earthquake in Japan",
                une=False,
                focus=False,
                **common,
            ),
            db.NewArticle(
                url="https://decoder/model",
                source="The Decoder",
                lang="en",
                title="New AI model",
                une=False,
                focus=True,
                **common,
            ),
        ],
        FETCHED_AT,
    )
    foundry.embed = lambda texts: [[1.0, 0.0] if "Jap" in text else [0.0, 1.0] for text in texts]

    def chat(system, payload):
        if system.startswith("Tu classes"):
            return {"articles": [{"id": item["id"], "tags": ["international"]} for item in payload]}
        return {
            "articles": [
                {"id": item["id"], "title": "Nouveau modèle d'IA", "summary": ""}
                for item in payload
            ]
        }

    foundry.chat = chat

    asyncio.run(ingest.enrich(conn, load_catalog().tags, foundry.client()))

    story = dict(conn.execute("SELECT url, story_id FROM articles"))
    assert (
        story["https://lemonde/seisme"]
        == story["https://bbc/quake"]
        != story["https://decoder/model"]
    )
    assert conn.execute("SELECT count(*) FROM articles WHERE tagged = 0").fetchone()[0] == 0
    # Seule la story sans article français est traduite, et seulement en français.
    translated = conn.execute(
        "SELECT id, title_fr, title_en FROM stories WHERE title_fr IS NOT NULL"
    )
    assert [tuple(row) for row in translated] == [
        (story["https://decoder/model"], "Nouveau modèle d'IA", None)
    ]


def test_run_cycle_keeps_articles_when_ai_fails_or_is_missing(tmp_path, foundry):
    catalog = Catalog(feeds=(FEED,), tags=())
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, content=fixture("rss.xml"))
    )

    def unavailable(texts):
        raise httpx2.ConnectError("Foundry indisponible")

    foundry.embed = unavailable
    for client in (foundry.client(), None):
        conn = db.connect(tmp_path / f"{client is None}.db")

        [result] = asyncio.run(ingest.run_cycle(conn, catalog, client, transport))

        assert result.new == 3
        assert (
            conn.execute("SELECT count(*) FROM articles WHERE story_id IS NULL").fetchone()[0] == 3
        )
