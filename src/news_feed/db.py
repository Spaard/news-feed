"""Accès SQLite : schéma, migrations et requêtes."""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass
from pathlib import Path

# Chaque entrée est une migration ; `PRAGMA user_version` retient la dernière appliquée.
MIGRATIONS = [
    """
    CREATE TABLE feeds (
        url TEXT PRIMARY KEY,
        etag TEXT,
        last_modified TEXT,
        fetched_at TEXT NOT NULL,
        error TEXT
    );

    -- Une story = un événement, toutes langues confondues.
    CREATE TABLE stories (
        id INTEGER PRIMARY KEY,
        centroid BLOB,               -- embedding moyen, effacé quand la story devient inactive
        size INTEGER NOT NULL,       -- nombre d'articles moyennés dans le centroïde
        last_seen_at TEXT NOT NULL,  -- date du dernier article rattaché
        title_fr TEXT,               -- traductions, utilisées faute d'article natif
        summary_fr TEXT,
        title_en TEXT,
        summary_en TEXT
    );

    CREATE INDEX stories_last_seen_at ON stories (last_seen_at);

    CREATE TABLE articles (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        lang TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        published_at TEXT NOT NULL,
        une INTEGER NOT NULL,
        paywall INTEGER NOT NULL,
        focus INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        story_id INTEGER REFERENCES stories (id) ON DELETE CASCADE,
        tagged INTEGER NOT NULL DEFAULT 0,
        content TEXT  -- texte extrait de la page ('' : illisible) ; NULL : pas encore tenté
    );

    CREATE INDEX articles_published_at ON articles (published_at);
    CREATE INDEX articles_story_id ON articles (story_id);

    CREATE TABLE article_tags (
        article_id INTEGER NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY (article_id, tag)
    );

    CREATE TABLE story_tags (
        story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY (story_id, tag)
    );

    CREATE INDEX story_tags_tag ON story_tags (tag);

    -- Recherche plein texte, insensible aux accents, synchronisée par triggers.
    CREATE VIRTUAL TABLE articles_fts USING fts5(
        title, summary, content = 'articles', content_rowid = 'id',
        tokenize = 'unicode61 remove_diacritics 2'
    );

    CREATE TRIGGER articles_fts_insert AFTER INSERT ON articles BEGIN
        INSERT INTO articles_fts (rowid, title, summary) VALUES (new.id, new.title, new.summary);
    END;

    CREATE TRIGGER articles_fts_delete AFTER DELETE ON articles BEGIN
        INSERT INTO articles_fts (articles_fts, rowid, title, summary)
        VALUES ('delete', old.id, old.title, old.summary);
    END;

    -- Sorties IA déjà produites (résumés, traductions, briefs), par empreinte de leur entrée.
    CREATE TABLE ai_cache (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    -- Fil de questions de suivi d'une story, dans une langue.
    CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY,
        story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
        lang TEXT NOT NULL,
        role TEXT NOT NULL,  -- 'user' ou 'assistant'
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE INDEX chat_messages_story ON chat_messages (story_id, lang);

    -- Sources numérotées d'un fil, citées [n] dans les réponses ; numéros attribués une fois pour
    -- toutes (articles de la story, puis pages trouvées par les recherches).
    CREATE TABLE chat_sources (
        story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
        lang TEXT NOT NULL,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        PRIMARY KEY (story_id, lang, number)
    );
    """,
    """
    -- Synthèse détaillée d'une story, dans une langue ; refaite quand la story grossit.
    -- Ses citations [n] renvoient aux sources du fil (chat_sources).
    CREATE TABLE syntheses (
        story_id INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
        lang TEXT NOT NULL,
        content TEXT NOT NULL,
        articles INTEGER NOT NULL,  -- nombre d'articles de la story quand elle a été rédigée
        created_at TEXT NOT NULL,
        PRIMARY KEY (story_id, lang)
    );
    """,
]

SORTS = {"top": "score DESC, last_at DESC", "recent": "last_at DESC"}
# Story « un minimum importante » : au moins deux médias, ou à la une, ou source spécialisée.
# S'applique à un GROUP BY story_id sur `articles a`.
NOTEWORTHY = "(count(DISTINCT a.source) >= 2 OR max(a.une) = 1 OR max(a.focus) = 1)"


@dataclass(frozen=True)
class NewArticle:
    url: str
    source: str
    lang: str
    title: str
    summary: str
    published_at: str
    une: bool
    paywall: bool
    focus: bool


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Le site ouvre la connexion avant de lancer sa boucle asyncio, puis ne s'en sert que depuis
    # cette boucle : un seul thread à la fois, jamais d'accès concurrent.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")


