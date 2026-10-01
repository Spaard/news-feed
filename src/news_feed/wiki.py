"""Recherche sur Wikipédia (API publique), pour les bases de connaissance citables."""

from dataclasses import dataclass

import httpx2

MAX_CHARS = 6_000


@dataclass(frozen=True)
class Page:
    title: str
    url: str
    extract: str


async def search(http: httpx2.AsyncClient, query: str, lang: str) -> Page | None:
    """Page Wikipédia (dans `lang`) la plus pertinente pour `query`, avec son texte brut."""
    response = await http.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": "1",
            "prop": "extracts|info",
            "explaintext": "1",
            "inprop": "url",
            "redirects": "1",
        },
    )
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    extract = page.get("extract", "")[:MAX_CHARS]
    return Page(title=page["title"], url=page["fullurl"], extract=extract)
