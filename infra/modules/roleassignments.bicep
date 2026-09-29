param identityPrincipalId string
param keyVaultName string

// AI Services role assignments moved to airoleassignments.bicep, which is deployed at the AI
// account's own (possibly cross-subscription) resource group scope (M6 plan C5, D-032).

resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' existing = {
  name: keyVaultName
}

// Key Vault Secrets User: getSecret + readMetadata only, all that Container Apps Key Vault secret
// references need. Upstream granted b86a8fe4-..., which is Key Vault Secrets Officer (full secrets
// data-plane access, secrets/*), under this same name (M6 plan T4, U14b).
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// The name seed is deliberately unchanged: an environment that already holds the old Officer
// assignment fails loudly with RoleAssignmentUpdateNotPermitted instead of silently keeping it.
// Delete that assignment first.
resource keyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identityPrincipalId, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}
