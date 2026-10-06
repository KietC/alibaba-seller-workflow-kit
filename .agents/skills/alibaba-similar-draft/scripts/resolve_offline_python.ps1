[CmdletBinding()]
param(
  [string]$SkillRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $SkillRoot).Path
$wheelhouse = Join-Path $resolvedRoot 'wheelhouse'
$requirements = Join-Path $resolvedRoot 'requirements.lock.txt'

function Test-PythonVersion([string]$Executable) {
  try {
    $version = & $Executable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    if ($version -notin @('3.11','3.12','3.13')) { return $null }
    return $version
  } catch { return $null }
}

function Test-ReadyRuntime([string]$Executable) {
  try {
    & $Executable -c "import jsonschema, referencing, rpds; print('READY')" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
  } catch { return $false }
}

$candidates = [System.Collections.Generic.List[string]]::new()
if ($env:SELLERKIT_ALIBABA_PYTHON) {
  $candidates.Add($env:SELLERKIT_ALIBABA_PYTHON)
} else {
  $codexPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
  if (Test-Path -LiteralPath $codexPython) { $candidates.Add($codexPython) }
  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if ($pythonCommand) { $candidates.Add($pythonCommand.Source) }
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    foreach ($line in (& $pyLauncher.Source -0p 2>$null)) {
      $candidate = ($line -replace '^\s*-V:[^\s]+\s*','').Trim()
      if (Test-Path -LiteralPath $candidate) { $candidates.Add($candidate) }
    }
  }
}

$basePython = $null
$baseVersion = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
  if (-not (Test-Path -LiteralPath $candidate)) { continue }
  $version = Test-PythonVersion $candidate
  if (-not $version) { continue }
  if (Test-ReadyRuntime $candidate) {
    Write-Output (Resolve-Path -LiteralPath $candidate).Path
    exit 0
  }
  if (-not $basePython -or ([version]$version -gt [version]$baseVersion)) {
    $basePython = $candidate
    $baseVersion = $version
  }
}

if (-not $basePython) {
  throw 'Python 3.11, 3.12, or 3.13 is required. Set SELLERKIT_ALIBABA_PYTHON to its python.exe.'
}
if (-not (Test-Path -LiteralPath $wheelhouse) -or -not (Test-Path -LiteralPath $requirements)) {
  throw 'Bundled offline wheelhouse or requirements.lock.txt is missing.'
}

$runtimeRoot = Join-Path $resolvedRoot ".runtime\python-$($baseVersion.Replace('.',''))"
$runtimePython = Join-Path $runtimeRoot 'Scripts\python.exe'
if (-not (Test-ReadyRuntime $runtimePython)) {
  New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeRoot) -Force | Out-Null
  if (-not (Test-Path -LiteralPath $runtimePython)) {
    & $basePython -m venv $runtimeRoot 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create offline Python runtime at $runtimeRoot" }
  }
  & $runtimePython -m pip install --disable-pip-version-check --no-index `
    --find-links $wheelhouse --requirement $requirements 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0 -or -not (Test-ReadyRuntime $runtimePython)) {
    throw 'Failed to install the hash-bundled offline Python dependencies.'
  }
}

Write-Output (Resolve-Path -LiteralPath $runtimePython).Path
