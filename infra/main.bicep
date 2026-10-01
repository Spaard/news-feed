// Infrastructure Azure de news-feed : une Container App toujours allumée, sa base SQLite sur
// Azure Files, l'accès protégé par Easy Auth (GitHub). Mise en place : voir le README.

@description('Nom de la Container App, préfixe des autres ressources.')
param name string = 'news-feed'

param location string = resourceGroup().location

@description('Image du conteneur. Le job deploy de la CI la remplace à chaque push sur main.')
param image string = 'ghcr.io/spaard/news-feed:latest'

@description('Compte GitHub propriétaire : le seul autorisé à utiliser l\'app.')
param allowedUser string

@description('Endpoint v1 de la ressource Microsoft Foundry, ex. https://<ressource>.openai.azure.com/openai/v1/')
param foundryEndpoint string

@secure()
param foundryKey string

@description('Compte GitHub qui tire l\'image depuis GHCR.')
param registryUser string

@secure()
@description('Token GitHub (classic) avec le scope read:packages.')
param registryToken string

@description('Client ID de l\'OAuth App GitHub utilisée par Easy Auth.')
param githubClientId string

@secure()
param githubClientSecret string

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('${replace(name, '-', '')}${uniqueString(resourceGroup().id)}', 24)
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: 'data'
  properties: { shareQuota: 5 }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        { server: 'ghcr.io', username: registryUser, passwordSecretRef: 'registry-token' }
      ]
      secrets: [
        { name: 'registry-token', value: registryToken }
        { name: 'foundry-key', value: foundryKey }
        { name: 'github-client-secret', value: githubClientSecret }
      ]
    }
    template: {
      containers: [
        {
          name: 'news-feed'
          image: image
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'OPENAI_BASE_URL', value: foundryEndpoint }
            { name: 'OPENAI_API_KEY', secretRef: 'foundry-key' }
            { name: 'NEWS_FEED_ALLOWED_USER', value: allowedUser }
          ]
          volumeMounts: [ { volumeName: 'data', mountPath: '/data' } ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000 } }
          ]
        }
      ]
      // Une seule instance : SQLite n'accepte qu'un écrivain.
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: environmentStorage.name
          // SQLite sur SMB : pas de verrous par plage (un seul process écrit), fichiers à l'uid de l'image.
          mountOptions: 'uid=1000,gid=1000,nobrl,mfsymlinks,cache=none'
        }
      ]
    }
  }
}

resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'github'
      // Le téléphone télécharge le manifest et les icônes sans cookie pour installer l'app.
      excludedPaths: [
        '/static/manifest.json'
        '/static/icon-192.png'
        '/static/icon-512.png'
        '/static/apple-touch-icon.png'
      ]
    }
    // Session de 30 jours (8 h par défaut) : l'app installée ne redemande pas le login chaque jour.
    login: {
      cookieExpiration: { convention: 'FixedTime', timeToExpiration: '30.00:00:00' }
    }
    identityProviders: {
      gitHub: {
        registration: {
          clientId: githubClientId
          clientSecretSettingName: 'github-client-secret'
        }
      }
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
