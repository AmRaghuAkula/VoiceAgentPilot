param location string
param environmentName string
param uniqueSuffix string
param tags object
param exists bool
param identityId string
param identityClientId string
param containerRegistryName string
param aiServicesEndpoint string
param modelDeploymentName string
param acsConnectionStringSecretUri string
param twilioAuthTokenSecretUri string = ''
param infobipApiKeySecretUri string = ''
param infobipApiBaseUrl string = ''
param genesysApiKeySecretUri string = ''
param sinchApplicationKeySecretUri string = ''
param sinchApplicationSecretSecretUri string = ''
param bandwidthClientIdSecretUri string = ''
param bandwidthClientSecretSecretUri string = ''
param bandwidthAccountId string = ''
param bandwidthApplicationId string = ''
param logAnalyticsWorkspaceName string
param appInsightsConnectionString string = ''
@description('The name of the container image')
param imageName string = ''
param debugMode bool = false
// Bridge settings (server/app/bridge_config.py). Each is added to the container's env only when
// non-empty, so an unset value falls back to the app's own default (or fails its own startup
// check) rather than being passed as an empty string.
@description('AI Services endpoint ACS uses for its own text-to-speech (ACS_COGNITIVE_SERVICES_ENDPOINT).')
param acsCognitiveServicesEndpoint string = ''
@description('AGENT_ROUTING_JSON: E.164 number -> {project, agent, version}. Set via azd env, never committed.')
param agentRoutingJson string = ''
param maxCallSeconds string = ''
param fallbackMessage string = ''
param ambientPreset string = ''
@description('ACS_CALLBACK_JWT_AUDIENCE: turns on the ACS callback JWT check. Confirmed at deploy time (Q-005).')
param acsCallbackJwtAudience string = ''
@description('Enable zone redundancy for the Container App Environment')
param zoneRedundant bool = true

// Helper to sanitize environmentName for valid container app name
var sanitizedEnvName = toLower(replace(replace(replace(environmentName, ' ', '-'), '--', '-'), '_', '-'))
var containerAppName = take('ca-${sanitizedEnvName}-${uniqueSuffix}', 32)
var containerEnvName = take('cae-${sanitizedEnvName}-${uniqueSuffix}', 32)

var optionalBridgeSettings = [
  { name: 'ACS_COGNITIVE_SERVICES_ENDPOINT', value: acsCognitiveServicesEndpoint }
  { name: 'AGENT_ROUTING_JSON', value: agentRoutingJson }
  { name: 'MAX_CALL_SECONDS', value: maxCallSeconds }
  { name: 'FALLBACK_MESSAGE', value: fallbackMessage }
  { name: 'AMBIENT_PRESET', value: ambientPreset }
  { name: 'ACS_CALLBACK_JWT_AUDIENCE', value: acsCallbackJwtAudience }
]
var bridgeEnv = filter(optionalBridgeSettings, setting => !empty(setting.value))

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = { name: logAnalyticsWorkspaceName }


