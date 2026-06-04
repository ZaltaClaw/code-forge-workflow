// User-assigned managed identities for session router and agent pods
param prefix string
param env string
param location string
param tags object

resource routerId 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${prefix}-router-${env}'
  location: location
  tags: tags
}

resource agentId 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-${prefix}-agent-${env}'
  location: location
  tags: tags
}

output routerIdentityId string = routerId.id
output routerIdentityClientId string = routerId.properties.clientId
output routerIdentityPrincipalId string = routerId.properties.principalId
output agentIdentityId string = agentId.id
output agentIdentityClientId string = agentId.properties.clientId
output agentIdentityPrincipalId string = agentId.properties.principalId
