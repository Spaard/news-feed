"""Appels aux modèles hébergés sur Microsoft Foundry, via son API compatible OpenAI (v1).

Configuration par les variables standard du SDK : OPENAI_BASE_URL (endpoint v1 de la ressource
Foundry) et OPENAI_API_KEY. Les modèles sont désignés par le nom de leur déploiement Foundry.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence

import numpy as np
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from news_feed.config import Tag

log = logging.getLogger(__name__)

EMBED_DEPLOYMENT = "embed"  # embeddings multilingues (text-embedding-3-large)
FAST_DEPLOYMENT = "fast"  # GPT mini, pour les tâches de masse (tags, traductions de titres)
MAIN_DEPLOYMENT = "main"  # GPT récent, pour ce qui est demandé explicitement
EMBED_DIMENSIONS = 512
EMBED_BATCH = 256
FAST_BATCH = 40
FAST_CONCURRENCY = 4
LANGUAGES = {"fr": "français", "en": "anglais"}
MAX_TOOL_ROUNDS = 4
# Consigne de forme commune aux textes affichés tels quels dans l'interface.
PLAIN_TEXT = "Texte simple : paragraphes courts, listes avec des tirets, aucun autre Markdown."

TAGGING_PROMPT = """Tu classes des articles d'actualité dans une taxonomie fixe.
Pour chaque article (id, titre, chapô), donne de 1 à 3 tags de thème de la liste suivante, du \
plus au moins pertinent, plus le tag géographique « france » s'il s'applique. N'utilise aucun \
autre tag.

{taxonomy}"""

TRANSLATION_PROMPT = """Traduis en {language} le titre et le chapô de chaque article \
d'actualité (id, titre, chapô), fidèlement et sans rien ajouter. Garde les noms propres. \
Un chapô vide reste vide."""

SUMMARY_PROMPT = f"""Résume cet article d'actualité en {{language}}, en 5 à 8 lignes : les faits \
essentiels, sans rien ajouter qui ne soit pas dans le texte. {PLAIN_TEXT}"""

ARTICLE_TRANSLATION_PROMPT = f"""Traduis cet article en {{language}}, fidèlement et en entier, \
en gardant ses paragraphes. {PLAIN_TEXT}"""

BRIEF_PROMPT = f"""Tu rédiges un brief d'actualité en {{language}} pour quelqu'un qui veut être \
au courant en deux minutes. À partir des stories numérotées (classées par importance), fais le \
point par grand thème, en quelques phrases par sujet majeur, et cite les stories [n]. N'invente \
rien qui ne soit pas dans leurs titres et chapôs. {PLAIN_TEXT}"""


class _ArticleTags(BaseModel):
    id: int
    tags: list[str]


class _Tagging(BaseModel):
    articles: list[_ArticleTags]


class _Translation(BaseModel):
    id: int
    title: str
    summary: str


class _Translations(BaseModel):
    articles: list[_Translation]


class Unavailable(Exception):
    """Le modèle n'a pas donné de réponse exploitable (filtre de contenu Azure, réponse vide…)."""


def cache_key(kind: str, lang: str, text: str) -> str:
    """Empreinte d'une sortie IA : même demande, même modèle, même entrée → même réponse."""
    return hashlib.sha256("\n".join([kind, MAIN_DEPLOYMENT, lang, text]).encode()).hexdigest()


def client() -> AsyncOpenAI | None:
    """Client Foundry, ou None si l'IA n'est pas configurée."""
    return AsyncOpenAI() if os.environ.get("OPENAI_API_KEY") else None


