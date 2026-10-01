<div align="center">

# news-feed

**Un fil d'actu unique : les médias français et anglophones regroupés par événement,
avec une IA pour comprendre et creuser, sources toujours citées.**

[![CI](https://github.com/Spaard/news-feed/actions/workflows/ci.yml/badge.svg)](https://github.com/Spaard/news-feed/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![uv](https://img.shields.io/badge/uv-managed-DE5FE9?logo=uv&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-FTS5-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![Azure Container Apps](https://img.shields.io/badge/Azure-Container%20Apps-0078D4)
![Microsoft Foundry](https://img.shields.io/badge/IA-Microsoft%20Foundry-5E5ADB)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

</div>

## L'app en bref

news-feed relève environ 80 flux RSS (Le Monde, France Info, BBC, The Guardian, NYT, médias tech, IA et cybersécurité…). Il regroupe en une seule *story* tous les articles qui parlent du même événement, quelle que soit leur langue, puis les classe par thème et par importance. On lit le tout en français ou en anglais, sur téléphone d'abord.

| | |
|---|---|
| 🗞️ **Tout suivre** | Un top par importance ou un fil chronologique, sur une heure, un jour, une semaine, un mois ou un an. Filtres France / Monde, par thème et recherche plein texte. |
| 🔗 **Une info, une story** | Les articles FR et EN d'un même événement sont regroupés par similarité sémantique : pas de doublon, et on voit combien de médias en parlent. |
| 🌍 **FR ⇄ EN en un clic** | Le même fil dans les deux langues. Titres traduits automatiquement quand aucun média ne couvre l'info dans la langue choisie. |
| 🔍 **Creuser** | Synthèse détaillée des sources, « Comprendre le contexte », questions de suivi. L'IA lit les articles gratuits, Wikipédia et l'archive de l'app. |
| 📚 **Sources citées** | Chaque réponse de l'IA renvoie à ses sources [n] : articles, pages Wikipédia, stories. |
| 💸 **IA maîtrisée** | Seuls le regroupement, les tags et les titres traduits tournent tout seuls, avec de petits modèles. Le reste ne part que sur un clic, et il est mis en cache. |

## Démarrage rapide

Prérequis : [uv](https://docs.astral.sh/uv/), et une ressource [Microsoft Foundry](docs/deploiement.md#1-microsoft-foundry) pour l'IA.

```bash
uv sync                                  # installe l'environnement
cp .env.example .env                     # puis y mettre l'endpoint et la clé Foundry
uv run --env-file .env news-feed fetch   # première relève (1 à 2 min)
uv run --env-file .env news-feed serve   # http://127.0.0.1:8000, relève toutes les 20 min
```

- **Sur téléphone** : lancer `serve --host 0.0.0.0`, puis ouvrir `http://<IP-du-PC>:8000` depuis le même Wi-Fi.
  - L'IP du PC est la ligne « Adresse IPv4 » de `ipconfig`.
  - En local, il n'y a pas de login : n'importe qui sur ce Wi-Fi a accès à l'app.
- **Sans `.env`** : les flux sont relevés, mais sans IA il n'y a ni story ni tag, et le site reste vide.
- **Les données** vont dans `data/news-feed.db`, un fichier ignoré par git.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `OPENAI_BASE_URL` | – | Endpoint v1 de la ressource Foundry : `https://<ressource>.openai.azure.com/openai/v1/` |
| `OPENAI_API_KEY` | – | Clé de la ressource Foundry. Sans elle, l'IA est désactivée |
| `NEWS_FEED_DATA_DIR` | `data` (`/data` dans l'image) | Dossier de la base SQLite |
| `NEWS_FEED_REFRESH_MINUTES` | `20` | Intervalle entre deux relèves ; `0` les désactive |
| `NEWS_FEED_ALLOWED_USER` | – | Seul login GitHub autorisé derrière Easy Auth (Azure). Absente : pas de contrôle d'accès |

L'app appelle trois **déploiements** Foundry, par leur nom :

| Déploiement | Modèle | Usage |
|---|---|---|
| `embed` | `text-embedding-3-large` | Regrouper les articles en stories |
| `fast` | GPT « mini » récent | En continu : tags, titres traduits |
| `main` | GPT récent | Sur un clic : synthèses, contexte, questions, résumés, traductions, briefs |

## Déploiement

L'app tourne dans un conteneur Docker sur **Azure Container Apps**, avec sa base SQLite sur Azure Files. L'accès passe par un login GitHub, réservé à un seul compte.
- L'infrastructure est décrite en **Bicep** ([infra/main.bicep](infra/main.bicep)).
- **GitHub Actions** teste, construit et publie l'image, puis la déploie à chaque push sur `main`.

- 🚀 [Mise en place pas à pas](docs/deploiement.md) : Foundry, OAuth GitHub, Bicep, déploiement continu, dépannage.
- 🧭 [Comment ça marche](docs/architecture.md) : le chemin d'une requête, où vit chaque morceau, le login, les secrets, la CI/CD, les coûts.

## Développement

```bash
uv run pytest                                       # tests (aucun appel réseau)
uv run pytest tests/test_ingest.py::test_parse_atom # un seul test
uv run ruff check --fix . && uv run ruff format .   # lint et formatage
git config core.hooksPath .githooks                 # une fois par clone : vérifications avant chaque push
```

- **Hook pre-push** : il lance exactement le job `check` de la CI. Un push qui casserait la CI est bloqué en local.
- **Image Docker** : à reconstruire et vérifier quand le Dockerfile ou les dépendances changent. C'est le même test que la CI :

  ```bash
  docker build -t news-feed . && docker run -d --name smoke -p 8000:8000 -e NEWS_FEED_REFRESH_MINUTES=0 news-feed
  curl -f http://localhost:8000/healthz; docker rm -f smoke
  ```

- **Sources et tags** : tout est dans [src/news_feed/sources.toml](src/news_feed/sources.toml), et `uv run pytest tests/test_config.py` le valide.
  - Un flux `[[feeds]]` déclare :
    - `source` : le nom du média ;
    - `url` : en https ;
    - `lang` : `fr` ou `en` ;
    - `une` : flux « à la une », un signal d'importance ;
    - `paywall` : articles souvent réservés aux abonnés ;
    - `focus` : source spécialisée, dont chaque article compte.
  - Un tag `[[tags]]` a une `description`, qui guide le classifieur. `france` et `international` servent aussi au filtre France / Monde.
  - Vérifier tout nouveau flux avec `uv run news-feed fetch` : les flux en erreur apparaissent dans les logs.

## Feuille de route

- [x] Relève, stockage, CLI et CI
- [x] Stories multilingues, tags, score par période, recherche plein texte
- [x] Site web pensé mobile d'abord, bascule FR/EN
- [x] Creuser : lecture, synthèse, contexte, questions de suivi, brief, sources citées
- [x] Mise en ligne sur Azure Container Apps (Bicep, Easy Auth, image publiée par la CI)
- [x] Déploiement continu par OIDC : chaque push vert sur `main` met l'app à jour
- [ ] Synthèse détaillée régénérée quand de nouveaux articles rejoignent la story
- [ ] PWA : installable sur l'écran d'accueil du téléphone
- [ ] Digest du matin
- [ ] Dossiers de fond : suivre une même affaire sur plusieurs mois
- [ ] Hébergement sur un NAS Synology, avec la même image
