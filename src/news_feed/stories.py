"""Stories : regroupement des articles par événement, tags, textes affichés et rétention."""

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from news_feed.db import NOTEWORTHY

# Une story reste ouverte aux nouveaux articles tant qu'elle en a reçu un dans cette fenêtre.
ACTIVE_WINDOW = timedelta(hours=48)
# Similarité cosinus minimale entre un article et le centroïde d'une story, calibrée sur une
# vraie journée de titres FR + EN (text-embedding-3-large, 512 dimensions). Les sources
# spécialisées (`focus`) titrent toutes pareil (« faille exploitée », « agents IA ») : plus bas,
# des événements distincts y fusionnent.
SIMILARITY_THRESHOLD = 0.62
FOCUS_SIMILARITY_THRESHOLD = 0.70
# Longueur visée pour l'aperçu d'une story (sans appel IA) : de quoi remplir l'écran sur
# téléphone. En dessous, on complète avec le chapô d'autres articles de la story.
OVERVIEW_MIN_CHARS = 700
# Les stories mineures (un seul média, ni à la une ni spécialisé) sont supprimées après ce délai.
MINOR_RETENTION = timedelta(days=30)
TEXT_COLUMNS = {"fr": ("title_fr", "summary_fr"), "en": ("title_en", "summary_en")}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def assign(conn: sqlite3.Connection, articles: Sequence[Mapping], vectors: np.ndarray) -> set[int]:
    """Rattache chaque article (dans l'ordre chronologique) à la story active la plus proche,
    ou lui crée une story. `vectors` : embeddings normalisés, alignés sur `articles`."""
    rows = conn.execute(
        "SELECT id, centroid, size, last_seen_at FROM stories WHERE centroid IS NOT NULL"
    ).fetchall()
    capacity = len(rows) + len(articles)
    centroids = np.zeros((capacity, vectors.shape[1]), dtype=np.float32)
    last_seen = np.zeros(capacity)
    ids, sizes = [], []
    for i, row in enumerate(rows):
        centroids[i] = np.frombuffer(row["centroid"], dtype=np.float32)
        last_seen[i] = datetime.fromisoformat(row["last_seen_at"]).timestamp()
        ids.append(row["id"])
        sizes.append(row["size"])

    touched = set()
    with conn:
        for index in sorted(range(len(articles)), key=lambda i: articles[i]["published_at"]):
            article, vector = articles[index], vectors[index]
            published = datetime.fromisoformat(article["published_at"]).timestamp()
            count = len(ids)
            best = -1
            if count:
                similarities = centroids[:count] @ vector
                similarities[last_seen[:count] < published - ACTIVE_WINDOW.total_seconds()] = -1
                best = int(np.argmax(similarities))
                threshold = FOCUS_SIMILARITY_THRESHOLD if article["focus"] else SIMILARITY_THRESHOLD
                if similarities[best] < threshold:
                    best = -1
            if best >= 0:
                merged = centroids[best] * sizes[best] + vector
                centroids[best] = merged / np.linalg.norm(merged)
                sizes[best] += 1
                last_seen[best] = max(last_seen[best], published)
            else:
                best = count
                centroids[best], last_seen[best] = vector, published
                sizes.append(1)
                ids.append(
                    conn.execute(
                        "INSERT INTO stories (size, last_seen_at) VALUES (1, ?)",
                        (article["published_at"],),
                    ).lastrowid
                )
            conn.execute(
                "UPDATE articles SET story_id = ? WHERE id = ?", (ids[best], article["id"])
            )
            touched.add(best)
        conn.executemany(
            "UPDATE stories SET centroid = ?, size = ?, last_seen_at = ? WHERE id = ?",
            [(centroids[i].tobytes(), sizes[i], _iso(last_seen[i]), ids[i]) for i in touched],
        )
    return {ids[i] for i in touched}


def story_tags(article_tags: Sequence[set[str]]) -> list[str]:
    """Tags portés par au moins un tiers des articles, et au minimum le plus fréquent."""
    counts = Counter(tag for tags in article_tags for tag in tags)
    if not counts:
        return []
    top = counts.most_common(1)[0][0]
    return sorted({tag for tag, n in counts.items() if n * 3 >= len(article_tags)} | {top})


