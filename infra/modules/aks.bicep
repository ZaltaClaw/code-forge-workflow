// AKS managed cluster with workload identity, OIDC, system + spot pools
// API version 2025-07-01 (verified against MS Docs 2026-06)
param prefix string
param env string
param location string
param tags object
param subnetIdAks string
param logAnalyticsWorkspaceId string
param acrId string
param routerIdentityId string
param routerIdentityClientId string
param agentIdentityId string

var dnsPrefix = '${prefix}-${env}'

resource aks 'Microsoft.ContainerService/managedClusters@2025-07-01' = {
  name: 'aks-${prefix}-${env}'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    kubernetesVersion: '1.31.1'
    dnsPrefix: dnsPrefix
    enableRBAC: true
    publicNetworkAccess: 'Enabled'   // private cluster optional; private endpoint adds complexity for first cut
    oidcIssuerProfile: { enabled: true }
    securityProfile: {
      workloadIdentity: { enabled: true }
      defender: {
        logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceId
        securityMonitoring: { enabled: true }
      }
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'cilium'
      networkDataplane: 'cilium'
      loadBalancerSku: 'standard'
      serviceCidr: '10.100.0.0/16'
      dnsServiceIP: '10.100.0.10'
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: { logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId }
      }
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: { enableSecretRotation: 'true', rotationPollInterval: '2m' }
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: 3
        vmSize: 'Standard_D4s_v5'
        osType: 'Linux'
        mode: 'System'
        type: 'VirtualMachineScaleSets'
        availabilityZones: [ '1', '2', '3' ]
        vnetSubnetID: subnetIdAks
        enableAutoScaling: true
        minCount: 3
        maxCount: 5
      }
    ]
  }
}

// Spot pool for agent pods — separate resource so we can scale independently
resource spotPool 'Microsoft.ContainerService/managedClusters/agentPools@2025-07-01' = {
  parent: aks
  name: 'spotagents'
  properties: {
    count: 6
    vmSize: 'Standard_D8s_v5'
    osType: 'Linux'
    mode: 'User'
    scaleSetPriority: 'Spot'
    scaleSetEvictionPolicy: 'Delete'
    spotMaxPrice: -1
    enableAutoScaling: true
    minCount: 3
    maxCount: 50
    vnetSubnetID: subnetIdAks
    nodeTaints: [ 'kubernetes.azure.com/scalesetpriority=spot:NoSchedule' ]
    nodeLabels: { workload: 'agent-pod', agentpool: 'spotagents' }
  }
}

// AKS kubelet identity gets AcrPull on the registry
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(aks.id, acrId, 'AcrPull')
  properties: {
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
    // AcrPull
    roleDefinitionId: '/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d'
  }
}

// Federated credentials so router + agent identities can be used by k8s service accounts
resource fedRouter 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  name: '${last(split(routerIdentityId, '/'))}/fed-router'
  properties: {
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:session-control:session-router'
    audiences: [ 'api://AzureADTokenExchange' ]
  }
}

resource fedAgent 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  name: '${last(split(agentIdentityId, '/'))}/fed-agent'
  properties: {
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:agent-pool:agent-pod'
    audiences: [ 'api://AzureADTokenExchange' ]
  }
}

output aksName string = aks.name
output controlPlaneFqdn string = aks.properties.fqdn
output oidcIssuerUrl string = aks.properties.oidcIssuerProfile.issuerURL
output kubeletObjectId string = aks.properties.identityProfile.kubeletidentity.objectId
