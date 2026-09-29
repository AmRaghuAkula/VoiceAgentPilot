// Role assignments on the AI Services (Foundry) account. Deployed at the AI account's own
// resource group scope, which may be in a different subscription from the rest of the stack
// (D-001: the pilot's AI resource lives in a separate subscription). Kept apart from
// roleassignments.bicep (Key Vault, same resource group as the app) because a module has a
// single deployment scope.
//
// Least privilege (D-032): only Foundry User for the app identity. The accelerator's default
// extra grants on the AI account (Cognitive Services OpenAI User, Reader) are not carried over.

@description('Name of the existing AI Services account in this module\'s resource group.')
param aiServicesName string

@description('Principal ID of the app\'s identity (the Container App\'s user-assigned identity, D-031). Granted Foundry User for the Voice Live agent connection.')
param appPrincipalId string

@description('Principal ID of the ACS resource\'s system-assigned identity. Granted Cognitive Services User so ACS can call the AI account for its own text-to-speech. Empty skips the assignment.')
param acsPrincipalId string = ''

// Built-in role definition IDs (checked with `az role definition list --name <id>`).
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Foundry User (formerly Azure AI User)
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: aiServicesName
}

resource foundryUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, appPrincipalId, foundryUserRoleId)
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource acsTtsRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(acsPrincipalId)) {
  name: guid(aiServices.id, acsPrincipalId, cognitiveServicesUserRoleId)
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: acsPrincipalId
    principalType: 'ServicePrincipal'
  }
}
