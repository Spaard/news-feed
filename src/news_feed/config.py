"""Réglages d'exécution (variables d'environnement) et catalogue des sources."""

import os
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Feed:
    url: str
    source: str
    lang: str
    une: bool = False  # flux « à la une » : signal d'importance
    paywall: bool = False  # articles souvent réservés aux abonnés
    focus: bool = False  # source spécialisée d'un sujet approfondi : chaque article compte


@dataclass(frozen=True)
class Tag:
    slug: str
    label_fr: str
    label_en: str
    description: str


@dataclass(frozen=True)
class Catalog:
    feeds: tuple[Feed, ...]
    tags: tuple[Tag, ...]


def load_catalog() -> Catalog:
    data = tomllib.loads(files("news_feed").joinpath("sources.toml").read_text(encoding="utf-8"))
    return Catalog(
        feeds=tuple(Feed(**feed) for feed in data["feeds"]),
        tags=tuple(Tag(**tag) for tag in data["tags"]),
    )


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    refresh_minutes: int = 20  # 0 : pas de relève automatique par le serveur web
    allowed_user: str | None = None  # compte GitHub seul autorisé derrière Easy Auth (ACA)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("NEWS_FEED_DATA_DIR", "data")),
            refresh_minutes=int(os.environ.get("NEWS_FEED_REFRESH_MINUTES", "20")),
            allowed_user=os.environ.get("NEWS_FEED_ALLOWED_USER") or None,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "news-feed.db"
