import sqlite3

from news_feed import db
from tests.helpers import NOW, add_story, article

SINCE = "2026-10-01T00:00:00+00:00"


def at(hour: int) -> str:
    return f"2026-10-01T{hour:02d}:00:00+00:00"


def ids(rows: list[sqlite3.Row]) -> list[int]:
    return [row["id"] for row in rows]


def test_connect_applies_migrations_once(tmp_path):
    path = tmp_path / "news.db"
    db.connect(path).close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_known_url_is_not_duplicated_but_inherits_une(tmp_path):
    conn = db.connect(tmp_path / "news.db")

    assert (
        db.insert_articles(conn, [article("https://ex.com/a"), article("https://ex.com/b")], NOW)
        == 2
    )
    assert db.insert_articles(conn, [article("https://ex.com/a", une=True)], NOW) == 0

    rows = conn.execute("SELECT url, une FROM articles ORDER BY url").fetchall()
    assert [tuple(row) for row in rows] == [("https://ex.com/a", 1), ("https://ex.com/b", 0)]


def test_list_stories_ranks_filters_and_searches(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    quake = add_story(
        conn,
        article(
            "https://lemonde/seisme",
            source="Le Monde",
            title="Séisme au Japon",
            published_at=at(8),
            une=True,
        ),
        article(
            "https://bbc/quake",
            source="BBC",
            lang="en",
            title="Earthquake in Japan",
            published_at=at(9),
        ),
        tags=("international",),
    )
    budget = add_story(
        conn,
        article(
            "https://fi/budget", source="France Info", title="Budget voté", published_at=at(10)
        ),
        article(
            "https://20min/budget",
            source="20 Minutes",
            title="Le budget adopté",
            published_at=at(11),
        ),
        article("https://rfi/budget", source="RFI", title="Budget", published_at=at(7)),
        tags=("politique", "economie"),
    )
    model = add_story(
        conn,
        article(
            "https://decoder/model",
            source="The Decoder",
            lang="en",
            title="New model",
            published_at=at(12),
            focus=True,
        ),
        tags=("ia",),
    )
    add_story(
        conn,
        article("https://fi/minor", source="France Info", title="Fait divers", published_at=at(10)),
    )
    old = add_story(
        conn,
        article(
            "https://lemonde/old",
            source="Le Monde",
            published_at="2026-09-20T08:00:00+00:00",
            une=True,
        ),
    )

    # Score = médias distincts + médias qui l'ont mise à la une ; un seul média : écartée.
    assert ids(db.list_stories(conn, since=SINCE)) == [budget, quake, model]
    assert ids(db.list_stories(conn, since=SINCE, sort="recent")) == [model, budget, quake]
    assert ids(db.list_stories(conn, since=SINCE, tags=["international", "ia"])) == [quake, model]
    assert ids(db.list_stories(conn, since=SINCE, query="seisme")) == [quake]
    assert ids(db.list_stories(conn, since=SINCE, query="japan")) == [quake]
    zone = db.list_stories(conn, since=SINCE, tags=["politique", "ia"], zone="economie")
    assert ids(zone) == [budget]
    [page] = db.list_stories(conn, since=SINCE, limit=1, offset=1)
    assert (page["id"], page["total"]) == (quake, 3)
    assert old in ids(db.list_stories(conn, since="2026-09-01T00:00:00+00:00"))

    # Seuls les articles de la période comptent.
    [first, second] = db.list_stories(conn, since=at(10))
    assert (first["id"], first["sources"], first["score"]) == (budget, 2, 2)
    assert second["id"] == model


def test_list_stories_accepts_any_search_text(tmp_path):
    conn = db.connect(tmp_path / "news.db")

    assert db.list_stories(conn, since=SINCE, query='AND OR "( NEAR -') == []
