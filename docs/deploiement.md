# Déployer news-feed sur Azure

Ce guide met l'app en ligne sur **Azure Container Apps**, derrière un login GitHub. Il faut compter une demi-heure, et la mise en place ne se fait qu'une fois. Pour comprendre ce que fait chaque étape, voir [Comment ça marche](architecture.md).

## Ce qu'on obtient

| Ressource | Type Azure | Créée par | Rôle |
|---|---|---|---|
| `news-feed` (nom libre) | Microsoft Foundry | toi, dans le portail | Les modèles `embed`, `fast` et `main` |
| `news-feed` | Container App | Bicep | L'app : toujours allumée, 0,25 vCPU, 0,5 Go, HTTPS, login GitHub |
| `news-feed-env` | Container Apps Environment | Bicep | L'environnement qui héberge la Container App |
| `newsfeed<suffixe>` | Compte de stockage, partage Azure Files `data` | Bicep | La base SQLite, montée sur `/data` |
| `news-feed-logs` | Log Analytics | Bicep | Les logs de l'app, gardés 30 jours |

Hors d'Azure :
- l'image Docker, sur GHCR (le registre de GitHub) ;
- une OAuth App GitHub, pour le login ;
- une inscription d'application Entra ID, `news-feed-ci`, pour le déploiement continu.

**Coût indicatif** : 5 à 15 € par mois pour la Container App, plus l'usage de Foundry.

## Prérequis

- **L'Azure CLI** : `winget install Microsoft.AzureCLI`. Rouvrir ensuite le terminal pour qu'il trouve `az`, puis lancer `az login`.
  - Si Windows ne propose que des comptes déjà enregistrés, et que « Autre compte » ne s'ouvre pas, utiliser `az login --use-device-code`.
- **Un groupe de ressources**. Les commandes sont pour PowerShell, à lancer depuis la racine du repo.

  ```powershell
  az group create --name news-feed --location francecentral
  ```

  Dans un abonnement d'entreprise, on n'a souvent pas le droit de créer un groupe. On utilise alors un groupe existant, en remplaçant `news-feed` par son nom partout où il désigne le groupe (`--resource-group`, `--name` de `az group show`, variable `AZURE_RESOURCE_GROUP`). Les ressources prennent la région de ce groupe.

## 1. Microsoft Foundry

