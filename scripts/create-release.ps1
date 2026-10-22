# 用 GitHub Token 创建快照版 Release（含安装包）
# 用法：$env:GITHUB_TOKEN="ghp_xxx"; .\scripts\create-release.ps1
#   可选：-Tag v4.0.1-snapshot -Installer dist\MakiseToolkit-Setup-4.0.1.exe
param(
  [string]$Tag = "v4.0.1-snapshot",
  [string]$Repo = "PLSSJX/BLG",
  [string]$Installer = ""
)
$ErrorActionPreference = "Stop"
if (-not $env:GITHUB_TOKEN) { throw "先设置环境变量 GITHUB_TOKEN" }

$root = Split-Path $PSScriptRoot -Parent
if (-not $Installer) {
  $ver = (Select-String -Path "$root\videograb\__init__.py" -Pattern '__version__ = "(.*)"').Matches[0].Groups[1].Value
  $Installer = "$root\dist\MakiseToolkit-Setup-$ver.exe"
}
if (-not (Test-Path $Installer)) { throw "找不到安装包：$Installer" }

# 清单里的安装包地址必须和标签一致，否则自动更新 404
Write-Host "重新生成清单（标签 $Tag）…"
& python "$root\make_manifest.py" --tag $Tag
if ($LASTEXITCODE -ne 0) { throw "清单生成失败" }

$notesFile = Join-Path $root "release-notes\$Tag.md"
if (-not (Test-Path $notesFile)) { throw "找不到发布说明：$notesFile" }

$headers = @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; Accept = "application/vnd.github+json" }
$body = @{
  tag_name   = $Tag
  name       = (Get-Content $notesFile -TotalCount 1).TrimStart('# ')
  body       = Get-Content $notesFile -Raw
  draft      = $false
  prerelease = $true
} | ConvertTo-Json -Depth 3

Write-Host "创建 Release $Tag …"
$release = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$Repo/releases" -Headers $headers -Body $body -ContentType "application/json"
Write-Host "已创建：$($release.html_url)"

$name = Split-Path $Installer -Leaf
Write-Host "上传 $name …"
$upHeaders = @{ Authorization = "Bearer $($env:GITHUB_TOKEN)"; "Content-Type" = "application/octet-stream" }
$asset = Invoke-RestMethod -Method Post -Uri "https://uploads.github.com/repos/$Repo/releases/$($release.id)/assets?name=$name" -Headers $upHeaders -InFile $Installer
Write-Host "上传完成：$($asset.browser_download_url)"
Write-Host ""
Write-Host "别忘了提交 update/manifest.json（哈希已按新安装包生成）。"
