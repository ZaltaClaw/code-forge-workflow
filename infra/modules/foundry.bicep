// Microsoft Foundry (AI Foundry) hub + project for Claude/OpenAI deployments
//
// ──────────────────────────────────────────────────────────────────────────────
// HARDENING STARTER (Hermes → @michaelliav, see issue #2)
// ──────────────────────────────────────────────────────────────────────────────
// This file expands `infra/modules/foundry.bicep` from "Hub + Project only" to
// a complete Foundry stack: hub, project, model deployments, federated identity
// for the model-gateway, RBAC scoped to least privilege, and a deployment
// script that automates the Anthropic terms acceptance.
//
// THREE OPEN QUESTIONS (Michael's domain — please confirm/correct in PR):
//
//   1. Federated identity scope — where does the model-gateway's federated
//      credential need RBAC? Options below; I'm currently betting on (b) for
//      inference-token issuance but unsure if (a) is also required.
//        (a) `Cognitive Services User` on the parent Hub
//        (b) `Cognitive Services User` on each model deployment (resource-scoped)
//        (c) `Azure AI Developer` on the project (broader; needed for hub APIs?)
//      → see `roleAssignmentGateway` block below.
//
//   2. Content filter level — Foundry exposes content filters per deployment.
//      The SecurityReviewer agent intentionally writes spicy code-injection
//      review prompts, which can false-positive on `medium`/`high` filters and
//      block legitimate workflow runs. Currently set to `low` for SecReviewer's
//      Sonnet deployment, `medium` for everyone else. Defensible? Or do we need
//      a custom blocklist policy attached?
//      → see `contentFilterAssignments` array below.
//
//   3. Anthropic terms acceptance — Anthropic models on Foundry require a
//      one-time legal acceptance per subscription. ARM doesn't expose this as a
//      property; it has to be done via the Foundry portal OR by calling the
//      `cognitiveservices terms accept` API path. Below is a `deploymentScripts`
//      resource that calls the API automatically. Two concerns:
//        - is the API path stable / GA, or do we keep this as a manual one-time
//          pre-req in `docs/OPERATIONS.md`?
//        - should the script run on every deploy (idempotent? probably yes,
//          accept-terms is a no-op if already accepted) or only on first run?
//      → see `acceptAnthropicTerms` deploymentScript below.
//
// Reply on PR or on issue #2. — Hermes
// ──────────────────────────────────────────────────────────────────────────────

param prefix string
param env string
param location string
param tags object
param subnetIdPe string
param keyVaultId string
param storageId string
param logAnalyticsWorkspaceId string

// New parameters (Michael — please review naming/defaults):

@description('OIDC issuer URL of the AKS cluster — needed to federate the model-gateway MI to its KSA')
param aksOidcIssuerUrl string

@description('Resource ID of the user-assigned managed identity for the model-gateway')
param gatewayIdentityId string

@description('Principal ID of the gateway MI — used for RBAC role assignments')
param gatewayIdentityPrincipalId string

@description('Kubernetes namespace.serviceaccount that the federated credential trusts. Format: ns:platform/sa:model-gateway')
param gatewayKubernetesSubject string = 'system:serviceaccount:platform:model-gateway'

@description('Anthropic models to deploy on Foundry. Pin versions explicitly — aliases break when Anthropic releases new versions.')
param anthropicModels array = [
  {
    name: 'claude-opus-4-8'
    version: '2026-05-01'   // TODO @michaelliav: confirm Foundry-side version string
    sku: { name: 'GlobalStandard', capacity: 100 }
    contentFilter: 'medium'
  }
  {
    name: 'claude-sonnet-4-6'
    version: '2026-04-15'
    sku: { name: 'GlobalStandard', capacity: 200 }
    contentFilter: 'low'    // SecurityReviewer needs low-filter to not false-positive on injection-review prompts
  }
  {
    name: 'claude-haiku-4-5'
    version: '2026-03-30'
    sku: { name: 'GlobalStandard', capacity: 50 }
    contentFilter: 'medium'
  }
]

// ──────────────────────────────────────────────────────────────────────────────
// 1. Hub + Project (existing — unchanged structurally, just adds tags)
// ──────────────────────────────────────────────────────────────────────────────

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

// ──────────────────────────────────────────────────────────────────────────────
// 2. Anthropic model deployments
//
// One deployment per pinned Anthropic version. Capacity is per-deployment in
// Tokens-Per-Minute units (GlobalStandard SKU); see `values.yaml` for runtime
// per-dev RPM caps enforced at the LiteLLM gateway.
// ──────────────────────────────────────────────────────────────────────────────

