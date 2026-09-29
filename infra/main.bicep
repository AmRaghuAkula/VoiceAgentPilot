targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the the environment which is used to generate a short unique hash used in all resources.')
param environmentName string

@minLength(1)
@description('Primary location for all resources. Regions with pre-deployed models (gpt-4o-mini): eastus2, japaneast, southeastasia, swedencentral, westus2. Other regions require BYOM. See https://learn.microsoft.com/azure/ai-services/speech-service/regions?tabs=voice-live')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadaeast'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'norwayeast'
  'southafricanorth'
  'southcentralus'
  'southeastasia'
  'swedencentral'
  'switzerlandnorth'
  'uksouth'
  'westeurope'
  'westus'
  'westus2'
  'westus3'
])
param location string

param appExists bool
@description('The OpenAI model name')
param modelName string = 'gpt-4o-mini'
@description('The selected telephony provider')
@allowed(['acs', 'twilio', 'infobip', 'genesys', 'sinch', 'bandwidth'])
param telephonyProvider string = 'acs'
@secure()
@description('Twilio Auth Token for webhook signature validation')
param twilioAuthToken string = ''
@secure()
@description('Infobip API Key for voice call handling')
param infobipApiKey string = ''
@description('Infobip API Base URL (e.g. https://xxxxx.api.infobip.com)')
param infobipApiBaseUrl string = ''
@secure()
@description('Genesys AudioHook API Key for Audio Connector authentication')
param genesysApiKey string = ''
@secure()
@description('Sinch Application Key for callback signature validation and WebSocket auth')
param sinchApplicationKey string = ''
@secure()
@description('Sinch Application Secret for callback signature validation')
param sinchApplicationSecret string = ''
@secure()
@description('Bandwidth OAuth 2.0 Client ID (used for API auth and webhook Basic Auth)')
param bandwidthClientId string = ''
@secure()
@description('Bandwidth OAuth 2.0 Client Secret (used for API auth and webhook Basic Auth)')
param bandwidthClientSecret string = ''
@description('Bandwidth account ID (required in the API path for all calls)')
param bandwidthAccountId string = ''
@description('Bandwidth Voice Application ID (auto-populated by postdeploy if empty)')
param bandwidthApplicationId string = ''
@description('Enable debug mode for verbose logging in the container app')
param debugMode bool = false

// [ Existing resources (M6 plan U14a) ]
// Each "existing" name below is optional: empty keeps the accelerator's create-new behavior.
// WARNING: when adopting existing resources, do NOT run `azd down` against that environment. It
// can delete resource groups this deployment touched, which then include the adopted resource
// group (with the ACS resource and its phone number) and, via the cross-subscription role
// assignment deployment, the AI account's resource group. Tear down individual resources instead.
// Set all of EXISTING_RESOURCE_GROUP_NAME, EXISTING_ACS_NAME and ACS_DATA_LOCATION together (a
// resource group without an ACS name would create a second ACS resource next to the real one).
@description('Existing resource group (in this deployment\'s subscription) to deploy into. Empty creates rg-<env>-<suffix>.')
param existingResourceGroupName string = ''
@description('Existing ACS resource (in the target resource group) to adopt. Empty creates a new one. Its system-assigned identity is turned on by this deployment.')
param existingAcsName string = ''
@description('ACS data location. Immutable: must equal the existing ACS resource\'s value when adopting one.')
param acsDataLocation string = 'United States'
@description('Existing AI Services (Foundry) account to use instead of creating one. Empty creates a new account.')
param existingAiServicesName string = ''
@description('Subscription of the existing AI Services account; may differ from this deployment\'s subscription. Empty means this subscription.')
param existingAiServicesSubscriptionId string = ''
@description('Resource group of the existing AI Services account. Required when existingAiServicesName is set.')
param existingAiServicesResourceGroup string = ''

// [ Bridge settings (server/app/bridge_config.py); empty values are left out of the container env ]
// AGENT_ROUTING_JSON arrives base64-encoded (azd env var AGENT_ROUTING_JSON_B64): azd substitutes
// ${VAR} into main.parameters.json as raw text with no JSON escaping, so a JSON value containing
// double quotes breaks the parameters file (verified with azd 1.34.2). Decoded back to the
// plain JSON string for the container's AGENT_ROUTING_JSON env var.
@description('Base64 of AGENT_ROUTING_JSON (E.164 number -> {project, agent, version}). Set with azd env set at deploy time; never committed.')
param agentRoutingJsonBase64 string = ''
param maxCallSeconds string = ''
param fallbackMessage string = ''
param ambientPreset string = ''
@description('ACS_CALLBACK_JWT_AUDIENCE. Must be set in a deployed environment once confirmed against a live callback (Q-005, D-036).')
param acsCallbackJwtAudience string = ''

