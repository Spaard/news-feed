import asyncio

import httpx2

from news_feed import wiki
from tests.helpers import WIKIPEDIA_PAGE


def test_search_returns_the_best_page_in_the_requested_language():
    def api(request):
        assert request.url.host == "en.wikipedia.org"
        assert request.url.params["gsrsearch"] == "Russo-Ukrainian war"
        return httpx2.Response(200, json=WIKIPEDIA_PAGE)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(api))

    page = asyncio.run(wiki.search(http, "Russo-Ukrainian war", "en"))

    assert page == wiki.Page(
        title="Guerre russo-ukrainienne",
        url="https://fr.wikipedia.org/wiki/Guerre_russo-ukrainienne",
        extract="La guerre russo-ukrainienne oppose la Russie et l'Ukraine depuis 2014.",
    )


def test_search_without_result():
    http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={}))
    )

    assert asyncio.run(wiki.search(http, "zzzz", "fr")) is None