module fetchLatestImage './fetch-container-image.bicep' = {
  name: '${containerAppName}-fetch-image'
  params: {
    exists: exists
    name: containerAppName
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: containerEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsWorkspace.properties.customerId
        sharedKey: logAnalyticsWorkspace.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: containerAppName
  location: location
  tags: union(tags, { 'azd-service-name': 'app' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: '${containerRegistryName}.azurecr.io'
          identity: identityId
        }
      ]
      secrets: concat(
        !empty(acsConnectionStringSecretUri) ? [
          {
            name: 'acs-connection-string'
            keyVaultUrl: acsConnectionStringSecretUri
            identity: identityId
          }
        ] : [],
        !empty(twilioAuthTokenSecretUri) ? [
          {
            name: 'twilio-auth-token'
            keyVaultUrl: twilioAuthTokenSecretUri
          identity: identityId
        }
      ] : [],
        !empty(infobipApiKeySecretUri) ? [
          {
            name: 'infobip-api-key'
            keyVaultUrl: infobipApiKeySecretUri
            identity: identityId
          }
        ] : [],
        !empty(genesysApiKeySecretUri) ? [
          {
            name: 'genesys-api-key'
            keyVaultUrl: genesysApiKeySecretUri
            identity: identityId
          }
        ] : [],
        !empty(sinchApplicationKeySecretUri) ? [
          {
            name: 'sinch-application-key'
            keyVaultUrl: sinchApplicationKeySecretUri
            identity: identityId
          }
        ] : [],
        !empty(sinchApplicationSecretSecretUri) ? [
          {
            name: 'sinch-application-secret'
            keyVaultUrl: sinchApplicationSecretSecretUri
            identity: identityId
          }
        ] : [],
        !empty(bandwidthClientIdSecretUri) ? [
          {
            name: 'bandwidth-client-id'
            keyVaultUrl: bandwidthClientIdSecretUri
            identity: identityId
          }
        ] : [],
        !empty(bandwidthClientSecretSecretUri) ? [
          {
            name: 'bandwidth-client-secret'
            keyVaultUrl: bandwidthClientSecretSecretUri
            identity: identityId
          }
        ] : [])
    }
    template: {
      containers: [
        {
          name: 'main'
          image: !empty(imageName) ? imageName : 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: concat([
            {
              name: 'AZURE_VOICE_LIVE_ENDPOINT'
              value: aiServicesEndpoint
            }
            {
              // Spec name (D-003); bridge_config.py prefers it over AZURE_VOICE_LIVE_ENDPOINT.
              name: 'VOICE_LIVE_ENDPOINT'
              value: aiServicesEndpoint
            }
            {
              name: 'AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID'
              value: identityClientId
            }
            {
              name: 'VOICE_LIVE_MODEL'
              value: modelDeploymentName
            }
            {
              name: 'DEBUG_MODE'
              value: string(debugMode)
            }
          ], bridgeEnv, !empty(acsConnectionStringSecretUri) ? [
            {
              name: 'ACS_CONNECTION_STRING'
              secretRef: 'acs-connection-string'
            }
          ] : [], !empty(twilioAuthTokenSecretUri) ? [
            {
              name: 'TWILIO_AUTH_TOKEN'
              secretRef: 'twilio-auth-token'
            }
          ] : [], !empty(infobipApiKeySecretUri) ? [
            {
              name: 'INFOBIP_API_KEY'
              secretRef: 'infobip-api-key'
            }
            {
              name: 'INFOBIP_API_BASE_URL'
              value: infobipApiBaseUrl
            }
          ] : [], !empty(genesysApiKeySecretUri) ? [
            {
              name: 'GENESYS_API_KEY'
              secretRef: 'genesys-api-key'
            }
          ] : [], !empty(sinchApplicationKeySecretUri) ? [
            {
              name: 'SINCH_APPLICATION_KEY'
              secretRef: 'sinch-application-key'
            }
          ] : [], !empty(sinchApplicationSecretSecretUri) ? [
            {
              name: 'SINCH_APPLICATION_SECRET'
              secretRef: 'sinch-application-secret'
            }
          ] : [], !empty(bandwidthClientIdSecretUri) ? [
            {
              name: 'BANDWIDTH_CLIENT_ID'
              secretRef: 'bandwidth-client-id'
            }
            {
              name: 'BANDWIDTH_ACCOUNT_ID'
              value: bandwidthAccountId
            }
            {
              name: 'BANDWIDTH_APPLICATION_ID'
              value: bandwidthApplicationId
            }
          ] : [], !empty(bandwidthClientSecretSecretUri) ? [
            {
              name: 'BANDWIDTH_CLIENT_SECRET'
              secretRef: 'bandwidth-client-secret'
            }
          ] : [])
          resources: {
            cpu: json('2.0')
            memory: '4.0Gi'
          }
        }
      ]
      // TODO add memory/cpu scaling
      // Fixed at one replica (spec §6: min 1, max 1). Call state is in-process, so a second
      // replica would split a call's callbacks/media across instances.
      scale: {
        minReplicas: 1
        maxReplicas: 1
        rules: [
          {
            name: 'http-scaler'
            http: {
              metadata: {
                concurrentRequests: '100'
              }
            }
          }
        ]
      }
    }
  }
}

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppId string = containerApp.id
