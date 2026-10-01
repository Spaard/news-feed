import sqlite3

import numpy as np

from news_feed import db, stories

NOW = "2026-10-01T12:00:00+00:00"
LONG_AGO = "2026-08-01T12:00:00+00:00"


def add_article(
    conn: sqlite3.Connection,
    url: str,
    *,
    source: str = "Le Monde",
    lang: str = "fr",
    title: str = "Titre",
    published_at: str = NOW,
    une: bool = False,
    focus: bool = False,
) -> sqlite3.Row:
    article = db.NewArticle(
        url=url,
        source=source,
        lang=lang,
        title=title,
        summary="",
        published_at=published_at,
        une=une,
        paywall=False,
        focus=focus,
    )
    db.insert_articles(conn, [article], NOW)
    return conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def story_of(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT url, story_id FROM articles"))


def test_assign_groups_close_vectors_across_languages(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    fr = add_article(conn, "https://lemonde/seisme", title="Séisme au Japon")
    en = add_article(
        conn, "https://bbc/quake", source="BBC", lang="en", title="Earthquake in Japan"
    )
    other = add_article(conn, "https://lemonde/budget", title="Budget voté")

    stories.assign(
        conn, [fr, en, other], np.stack([unit(1, 0, 0), unit(0.95, 0.3, 0), unit(0, 0, 1)])
    )

    story = story_of(conn)
    assert (
        story["https://lemonde/seisme"]
        == story["https://bbc/quake"]
        != story["https://lemonde/budget"]
    )
    size = conn.execute("SELECT size FROM stories WHERE id = ?", (story["https://bbc/quake"],))
    assert size.fetchone()[0] == 2


def test_assign_is_stricter_for_specialized_sources(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    first = add_article(conn, "https://thn/citrix", source="The Hacker News", focus=True)
    general = add_article(conn, "https://lemonde/citrix")
    specialized = add_article(conn, "https://thn/sharepoint", source="The Hacker News", focus=True)
    # Similarité ≈ 0,66 avec la story : assez pour un généraliste, pas pour une source spécialisée.
    vectors = np.stack([unit(1, 0, 0), unit(0.66, 0.75, 0), unit(0.66, 0, 0.75)])

    stories.assign(conn, [first, specialized, general], vectors)

    story = story_of(conn)
    assert story["https://lemonde/citrix"] == story["https://thn/citrix"]
    assert story["https://thn/sharepoint"] != story["https://thn/citrix"]


def test_assign_does_not_reopen_inactive_stories(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    old = add_article(conn, "https://lemonde/old", published_at="2026-09-27T12:00:00+00:00")
    stories.assign(conn, [old], np.stack([unit(1, 0)]))

    new = add_article(conn, "https://lemonde/new")
    stories.assign(conn, [new], np.stack([unit(1, 0)]))

    assert len(set(story_of(conn).values())) == 2


def test_story_tags_keeps_tags_of_a_third_of_articles_and_the_most_frequent():
    assert stories.story_tags(
        [{"international"}] * 3 + [{"international", "economie"}] * 2 + [{"sport"}]
    ) == ["economie", "international"]
    assert stories.story_tags([{"sport"}, set(), set(), set()]) == ["sport"]
    assert stories.story_tags([set()]) == []


def test_save_tags_marks_articles_and_recomputes_story_tags(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    first = add_article(conn, "https://lemonde/a")
    second = add_article(conn, "https://bbc/a", source="BBC", lang="en")
    stories.assign(conn, [first, second], np.stack([unit(1, 0), unit(1, 0)]))

    stories.save_tags(
        conn, {first["id"]: ["international"], second["id"]: ["international", "economie"]}
    )

    tags = conn.execute("SELECT tag FROM story_tags ORDER BY tag").fetchall()
    assert [row["tag"] for row in tags] == ["economie", "international"]
    assert conn.execute("SELECT count(*) FROM articles WHERE tagged = 0").fetchone()[0] == 0


def test_overview_combines_distinct_summaries_until_long_enough(monkeypatch):
    first = {"une": True, "published_at": NOW, "summary": "Résumé court."}
    second = {"une": False, "published_at": NOW, "summary": "Un autre angle, détaillé."}
    duplicate = {"une": False, "published_at": NOW, "summary": "Résumé court."}
    empty = {"une": False, "published_at": NOW, "summary": ""}

    monkeypatch.setattr(stories, "OVERVIEW_MIN_CHARS", 20)
    assert stories.overview([second, first]) == "Résumé court. Un autre angle, détaillé."

    # Le premier chapô (à la une) atteint déjà la longueur visée : pas besoin des autres.
    monkeypatch.setattr(stories, "OVERVIEW_MIN_CHARS", 10)
    assert stories.overview([first, second]) == "Résumé court."
    # Les chapôs identiques ou vides ne sont pas répétés.
    assert stories.overview([first, duplicate, empty]) == "Résumé court."


def test_representative_prefers_une_then_earliest():
    earliest = {"une": 0, "published_at": "2026-10-01T08:00:00+00:00"}
    later_une = {"une": 1, "published_at": "2026-10-01T10:00:00+00:00"}

    assert stories.representative([earliest, later_une]) is later_une
    assert stories.representative([{"une": 0, "published_at": NOW}, earliest]) is earliest


def test_display_prefers_native_then_translation_then_original():
    fr = {"lang": "fr", "une": 0, "published_at": NOW, "title": "Séisme au Japon", "summary": "FR"}
    en = {
        "lang": "en",
        "une": 0,
        "published_at": NOW,
        "title": "Earthquake in Japan",
        "summary": "EN",
    }
    story = {
        "title_fr": "Tremblement de terre",
        "summary_fr": "",
        "title_en": None,
        "summary_en": None,
    }

    assert stories.display(story, [en, fr], "fr") == ("Séisme au Japon", "FR", False)
    assert stories.display(story, [en], "fr") == ("Tremblement de terre", "", True)
    assert stories.display({**story, "title_fr": None}, [en], "fr") == (
        "Earthquake in Japan",
        "EN",
        False,
    )


def test_untranslated_lists_noteworthy_stories_missing_a_language(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    bbc = add_article(
        conn, "https://bbc/quake", source="BBC", lang="en", title="Earthquake in Japan"
    )
    guardian = add_article(
        conn,
        "https://guardian/quake",
        source="The Guardian",
        lang="en",
        title="Japan quake",
        une=True,
    )
    minor = add_article(conn, "https://bbc/minor", source="BBC", lang="en", title="Minor news")
    stories.assign(conn, [bbc, guardian, minor], np.stack([unit(1, 0), unit(1, 0), unit(0, 1)]))

    [(story_id, title, summary)] = stories.untranslated(conn, "fr")
    assert (title, summary) == ("Japan quake", "")
    assert stories.untranslated(conn, "en") == []

    stories.save_translations(conn, "fr", {story_id: ("Séisme au Japon", "")})
    assert stories.untranslated(conn, "fr") == []


def test_purge_drops_old_minor_content_and_clears_inactive_centroids(tmp_path):
    conn = db.connect(tmp_path / "news.db")
    minor = add_article(
        conn, "https://lemonde/minor", title="Fait divers ancien", published_at=LONG_AGO
    )
    big_fr = add_article(conn, "https://lemonde/big", published_at=LONG_AGO)
    big_en = add_article(conn, "https://bbc/big", source="BBC", lang="en", published_at=LONG_AGO)
    recent = add_article(conn, "https://lemonde/recent", title="Fait divers récent")
    stories.assign(
        conn,
        [minor, big_fr, big_en, recent],
        np.stack([unit(0, 1), unit(1, 0), unit(1, 0), unit(0, 1)]),
    )
    add_article(conn, "https://lemonde/unassigned", published_at=LONG_AGO)

    stories.purge(conn, NOW)

    assert set(story_of(conn)) == {
        "https://lemonde/big",
        "https://bbc/big",
        "https://lemonde/recent",
    }
    assert (
        conn.execute("SELECT count(*) FROM stories WHERE centroid IS NOT NULL").fetchone()[0] == 1
    )
    matches = conn.execute("SELECT count(*) FROM articles_fts WHERE articles_fts MATCH 'ancien'")
    assert matches.fetchone()[0] == 0
