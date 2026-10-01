// Resource-group-scoped resources for sms-notify. No secret values appear here (spec K7, section 7).
param location string
param tags object
param vaultOfficerPrincipalId string
@description('Non-secret SMS_* app settings (spec section 7).')
param appSettings object

var token = toLower(uniqueString(subscription().id, resourceGroup().id, location))
var storageName = 'stsms${take(token, 18)}'
var keyVaultName = 'kv-sms-${take(token, 13)}'
var functionAppName = 'func-sms-${token}'
var planName = 'plan-sms-${token}'
var deploymentContainer = 'app-package'
var stateContainer = 'sms-state'

// Built-in role definition IDs.
var roleStorageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var roleStorageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleKeyVaultSecretsOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var roleMonitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-sms-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-sms-${token}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainer
  properties: { publicAccess: 'None' }
}

resource smsStateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: stateContainer
  properties: { publicAccess: 'None' }
}

// State retention (spec section 5): dedupe hashes and hourly slots are deleted one day after last change.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-sms-state-after-1-day'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${stateContainer}/dedupe/', '${stateContainer}/rate/']
            }
            actions: {
              baseBlob: {
                delete: { daysAfterModificationGreaterThan: 1 }
              }
            }
          }
        }
      ]
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  dependsOn: [packageContainer]
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientCertEnabled: false
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      http20Enabled: true
      cors: { allowedOrigins: [] }
      appSettings: concat(
        [
          { name: 'AzureWebJobsStorage__accountName', value: storage.name }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
          { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD' }
          { name: 'KEY_VAULT_URI', value: vault.properties.vaultUri }
          { name: 'STATE_BLOB_URL', value: '${storage.properties.primaryEndpoints.blob}${stateContainer}' }
        ],
        map(items(appSettings), s => { name: s.key, value: s.value })
      )
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.12' }
    }
  }
}

resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'ftp'
  properties: { allow: false }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-04-01' = {
  parent: site
  name: 'scm'
  properties: { allow: false }
}

// The Functions host itself (AzureWebJobsStorage, identity-based) and the Flex deployment
// package need Blob Data Owner on the account; the spec's container-scoped grant on sms-state
// is kept as well so the state access is explicit.
resource hostStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, site.id, roleStorageBlobDataOwner)
  scope: storage
  properties: {
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataOwner)
  }
}

resource stateRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(smsStateContainer.id, site.id, roleStorageBlobDataContributor)
  scope: smsStateContainer
  properties: {
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
  }
}

resource vaultReaderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, site.id, roleKeyVaultSecretsUser)
  scope: vault
  properties: {
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
  }
}

resource vaultOfficerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(vaultOfficerPrincipalId)) {
  name: guid(vault.id, vaultOfficerPrincipalId, roleKeyVaultSecretsOfficer)
  scope: vault
  properties: {
    principalId: vaultOfficerPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsOfficer)
  }
}

resource insightsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, site.id, roleMonitoringMetricsPublisher)
  scope: insights
  properties: {
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleMonitoringMetricsPublisher)
  }
}

output functionAppName string = site.name
output functionHost string = site.properties.defaultHostName
output keyVaultName string = vault.name
output keyVaultUri string = vault.properties.vaultUri
output functionPrincipalId string = site.identity.principalId
