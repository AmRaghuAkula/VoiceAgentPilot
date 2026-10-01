// sms-notify: its own resource group, Flex Consumption Function app, storage and Key Vault (spec K9, section 6).
// Written and validated in USMS01; provisioned only in USMS02 (`-e sms-<env>`, AGENT_TOOL=sms-notify).
targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('azd environment name, without the sms- prefix handling (for example sms-demo).')
param environmentName string

@minLength(1)
@description('Azure region. East US 2 for the demo only (Q-085); moves to Canada under Q-074.')
param location string

@allowed(['sms-notify'])
@description('Guard: the azd environment must set AGENT_TOOL=sms-notify (CLAUDE.md section 10).')
param agentTool string

@description('Object ID of the person who loads the Key Vault secrets (Key Vault Secrets Officer). Empty to skip.')
param vaultOfficerPrincipalId string = ''

@description('Sender number in E.164. Comes from the azd environment only, never committed.')
param smsFromNumber string

@description('Comma-separated object IDs allowed to call the tool (the Foundry resource managed identity).')
param smsAllowedPrincipals string

@description('Application ID URI of the sms-notify-api Entra app (api://<app id>).')
param smsAuthAudience string

param smsAuthTenantId string = tenant().tenantId
param smsAllowedCountries string = 'CA'
param smsRequireRole string = 'false'
param smsPrefix string = ''
param smsMaxChars string = '480'
param smsMaxLines string = '8'
param smsMinIntervalSeconds string = '90'
param smsMaxPerHour string = '6'
param smsDedupeMinutes string = '30'

var tags = {
  'azd-env-name': environmentName
  'agent-tool': agentTool
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-sms-notify-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'sms-notify-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    vaultOfficerPrincipalId: vaultOfficerPrincipalId
    appSettings: {
      SMS_FROM_NUMBER: smsFromNumber
      SMS_ALLOWED_COUNTRIES: smsAllowedCountries
      SMS_ALLOWED_PRINCIPALS: smsAllowedPrincipals
      SMS_AUTH_AUDIENCE: smsAuthAudience
      SMS_AUTH_TENANT_ID: smsAuthTenantId
      SMS_REQUIRE_ROLE: smsRequireRole
      SMS_PREFIX: smsPrefix
      SMS_MAX_CHARS: smsMaxChars
      SMS_MAX_LINES: smsMaxLines
      SMS_MIN_INTERVAL_SECONDS: smsMinIntervalSeconds
      SMS_MAX_PER_HOUR: smsMaxPerHour
      SMS_DEDUPE_MINUTES: smsDedupeMinutes
    }
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output FUNCTION_APP_NAME string = resources.outputs.functionAppName
output FUNCTION_HOST string = resources.outputs.functionHost
output KEY_VAULT_NAME string = resources.outputs.keyVaultName
output KEY_VAULT_URI string = resources.outputs.keyVaultUri
output FUNCTION_PRINCIPAL_ID string = resources.outputs.functionPrincipalId
