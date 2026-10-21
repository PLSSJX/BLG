# 用 GitHub Token 一键创建 4.0.0 快照版 Release（含安装包）
# 用法：$env:GITHUB_TOKEN="ghp_xxx"; .\scripts\create-release.ps1
param(
  [string]$Tag = "v4.0.0-snapshot",
  [string]$Repo = "PLSSJX/BLG",
  [string]$Installer = "$PSScriptRoot\..\dist\MakiseToolkit-Setup-4.0.0.exe"
)
$ErrorActionPreference = "Stop"
if (-not $env:GITHUB_TOKEN) { throw "先设置环境变量 GITHUB_TOKEN" }
if (-not (Test-Path $Installer)) { throw "找不到安装包：$Installer" }

$headers = @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" }
$body = @{
  tag_name   = $Tag
  name       = "4.0.0 快照版（不稳定）"
  body       = Get-Content "$PSScriptRoot\..\release-notes\v4.0.0-snapshot.md" -Raw
  draft      = $false
  prerelease = $true
} | ConvertTo-Json -Depth 3

Write-Host "创建 Release $Tag …"
$release = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$Repo/releases" `
  -Headers $headers -Body $body -ContentType "application/json"
Write-Host "Release 已创建：$($release.html_url)"

$name = Split-Path $Installer -Leaf
$upload = "https://uploads.github.com/repos/$Repo/releases/$($release.id)/assets?name=$name"
Write-Host "上传安装包 $name（122 MB，可能要几分钟）…"
$upHeaders = @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; "Content-Type" = "application/octet-stream" }
$asset = Invoke-RestMethod -Method Post -Uri $upload -Headers $upHeaders -InFile $Installer
Write-Host "上传完成：$($asset.browser_download_url)"
Write-Host ""
Write-Host "别忘了把 dist\MakiseToolkit-Setup-4.0.0.exe 的 sha256 更新到 update/manifest.json（构建时已自动写入）。"
