{{/*
Expand the name of the chart.
*/}}
{{- define "code-forge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "code-forge.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "code-forge.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "code-forge.labels" -}}
app.kubernetes.io/name: {{ include "code-forge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* Foundry base URL — explicit override wins; otherwise build from resource name. */}}
{{- define "code-forge.foundryBaseUrl" -}}
{{- if .Values.global.foundry.baseUrl -}}
{{ .Values.global.foundry.baseUrl }}
{{- else -}}
https://{{ .Values.global.foundry.resource }}.services.ai.azure.com/anthropic
{{- end -}}
{{- end -}}

{{/* Common Claude-Code-on-Foundry env block — reused by agent pods. */}}
{{- define "code-forge.claudeCodeFoundryEnv" -}}
- name: CLAUDE_CODE_USE_FOUNDRY
  value: "1"
# Point at the in-cluster model gateway, NOT directly at Foundry.
# The gateway terminates auth, enforces budgets, and forwards to Foundry.
- name: ANTHROPIC_FOUNDRY_BASE_URL
  value: "http://model-gateway.{{ .Values.namespaces.platform }}.svc.cluster.local/anthropic"
- name: ANTHROPIC_FOUNDRY_RESOURCE
  value: "{{ .Values.global.foundry.resource }}"
# Pin model versions explicitly — required for multi-user deployments.
- name: ANTHROPIC_DEFAULT_OPUS_MODEL
  value: "{{ .Values.global.foundry.models.opus }}"
- name: ANTHROPIC_DEFAULT_SONNET_MODEL
  value: "{{ .Values.global.foundry.models.sonnet }}"
- name: ANTHROPIC_DEFAULT_HAIKU_MODEL
  value: "{{ .Values.global.foundry.models.haiku }}"
- name: ENABLE_PROMPT_CACHING_1H
  value: "1"
# Per-pod virtual key — rotated on session bind. Router writes via downward API.
- name: ANTHROPIC_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: agent-virtual-key
      key: token
      optional: true
{{- end -}}
