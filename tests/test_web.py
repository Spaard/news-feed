import base64
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import httpx2
from fastapi.testclient import TestClient

from news_feed import db, ingest, web
from news_feed.config import Settings, load_catalog
from tests.helpers import add_story, article, web_pages

EARLIER = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
RECENT = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")


def make_app(tmp_path, client=None, **settings) -> tuple[TestClient, sqlite3.Connection]:
    config = Settings(data_dir=tmp_path, **{"refresh_minutes": 0, **settings})
    app = web.create_app(config, load_catalog(), client, httpx2.MockTransport(web_pages))
    return TestClient(app), db.connect(config.db_path)


def principal(login: str) -> str:
    """X-MS-CLIENT-PRINCIPAL tel qu'Easy Auth (ACA) le formate pour le fournisseur GitHub :
    X-MS-CLIENT-PRINCIPAL-NAME reste vide, le login est un claim de ce principal."""
    claims = {"claims": [{"typ": "urn:github:login", "val": login}]}
    return base64.b64encode(json.dumps(claims).encode()).decode()


def add_quake(conn: sqlite3.Connection) -> int:
    return add_story(
        conn,
        article(
            "https://lemonde/seisme",
            source="Le Monde",
            title="Séisme au Japon",
            published_at=EARLIER,
            une=True,
        ),
        article(
            "https://bbc/quake",
            source="BBC",
            lang="en",
            title="Earthquake in Japan",
            published_at=RECENT,
        ),
        tags=("international",),
    )


def add_model(conn: sqlite3.Connection) -> int:
    story_id = add_story(
        conn,
        article(
            "https://decoder/model",
            source="The Decoder",
            lang="en",
            title="New AI model",
            published_at=RECENT,
            focus=True,
        ),
        tags=("ia",),
    )
    with conn:
        conn.execute("UPDATE stories SET title_fr = 'Nouveau modèle IA' WHERE id = ?", (story_id,))
    return story_id


def test_healthz(tmp_path):
    client, _ = make_app(tmp_path)

    assert client.get("/healthz").text == "ok"


def test_index_is_french_by_default_and_switches_to_english(tmp_path):
    client, conn = make_app(tmp_path)
    add_quake(conn)

    page = client.get("/")
    assert "Séisme au Japon" in page.text
    assert "Earthquake in Japan" not in page.text
    assert "2 médias" in page.text

    switched = client.get("/lang/en?next=/", follow_redirects=False)
    assert (switched.status_code, switched.headers["location"]) == (303, "/")
    english = client.get("/")
    assert '<html lang="en">' in english.text
    assert "Earthquake in Japan" in english.text


def test_story_without_native_article_shows_its_translation(tmp_path):
    client, conn = make_app(tmp_path)
    add_model(conn)

    page = client.get("/").text

    assert "Nouveau modèle IA" in page
    assert "traduit" in page


def test_index_filters_by_tag_and_full_text_search(tmp_path):
    client, conn = make_app(tmp_path)
    add_quake(conn)
    add_model(conn)

    by_tag = client.get("/?tag=ia").text
    assert "Nouveau modèle IA" in by_tag
    assert "Séisme au Japon" not in by_tag
    by_search = client.get("/?q=seisme").text
    assert "Séisme au Japon" in by_search
    assert "Nouveau modèle IA" not in by_search


def add_budget(conn: sqlite3.Connection) -> int:
    return add_story(
        conn,
        article(
            "https://lemonde/budget",
            source="Le Monde",
            title="Budget voté",
            published_at=RECENT,
            une=True,
        ),
        tags=("politique", "france"),
    )


def test_index_filters_france_or_world_and_counts_the_stories(tmp_path):
    client, conn = make_app(tmp_path)
    add_quake(conn)
    add_budget(conn)

    assert "2 sujets" in client.get("/").text
    france = client.get("/?zone=france").text
    assert "Budget voté" in france
    assert "Séisme au Japon" not in france
    world = client.get("/?zone=monde").text
    assert "Séisme au Japon" in world
    assert "Budget voté" not in world
    # Le tag « France » d'une carte mène au filtre France, pas à un tag caché.
    assert "zone=france" in france