Dans [ai.azure.com](https://ai.azure.com), créer une ressource Foundry dans ton groupe.
- **Région** : choisir celle qui propose les modèles voulus. *Sweden Central* et *East US 2* ont le catalogue le plus large.
- **La région peut différer de celle du groupe.**

Dans *Modèles + points de terminaison* → *Déployer un modèle*, créer trois déploiements de type *Global Standard*. Leurs **noms** doivent être exactement les suivants :

| Nom | Modèle | Quota conseillé |
|---|---|---|
| `embed` | `text-embedding-3-large` | ≥ 150 k tokens/min |
| `fast` | le GPT « mini » le plus récent | ≥ 200 k tokens/min (la première relève classe environ 1 800 articles) |
| `main` | le GPT le plus récent | celui par défaut |

Noter ensuite l'**endpoint** et la **clé**, dans la vue d'ensemble de la ressource. L'endpoint à utiliser est `https://<ressource>.openai.azure.com/openai/v1/` : reprends exactement le sous-domaine affiché.

Les mettre dans `.env` (modèle : [.env.example](../.env.example)) et tester en local avant d'aller plus loin.

**Filtre de contenu.** Azure filtre parfois l'actualité : attentats, guerres, propos haineux cités. L'app s'en accommode :
- en continu, un lot rejeté est coupé en deux jusqu'à isoler l'article en cause ;
- sur un clic, elle affiche « L'IA n'a pas pu répondre ».

Si ça arrive souvent : dans *Guardrails + controls* → *Content filters*, créer un filtre au seuil *High* pour la haine et la violence, et l'attacher aux déploiements `fast` et `main`.

## 2. Publier l'image

Pousser sur `main`. La CI construit l'image, vérifie qu'elle démarre, puis la publie en paquet privé sous `ghcr.io/<compte>/news-feed`, avec deux étiquettes : `<sha du commit>` et `latest`.

À ce stade, le job `deploy` est sauté : c'est normal.

## 3. OAuth App GitHub

Ouvrir [github.com/settings/applications/new](https://github.com/settings/applications/new) :
- **Homepage URL** et **Authorization callback URL** : `https://github.com`, de façon provisoire. On ne connaît pas encore l'URL de l'app.
- Laisser les cases par défaut.

Noter le **Client ID**, puis générer et noter un **Client secret**. GitHub ne le réaffichera plus.

## 4. Token GHCR

Ouvrir [github.com/settings/tokens](https://github.com/settings/tokens), puis *Generate new token (classic)*.
- **Scope** : `read:packages` uniquement.
- **Expiration** : aucune, ou une date avec un rappel pour le renouveler.

Azure s'en sert pour télécharger l'image privée.

## 5. Déployer l'infrastructure

Cette commande crée toutes les ressources « Bicep » du tableau, en 3 à 5 minutes, et affiche l'URL de l'app :

```powershell
az deployment group create --resource-group news-feed --template-file infra/main.bicep `
  --parameters allowedUser=<login-github> registryUser=<login-github> registryToken=<token-ghcr> `
               foundryEndpoint=https://<ressource>.openai.azure.com/openai/v1/ foundryKey=<clé-foundry> `
               githubClientId=<client-id> githubClientSecret=<client-secret> `
  --query properties.outputs.url.value
```

`allowedUser` est le seul compte GitHub qui aura accès à l'app.

## 6. Finaliser l'OAuth App

Dans l'OAuth App de l'étape 3, remplacer les deux URL provisoires :
- **Homepage URL** : l'URL affichée à l'étape 5 ;
- **Authorization callback URL** : `<url>/.auth/login/github/callback`.

Ouvrir ensuite l'URL. GitHub demande de se connecter et d'autoriser l'app, puis le site s'affiche.

Il reste vide les premières minutes : la première relève démarre 60 s après le lancement et dure 1 à 2 minutes. La base de production part de zéro, sans reprendre la base locale.

## 7. Déploiement continu (OIDC)

Cette étape permet à GitHub Actions de déployer chaque push vert sur `main`, sans stocker de mot de passe Azure dans GitHub.

Créer l'identité de la CI et lui donner le droit d'agir sur le groupe :

```powershell
$clientId = az ad app create --display-name news-feed-ci --query appId -o tsv
az ad sp create --id $clientId
'{"name": "main", "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<compte>/news-feed:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]}' | Set-Content credential.json
az ad app federated-credential create --id $clientId --parameters credential.json
Remove-Item credential.json
az role assignment create --assignee $clientId --role Contributor `
  --scope (az group show --name news-feed --query id -o tsv)
"AZURE_CLIENT_ID=$clientId"
az account show --query "{AZURE_TENANT_ID: tenantId, AZURE_SUBSCRIPTION_ID: id}"
```

Dans le repo GitHub, ouvrir *Settings* → *Secrets and variables* → *Actions* → onglet **Variables**, et créer :

| Variable | Valeur |
|---|---|
| `AZURE_CLIENT_ID` | affichée par la commande |
| `AZURE_TENANT_ID` | affichée par la commande |
| `AZURE_SUBSCRIPTION_ID` | affichée par la commande |
| `AZURE_RESOURCE_GROUP` | le nom du groupe |
| `AZURE_CONTAINERAPP_NAME` | `news-feed` |

Dès le push suivant, le job `deploy` passe la Container App sur l'image du commit.

**Si les droits manquent.** Attribuer un rôle demande d'être *Owner* ou *User Access Administrator* sur le groupe, et créer une inscription d'application peut être réservé aux admins de l'annuaire. Si la commande est refusée, il faut demander l'un de ces droits à l'administrateur Azure, ou rester en mise à jour manuelle (voir ci-dessous).

## Au quotidien

| Je veux… | Je fais… |
|---|---|
| Mettre à jour le code | Pousser sur `main`. Avec l'étape 7, la CI déploie toute seule |
| Mettre à jour sans l'étape 7 | Attendre que le job `image` du commit soit vert, puis lancer `az containerapp update --name news-feed --resource-group news-feed --image ghcr.io/<compte>/news-feed:<sha complet>`. Préférer le sha à `latest`, qui peut encore désigner l'image précédente pendant que la CI tourne |
| Changer l'infra, une variable d'environnement ou un secret (clé Foundry, token GHCR) | Relancer la commande de l'étape 5 |
| Lire les logs en direct | `az containerapp logs show --name news-feed --resource-group news-feed --follow` |

## Dépannage

| Symptôme | Cause | Solution |
|---|---|---|
| `az` n'est pas reconnu après l'installation | Le terminal a gardé l'ancien PATH | Fermer et rouvrir le terminal, voire VS Code |
| `az login` ne propose que de mauvais comptes | Le broker de comptes de Windows | `az login --use-device-code` |
| `AuthorizationFailed` sur `az group create` | Pas le droit de créer un groupe | Utiliser un groupe existant (voir Prérequis) |
| `401` de Foundry en local | L'endpoint ne correspond pas à la ressource de la clé | Recopier exactement le sous-domaine de la ressource |
| GitHub affiche « redirect_uri is not associated » | L'OAuth App a encore l'URL provisoire | Étape 6 |
| « Accès refusé » après le login | Le compte GitHub n'est pas `allowedUser` | Relancer l'étape 5 avec le bon login |
| `{"detail":"Not Found"}` sur `/.auth/me` | Cette page d'Easy Auth n'existe pas ici, faute de stockage de jetons | Ouvrir la racine de l'URL |
| L'app ne change pas après un `az containerapp update …:latest` | `latest` désignait encore l'ancienne image | Utiliser le sha complet du commit |
