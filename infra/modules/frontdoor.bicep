// Front Door Premium with WAF + per-tenant routing rules
param prefix string
param env string
param tags object
param tenants array
param aksFqdn string

resource fd 'Microsoft.Cdn/profiles@2024-09-01' = {
  name: 'fd-${prefix}-${env}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
}

resource ep 'Microsoft.Cdn/profiles/afdEndpoints@2024-09-01' = {
  parent: fd
  name: 'afe-${prefix}-${env}'
  location: 'global'
  properties: { enabledState: 'Enabled' }
}

resource origin 'Microsoft.Cdn/profiles/originGroups@2024-09-01' = {
  parent: fd
  name: 'og-aks'
  properties: {
    healthProbeSettings: {
      probePath: '/healthz'
      probeRequestType: 'GET'
      probeProtocol: 'Https'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
    }
  }
}

resource originHost 'Microsoft.Cdn/profiles/originGroups/origins@2024-09-01' = {
  parent: origin
  name: 'aks-origin'
  properties: {
    hostName: aksFqdn
    httpPort: 80
    httpsPort: 443
    originHostHeader: aksFqdn
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
  }
}

// Per-tenant route — uses request header match
resource routes 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-09-01' = [for (t, i) in tenants: {
  parent: ep
  name: 'route-${t}'
  dependsOn: [ originHost ]
  properties: {
    customDomains: []
    originGroup: { id: origin.id }
    supportedProtocols: [ 'Https' ]
    patternsToMatch: [ '/${t}/*' ]
    forwardingProtocol: 'HttpsOnly'
    linkToDefaultDomain: 'Enabled'
    httpsRedirect: 'Enabled'
  }
}]

// WAF policy
resource waf 'Microsoft.Network/FrontDoorWebApplicationFirewallPolicies@2024-02-01' = {
  name: 'waf${prefix}${env}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: 'Enabled'
    }
    managedRules: {
      managedRuleSets: [
        { ruleSetType: 'Microsoft_DefaultRuleSet', ruleSetVersion: '2.1' }
        { ruleSetType: 'Microsoft_BotManagerRuleSet', ruleSetVersion: '1.1' }
      ]
    }
  }
}

output endpointHostName string = ep.properties.hostName
