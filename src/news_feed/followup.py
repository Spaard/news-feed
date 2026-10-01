"""Questions de suivi sur une story : contexte des sources, recherches, fil conservé en base."""

import sqlite3

import httpx2
from openai import AsyncOpenAI

from news_feed import ai, db, reader, stories, wiki
from news_feed.ingest import now

CONTEXT_ARTICLES = 5  # articles dont le texte complet est donné au modèle
PROMPT = f"""Tu aides une personne qui commence à suivre l'actualité, et à qui il manque parfois \
des bases, à comprendre une info. Réponds en {{language}}, clairement, en expliquant les notions \
et les sigles. Appuie les faits sur les sources numérotées et cite-les [n] ; si une information \
vient de tes connaissances générales, dis-le. Pour les bases (histoire, acteurs, notions), utilise \
search_wikipedia ; pour ce qui s'est passé avant sur le sujet, utilise search_news. {ai.PLAIN_TEXT}

L'actualité : {{title}}

Sources :
{{sources}}"""


def thread(conn: sqlite3.Connection, story_id: int, lang: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT role, content FROM chat_messages WHERE story_id = ? AND lang = ? ORDER BY id",
        (story_id, lang),
    ).fetchall()


def sources(conn: sqlite3.Connection, story_id: int, lang: str) -> dict[int, tuple[str, str]]:
    """Sources numérotées du fil : numéro → (titre, url)."""
    rows = conn.execute(
        "SELECT number, title, url FROM chat_sources WHERE story_id = ? AND lang = ?",
        (story_id, lang),
    )
    return {row["number"]: (row["title"], row["url"]) for row in rows}


def _number(
    conn: sqlite3.Connection,
    story_id: int,
    lang: str,
    known: dict[int, tuple[str, str]],
    title: str,
    url: str,
) -> int:
    """Numéro de `url` dans le fil, attribué (et enregistré) à sa première apparition."""
    for number, (_, existing) in known.items():
        if existing == url:
            return number
    number = max(known, default=0) + 1
    with conn:
        conn.execute(
            "INSERT INTO chat_sources (story_id, lang, number, title, url) VALUES (?, ?, ?, ?, ?)",
            (story_id, lang, number, title, url),
        )
    known[number] = (title, url)
    return number


async def ask(
    conn: sqlite3.Connection,
    client: AsyncOpenAI,
    http: httpx2.AsyncClient,
    story_id: int,
    lang: str,
    question: str,
) -> None:
    """Pose `question` sur la story et enregistre la question et la réponse dans le fil."""
    [(story, articles, _)] = db.load_stories(conn, [story_id])
    known = sources(conn, story_id, lang)

    def number(title: str, url: str) -> int:
        return _number(conn, story_id, lang, known, title, url)

    numbers = {a["id"]: number(f"{a['source']} — {a['title']}", a["url"]) for a in articles}
    # Les versions gratuites d'abord : ce sont celles dont on obtient le texte complet.
    readable = sorted(articles, key=lambda a: (a["paywall"], -a["une"], a["published_at"]))
    texts = await reader.contents(conn, http, readable[:CONTEXT_ARTICLES])
    context = []
    for article in articles:
        context.append(
            f"[{numbers[article['id']]}] {article['source']} ({article['lang']}, "
            f"{article['published_at'][:10]}) : {article['title']}\n{article['summary']}"
        )
        if texts.get(article["id"]):
            context.append(texts[article["id"]])
    earlier = [f"[{n}] {title}" for n, (title, _) in known.items() if n not in numbers.values()]
    if earlier:
        context.append("Trouvées lors des questions précédentes :\n" + "\n".join(earlier))

    async def search_wikipedia(query: str) -> str:
        try:
            page = await wiki.search(http, query, lang)
        except httpx2.HTTPError:
            return "Wikipédia est injoignable."
        if page is None:
            return "Aucune page trouvée."
        return f"[{number(f'Wikipédia : {page.title}', page.url)}] {page.title}\n{page.extract}"

    async def search_news(query: str) -> str:
        rows = db.list_stories(conn, since="", sort="recent", query=query, limit=5)
        found = []
        for other, other_articles, _ in db.load_stories(conn, [row["id"] for row in rows]):
            title, summary, _ = stories.display(other, other_articles, lang)
            chosen = stories.representative(other_articles)
            day = max(a["published_at"] for a in other_articles)[:10]
            n = number(f"{chosen['source']} — {title}", chosen["url"])
            found.append(f"[{n}] {day} : {title}\n{summary}")
        return "\n\n".join(found) or "Rien dans l'archive."

    title, _, _ = stories.display(story, articles, lang)
    system = PROMPT.format(language=ai.LANGUAGES[lang], title=title, sources="\n\n".join(context))
    messages = [{"role": m["role"], "content": m["content"]} for m in thread(conn, story_id, lang)]
    messages.append({"role": "user", "content": question})
    answer = await ai.chat(
        client, system, messages, {"search_wikipedia": search_wikipedia, "search_news": search_news}
    )
    with conn:
        conn.executemany(
            "INSERT INTO chat_messages (story_id, lang, role, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (story_id, lang, "user", question, now()),
                (story_id, lang, "assistant", answer, now()),
            ],
        )