def save_tags(conn: sqlite3.Connection, tags: Mapping[int, Iterable[str]]) -> None:
    """Enregistre les tags des articles classés, puis recalcule ceux de leurs stories."""
    if not tags:
        return
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO article_tags (article_id, tag) VALUES (?, ?)",
            [(article_id, tag) for article_id, slugs in tags.items() for tag in slugs],
        )
        conn.executemany("UPDATE articles SET tagged = 1 WHERE id = ?", [(i,) for i in tags])
        placeholders = ", ".join("?" * len(tags))
        rows = conn.execute(
            f"""
            SELECT a.story_id, a.id, t.tag FROM articles a
            LEFT JOIN article_tags t ON t.article_id = a.id
            WHERE a.tagged = 1 AND a.story_id IN (
                SELECT story_id FROM articles WHERE id IN ({placeholders}))
            """,
            list(tags),
        ).fetchall()
        per_story: dict[int, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        for row in rows:
            article_tags = per_story[row["story_id"]][row["id"]]
            if row["tag"]:
                article_tags.add(row["tag"])
        for story_id, articles in per_story.items():
            conn.execute("DELETE FROM story_tags WHERE story_id = ?", (story_id,))
            conn.executemany(
                "INSERT INTO story_tags (story_id, tag) VALUES (?, ?)",
                [(story_id, tag) for tag in story_tags(list(articles.values()))],
            )


def representative(articles: Iterable[Mapping]) -> Mapping:
    """L'article qui incarne la story : d'abord un article à la une, sinon le premier publié."""
    return min(articles, key=lambda a: (-a["une"], a["published_at"]))


def overview(articles: Iterable[Mapping]) -> str:
    """Aperçu de la story, sans IA : le chapô de l'article représentatif, complété par les
    chapôs distincts des autres articles (par ordre de pertinence) jusqu'à une longueur
    correcte à lire. Simple concaténation : les angles peuvent se répéter d'un média à l'autre,
    la synthèse détaillée (IA, à la demande) les met en regard."""
    ordered = sorted(articles, key=lambda a: (-a["une"], a["published_at"]))
    parts: list[str] = []
    length = 0
    for article in ordered:
        summary = article["summary"].strip()
        if not summary or summary in parts:
            continue
        parts.append(summary)
        length += len(summary)
        if length >= OVERVIEW_MIN_CHARS:
            break
    return " ".join(parts)


def display(story: Mapping, articles: Sequence[Mapping], lang: str) -> tuple[str, str, bool]:
    """(titre, aperçu, traduit) d'une story dans `lang` : article natif, sinon traduction,
    sinon article d'origine."""
    native = [a for a in articles if a["lang"] == lang]
    title_column, summary_column = TEXT_COLUMNS[lang]
    if not native and story[title_column]:
        return story[title_column], story[summary_column] or "", True
    chosen = native or articles
    return representative(chosen)["title"], overview(chosen), False


def untranslated(conn: sqlite3.Connection, lang: str) -> list[tuple[int, str, str]]:
    """Stories un minimum importantes sans article ni traduction dans `lang` :
    (id, titre, chapô) de leur article représentatif, à traduire."""
    title_column, _ = TEXT_COLUMNS[lang]
    story_ids = [
        row[0]
        for row in conn.execute(
            f"""
            SELECT a.story_id FROM articles a JOIN stories s ON s.id = a.story_id
            WHERE s.{title_column} IS NULL
            GROUP BY a.story_id
            HAVING {NOTEWORTHY} AND sum(a.lang = ?) = 0
            """,
            (lang,),
        )
    ]
    if not story_ids:
        return []
    by_story = defaultdict(list)
    placeholders = ", ".join("?" * len(story_ids))
    for row in conn.execute(
        f"SELECT * FROM articles WHERE story_id IN ({placeholders})", story_ids
    ):
        by_story[row["story_id"]].append(row)
    return [
        (story_id, chosen["title"], chosen["summary"])
        for story_id, articles in by_story.items()
        for chosen in [representative(articles)]
    ]


def save_translations(
    conn: sqlite3.Connection, lang: str, translations: Mapping[int, tuple[str, str]]
) -> None:
    title_column, summary_column = TEXT_COLUMNS[lang]
    with conn:
        conn.executemany(
            f"UPDATE stories SET {title_column} = ?, {summary_column} = ? WHERE id = ?",
            [(title, summary, story_id) for story_id, (title, summary) in translations.items()],
        )


def purge(conn: sqlite3.Connection, now: str) -> None:
    """Efface les centroïdes des stories inactives et supprime le contenu mineur ancien
    (sauf les stories sur lesquelles des questions ont été posées)."""
    current = datetime.fromisoformat(now)
    inactive_before = (current - ACTIVE_WINDOW).isoformat(timespec="seconds")
    minor_before = (current - MINOR_RETENTION).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "UPDATE stories SET centroid = NULL WHERE centroid IS NOT NULL AND last_seen_at < ?",
            (inactive_before,),
        )
        conn.execute(
            f"""
            DELETE FROM stories WHERE last_seen_at < ? AND id NOT IN (
                SELECT a.story_id FROM articles a WHERE a.story_id IS NOT NULL
                GROUP BY a.story_id HAVING {NOTEWORTHY})
            AND id NOT IN (SELECT story_id FROM chat_messages)
            """,
            (minor_before,),
        )
        conn.execute(
            "DELETE FROM articles WHERE story_id IS NULL AND published_at < ?", (minor_before,)
        )
