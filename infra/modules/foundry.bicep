// Microsoft Foundry (AI Foundry) hub + project for Claude/OpenAI deployments
// Note: Foundry deployments for Claude must be created post-deploy via portal/CLI
// because Anthropic models require terms-of-use acceptance per subscription.
param prefix string
param env string
param location string
param tags object
param subnetIdPe string
param keyVaultId string
param storageId string
param logAnalyticsWorkspaceId string

resource hub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: 'fdry-${prefix}-${env}'
  location: location
  tags: tags
  kind: 'Hub'
  identity: { type: 'SystemAssigned' }
  properties: {
    friendlyName: 'Code Forge Foundry ${env}'
    keyVault: keyVaultId
    storageAccount: storageId
    publicNetworkAccess: 'Disabled'
    managedNetwork: { isolationMode: 'AllowInternetOutbound' }
  }
}

resource project 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: 'fdryproj-${prefix}-${env}'
  location: location
  tags: tags
  kind: 'Project'
  identity: { type: 'SystemAssigned' }
  properties: {
    friendlyName: 'Code Forge ${env}'
    hubResourceId: hub.id
  }
}

// Diagnostic settings — every prompt/response logged
resource hubDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: hub
  name: 'allLogs'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [ { categoryGroup: 'audit', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

output hubId string = hub.id
output projectId string = project.id
output endpoint string = 'https://${hub.name}.${location}.api.azureml.ms'
