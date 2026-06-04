// Premium ACR (Premium needed for PE + geo-replication)
param prefix string
param env string
param location string
param tags object
param subnetIdPe string

resource acr 'Microsoft.ContainerRegistry/registries@2024-11-01-preview' = {
  name: take('acr${prefix}${env}${uniqueString(resourceGroup().id)}', 50)
  location: location
  tags: tags
  sku: { name: 'Premium' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Disabled'
    networkRuleBypassOptions: 'AzureServices'
    zoneRedundancy: 'Enabled'
  }
}

resource peAcr 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${acr.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: acr.id
        groupIds: [ 'registry' ]
      }
    } ]
  }
}

output acrId string = acr.id
output acrName string = acr.name
output loginServer string = acr.properties.loginServer
