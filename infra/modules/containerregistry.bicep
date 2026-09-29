param location string
param uniqueSuffix string
param identityName string
param tags object = {}

// Admin user off: the Container App pulls with AcrPull on its managed identity (below), and
// azd's remote build (ACR Tasks) runs under the deploying principal's ARM token, never admin creds.
param adminUserEnabled bool = false
param dataEndpointEnabled bool = false
param encryption object = {
  status: 'disabled'
}
param networkRuleBypassOptions string = 'AzureServices'
param publicNetworkAccess string = 'Enabled'
// Basic: one image, one pilot app; ACR Tasks (remote build) is available on every tier.
param sku object = {
  name: 'Basic'
}
param zoneRedundancy string = 'Disabled'

// Hardcode container registry name with unique suffix
var containerRegistryName = take('cr${uniqueSuffix}', 32)

// 2023-07-01 stable API version
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: containerRegistryName
  location: location
  tags: tags
  sku: sku
  properties: {
    adminUserEnabled: adminUserEnabled
    dataEndpointEnabled: dataEndpointEnabled
    encryption: encryption
    networkRuleBypassOptions: networkRuleBypassOptions
    publicNetworkAccess: publicNetworkAccess
    zoneRedundancy: zoneRedundancy
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: identityName }

resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: containerRegistry
  name: guid(subscription().id, resourceGroup().id, appIdentity.id, 'acrPullRole')
  properties: {
    roleDefinitionId:  subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalType: 'ServicePrincipal'
    principalId: appIdentity.properties.principalId
  }
}

output loginServer string = containerRegistry.properties.loginServer
output name string = containerRegistry.name
