# news-feed

Agrégateur d'actualité personnel, pensé pour être une source d'actu unique. Il suit tous les sujets et regroupe chaque événement couvert par plusieurs médias français et anglophones. Il permet aussi de creuser : comprendre le contexte d'une info et poser des questions de suivi, sources citées. Les approfondissements portent surtout sur l'IA et la cybersécurité.

## Fonctionnalités

**Disponible aujourd'hui**
- **Relève** d'environ 80 flux RSS/Atom FR et EN : généralistes (à la une et rubriques), IA, cybersécurité.
  - Requêtes conditionnelles (ETag / Last-Modified), pour ne retélécharger que ce qui a changé.
  - Dédoublonnage par URL, en retirant les paramètres de suivi.
  - Seuls les articles de moins de 7 jours sont gardés : certains flux exposent toute leur archive.
- **Stories** : un événement = une story, toutes langues confondues. Deux articles vont dans la même story si leurs embeddings multilingues sont assez proches, ce qui évite les doublons FR/EN.
- **Aperçu** d'une story, sans IA : les chapôs distincts de ses articles, combinés jusqu'à une longueur correcte à lire. Dépend de ce que fournissent les flux : parfois juste quelques lignes, souvent assez pour tenir l'écran d'un téléphone. En un clic, la « Synthèse détaillée » (IA) donne un vrai aperçu détaillé, quelles que soient les sources.
- **Tags multiples** par article (13 thèmes, dont IA et cyber, plus le tag géographique `france`), attribués par un petit modèle. Les tags d'une story sont ceux portés par au moins un tiers de ses articles.
- **Traduction automatique** du titre et du chapô des stories importantes qui n'ont aucun article dans l'une des deux langues.
- **Site web** :
  - tri *Top* (par importance) ou *Récent* (chronologique, sur toutes les stories un minimum importantes) ;
  - filtres :
    - période (heure, jour, semaine, mois, année) ;
    - zone (*Tout*, *France* : tag `france`, *Monde* : tag `international`) ;
    - thèmes ;
    - recherche plein texte insensible aux accents ;
  - nombre de sujets correspondants, pagination numérotée ;
  - heure de la dernière relève et bouton *Actualiser*, qui lance une relève tout de suite ;
  - page story qui liste toutes les sources ;
  - bascule FR/EN en un clic ;
  - mise en page pensée d'abord pour le téléphone.
