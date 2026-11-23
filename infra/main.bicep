// Agenda Watch — Azure resources for the hosted daily run.
//
//   Storage account     static website ($web, public) + private "state" container (SQLite database)
//   Container registry  private image store (Basic); the job pulls with its managed identity
//   Identities          job identity: blob read/write + image pull
//                       deploy identity: GitHub Actions (OIDC) pushes images and updates the job
//   Log Analytics       container logs (the job writes one JSON object per line)
//   Container Apps      environment + scheduled job running `agenda_watch.py job`
//   Alert (optional)    email when no successful run has been logged for 26 hours
//   Key Vault           Google service-account key for the optional Google Calendar push
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

@description('Public URL of the site, for absolute links in the feeds. Empty = https://<customDomain>, or the storage static website URL when there is no custom domain.')
param siteUrl string = ''

@description('Custom domain served through Cloudflare, e.g. agendas.urbancivic.cloud. Registered on the storage account with indirect (asverify) validation, so the CNAME "asverify.<domain>" → "asverify.<storage>.blob.core.windows.net" (not proxied) must exist before deploying. Empty = none.')
param customDomain string = ''

@description('Drop stored page text for meetings older than this many days (0 = keep everything).')
param pageRetentionDays int = 90

@description('GitHub repository (owner/name) allowed to deploy via OIDC.')
param githubRepo string = 'patmorgan235/AgendaWatch'

@description('Branch allowed to deploy.')
param githubBranch string = 'main'

@description('GitHub owner and repository numeric IDs. GitHub issues OIDC subjects in the immutable-ID form "repo:owner@ownerId/name@repoId:ref:…" for newer repositories; deploy.sh looks these up with the gh CLI. Empty = only the classic subject is registered.')
param githubOwnerId string = ''
param githubRepoId string = ''

@description('Email address for failure alerts. Empty = no alert.')
param alertEmail string = ''

@description('vCPU and memory for a run. Large agenda packets (100+ MB PDFs) need headroom.')
param cpu string = '2.0'
param memory string = '4Gi'

@description('Push meetings to Google Calendars (google_calendar in config.yaml). Turn on only after the service-account key is stored in the Key Vault as the secret "google-credentials" (see docs/runbook.md, "Google Calendar sync").')
param googleCalendar bool = false

@description('Object ID of the person who stores the Google key in the Key Vault (granted Key Vault Secrets Officer). deploy.sh fills in the signed-in user. Empty = no grant.')
param secretsOfficerPrincipalId string = ''

var suffix = uniqueString(resourceGroup().id)
var storageName = toLower('${take(prefix, 12)}${take(suffix, 12)}')
var registryName = toLower('${take(prefix, 12)}acr${take(suffix, 10)}')
var vaultName = toLower('${take(prefix, 8)}kv${take(suffix, 13)}')
var isPlaceholder = startsWith(image, 'mcr.microsoft.com/k8se/')

// Built-in role definition IDs
var roles = {
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

// ---------------------------------------------------------------- Key Vault (Google Calendar key)
// Holds the Google service-account key. Redeploys never touch secret values, so the key survives them.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource jobVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, jobIdentity.id, roles.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: jobIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource ownerVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(secretsOfficerPrincipalId)) {
  scope: vault
  name: guid(vault.id, secretsOfficerPrincipalId, roles.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: secretsOfficerPrincipalId
  }
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
    // Lets the static website answer requests for the custom domain. Visitors get HTTPS from
    // Cloudflare's proxy (SSL mode "Full"); Azure Storage can't hold a certificate for it.
    customDomain: empty(customDomain) ? null : { name: customDomain, useSubDomainName: true }
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

// The same branch in the immutable-ID subject format. Federated credentials on one identity must be
// written one at a time, hence the dependsOn.
var githubOwner = split(githubRepo, '/')[0]
var githubName = split(githubRepo, '/')[1]
resource githubFederationById 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (!empty(githubOwnerId) && !empty(githubRepoId)) {
  parent: deployIdentity
  name: 'github-${githubBranch}-ids'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubOwner}@${githubOwnerId}/${githubName}@${githubRepoId}:ref:refs/heads/${githubBranch}'
    audiences: [ 'api://AzureADTokenExchange' ]
  }
  dependsOn: [ githubFederation ]
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
var effectiveSiteUrl = !empty(siteUrl) ? siteUrl : (!empty(customDomain) ? 'https://${customDomain}' : storage.properties.primaryEndpoints.web)

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
      replicaTimeout: 14400         // 4 hours: daily runs take minutes, a 90-day backfill about an hour
      replicaRetryLimit: 0          // a failed run is retried by tomorrow's schedule, not in a loop
      registries: isPlaceholder ? [] : [
        { server: registry.properties.loginServer, identity: jobIdentity.id }
      ]
      // Read from Key Vault with the job identity at each start, so a rotated key needs no redeploy.
      secrets: googleCalendar ? [
        { name: 'google-credentials', keyVaultUrl: '${vault.properties.vaultUri}secrets/google-credentials', identity: jobIdentity.id }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'agendawatch'
          image: image
          resources: { cpu: json(cpu), memory: memory }
          env: concat([
            { name: 'AGENDAWATCH_STORAGE', value: 'azure' }
            { name: 'AGENDAWATCH_AZURE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
            { name: 'AGENDAWATCH_AZURE_CLIENT_ID', value: jobIdentity.properties.clientId }
            { name: 'AGENDAWATCH_SETTING_SITE_URL', value: effectiveSiteUrl }
            { name: 'AGENDAWATCH_SETTING_PAGE_RETENTION_DAYS', value: string(pageRetentionDays) }
            { name: 'AGENDAWATCH_LOG_FORMAT', value: 'json' }
            { name: 'TZ', value: 'America/Chicago' }   // report timestamps are local (Central) time
          ], googleCalendar ? [
            { name: 'AGENDAWATCH_GOOGLE_CREDENTIALS', secretRef: 'google-credentials' }
          ] : [])
        }
      ]
    }
  }
  dependsOn: [ jobBlobAccess, jobPull, jobVaultAccess ]
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
output webEndpointHost string = replace(replace(storage.properties.primaryEndpoints.web, 'https://', ''), '/', '')
output asverifyTarget string = 'asverify.${storage.name}.blob.${environment().suffixes.storage}'
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output jobName string = job.name
output jobIdentityClientId string = jobIdentity.properties.clientId
output deployIdentityClientId string = deployIdentity.properties.clientId
output logAnalyticsWorkspace string = logs.name
output keyVaultName string = vault.name
