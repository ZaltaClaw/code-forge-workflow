// Storage Account (Azure Files Premium for workspace PVCs + Blob for artifacts)
param prefix string
param env string
param location string
param tags object
param subnetIdPe string

var storageName = toLower('st${prefix}${env}${uniqueString(resourceGroup().id)}')

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: take(storageName, 24)
  location: location
  tags: tags
  sku: { name: 'Premium_LRS' }
  kind: 'FileStorage'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    isHnsEnabled: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

resource artifacts 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: take('blob${prefix}${env}${uniqueString(resourceGroup().id)}', 24)
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    isHnsEnabled: true
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

resource peFiles 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storage.name}-file'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: storage.id
        groupIds: [ 'file' ]
      }
    } ]
  }
}

resource peBlob 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${artifacts.name}-blob'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: artifacts.id
        groupIds: [ 'blob' ]
      }
    } ]
  }
}

output storageId string = storage.id
output storageName string = storage.name
output artifactsId string = artifacts.id
output artifactsName string = artifacts.name