- **Creuser** :
  - **lecture** d'un article en mode lecture dans l'app (texte extrait de la page, quand elle n'est pas réservée aux abonnés), **traduction** et **résumé** à la demande ;
  - sur chaque story, un fil de **questions de suivi**, conservé. Deux questions toutes prêtes : « Résumé des sources » (faits établis, divergences, incertitudes) — une synthèse détaillée, affichée juste sous le chapô dès qu'elle a été demandée une fois — et « Comprendre le contexte » (historique, acteurs, notions clés, enjeux). Pour répondre, l'IA lit les versions gratuites des articles. Elle peut aussi chercher sur **Wikipédia** (les bases) et dans **l'archive de l'app** (ce qui s'est passé avant) ;
  - **brief** de la sélection affichée (par exemple « la semaine en géopolitique »).
- **Sources citées** : chaque réponse de l'IA cite ses sources en [n], des liens cliquables vers les articles, les pages Wikipédia ou les stories.
- **Rien n'est payé deux fois** : résumés, traductions et briefs sont mis en cache. Seuls les embeddings, les tags et la traduction des titres tournent automatiquement ; tout le reste de l'IA ne part que sur un clic.
- **Importance** d'une story sur une période : nombre de médias distincts, plus ceux qui l'ont mise à la une. Une story est « un minimum importante » si au moins deux médias la couvrent, ou si elle est à la une, ou si elle vient d'une source spécialisée.
- **Rétention** : les stories importantes sont gardées sans limite, pour les tops sur un mois ou un an et la recherche. Les stories mineures sont supprimées après 30 jours.

**Feuille de route**
1. Socle : catalogue, relève, stockage, CLI, CI. ✅
2. Enrichissement : stories multilingues, tags, score par période, recherche plein texte. ✅
3. Site web et mise en ligne sur Azure Container Apps (CI/CD). ✅
4. Creuser : lecture, traduction et résumé d'article, questions de suivi avec Wikipédia et l'archive, brief, sources citées. ✅
5. Plus tard : PWA, digest du matin, dossiers de fond, hébergement sur NAS Synology.

## Démarrage

Prérequis : [uv](https://docs.astral.sh/uv/), et une ressource Microsoft Foundry pour l'IA (voir [Déploiement](#déploiement-sur-azure-container-apps), étape 1).

```bash
uv sync                                  # installe l'environnement
cp .env.example .env                     # puis y mettre l'endpoint et la clé Foundry
uv run --env-file .env news-feed fetch   # une première relève avec l'IA (1 à 2 min)
uv run --env-file .env news-feed serve   # site sur http://127.0.0.1:8000, avec relève périodique
```

Sans `.env`, `uv run news-feed fetch` relève les flux sans IA : pas de stories, donc un site vide.

**Tester sur téléphone** : lancer `uv run --env-file .env news-feed serve --host 0.0.0.0`, puis ouvrir `http://<IP-du-PC>:8000` depuis le téléphone, sur le même Wi-Fi. Pour trouver l'IP : `ipconfig`, ligne « Adresse IPv4 ». Il faut autoriser Python dans le pare-feu Windows quand il le demande. Attention : sur le réseau local, il n'y a pas de login. N'importe qui sur ce Wi-Fi peut utiliser l'app, et donc l'IA.

Les données sont stockées dans `data/news-feed.db`, un fichier ignoré par git.

Une relève enchaîne les étapes suivantes :
1. téléchargement des flux ;
2. embeddings et rattachement aux stories ;
3. tags ;
4. traductions ;
5. rétention.

Les étapes IA sont sautées, avec un avertissement, si l'IA n'est pas configurée. Une étape IA interrompue est reprise à la relève suivante. Le serveur fait sa première relève 60 s après le démarrage, puis une toutes les `NEWS_FEED_REFRESH_MINUTES` (20 min par défaut). Le bouton *Actualiser* en lance une immédiatement, sauf si une relève est déjà en cours. Une page ouverte ne se recharge pas d'elle-même.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `NEWS_FEED_DATA_DIR` | `data` (`/data` dans l'image) | Dossier de la base SQLite |
| `NEWS_FEED_REFRESH_MINUTES` | `20` | Intervalle de relève du serveur ; `0` la désactive |
| `NEWS_FEED_ALLOWED_USER` | – | Login GitHub seul autorisé, derrière Easy Auth (Azure). Absent : pas de contrôle |
| `OPENAI_BASE_URL` | – | Endpoint v1 de la ressource Microsoft Foundry, par exemple `https://<ressource>.openai.azure.com/openai/v1/` |
| `OPENAI_API_KEY` | – | Clé de la ressource Foundry. Sans elle, l'IA est désactivée |

En local, mettre ces variables dans un fichier `.env` (ignoré par git, modèle : [.env.example](.env.example)) et lancer les commandes avec `uv run --env-file .env …`. Sur Azure, elles sont posées par le Bicep.

L'app appelle les modèles par leur **nom de déploiement** Foundry. Il faut donc créer, dans la ressource :

| Déploiement | Modèle | Usage |
|---|---|---|
| `embed` | `text-embedding-3-large` | Embeddings (512 dimensions) pour regrouper les stories |
| `fast` | un GPT « mini » récent | Tâches de masse : tags, traductions de titres |
| `main` | un GPT récent | À la demande : résumés, traductions d'articles, briefs, questions de suivi (avec appels d'outils) |

Le filtre de contenu d'Azure se déclenche parfois sur l'actualité (attentats, guerres, propos haineux cités).
- Pour les tags et les traductions de titres, un lot rejeté est coupé en deux jusqu'à isoler l'article en cause. Seul cet article reste sans tag, et il est retenté à la relève suivante.
- À la demande, l'app affiche « L'IA n'a pas pu répondre » au lieu d'une erreur.

Si ça arrive souvent, il faut assouplir le filtre : dans Foundry, *Guardrails + controls* → *Content filters*, créer un filtre au seuil *High* pour la haine et la violence, puis l'attacher aux déploiements `fast` et `main`.

Les seuils de regroupement sont dans [stories.py](src/news_feed/stories.py) : `SIMILARITY_THRESHOLD`, et `FOCUS_SIMILARITY_THRESHOLD`, plus strict pour les sources spécialisées. Ils ont été calibrés sur une vraie journée de titres FR + EN avec `text-embedding-3-large` ; changer de modèle d'embeddings demande de les recalibrer.

## Sources et tags

Le catalogue est dans [src/news_feed/sources.toml](src/news_feed/sources.toml). La commande `uv run pytest tests/test_config.py` le valide.

- **Ajouter un flux** : ajouter un bloc `[[feeds]]` avec les champs suivants.
  - `source` : nom du média. Plusieurs rubriques d'un même média comptent pour une seule source.
  - `url` : en https.
  - `lang` : `fr` ou `en`.
  - `une` : flux « à la une », qui compte comme signal d'importance.
  - `paywall` : articles souvent payants.
  - `focus` : source spécialisée d'un sujet approfondi, dont chaque article compte même s'il est seul sur son sujet.
- **Tags** : blocs `[[tags]]`. La `description` guide le classifieur IA. Les tags `france` et `international` alimentent aussi le filtre France / Monde (`ZONES` dans [web.py](src/news_feed/web.py)).

Avant de valider un nouveau flux, lancer `uv run news-feed fetch` : les flux en erreur apparaissent dans les logs.

## Qualité et CI/CD

```bash
uv run pytest                                       # tests (aucun appel réseau)
uv run pytest tests/test_ingest.py::test_parse_atom # un seul test
uv run ruff check --fix . && uv run ruff format .   # lint et formatage
```

- **Hook local** : `git config core.hooksPath .githooks` (une fois par clone). Avant chaque `git push`, il lance exactement les vérifications du job `check` de la CI (`ruff check`, `ruff format --check`, `pytest`) et bloque le push si l'une échoue.
- **Image Docker**, à vérifier en local quand le Dockerfile ou les dépendances changent. C'est le même smoke test que la CI :

  ```bash
  docker build -t news-feed .
  docker run -d --name smoke -p 8000:8000 -e NEWS_FEED_REFRESH_MINUTES=0 news-feed
  curl -f http://localhost:8000/healthz; docker rm -f smoke
  ```

- **CI GitHub Actions** ([.github/workflows/ci.yml](.github/workflows/ci.yml)) :
  1. `check`, sur chaque push et chaque PR : lint, formatage, tests et validation de l'infra Bicep.
  2. `image` : build de l'image et smoke test. Sur `main`, l'image est aussi publiée sur GHCR (`ghcr.io/spaard/news-feed`, tags `<sha>` et `latest`).
  3. `deploy`, sur `main` uniquement et si les deux jobs précédents passent : la Container App passe sur la nouvelle image. Ce job est sauté tant que les variables Azure du repo ne sont pas définies.

## Déploiement sur Azure Container Apps

À la fin, le groupe de ressources `news-feed` contient :

| Ressource | Type Azure | Créée par | Rôle |
|---|---|---|---|
| `news-feed-ai` (nom libre) | Microsoft Foundry (*Azure AI Services*) | toi, dans le portail | Les modèles `embed`, `fast` et `main` |
| `news-feed` | Container App | [infra/main.bicep](infra/main.bicep) | L'app : 1 réplique toujours allumée, 0,25 vCPU / 0,5 Go, HTTPS, login GitHub (Easy Auth) restreint à `allowedUser` |
| `news-feed-env` | Container Apps Environment | Bicep | L'environnement qui héberge la Container App |
| `newsfeed<suffixe>` | Storage account + partage Azure Files `data` | Bicep | La base SQLite, montée sur `/data` |
| `news-feed-logs` | Log Analytics workspace | Bicep | Les logs de l'app (30 jours) |

Hors du groupe : une inscription d'application Entra ID `news-feed-ci`, que GitHub Actions utilise pour déployer (étape 7). L'image est sur GHCR (GitHub), pas dans un registre Azure.

Coût indicatif : 5 à 15 €/mois pour la Container App, plus l'usage de Foundry.

**Prérequis** : l'Azure CLI en local (`winget install Microsoft.AzureCLI`, puis `az login`). Les commandes ci-dessous sont pour PowerShell, à lancer depuis la racine du repo. Ensuite :

```powershell
az group create --name news-feed --location francecentral
```

### Mise en place, une seule fois

1. **Foundry**. Dans le portail [ai.azure.com](https://ai.azure.com), créer une ressource Foundry :
   - groupe de ressources : `news-feed` ;
   - région : celle qui propose les modèles voulus (*Sweden Central* ou *East US 2* ont le catalogue le plus large ; elle peut différer de celle du groupe).

   Puis, dans *Modèles + points de terminaison* → *Déployer un modèle*, créer trois déploiements, de type *Global Standard*. Leurs **noms** doivent être exactement :

   | Nom du déploiement | Modèle | Quota conseillé |
   |---|---|---|
   | `embed` | `text-embedding-3-large` | ≥ 150 k tokens/min |
   | `fast` | le GPT « mini » le plus récent | ≥ 200 k tokens/min : la première relève classe environ 1 800 articles |
   | `main` | le GPT le plus récent | celui proposé par défaut |

   Enfin, noter l'endpoint et la clé (vue d'ensemble de la ressource). L'endpoint à utiliser est `https://<ressource>.openai.azure.com/openai/v1/`. Les mettre dans `.env` (voir [.env.example](.env.example)), puis **tester en local** (voir [Démarrage](#démarrage)).
2. **Publier l'image** : committer, puis pousser sur `main`. La CI publie `ghcr.io/spaard/news-feed:latest`, en paquet privé. Le job `deploy` est sauté à ce stade, c'est normal.
3. **OAuth App GitHub** (GitHub → Settings → Developer settings → OAuth Apps → New OAuth App) : mettre une URL provisoire, par exemple `https://github.com`. Noter le *Client ID* et générer un *Client secret*.
4. **Token GHCR** (GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)) : scope `read:packages`, sans expiration ou avec un rappel pour le renouveler. Azure s'en sert pour tirer l'image.
5. **Déployer l'infra**. Cette commande crée toutes les ressources « Bicep » du tableau :

   ```powershell
   az deployment group create --resource-group news-feed --template-file infra/main.bicep `
     --parameters allowedUser=<login-github> registryUser=<login-github> registryToken=<token-ghcr> `
                  foundryEndpoint=https://<ressource>.openai.azure.com/openai/v1/ foundryKey=<clé-foundry> `
                  githubClientId=<client-id> githubClientSecret=<client-secret> `
     --query properties.outputs.url.value
   ```

