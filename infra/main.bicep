// Agenda Watch — Azure resources for the hosted daily run.
//
//   Storage account     static website ($web, public) + private "state" container (SQLite database)
//   Container registry  private image store (Basic); the job pulls with its managed identity
//   Identities          job identity: blob read/write + image pull
//                       deploy identity: GitHub Actions (OIDC) pushes images and updates the job
//   Log Analytics       container logs (the job writes one JSON object per line)
//   Container Apps      environment + scheduled job running `agenda_watch.py job`
//   Alert (optional)    email when no successful run has been logged for 26 hours
//
// Deploy with infra/deploy.sh, which also turns on the static website (not settable from Bicep).

targetScope = 'resourceGroup'

@description('Azure region for everything.')
param location string = resourceGroup().location

@description('Short name used to build resource names.')
@maxLength(12)
param prefix string = 'agendawatch'

@description('Container image to run. Use the placeholder until the first image is pushed.')
param image string = 'mcr.microsoft.com/k8se/quickstart-jobs:latest'

@description('When the daily run starts, as a cron expression in UTC. 11:00 UTC = 5 or 6 AM Central.')
param scheduleCron string = '0 11 * * *'

@description('Public URL of the site, for absolute links in the feeds. Empty = the storage static website URL.')
param siteUrl string = ''

@description('Drop stored page text for meetings older than this many days (0 = keep everything).')
param pageRetentionDays int = 90

@description('GitHub repository (owner/name) allowed to deploy via OIDC.')
param githubRepo string = 'patmorgan235/AgendaWatch'

@description('Branch allowed to deploy.')
param githubBranch string = 'main'

@description('Email address for failure alerts. Empty = no alert.')
param alertEmail string = ''

@description('vCPU and memory for a run. Large agenda packets (100+ MB PDFs) need headroom.')
param cpu string = '2.0'
param memory string = '4Gi'

var suffix = uniqueString(resourceGroup().id)
var storageName = toLower('${take(prefix, 12)}${take(suffix, 12)}')
var registryName = toLower('${take(prefix, 12)}acr${take(suffix, 10)}')
var isPlaceholder = startsWith(image, 'mcr.microsoft.com/k8se/')

// Built-in role definition IDs
var roles = {
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
}

// ---------------------------------------------------------------- storage
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false   // the static website endpoint is public on its own
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 14 }   // undo an accidental overwrite of the database
  }
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'state'
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------- registry
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------------------------------------------------------------- identities
resource jobIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-job-id'
  location: location
}

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-deploy-id'
  location: location
}

resource githubFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: deployIdentity
  name: 'github-${githubBranch}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepo}:ref:refs/heads/${githubBranch}'
    audiences: [ 'api://AzureADTokenExchange' ]
  }
}

resource jobBlobAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, jobIdentity.id, roles.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource jobPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, jobIdentity.id, roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deployPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, deployIdentity.id, roles.acrPush)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Updating the job's image re-applies its identity, which needs "assign" on that identity.
resource deployAssignJobIdentity 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: jobIdentity
  name: guid(jobIdentity.id, deployIdentity.id, roles.managedIdentityOperator)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.managedIdentityOperator)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- logs
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------- container apps
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// The site code strips a trailing slash, so the endpoint can be used as-is.
var effectiveSiteUrl = empty(siteUrl) ? storage.properties.primaryEndpoints.web : siteUrl

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-daily'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${jobIdentity.id}': {} }
  }
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: scheduleCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      replicaTimeout: 7200          // 2 hours; a full run takes well under one
      replicaRetryLimit: 0          // a failed run is retried by tomorrow's schedule, not in a loop
      registries: isPlaceholder ? [] : [
        { server: registry.properties.loginServer, identity: jobIdentity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'agendawatch'
          image: image
          resources: { cpu: json(cpu), memory: memory }
          env: [
            { name: 'AGENDAWATCH_STORAGE', value: 'azure' }
            { name: 'AGENDAWATCH_AZURE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
            { name: 'AGENDAWATCH_AZURE_CLIENT_ID', value: jobIdentity.properties.clientId }
            { name: 'AGENDAWATCH_SETTING_SITE_URL', value: effectiveSiteUrl }
            { name: 'AGENDAWATCH_SETTING_PAGE_RETENTION_DAYS', value: string(pageRetentionDays) }
            { name: 'AGENDAWATCH_LOG_FORMAT', value: 'json' }
          ]
        }
      ]
    }
  }
  dependsOn: [ jobBlobAccess, jobPull ]
}

resource deployUpdatesJob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: job
  name: guid(job.id, deployIdentity.id, roles.contributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- alert
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: '${prefix}-alerts'
  location: 'global'
  properties: {
    groupShortName: 'agendawatch'
    enabled: true
    emailReceivers: [
      { name: 'owner', emailAddress: alertEmail, useCommonAlertSchema: true }
    ]
  }
}

// Fires when no run in the last 26 hours logged a run_finished event with at least one source OK.
// That covers crashes, hangs, a stuck lock, all sources failing, and the job not starting at all.
resource noSuccessfulRun 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (!empty(alertEmail)) {
  name: '${prefix}-no-successful-run'
  location: location
  properties: {
    displayName: 'Agenda Watch: no successful run in 26 hours'
    description: 'The daily job has not logged a successful run_finished event in 26 hours. Check the job executions and ContainerAppConsoleLogs_CL.'
    severity: 2
    enabled: true
    scopes: [ logs.id ]
    evaluationFrequency: 'PT1H'
    windowSize: 'P2D'
    skipQueryValidation: true       // the log table only exists after the first run
    criteria: {
      allOf: [
        {
          query: '''
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(26h)
| where Log_s has "run_finished"
| extend entry = parse_json(Log_s)
| where tostring(entry.event) == "run_finished" and toint(entry.sources_ok) > 0
| count
'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: true
    actions: { actionGroups: [ actionGroup.id ] }
  }
}

// ---------------------------------------------------------------- outputs
output storageAccount string = storage.name
output siteUrl string = effectiveSiteUrl
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output jobName string = job.name
output jobIdentityClientId string = jobIdentity.properties.clientId
output deployIdentityClientId string = deployIdentity.properties.clientId
output logAnalyticsWorkspace string = logs.name
