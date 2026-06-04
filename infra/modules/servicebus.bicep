// Service Bus namespace + 1 queue per tenant
param prefix string
param env string
param location string
param tags object
param subnetIdPe string
param tenants array

resource sb 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-${prefix}-${env}-${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: 'Premium', tier: 'Premium', capacity: 1 }
  properties: {
    publicNetworkAccess: 'Disabled'
    minimumTlsVersion: '1.2'
    zoneRedundant: true
  }
}

resource pendingQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: sb
  name: 'pending-sessions'
  properties: {
    lockDuration: 'PT5M'
    maxDeliveryCount: 5
    requiresDuplicateDetection: false
    enablePartitioning: false
  }
}

resource tenantQueues 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = [for t in tenants: {
  parent: sb
  name: 'sb-${t}'
  properties: {
    lockDuration: 'PT5M'
    maxDeliveryCount: 5
  }
}]

resource peSb 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${sb.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: sb.id
        groupIds: [ 'namespace' ]
      }
    } ]
  }
}

output sbNamespace string = sb.name
output sbId string = sb.id