def test_index_shows_page_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_SIZE", 1)
    client, conn = make_app(tmp_path)
    add_quake(conn)
    add_model(conn)
    add_budget(conn)

    page = client.get("/?page=2").text

    assert '<span class="current" aria-current="page">2</span>' in page
    assert 'href="?page=1">1</a>' in page
    assert 'href="?page=3">3</a>' in page


def test_page_numbers_keep_the_ends_and_the_neighbours():
    assert web.page_numbers(1, 1) == [1]
    assert web.page_numbers(1, 4) == [1, 2, 3, 4]
    assert web.page_numbers(6, 12) == [1, None, 4, 5, 6, 7, 8, None, 12]
    assert web.page_numbers(1, 0) == []


def test_manual_refresh_runs_a_cycle_and_returns_to_the_page(tmp_path, monkeypatch):
    done = threading.Event()

    async def fake_cycle(conn, catalog, client):
        done.set()
        return []

    monkeypatch.setattr(ingest, "run_cycle", fake_cycle)
    client, _ = make_app(tmp_path)

    with client:
        response = client.post("/refresh", data={"next": "/?period=week"}, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/?period=week")
        assert done.wait(timeout=5)


def test_story_page_lists_every_source(tmp_path):
    client, conn = make_app(tmp_path)
    story_id = add_quake(conn)

    page = client.get(f"/stories/{story_id}").text

    assert 'href="https://lemonde/seisme"' in page
    assert 'href="https://bbc/quake"' in page
    assert 'href="#chat"' in page
    assert client.get("/stories/999").status_code == 404


def test_language_switch_refuses_external_redirects(tmp_path):
    client, _ = make_app(tmp_path)

    for target in ("//evil.example", "/%5Cevil.example", "https://evil.example"):
        response = client.get(f"/lang/en?next={target}", follow_redirects=False)
        assert response.headers["location"] == "/", target


def test_only_the_owner_gets_through(tmp_path):
    client, _ = make_app(tmp_path, allowed_user="spaard")

    def status(login: str | None) -> int:
        headers = {"X-MS-CLIENT-PRINCIPAL": principal(login)} if login else {}
        return client.get("/", headers=headers).status_code

    assert client.get("/").status_code == 403
    assert status("intrus") == 403
    assert status("spaard") == 200
    assert status("Spaard") == 200
    # X-MS-CLIENT-PRINCIPAL-NAME est vide pour le fournisseur GitHub sur Container Apps :
    # le vérifier directement ne doit donc pas suffire à passer.
    assert client.get("/", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "spaard"}).status_code == 403
    assert client.get("/", headers={"X-MS-CLIENT-PRINCIPAL": "pas du base64"}).status_code == 403
    assert client.get("/healthz").status_code == 200


def test_background_refresh_starts_with_the_app(tmp_path, monkeypatch):
    started = threading.Event()

    async def fake_cycle(conn, catalog, client):
        started.set()
        return []

    monkeypatch.setattr(ingest, "run_cycle", fake_cycle)
    monkeypatch.setattr(web, "FIRST_REFRESH_DELAY", 0)
    client, _ = make_app(tmp_path, refresh_minutes=20)

    with client:
        assert started.wait(timeout=5)


def article_id(conn: sqlite3.Connection, url: str) -> int:
    return conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()[0]


def test_article_page_shows_the_text_and_summarizes_on_demand(tmp_path, foundry):
    foundry.main = lambda body: {"content": "Le résumé."}
    client, conn = make_app(tmp_path, foundry.client())
    add_quake(conn)
    page_url = f"/articles/{article_id(conn, 'https://lemonde/seisme')}"

    page = client.get(page_url).text
    assert "premier paragraphe" in page
    assert "Résumer</button>" in page

    response = client.post(f"{page_url}/summary", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, page_url)
    client.post(f"{page_url}/summary")  # déjà en cache : pas de nouvel appel
    page = client.get(page_url).text
    assert "Le résumé." in page
    assert "Résumer</button>" not in page
    assert len(foundry.requests) == 1


