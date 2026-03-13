# DeerFlow Start Script
$env:DEER_FLOW_ROOT = $PSScriptRoot
Set-Location "$PSScriptRoot\docker"
docker compose -p deer-flow-dev -f docker-compose-dev.yaml up -d
Write-Host ""
Write-Host "DeerFlow started!"
Write-Host "  Web: http://localhost:2026"
Write-Host ""
Write-Host "Commands:"
Write-Host "  Stop:   .\stop.ps1"
Write-Host "  Logs:   docker logs deer-flow-gateway --tail 50"
Write-Host "  Status: docker ps --filter 'name=deer-flow'"
