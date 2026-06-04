// Key Vault (RBAC mode, PE only) for per-dev secrets
param prefix string
param env string
param location string
param tags object
param subnetIdPe string
param adminGroupObjectId string

resource kv 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: take('kv-${prefix}-${env}-${uniqueString(resourceGroup().id)}', 24)
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enablePurgeProtection: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'AzureServices' }
  }
}

resource peKv 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${kv.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: kv.id
        groupIds: [ 'vault' ]
      }
    } ]
  }
}

// Admins get Key Vault Administrator
resource adminAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, adminGroupObjectId, 'kv-admin')
  properties: {
    principalId: adminGroupObjectId
    principalType: 'Group'
    // Key Vault Administrator built-in role
    roleDefinitionId: '/providers/Microsoft.Authorization/roleDefinitions/00482a5a-887f-4fb3-b363-3b7fe8e74483'
  }
}

output kvId string = kv.id
output kvName string = kv.name
output kvUri string = kv.properties.vaultUri
