import argparse
import asyncio
import logging

import uvicorn

from news_feed import ai, db, ingest, web
from news_feed.config import Settings, load_catalog


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="news-feed", description="Agrégateur d'actualité personnel."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "fetch", help="relève tous les flux une fois, puis les enrichit (si l'IA est configurée)"
    )
    serve = commands.add_parser("serve", help="lance le site web et sa relève périodique")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # une ligne par requête, sinon
    settings, catalog = Settings.from_env(), load_catalog()
    if args.command == "fetch":
        conn = db.connect(settings.db_path)
        results = asyncio.run(ingest.run_cycle(conn, catalog, ai.client()))
        errors = sum(1 for result in results if result.error)
        new = sum(result.new for result in results)
        print(f"{len(results)} flux relevés, {new} nouveaux articles, {errors} en erreur")
    else:
        app = web.create_app(settings, catalog, ai.client())
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)
