// Log Analytics workspace + diagnostic rule defaults
param prefix string
param env string
param location string
param tags object

resource law 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: 'law-${prefix}-${env}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 90
    features: { enableDataExport: true }
  }
}

output workspaceId string = law.id
output workspaceName string = law.name
output customerId string = law.properties.customerId
