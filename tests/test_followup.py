import asyncio
from datetime import UTC, datetime, timedelta

import httpx2

from news_feed import db, followup
from tests.helpers import add_story, add_to_story, article, tool_call, web_pages

EARLIER = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
LATER = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")


def test_ask_answers_with_numbered_sources_and_keeps_the_thread(tmp_path, foundry):
    conn = db.connect(tmp_path / "news.db")
    story_id = add_story(
        conn,
        article(
            "https://franceinfo.example/ukraine",
            source="France Info",
            title="Offensive en Ukraine",
            published_at=EARLIER,
        ),
        article(
            "https://lemonde.example/ukraine",
            source="Le Monde",
            title="Kiev sous pression",
            published_at=LATER,
            paywall=True,
        ),
    )
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(web_pages))
    replies = iter(
        [
            {
                "tool_calls": [
                    tool_call("search_wikipedia", "guerre russo-ukrainienne"),
                    tool_call("search_news", "Ukraine", call_id="call_2"),
                ]
            },
            {"content": "La guerre a commencé en 2014 [3] ; l'offensive est rapportée par [1]."},
        ]
    )
    foundry.main = lambda body: next(replies)

    asyncio.run(followup.ask(conn, foundry.client(), http, story_id, "fr", "Le contexte ?"))

    assert [tuple(m) for m in followup.thread(conn, story_id, "fr")] == [
        ("user", "Le contexte ?"),
        ("assistant", "La guerre a commencé en 2014 [3] ; l'offensive est rapportée par [1]."),
    ]
    sources = followup.sources(conn, story_id, "fr")
    assert sources == {
        1: ("France Info — Offensive en Ukraine", "https://franceinfo.example/ukraine"),
        2: ("Le Monde — Kiev sous pression", "https://lemonde.example/ukraine"),
        3: (
            "Wikipédia : Guerre russo-ukrainienne",
            "https://fr.wikipedia.org/wiki/Guerre_russo-ukrainienne",
        ),
    }
    first_call = foundry.requests[0]["messages"]
    assert "Réponds en français" in first_call[0]["content"]
    assert "premier paragraphe" in first_call[0]["content"]  # texte de la version gratuite
    tool_results = [m["content"] for m in foundry.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_results[0].startswith("[3] Guerre russo-ukrainienne")
    assert "Offensive en Ukraine" in tool_results[1]  # l'archive retrouve la story (source [1])

    foundry.main = lambda body: {"content": "Oui, voir [3]."}
    asyncio.run(followup.ask(conn, foundry.client(), http, story_id, "fr", "Depuis 2014 ?"))

    follow_up = foundry.requests[-1]["messages"]
    assert [m["role"] for m in follow_up[1:]] == ["user", "assistant", "user"]
    assert "[3] Wikipédia : Guerre russo-ukrainienne" in follow_up[0]["content"]
    assert len(followup.thread(conn, story_id, "fr")) == 4
    assert followup.thread(conn, story_id, "en") == []


def quake(conn) -> int:
    return add_story(
        conn,
        article("https://lemonde.example/seisme", source="Le Monde", published_at=EARLIER),
        article("https://bbc.example/quake", source="BBC", lang="en", published_at=LATER),
    )


def test_synthesis_is_kept_apart_from_the_thread_and_cites_the_sources(tmp_path, foundry):
    conn = db.connect(tmp_path / "news.db")
    story_id = quake(conn)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(web_pages))
    foundry.main = lambda body: {"content": "Les faits établis [1], vus aussi par [2]."}

    asyncio.run(followup.synthesize(conn, foundry.client(), http, story_id, "fr"))

    synthesis = followup.synthesis(conn, story_id, "fr")
    assert (synthesis["content"], synthesis["articles"]) == (
        "Les faits établis [1], vus aussi par [2].",
        2,
    )
    assert followup.thread(conn, story_id, "fr") == []
    assert set(followup.sources(conn, story_id, "fr")) == {1, 2}
    messages = foundry.requests[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == followup.SYNTHESIS_REQUEST
    assert followup.synthesis(conn, story_id, "en") is None


def test_syntheses_are_redone_once_their_story_has_grown_enough(tmp_path, foundry):
    conn = db.connect(tmp_path / "news.db")
    story_id = quake(conn)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(web_pages))
    versions = iter(["Première version.", "Version à jour."])
    foundry.main = lambda body: {"content": next(versions)}
    asyncio.run(followup.synthesize(conn, foundry.client(), http, story_id, "fr"))

    # Un seul article de plus : en dessous du seuil (au moins 2), rien ne change.
    add_to_story(conn, story_id, article("https://rfi.example/seisme", source="RFI"))
    asyncio.run(followup.refresh_syntheses(conn, foundry.client(), http))
    assert followup.synthesis(conn, story_id, "fr")["content"] == "Première version."

    add_to_story(conn, story_id, article("https://dw.example/quake", source="DW", lang="en"))
    asyncio.run(followup.refresh_syntheses(conn, foundry.client(), http))
    synthesis = followup.synthesis(conn, story_id, "fr")
    assert (synthesis["content"], synthesis["articles"]) == ("Version à jour.", 4)


def test_a_failed_synthesis_update_keeps_the_previous_one(tmp_path, foundry):
    conn = db.connect(tmp_path / "news.db")
    story_id = quake(conn)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(web_pages))
    foundry.main = lambda body: {"content": "Première version."}
    asyncio.run(followup.synthesize(conn, foundry.client(), http, story_id, "fr"))
    add_to_story(
        conn,
        story_id,
        article("https://rfi.example/seisme", source="RFI"),
        article("https://dw.example/quake", source="DW", lang="en"),
    )

    foundry.main = lambda body: {}  # réponse filtrée par Azure
    asyncio.run(followup.refresh_syntheses(conn, foundry.client(), http))

    assert followup.synthesis(conn, story_id, "fr")["content"] == "Première version."