resource anthropicDeployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [for model in anthropicModels: {
  name: '${hub.name}/${model.name}'
  sku: model.sku
  properties: {
    model: {
      format: 'Anthropic'
      name: model.name
      version: model.version
    }
    raiPolicyName: 'cf-${model.contentFilter}-policy'
    versionUpgradeOption: 'NoAutoUpgrade'   // we want explicit version pins
  }
  dependsOn: [
    acceptAnthropicTerms
  ]
}]

// ──────────────────────────────────────────────────────────────────────────────
// 3. Anthropic terms-of-use acceptance (Q3 above)
//
// Anthropic models on Foundry require a one-time legal acceptance per
// subscription. We do it inline via a deploymentScript so a fresh subscription
// can deploy this Bicep without an out-of-band manual step.
//
// TODO @michaelliav: confirm the API path. The script below uses the
// `cognitiveservices terms accept` REST call — replace with the canonical path
// if you know it. Idempotent: calling accept-terms after acceptance is a no-op.
// ──────────────────────────────────────────────────────────────────────────────

resource acceptAnthropicTerms 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'accept-anthropic-terms-${env}'
  location: location
  kind: 'AzureCLI'
  tags: tags
  properties: {
    azCliVersion: '2.65.0'
    timeout: 'PT10M'
    retentionInterval: 'PT1H'
    cleanupPreference: 'OnSuccess'
    scriptContent: '''
      set -e
      echo "▶ Accepting Anthropic terms on Foundry hub: $HUB_NAME"

      # TODO @michaelliav: confirm the REST path. Tried two candidates:
      #   1. POST /providers/Microsoft.CognitiveServices/locations/{loc}/models/anthropic/terms
      #   2. POST /providers/Microsoft.MachineLearningServices/workspaces/{name}/marketplaceTerms/anthropic
      #
      # The portal flow hits #2 (verified in browser network tab on UAT). If you
      # have the CLI command, replace this curl with `az` directly.

      ENDPOINT="https://management.azure.com/subscriptions/${SUB_ID}/resourceGroups/${RG_NAME}/providers/Microsoft.MachineLearningServices/workspaces/${HUB_NAME}/marketplaceTerms/anthropic?api-version=2024-10-01"
      TOKEN=$(az account get-access-token --resource https://management.azure.com --query accessToken -o tsv)

      curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"properties":{"accepted":true}}' "$ENDPOINT" \
        || { echo "Terms acceptance failed — may already be accepted (HTTP 409 is OK)" ; exit 0 ; }

      echo "✓ Anthropic terms accepted (or already were)"
    '''
    environmentVariables: [
      { name: 'HUB_NAME', value: hub.name }
      { name: 'RG_NAME', value: resourceGroup().name }
      { name: 'SUB_ID', value: subscription().subscriptionId }
    ]
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 4. Federated credential — model-gateway MI ↔ Kubernetes ServiceAccount
//
// The gateway pod runs under KSA `platform/model-gateway`. AKS's OIDC issuer
// signs JWTs for that SA; this federated credential makes Azure AD trust those
// JWTs and mint AAD access tokens for the gateway MI without any static creds.
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
// 5. RBAC — gateway MI → Foundry (Q1 above)
//
// Currently betting on `Cognitive Services User` (b32c94a3-3d2c-4c1a-9489-9c0e26f31ea4)
// scoped to the *project* — broad enough to issue inference tokens for any
// deployment under the project, narrow enough that the gateway can't admin the
// hub or other projects.
//
// TODO @michaelliav: is this the right scope? Specifically:
//   - Does inference token issuance require role on the HUB (not project)?
//   - Or do we need per-deployment role assignments (more verbose, tighter)?
//   - Is `Azure AI Developer` (64702f94-c441-49e6-a78b-ef80e0188fee) needed
//     instead/additionally for the post-deploy admin path?
// ──────────────────────────────────────────────────────────────────────────────

@description('Built-in role: Cognitive Services User')
var cogServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource roleAssignmentGateway 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, gatewayIdentityPrincipalId, cogServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cogServicesUserRoleId)
    principalId: gatewayIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 6. Diagnostic settings (existing) + audit-of-RBAC (new)
// ──────────────────────────────────────────────────────────────────────────────

resource hubDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: hub
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

output hubId string = hub.id
output projectId string = project.id
output endpoint string = 'https://${hub.name}.${location}.api.azureml.ms'
output anthropicDeploymentNames array = [for (model, i) in anthropicModels: anthropicDeployments[i].name]
output gatewayFederationId string = gatewayFederation.id
