# Docker VHDX 压缩脚本
# 需要以管理员身份运行

Write-Host "关闭 WSL..." -ForegroundColor Yellow
wsl --shutdown
Start-Sleep -Seconds 2

Write-Host "压缩 Docker 虚拟磁盘..." -ForegroundColor Yellow
$vhdxPath = "D:\docker\wsl\DockerDesktopWSL\disk\docker_data.vhdx"

$before = (Get-Item $vhdxPath).Length / 1GB
Write-Host "压缩前大小: $([math]::Round($before, 2)) GB" -ForegroundColor Cyan

$commands = @"
select vdisk file="$vhdxPath"
compact vdisk
exit
"@

$commands | diskpart

$after = (Get-Item $vhdxPath).Length / 1GB
$saved = $before - $after

Write-Host ""
Write-Host "压缩后大小: $([math]::Round($after, 2)) GB" -ForegroundColor Green
Write-Host "释放空间: $([math]::Round($saved, 2)) GB" -ForegroundColor Green
Write-Host ""
Write-Host "完成！请重新启动 Docker Desktop" -ForegroundColor Yellow
