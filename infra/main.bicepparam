using 'main.bicep'

param env = 'dev'
param location = 'eastus2'   // Claude on Foundry: eastus2 or swedencentral only
param prefix = 'codeforge'
// REPLACE with your platform-admins Entra group object id
param adminGroupObjectId = '00000000-0000-0000-0000-000000000000'
param tenants = [ 'tenant-a', 'tenant-b', 'tenant-c' ]
