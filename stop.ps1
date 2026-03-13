# DeerFlow Stop Script
Set-Location "$PSScriptRoot\docker"
docker compose -p deer-flow-dev -f docker-compose-dev.yaml stop
Write-Host ""
Write-Host "DeerFlow stopped."
Write-Host "To start again: .\start.ps1"
