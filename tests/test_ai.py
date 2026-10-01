import asyncio

import httpx2
import numpy as np
import pytest

from news_feed import ai
from news_feed.config import Tag
from tests.helpers import tool_call

TAGS = [
    Tag(slug="international", label_fr="International", label_en="World", description="Monde"),
    Tag(slug="sport", label_fr="Sport", label_en="Sports", description="Compétitions"),
]


def test_client_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert ai.client() is None


def test_embed_normalizes_vectors_and_requests_reduced_dimensions(foundry):
    foundry.embed = lambda texts: [[3.0, 4.0] for _ in texts]

    vectors = asyncio.run(ai.embed(foundry.client(), ["a", "b"]))

    assert np.allclose(vectors, [[0.6, 0.8], [0.6, 0.8]])
    assert foundry.requests[0]["model"] == ai.EMBED_DEPLOYMENT
    assert foundry.requests[0]["dimensions"] == ai.EMBED_DIMENSIONS


def test_tag_articles_keeps_only_known_articles_and_tags(foundry):
    def chat(system, payload):
        tagged = [{"id": item["id"], "tags": ["international", "inventé"]} for item in payload]
        return {"articles": [*tagged, {"id": 99, "tags": ["sport"]}]}

    foundry.chat = chat

    tags = asyncio.run(ai.tag_articles(foundry.client(), [(1, "Titre", "Chapô")], TAGS))

    assert tags == {1: ["international"]}
    request = foundry.requests[0]
    assert request["model"] == ai.FAST_DEPLOYMENT
    assert "- sport : Compétitions" in request["messages"][0]["content"]


def test_failed_batch_is_skipped_and_the_others_are_kept(foundry, monkeypatch):
    monkeypatch.setattr(ai, "FAST_BATCH", 1)

    def chat(system, payload):
        if payload[0]["id"] == 2:
            raise httpx2.ConnectError("Foundry indisponible")
        return {"articles": [{"id": payload[0]["id"], "tags": ["sport"]}]}

    foundry.chat = chat

    tags = asyncio.run(ai.tag_articles(foundry.client(), [(1, "a", ""), (2, "b", "")], TAGS))

    assert tags == {1: ["sport"]}


def test_filtered_batch_is_split_to_keep_the_other_articles(foundry, monkeypatch):
    monkeypatch.setattr(ai, "FAST_BATCH", 4)

    def chat(system, payload):
        if any(item["id"] == 3 for item in payload):
            return None
        return {"articles": [{"id": item["id"], "tags": ["sport"]} for item in payload]}

    foundry.chat = chat
    articles = [(id_, "titre", "") for id_ in (1, 2, 3, 4)]

    tags = asyncio.run(ai.tag_articles(foundry.client(), articles, TAGS))

    assert tags == {1: ["sport"], 2: ["sport"], 4: ["sport"]}


def test_translate_titles(foundry):
    foundry.chat = lambda system, payload: {
        "articles": [
            {"id": item["id"], "title": "Séisme au Japon", "summary": ""} for item in payload
        ]
    }

    translations = asyncio.run(
        ai.translate_titles(foundry.client(), [(7, "Earthquake in Japan", "")], "fr")
    )

    assert translations == {7: ("Séisme au Japon", "")}
    assert "Traduis en français" in foundry.requests[0]["messages"][0]["content"]


def test_summarize_article_uses_the_main_model_in_the_requested_language(foundry):
    foundry.main = lambda body: {"content": "Le résumé."}

    summary = asyncio.run(ai.summarize_article(foundry.client(), "Titre", "Texte", "en"))

    assert summary == "Le résumé."
    request = foundry.requests[0]
    assert request["model"] == ai.MAIN_DEPLOYMENT
    assert "en anglais" in request["messages"][0]["content"]


def test_filtered_answer_raises_unavailable(foundry):
    foundry.main = lambda body: {}

    with pytest.raises(ai.Unavailable):
        asyncio.run(ai.brief(foundry.client(), "[1] Séisme au Japon", "fr"))


def test_chat_runs_the_tools_until_the_final_answer(foundry):
    replies = iter(
        [{"tool_calls": [tool_call("search_wikipedia", "OTAN")]}, {"content": "Alliance [2]."}]
    )
    foundry.main = lambda body: next(replies)
    queries = []

    async def search_wikipedia(query):
        queries.append(query)
        return "[2] OTAN\nOrganisation du traité de l'Atlantique nord."

    answer = asyncio.run(
        ai.chat(
            foundry.client(),
            "Système",
            [{"role": "user", "content": "L'OTAN ?"}],
            {"search_wikipedia": search_wikipedia},
        )
    )

    assert answer == "Alliance [2]."
    assert queries == ["OTAN"]
    assert foundry.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "[2] OTAN\nOrganisation du traité de l'Atlantique nord.",
    }


def test_chat_stops_offering_tools_after_the_last_round(foundry):
    def main(body):
        if body["tool_choice"] == "none":
            return {"content": "Réponse finale."}
        return {"tool_calls": [tool_call("search_news", "Ukraine")]}

    foundry.main = main

    async def search_news(query):
        return "Rien dans l'archive."

    answer = asyncio.run(
        ai.chat(
            foundry.client(),
            "Système",
            [{"role": "user", "content": "Et avant ?"}],
            {"search_news": search_news},
        )
    )

    assert answer == "Réponse finale."
    assert len(foundry.requests) == ai.MAX_TOOL_ROUNDS + 1
