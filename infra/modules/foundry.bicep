// Microsoft Foundry (AI Foundry) account + Anthropic/Claude model deployments
//
// ──────────────────────────────────────────────────────────────────────────────
// HARDENING (Hermes → @michaelliav, resolves issue #2)
// ──────────────────────────────────────────────────────────────────────────────
// Rewritten from the hub/project (ML workspace) starter to the CORRECT runtime
// model: a single `Microsoft.CognitiveServices/accounts` resource with
// `kind: 'AIServices'`. Claude on Foundry is served from this account's
// `https://<account>.services.ai.azure.com/anthropic` endpoint — which is exactly
// what `charts/code-forge` already expects (see _helpers.tpl `foundryBaseUrl`).
// The previous ML-workspace model produced an `api.azureml.ms` endpoint the
// model-gateway can never authenticate against keylessly, so it is removed.
//
// THREE ISSUE-#2 QUESTIONS — RESOLVED (grounded in Microsoft Learn):
//
//   1. Federated-identity RBAC scope.
//      → `Cognitive Services User` (a97b65f3-24c7-4388-baec-2e87135dc908) scoped
//        to THE ACCOUNT. There is no hub/project anymore. This role lets the
//        gateway MI issue inference tokens for every deployment under the account
//        and nothing else (it cannot manage the account or other resources).
//        Data-plane token scope is `https://ai.azure.com/.default`.
//        Custom subdomain (set below) is MANDATORY for Entra/MI auth — regional
//        endpoints do not support token auth.
//
//   2. Content-filter level for SecurityReviewer.
//      → N/A at the infra layer. Foundry does NOT apply deployment-time RAI /
//        content-filter policies to Anthropic models — Claude ships with
//        Anthropic's own safety stack. `raiPolicyName` is a no-op for
//        `format: 'Anthropic'`, so the whole content-filter block is REMOVED
//        rather than tuned. If SecurityReviewer ever needs softer gating, do it
//        at the LiteLLM gateway / prompt layer, not here.
//
//   3. Anthropic terms acceptance.
//      → It is a Marketplace SaaS subscription prerequisite, NOT an ARM property
//        or a callable REST path. The `deploymentScripts` curl hack is removed.
//        Documented as a one-time pre-req in docs/OPERATIONS.md. Requires an
//        Enterprise or MCA-E subscription with Marketplace access, in East US 2
//        or Sweden Central.
// ──────────────────────────────────────────────────────────────────────────────

param prefix string
param env string

@description('Must be a Claude-supported Foundry region: eastus2 or swedencentral.')
@allowed([
  'eastus2'
  'swedencentral'
])
param location string

param tags object

@description('PE subnet — the account is private-only, mirroring Key Vault / Storage.')
param subnetIdPe string

@description('Log Analytics workspace for diagnostic settings.')
param logAnalyticsWorkspaceId string

@description('OIDC issuer URL of the AKS cluster — federates the model-gateway MI to its KSA.')
param aksOidcIssuerUrl string

@description('Resource ID of the user-assigned managed identity for the model-gateway.')
param gatewayIdentityId string

@description('Principal ID of the gateway MI — used for the account-scoped role assignment.')
param gatewayIdentityPrincipalId string

@description('Kubernetes namespace/serviceaccount the federated credential trusts.')
param gatewayKubernetesSubject string = 'system:serviceaccount:platform:model-gateway'

@description('Anthropic models to deploy. Pin versions explicitly — confirm the Foundry-side version strings with `az cognitiveservices model list -l <region>` before deploy.')
param anthropicModels array = [
  {
    name: 'claude-opus-4-8'
    version: '2026-05-01'
    capacity: 100
  }
  {
    name: 'claude-sonnet-4-6'
    version: '2026-04-15'
    capacity: 200
  }
  {
    name: 'claude-haiku-4-5'
    version: '2026-03-30'
    capacity: 50
  }
]

// ──────────────────────────────────────────────────────────────────────────────
// 1. Foundry (AI Services) account
//
// `customSubDomainName` == account name is REQUIRED so the endpoint resolves to
// https://<name>.services.ai.azure.com and so Entra/managed-identity (keyless)
// auth works at all. Private-only + key auth disabled = least privilege.
// The subdomain is globally unique; uniqueString keeps redeploys collision-free.
// Consumers wire the Helm value `global.foundry.resource` from the `accountName`
// output (or `global.foundry.baseUrl` from the `endpoint` output).
// ──────────────────────────────────────────────────────────────────────────────

var accountName = take('fdry-${prefix}-${env}-${uniqueString(resourceGroup().id)}', 63)

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
    }
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 2. Anthropic model deployments (children of the account)
//
// @batchSize(1): CognitiveServices deployments under one account must be applied
// serially or ARM throws conflicting-operation errors. No raiPolicyName — see Q2.
// ──────────────────────────────────────────────────────────────────────────────

@batchSize(1)
resource anthropicDeployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for model in anthropicModels: {
  parent: account
  name: model.name
  sku: {
    name: 'GlobalStandard'
    capacity: model.capacity
  }
  properties: {
    model: {
      format: 'Anthropic'
      name: model.name
      version: model.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]

// ──────────────────────────────────────────────────────────────────────────────
// 3. Private endpoint (group: account) — mirrors keyvault.bicep house style.
//    No inline DNS zone group; private DNS is managed centrally.
// ──────────────────────────────────────────────────────────────────────────────

resource peFoundry 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${account.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetIdPe }
    privateLinkServiceConnections: [ {
      name: 'plsc'
      properties: {
        privateLinkServiceId: account.id
        groupIds: [ 'account' ]
      }
    } ]
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 4. Federated credential — model-gateway MI ↔ Kubernetes ServiceAccount.
//    AKS OIDC-signed SA JWTs are exchanged for AAD tokens; no static creds.
// ──────────────────────────────────────────────────────────────────────────────

resource gatewayFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  name: '${last(split(gatewayIdentityId, '/'))}/k8s-model-gateway'
  properties: {
    issuer: aksOidcIssuerUrl
    subject: gatewayKubernetesSubject
    audiences: [ 'api://AzureADTokenExchange' ]
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 5. RBAC — gateway MI → account (Q1). Cognitive Services User, account scope.
//    The agent-pod MI is intentionally absent: agents reach Foundry only via the
//    gateway, never directly (see docs/SECURITY.md).
// ──────────────────────────────────────────────────────────────────────────────

@description('Built-in role: Cognitive Services User')
var cogServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource roleAssignmentGateway 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, gatewayIdentityPrincipalId, cogServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cogServicesUserRoleId)
    principalId: gatewayIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 6. Diagnostic settings → Log Analytics
// ──────────────────────────────────────────────────────────────────────────────

resource accountDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: account
  name: 'allLogs'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [ { categoryGroup: 'audit', enabled: true } ]
    metrics: [ { category: 'AllMetrics', enabled: true } ]
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Outputs
// ──────────────────────────────────────────────────────────────────────────────

@description('AIServices account name — set as global.foundry.resource in Helm values.')
output accountName string = account.name

@description('Anthropic base URL — matches charts/_helpers.tpl foundryBaseUrl. Set as global.foundry.baseUrl (optional override).')
output endpoint string = 'https://${account.name}.services.ai.azure.com/anthropic'

output accountId string = account.id
output anthropicDeploymentNames array = [for (model, i) in anthropicModels: anthropicDeployments[i].name]
output gatewayFederationId string = gatewayFederation.id