6. **Finaliser l'OAuth App** : *Homepage URL* = l'URL affichée à l'étape 5 ; *Authorization callback URL* = `<url>/.auth/login/github/callback`.

   Ouvrir l'URL, sur ordinateur ou sur téléphone : GitHub demande de se connecter, puis le site s'affiche. Il reste vide pendant les premières minutes : la première relève démarre 60 s après le lancement et dure 1 à 2 minutes.
7. **Déploiement continu**, par OIDC, sans secret stocké dans GitHub :

   ```powershell
   $clientId = az ad app create --display-name news-feed-ci --query appId -o tsv
   az ad sp create --id $clientId
   '{"name": "main", "issuer": "https://token.actions.githubusercontent.com",
     "subject": "repo:Spaard/news-feed:ref:refs/heads/main",
     "audiences": ["api://AzureADTokenExchange"]}' | Set-Content credential.json
   az ad app federated-credential create --id $clientId --parameters credential.json
   Remove-Item credential.json
   az role assignment create --assignee $clientId --role Contributor `
     --scope (az group show --name news-feed --query id -o tsv)
   "AZURE_CLIENT_ID=$clientId"
   az account show --query "{AZURE_TENANT_ID: tenantId, AZURE_SUBSCRIPTION_ID: id}"
   ```

   Ensuite, dans le repo GitHub (Settings → Secrets and variables → Actions → **Variables**), créer :
   - `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` et `AZURE_SUBSCRIPTION_ID`, avec les valeurs affichées ;
   - `AZURE_RESOURCE_GROUP` = `news-feed` ;
   - `AZURE_CONTAINERAPP_NAME` = `news-feed`.

   À partir de là, chaque push sur `main` qui passe la CI met l'app à jour, avec une nouvelle révision sur l'image du commit. Pour vérifier un déploiement, regarder le job `deploy` dans l'onglet *Actions* du repo, puis ouvrir l'URL.

Le job `deploy` ne change que l'image. Pour une modification de [infra/main.bicep](infra/main.bicep) (ressources, variables d'environnement) ou d'un secret (clé Foundry, token GHCR), relancer la commande de l'étape 5.

Logs en direct : `az containerapp logs show --name news-feed --resource-group news-feed --follow`.
