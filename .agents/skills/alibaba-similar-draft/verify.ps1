[CmdletBinding()]
param(
  [string]$SkillRoot = $PSScriptRoot,
  [string]$Workspace,
  [switch]$StrictWorkspace
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $SkillRoot).Path
$python = & (Join-Path $resolvedRoot 'scripts\resolve_offline_python.ps1') -SkillRoot $resolvedRoot
if (-not $python -or -not (Test-Path -LiteralPath $python)) { throw 'Offline Python runtime resolution failed.' }
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'

# A portable/extracted Skill is no longer beneath the business workspace, so
# cwd ancestor discovery cannot bind the workspace-dependent tests.  Bridge
# the explicit -Workspace argument into the same environment contract used by
# runtime_config.py, then restore the caller's environment on every exit.
$resolvedWorkspace = $null
$workspaceEnvironmentWasSet = Test-Path Env:SELLERKIT_ALIBABA_WORKSPACE
$previousWorkspaceEnvironment = $env:SELLERKIT_ALIBABA_WORKSPACE
if ($Workspace) {
  $resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
  $env:SELLERKIT_ALIBABA_WORKSPACE = $resolvedWorkspace
}

try {

# unittest writes ordinary progress to stderr.  Windows PowerShell 5 turns
# redirected native stderr into ErrorRecord objects; with Stop semantics that
# used to abort a perfectly healthy portable acceptance run after the first
# test line.  Capture under Continue, preserve the real process exit code, and
# then restore fail-fast behavior for the remaining orchestration.
$savedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$testOutput = & $python -m unittest discover -s (Join-Path $resolvedRoot 'scripts') -p 'test_*.py' -v 2>&1
$testExitCode = $LASTEXITCODE
$ErrorActionPreference = $savedErrorActionPreference
$testOutput | ForEach-Object { Write-Output $_ }
if ($testExitCode -ne 0) { throw "Python test suite failed with exit code $testExitCode" }

$doctorArgs = @((Join-Path $resolvedRoot 'scripts\runtime_config.py'),'doctor','--skill-root',$resolvedRoot,'--pretty')
if ($resolvedWorkspace) { $doctorArgs += @('--workspace',$resolvedWorkspace) }
if ($StrictWorkspace) { $doctorArgs += '--strict-workspace' }
& $python @doctorArgs
if ($LASTEXITCODE -ne 0) { throw "Runtime doctor failed with exit code $LASTEXITCODE" }

[ordered]@{
  status = 'PASS_LOCAL_SOURCE_VERIFICATION'
  trust_status = 'LOCAL_CONTRACT_ONLY'
  skill_root = $resolvedRoot
  python = $python
  browser_action_count = 0
  network_request_count = 0
  external_write_count = 0
  implementation_ready = $false
  final_write_enabled = $false
} | ConvertTo-Json -Depth 4
} finally {
  if ($Workspace) {
    if ($workspaceEnvironmentWasSet) {
      $env:SELLERKIT_ALIBABA_WORKSPACE = $previousWorkspaceEnvironment
    } else {
      Remove-Item Env:SELLERKIT_ALIBABA_WORKSPACE -ErrorAction SilentlyContinue
    }
  }
}
