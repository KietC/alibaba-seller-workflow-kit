[CmdletBinding()]
param(
  [string]$OutputDirectory = (Join-Path $PSScriptRoot 'out')
)
$ErrorActionPreference = 'Stop'
$playwright = $env:SELLERKIT_PLAYWRIGHT_ENTRY
$nodeCandidates = @($env:SELLERKIT_NODE_EXECUTABLE)
$pathNode = Get-Command node -ErrorAction SilentlyContinue
if ($pathNode) { $nodeCandidates += $pathNode.Source }
$codexRuntimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
if (Test-Path -LiteralPath $codexRuntimeRoot) {
  $nodeCandidates += Get-ChildItem -LiteralPath $codexRuntimeRoot -Recurse -Filter node.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like '*\dependencies\node\bin\node.exe' } |
    Select-Object -ExpandProperty FullName
}
$nodeCandidates = $nodeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
$node = $null
foreach ($candidate in $nodeCandidates) {
  $candidatePlaywright = $playwright
  if (-not $candidatePlaywright) {
    $nodeRoot = Split-Path -Parent (Split-Path -Parent $candidate)
    $candidatePlaywright = Join-Path $nodeRoot 'node_modules\playwright\index.js'
  }
  if (-not (Test-Path -LiteralPath $candidatePlaywright)) {
    $candidatePlaywright = & $candidate -e "try { process.stdout.write(require.resolve('playwright')) } catch {}"
  }
  if ($candidatePlaywright -and (Test-Path -LiteralPath $candidatePlaywright)) {
    $node = $candidate; $playwright = $candidatePlaywright; break
  }
}
if (-not $node) { throw 'No Node.js runtime with Playwright was found; set SELLERKIT_NODE_EXECUTABLE and SELLERKIT_PLAYWRIGHT_ENTRY' }
if (-not (Test-Path -LiteralPath $playwright)) { throw "Playwright package not found: $playwright" }
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $resolvedOutput) { throw "Synthetic harness output directory must be new: $resolvedOutput" }
New-Item -ItemType Directory -Path $resolvedOutput | Out-Null
$env:SELLERKIT_PLAYWRIGHT_ENTRY = $playwright
$env:SELLERKIT_HARNESS_OUTPUT = $resolvedOutput
$chromiumCandidates = @($env:SELLERKIT_CHROMIUM_EXECUTABLE)
foreach ($name in @('chrome', 'msedge', 'chromium')) {
  $command = Get-Command $name -ErrorAction SilentlyContinue
  if ($command) { $chromiumCandidates += $command.Source }
}
foreach ($app in @('chrome.exe', 'msedge.exe')) {
  foreach ($hive in @('HKLM:', 'HKCU:')) {
    $key = Join-Path $hive "Software\Microsoft\Windows\CurrentVersion\App Paths\$app"
    $value = (Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).'(default)'
    if ($value) { $chromiumCandidates += $value }
  }
}
$chromiumCandidates = $chromiumCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
if (-not $chromiumCandidates) { throw 'No Chromium-compatible browser executable was found for the synthetic harness' }
$env:SELLERKIT_CHROMIUM_EXECUTABLE = $chromiumCandidates[0]
& $node (Join-Path $PSScriptRoot 'harness.spec.mjs')
if ($LASTEXITCODE -ne 0) { throw "Synthetic loopback browser harness failed with exit code $LASTEXITCODE" }
