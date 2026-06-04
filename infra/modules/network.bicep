// VNet, subnets (AKS, PE, AzureFirewall optional), NSGs
param prefix string
param env string
param location string
param tags object

var vnetName = 'vnet-${prefix}-${env}'

resource nsgAks 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-aks-${env}'
  location: location
  tags: tags
}

resource nsgPe 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-pe-${env}'
  location: location
  tags: tags
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.40.0.0/16' ] }
    subnets: [
      {
        name: 'snet-aks'
        properties: {
          addressPrefix: '10.40.0.0/20'
          networkSecurityGroup: { id: nsgAks.id }
          serviceEndpoints: [
            { service: 'Microsoft.Storage' }
            { service: 'Microsoft.KeyVault' }
          ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.40.16.0/24'
          networkSecurityGroup: { id: nsgPe.id }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'snet-router'
        properties: {
          addressPrefix: '10.40.17.0/24'
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output aksSubnetId string = '${vnet.id}/subnets/snet-aks'
output peSubnetId string = '${vnet.id}/subnets/snet-pe'