def test_foreign_article_can_be_translated(tmp_path, foundry):
    foundry.main = lambda body: {"content": "Traduction française."}
    client, conn = make_app(tmp_path, foundry.client())
    add_quake(conn)
    page_url = f"/articles/{article_id(conn, 'https://bbc/quake')}"

    assert "Traduire</button>" in client.get(page_url).text
    client.post(f"{page_url}/translation")

    page = client.get(page_url).text
    assert "Traduction française." in page
    assert "Texte original" in page


def test_ai_actions_report_errors_instead_of_failing(tmp_path):
    client, conn = make_app(tmp_path)
    story_id = add_quake(conn)
    page_url = f"/articles/{article_id(conn, 'https://lemonde/seisme')}"

    response = client.post(f"{page_url}/summary", follow_redirects=False)
    assert response.headers["location"] == f"{page_url}?error=1"
    response = client.post(
        f"/stories/{story_id}/ask", data={"question": "?"}, follow_redirects=False
    )
    assert response.headers["location"] == f"/stories/{story_id}?error=1#chat"
    assert "IA n&#39;a pas pu répondre" in client.get(f"/stories/{story_id}?error=1").text


def test_brief_links_to_the_stories_it_cites(tmp_path, foundry):
    foundry.main = lambda body: {"content": "Le séisme domine l'actualité [1]."}
    client, conn = make_app(tmp_path, foundry.client())
    story_id = add_quake(conn)

    response = client.post("/brief", data={"period": "day", "sort": "top"}, follow_redirects=False)
    location = response.headers["location"]
    assert location.startswith("/?period=day&sort=top&brief=")

    page = client.get(location).text
    assert "Le séisme domine" in page
    assert f'<a class="cite" href="/stories/{story_id}"' in page
    assert "Séisme au Japon" in foundry.requests[0]["messages"][1]["content"]


def test_questions_on_a_story_are_answered_with_linked_sources(tmp_path, foundry):
    foundry.main = lambda body: {"content": "D'après Le Monde [1]."}
    client, conn = make_app(tmp_path, foundry.client())
    story_id = add_quake(conn)

    response = client.post(
        f"/stories/{story_id}/ask", data={"question": "Où ?"}, follow_redirects=False
    )
    assert response.headers["location"] == f"/stories/{story_id}#chat"

    page = client.get(f"/stories/{story_id}").text
    assert "Où ?" in page
    assert '<a class="cite" href="https://lemonde/seisme"' in page


def test_story_page_shows_a_detailed_synthesis_once_asked(tmp_path, foundry):
    foundry.main = lambda body: {"content": "Synthèse des sources [1]."}
    client, conn = make_app(tmp_path, foundry.client())
    story_id = add_quake(conn)

    # Avant toute question : un bouton pour générer la synthèse, pas de section.
    before = client.get(f"/stories/{story_id}").text
    assert "Résumé des sources</button>" in before
    assert "Synthèse détaillée" not in before

    client.post(f"/stories/{story_id}/ask", data={"question": web.TEXTS["fr"]["preset_sources"]})

    after = client.get(f"/stories/{story_id}").text
    assert "Synthèse détaillée" in after
    assert "Synthèse des sources" in after
    assert '<a class="cite" href="https://lemonde/seisme"' in after
    # Déjà affichée en haut : plus besoin du bouton en double dans le fil.
    assert "Résumé des sources</button>" not in after
    assert "Comprendre le contexte</button>" in after


def test_cross_site_posts_are_rejected(tmp_path, foundry):
    client, conn = make_app(tmp_path, foundry.client())
    story_id = add_quake(conn)

    response = client.post(
        f"/stories/{story_id}/ask", data={"question": "?"}, headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert foundry.requests == []


def test_rich_text_renders_paragraphs_lists_and_known_citations():
    sources = {1: ("Le Monde", "https://a.example"), 2: ("Story", "/stories/2")}

    html = web.rich_text("Intro [1, 2] <b>!</b>\n\n- point [3]\n- autre", sources)

    assert html == (
        '<p>Intro <a class="cite" href="https://a.example" title="Le Monde">[1]</a>'
        '<a class="cite" href="/stories/2" title="Story">[2]</a> &lt;b&gt;!&lt;/b&gt;</p>'
        "<ul><li>point [3]</li><li>autre</li></ul>"
    )