def feed_state(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()


def save_feed_state(
    conn: sqlite3.Connection,
    url: str,
    *,
    etag: str | None,
    last_modified: str | None,
    fetched_at: str,
    error: str | None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO feeds (url, etag, last_modified, fetched_at, error) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET etag = excluded.etag,
                last_modified = excluded.last_modified, fetched_at = excluded.fetched_at,
                error = excluded.error
            """,
            (url, etag, last_modified, fetched_at, error),
        )


def insert_articles(
    conn: sqlite3.Connection, articles: Iterable[NewArticle], fetched_at: str
) -> int:
    """Insère les articles inconnus et renvoie leur nombre.

    Une URL déjà connue n'est pas dupliquée, mais elle hérite des signaux « à la une » et
    « focus » si elle réapparaît dans un tel flux.
    """
    articles = list(articles)
    with conn:
        inserted = conn.executemany(
            """
            INSERT OR IGNORE INTO articles
                (url, source, lang, title, summary, published_at, une, paywall, focus, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*astuple(article), fetched_at) for article in articles],
        ).rowcount
        conn.executemany(
            "UPDATE articles SET une = max(une, ?), focus = max(focus, ?) WHERE url = ?",
            [(a.une, a.focus, a.url) for a in articles if a.une or a.focus],
        )
    return inserted


def fts_query(text: str) -> str:
    """Requête FTS5 sûre : chaque mot devient une chaîne exacte, tous les mots sont requis."""
    return " ".join('"' + word.replace('"', '""') + '"' for word in text.split())


def list_stories(
    conn: sqlite3.Connection,
    *,
    since: str,
    tags: Iterable[str] = (),
    zone: str | None = None,
    sort: str = "top",
    query: str = "",
    limit: int = 30,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Stories un minimum importantes (NOTEWORTHY) ayant des articles depuis `since`.

    Tout est calculé sur les articles de la période : le score compte les médias distincts,
    ×2 pour ceux qui l'ont mise à la une. `tags` : au moins un des tags ; `zone` : un tag
    obligatoire en plus ; `query` : recherche plein texte ; `sort` : clé de SORTS. Chaque ligne
    porte aussi `total`, le nombre de stories correspondantes, toutes pages confondues.
    """
    tags = list(tags)
    filters = ["a.story_id IS NOT NULL", "a.published_at >= :since"]
    params: dict[str, object] = {"since": since, "limit": limit, "offset": offset}
    if tags:
        params |= {f"tag{i}": tag for i, tag in enumerate(tags)}
        names = ", ".join(f":tag{i}" for i in range(len(tags)))
        filters.append(f"a.story_id IN (SELECT story_id FROM story_tags WHERE tag IN ({names}))")
    if zone:
        filters.append("a.story_id IN (SELECT story_id FROM story_tags WHERE tag = :zone)")
        params["zone"] = zone
    if query.strip():
        filters.append(
            "a.story_id IN (SELECT m.story_id FROM articles_fts"
            " JOIN articles m ON m.id = articles_fts.rowid WHERE articles_fts MATCH :query)"
        )
        params["query"] = fts_query(query)
    return conn.execute(
        f"""
        SELECT a.story_id AS id,
               count(DISTINCT a.source)
                   + count(DISTINCT CASE WHEN a.une = 1 THEN a.source END) AS score,
               count(DISTINCT a.source) AS sources,
               max(a.published_at) AS last_at,
               count(*) OVER () AS total
        FROM articles a
        WHERE {" AND ".join(filters)}
        GROUP BY a.story_id
        HAVING {NOTEWORTHY}
        ORDER BY {SORTS[sort]}
        LIMIT :limit OFFSET :offset
        """,
        params,
    ).fetchall()


def load_stories(
    conn: sqlite3.Connection, ids: Sequence[int]
) -> list[tuple[sqlite3.Row, list[sqlite3.Row], list[str]]]:
    """(story, ses articles par date, ses tags) pour chaque id connu, dans l'ordre de `ids`."""
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    found = {
        row["id"]: row for row in conn.execute(f"SELECT * FROM stories WHERE id IN ({marks})", ids)
    }
    articles = defaultdict(list)
    for row in conn.execute(
        f"SELECT * FROM articles WHERE story_id IN ({marks}) ORDER BY published_at", ids
    ):
        articles[row["story_id"]].append(row)
    tags = defaultdict(list)
    for row in conn.execute(
        f"SELECT story_id, tag FROM story_tags WHERE story_id IN ({marks}) ORDER BY tag", ids
    ):
        tags[row["story_id"]].append(row["tag"])
    return [(found[i], articles[i], tags[i]) for i in ids if i in found]


def cache_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ai_cache WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def cache_put(conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache (key, value, created_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
