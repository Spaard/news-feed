import re

from news_feed.config import load_catalog


def test_feeds_are_valid_and_unique():
    feeds = load_catalog().feeds
    urls = [feed.url for feed in feeds]

    assert len(urls) == len(set(urls))
    for feed in feeds:
        assert feed.url.startswith("https://"), feed.url
        assert feed.lang in {"fr", "en"}, feed.url
        assert feed.source.strip(), feed.url


def test_tags_are_valid_and_unique():
    tags = load_catalog().tags
    slugs = [tag.slug for tag in tags]

    assert len(slugs) == len(set(slugs))
    for tag in tags:
        assert re.fullmatch(r"[a-z]+", tag.slug), tag.slug
        assert tag.label_fr and tag.label_en and tag.description, tag.slug
