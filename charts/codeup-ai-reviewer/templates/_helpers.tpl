{{- define "car.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "car.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "car.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "car.labels" -}}
app.kubernetes.io/name: {{ include "car.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "car.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "car.fullname" . -}}
{{- end -}}
{{- end -}}