async def embed(client: AsyncOpenAI, texts: Sequence[str]) -> np.ndarray:
    """Embeddings normalisés, une ligne par texte."""
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        response = await client.embeddings.create(
            model=EMBED_DEPLOYMENT,
            input=list(texts[start : start + EMBED_BATCH]),
            dimensions=EMBED_DIMENSIONS,
        )
        vectors.extend(item.embedding for item in sorted(response.data, key=lambda i: i.index))
    matrix = np.array(vectors, dtype=np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


async def _in_batches[T: BaseModel](
    client: AsyncOpenAI, system: str, articles: Sequence[tuple[int, str, str]], schema: type[T]
) -> list[T]:
    """Soumet (id, titre, chapô) par lots au modèle rapide. Un lot en échec est journalisé et
    ignoré : ses articles restent à traiter et seront repris à la relève suivante. Un lot rejeté
    par le filtre de contenu d'Azure est coupé en deux, pour ne perdre que l'article en cause."""
    semaphore = asyncio.Semaphore(FAST_CONCURRENCY)

    async def run(batch: Sequence[tuple[int, str, str]]) -> list[T]:
        payload = [{"id": id_, "titre": title, "chapô": summary} for id_, title, summary in batch]
        try:
            async with semaphore:
                completion = await client.chat.completions.parse(
                    model=FAST_DEPLOYMENT,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    response_format=schema,
                )
            message = completion.choices[0].message
            if message.parsed is None:
                raise ValueError(message.refusal or "réponse vide")
            return [message.parsed]
        except openai.BadRequestError as error:
            if error.code != "content_filter" or len(batch) == 1:
                log.warning("lot IA en échec (repris à la prochaine relève) : %s", error)
                return []
            middle = len(batch) // 2
            halves = await asyncio.gather(run(batch[:middle]), run(batch[middle:]))
            return [*halves[0], *halves[1]]
        except (openai.OpenAIError, ValueError) as error:
            log.warning("lot IA en échec (repris à la prochaine relève) : %s", error)
            return []

    batches = [articles[i : i + FAST_BATCH] for i in range(0, len(articles), FAST_BATCH)]
    results = await asyncio.gather(*(run(batch) for batch in batches))
    return [response for result in results for response in result]


async def tag_articles(
    client: AsyncOpenAI, articles: Sequence[tuple[int, str, str]], tags: Sequence[Tag]
) -> dict[int, list[str]]:
    """Tags de la taxonomie pour chaque (id, titre, chapô) ; les tags inventés sont écartés."""
    allowed = {tag.slug for tag in tags}
    known = {article[0] for article in articles}
    taxonomy = "\n".join(f"- {tag.slug} : {tag.description}" for tag in tags)
    responses = await _in_batches(
        client, TAGGING_PROMPT.format(taxonomy=taxonomy), articles, _Tagging
    )
    return {
        item.id: [tag for tag in item.tags if tag in allowed]
        for response in responses
        for item in response.articles
        if item.id in known
    }


async def translate_titles(
    client: AsyncOpenAI, articles: Sequence[tuple[int, str, str]], lang: str
) -> dict[int, tuple[str, str]]:
    """(titre, chapô) traduits dans `lang`, pour chaque (id, titre, chapô)."""
    known = {article[0] for article in articles}
    responses = await _in_batches(
        client, TRANSLATION_PROMPT.format(language=LANGUAGES[lang]), articles, _Translations
    )
    return {
        item.id: (item.title, item.summary)
        for response in responses
        for item in response.articles
        if item.id in known
    }


async def _complete(client: AsyncOpenAI, system: str, user: str) -> str:
    completion = await client.chat.completions.create(
        model=MAIN_DEPLOYMENT,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    choice = completion.choices[0]
    if choice.finish_reason == "content_filter" or not choice.message.content:
        raise Unavailable(choice.finish_reason)
    return choice.message.content


async def summarize_article(client: AsyncOpenAI, title: str, text: str, lang: str) -> str:
    return await _complete(
        client, SUMMARY_PROMPT.format(language=LANGUAGES[lang]), f"{title}\n\n{text}"
    )


async def translate_article(client: AsyncOpenAI, text: str, lang: str) -> str:
    return await _complete(
        client, ARTICLE_TRANSLATION_PROMPT.format(language=LANGUAGES[lang]), text
    )


async def brief(client: AsyncOpenAI, numbered_stories: str, lang: str) -> str:
    return await _complete(client, BRIEF_PROMPT.format(language=LANGUAGES[lang]), numbered_stories)


Tool = Callable[[str], Awaitable[str]]

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Mots-clés de recherche."}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
    for name, description in [
        (
            "search_wikipedia",
            "Cherche une page Wikipédia : bases de connaissance (histoire, acteurs, notions).",
        ),
        ("search_news", "Cherche dans l'archive d'actualités de l'app : ce qui s'est passé avant."),
    ]
]


async def chat(
    client: AsyncOpenAI, system: str, messages: Sequence[Mapping], tools: Mapping[str, Tool]
) -> str:
    """Réponse du modèle principal à la fin de `messages`, en le laissant appeler `tools`
    (au plus MAX_TOOL_ROUNDS tours d'appels)."""
    history: list[dict] = [{"role": "system", "content": system}, *map(dict, messages)]
    for round_ in range(MAX_TOOL_ROUNDS + 1):
        completion = await client.chat.completions.create(
            model=MAIN_DEPLOYMENT,
            messages=history,
            tools=TOOL_SCHEMAS,
            tool_choice="auto" if round_ < MAX_TOOL_ROUNDS else "none",
        )
        choice = completion.choices[0]
        calls = choice.message.tool_calls or []
        if not calls:
            if choice.finish_reason == "content_filter" or not choice.message.content:
                raise Unavailable(choice.finish_reason)
            return choice.message.content
        history.append(
            {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [call.model_dump() for call in calls],
            }
        )
        for call in calls:
            try:
                query = json.loads(call.function.arguments)["query"]
                result = await tools[call.function.name](query)
            except (KeyError, ValueError) as exc:
                result = f"Appel invalide : {exc}"
            history.append({"role": "tool", "tool_call_id": call.id, "content": result})
    raise Unavailable("trop d'appels d'outils")
