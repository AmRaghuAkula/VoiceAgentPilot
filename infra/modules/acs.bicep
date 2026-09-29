param environmentName string
param uniqueSuffix string
param tags object = {}
@description('Name of an existing ACS resource in this resource group to adopt. Empty creates acs-<env>-<suffix>. Adopting redeploys the resource (to turn on its system-assigned identity), so dataLocation must match the existing resource\'s own value.')
param existingAcsName string = ''
@description('ACS data location. Immutable after creation: must equal the existing resource\'s value when adopting one.')
param dataLocation string = 'United States'

// Adopting an existing resource is a full PUT: tags are replaced and properties not declared here
// (e.g. linkedDomains) would be reset. Re-run `azd provision --preview` before each provision and
// mirror any portal-side ACS configuration here first.
var acsName string = empty(existingAcsName) ? 'acs-${environmentName}-${uniqueSuffix}' : existingAcsName

resource acs 'Microsoft.Communication/communicationServices@2025-05-01-preview' = {
  name: acsName
  location: 'global'
  tags: tags
  // System-assigned identity: ACS uses it to call the AI Services account for its own
  // text-to-speech (play_media). Granted Cognitive Services User in airoleassignments.bicep.
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dataLocation: dataLocation
  }
}

@secure()
output acsConnectionString string = acs.listKeys().primaryConnectionString
output acsResourceId string = acs.id
output acsPrincipalId string = acs.identity.principalId