var uniqueSuffix = substring(uniqueString(subscription().id, environmentName), 0, 5)
var tags = {'azd-env-name': environmentName }
var useExistingResourceGroup = !empty(existingResourceGroupName)
var rgName = useExistingResourceGroup ? existingResourceGroupName : 'rg-${environmentName}-${uniqueSuffix}'

resource newRg 'Microsoft.Resources/resourceGroups@2024-11-01' = if (!useExistingResourceGroup) {
  name: rgName
  location: location
  tags: tags
}

// Every module deploys into resourceGroup(rgName) with an explicit dependsOn on newRg, which orders
// creation when the group is new (a no-op when newRg's condition is false). Do not add an `existing`
// resource-group symbol with dependsOn: [newRg]: it compiles to the same resource ID as newRg, and
// ARM's sequencer rejects it as a self-dependency (Q-058).

var useExistingAiServices = !empty(existingAiServicesName)
// Only honor the subscription override when an existing account is actually named; otherwise the
// role-assignment module would target the new account's resource group in the wrong subscription.
var aiServicesSubscriptionId = (useExistingAiServices && !empty(existingAiServicesSubscriptionId)) ? existingAiServicesSubscriptionId : subscription().subscriptionId

resource existingAiServices 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = if (useExistingAiServices) {
  name: existingAiServicesName
  scope: resourceGroup(aiServicesSubscriptionId, existingAiServicesResourceGroup)
}

// [ User Assigned Identity for App to avoid circular dependency ]
module appIdentity './modules/identity.bicep' = {
  name: 'uami'
  scope: resourceGroup(rgName)
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
  }
  dependsOn: [ newRg ]
}

var sanitizedEnvName = toLower(replace(replace(replace(environmentName, ' ', '-'), '--', '-'), '_', '-'))
var logAnalyticsName = take('log-${sanitizedEnvName}-${uniqueSuffix}', 63)
var appInsightsName = take('insights-${sanitizedEnvName}-${uniqueSuffix}', 63)
module monitoring 'modules/monitoring/monitor.bicep' = {
  name: 'monitor'
  scope: resourceGroup(rgName)
  params: {
    logAnalyticsName: logAnalyticsName
    appInsightsName: appInsightsName
    tags: tags
  }
  dependsOn: [ newRg ]
}

module registry 'modules/containerregistry.bicep' = {
  name: 'registry'
  scope: resourceGroup(rgName)
  params: {
    location: location
    uniqueSuffix: uniqueSuffix
    identityName: appIdentity.outputs.name
    tags: tags
  }
  dependsOn: [ newRg ]
}


module aiServices 'modules/aiservices.bicep' = if (!useExistingAiServices) {
  name: 'ai-foundry-deployment'
  scope: resourceGroup(rgName)
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    identityId: appIdentity.outputs.identityId
    tags: tags
  }
  dependsOn: [ newRg ]
}

#disable-next-line BCP318
var aiServicesEndpoint = useExistingAiServices ? existingAiServices.properties.endpoint : aiServices.outputs.aiServicesEndpoint
#disable-next-line BCP318
var aiServicesName = useExistingAiServices ? existingAiServicesName : aiServices.outputs.aiServicesName
var aiServicesResourceGroup = useExistingAiServices ? existingAiServicesResourceGroup : rgName

module acs 'modules/acs.bicep' = if (telephonyProvider == 'acs') {
  name: 'acs-deployment'
  scope: resourceGroup(rgName)
  params: {
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    tags: tags
    existingAcsName: existingAcsName
    dataLocation: acsDataLocation
  }
  dependsOn: [ newRg ]
}

var rawKvName = take(toLower(replace(replace(replace(replace('kv-${environmentName}-${uniqueSuffix}', ' ', ''), '.', ''), '--', '-'), '_', '')), 24)
var keyVaultName = endsWith(rawKvName, '-') ? take(rawKvName, length(rawKvName) - 1) : rawKvName
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault-deployment'
  scope: resourceGroup(rgName)
  params: {
    location: location
    keyVaultName: keyVaultName
    tags: tags
    #disable-next-line BCP327
    acsConnectionString: (telephonyProvider == 'acs') ? acs.outputs.acsConnectionString : ''
    twilioAuthToken: twilioAuthToken
    infobipApiKey: infobipApiKey
    genesysApiKey: genesysApiKey
    sinchApplicationKey: sinchApplicationKey
    sinchApplicationSecret: sinchApplicationSecret
    bandwidthClientId: bandwidthClientId
    bandwidthClientSecret: bandwidthClientSecret
  }
  dependsOn: [ newRg ]
}

// Add role assignments 
module RoleAssignments 'modules/roleassignments.bicep' = {
  scope: resourceGroup(rgName)
  name: 'role-assignments'
  params: {
    identityPrincipalId: appIdentity.outputs.principalId
    keyVaultName: keyVaultName
  }
  dependsOn: [ newRg, keyvault ]
}

// AI Services role assignments, deployed at the AI account's own resource group scope, which may
// be in another subscription (D-001). The deploying principal needs Owner or User Access
// Administrator there too.
module aiRoleAssignments 'modules/airoleassignments.bicep' = {
  name: 'ai-role-assignments'
  scope: resourceGroup(aiServicesSubscriptionId, aiServicesResourceGroup)
  params: {
    aiServicesName: aiServicesName
    appPrincipalId: appIdentity.outputs.principalId
    #disable-next-line BCP318
    acsPrincipalId: (telephonyProvider == 'acs') ? acs.outputs.acsPrincipalId : ''
  }
}

module containerapp 'modules/containerapp.bicep' = {
  name: 'containerapp-deployment'
  scope: resourceGroup(rgName)
  params: {
    location: location
    environmentName: environmentName
    uniqueSuffix: uniqueSuffix
    tags: tags
    exists: appExists
    identityId: appIdentity.outputs.identityId
    identityClientId: appIdentity.outputs.clientId
    containerRegistryName: registry.outputs.name
    aiServicesEndpoint: aiServicesEndpoint
    // ACS text-to-speech uses the same AI Services account's endpoint (M6 plan C3). Only set for
    // ACS: bridge_config.py requires it when ACS is active and ignores it otherwise.
    acsCognitiveServicesEndpoint: (telephonyProvider == 'acs') ? aiServicesEndpoint : ''
    agentRoutingJson: empty(agentRoutingJsonBase64) ? '' : base64ToString(agentRoutingJsonBase64)
    maxCallSeconds: maxCallSeconds
    fallbackMessage: fallbackMessage
    ambientPreset: ambientPreset
    acsCallbackJwtAudience: acsCallbackJwtAudience
    modelDeploymentName: modelName
    acsConnectionStringSecretUri: keyvault.outputs.acsConnectionStringUri
    twilioAuthTokenSecretUri: keyvault.outputs.twilioAuthTokenUri
    infobipApiKeySecretUri: keyvault.outputs.infobipApiKeyUri
    infobipApiBaseUrl: infobipApiBaseUrl
    genesysApiKeySecretUri: keyvault.outputs.genesysApiKeyUri
    sinchApplicationKeySecretUri: keyvault.outputs.sinchApplicationKeyUri
    sinchApplicationSecretSecretUri: keyvault.outputs.sinchApplicationSecretUri
    bandwidthClientIdSecretUri: keyvault.outputs.bandwidthClientIdUri
    bandwidthClientSecretSecretUri: keyvault.outputs.bandwidthClientSecretUri
    bandwidthAccountId: bandwidthAccountId
    bandwidthApplicationId: bandwidthApplicationId
    logAnalyticsWorkspaceName: logAnalyticsName
    debugMode: debugMode
    imageName: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
  }
  dependsOn: [ newRg, RoleAssignments, aiRoleAssignments ]
}


// OUTPUTS will be saved in azd env for later use
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rgName
output AZURE_USER_ASSIGNED_IDENTITY_ID string = appIdentity.outputs.identityId
output AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID string = appIdentity.outputs.clientId

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer

// Provider endpoint mapping — add new providers here
var providerEndpoints = {
  acs: 'https://${containerapp.outputs.containerAppFqdn}/acs/incomingcall'
  twilio: 'https://${containerapp.outputs.containerAppFqdn}/voice'
  infobip: 'https://${containerapp.outputs.containerAppFqdn}/infobip/incoming'
  genesys: 'wss://${containerapp.outputs.containerAppFqdn}/audiohook/ws'
  sinch: 'https://${containerapp.outputs.containerAppFqdn}/sinch/callbacks'
  bandwidth: 'https://${containerapp.outputs.containerAppFqdn}/bandwidth/incoming'
}
output SERVICE_API_ENDPOINTS array = [providerEndpoints[telephonyProvider]]
output AZURE_VOICE_LIVE_ENDPOINT string = aiServicesEndpoint
output AZURE_VOICE_LIVE_MODEL string = modelName
