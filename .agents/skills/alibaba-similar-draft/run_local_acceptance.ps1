[CmdletBinding()]
param(
  [string]$SkillRoot = $PSScriptRoot,
  [Parameter(Mandatory=$true)][string]$Workspace,
  [Parameter(Mandatory=$true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $SkillRoot).Path
$resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
$rootPrefix = $resolvedRoot.TrimEnd([char[]]@('\','/')) + [System.IO.Path]::DirectorySeparatorChar
if ($resolvedOutput.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedOutput.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw 'Local acceptance output directory must be outside the Skill root.'
}
if (Test-Path -LiteralPath $resolvedOutput) { throw "Local acceptance output directory must be new: $resolvedOutput" }
New-Item -ItemType Directory -Path $resolvedOutput | Out-Null

$verifyLog = Join-Path $resolvedOutput '01_source_verify.log'
try {
  & (Join-Path $resolvedRoot 'verify.ps1') -SkillRoot $resolvedRoot -Workspace $resolvedWorkspace -StrictWorkspace 2>&1 |
    Tee-Object -LiteralPath $verifyLog
  if ($LASTEXITCODE -ne 0) { throw "verify.ps1 failed with exit code $LASTEXITCODE" }

  $harnessOutput = Join-Path $resolvedOutput '02_synthetic_loopback_harness'
  & (Join-Path $resolvedRoot 'tests\offline-iab-harness\run.ps1') -OutputDirectory $harnessOutput
  $harnessReceiptPath = Join-Path $harnessOutput 'offline_iab_harness_receipt.json'
  $harnessReceipt = Get-Content -LiteralPath $harnessReceiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($harnessReceipt.state -ne 'FINAL_REVIEW_READY_LOCAL_CONTRACT' -or
      $harnessReceipt.browser_live_attested -ne $false -or
      $harnessReceipt.alibaba_live_executed -ne $false -or
      $harnessReceipt.implementation_ready -ne $false -or
      $harnessReceipt.final_write_enabled -ne $false) {
    throw 'Synthetic harness receipt has unexpected trust or execution semantics.'
  }

  $result = [ordered]@{
    schema_version = 'sellerkit.alibaba.local-acceptance-receipt.v1'
    generated_at = [DateTime]::UtcNow.ToString('o')
    status = 'PASS_LOCAL_CONTRACT_ACCEPTANCE'
    trust_status = 'LOCAL_CONTRACT_ONLY'
    execution_authority = 'NONE'
    live_execution_status = 'BLOCKED_LIVE_ATTESTATION'
    skill_root = $resolvedRoot
    workspace = $resolvedWorkspace
    source_verify_log = [ordered]@{
      path = $verifyLog
      bytes = (Get-Item -LiteralPath $verifyLog).Length
      sha256 = (Get-FileHash -LiteralPath $verifyLog -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    synthetic_harness_receipt = [ordered]@{
      path = $harnessReceiptPath
      bytes = (Get-Item -LiteralPath $harnessReceiptPath).Length
      sha256 = (Get-FileHash -LiteralPath $harnessReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
      locale_count = 18
      synthetic_draft_write_count = $harnessReceipt.request_counts.serverDraftWrites
      synthetic_final_write_count = $harnessReceipt.request_counts.serverFinalWrites
    }
    live_browser_action_count = 0
    synthetic_browser_harness_executed = $true
    alibaba_network_request_count = 0
    alibaba_external_write_count = 0
    local_artifact_writes_performed = $true
    implementation_ready = $false
    final_write_enabled = $false
  }
  $receiptPath = Join-Path $resolvedOutput 'local_acceptance_receipt.json'
  [System.IO.File]::WriteAllText($receiptPath, (($result | ConvertTo-Json -Depth 8) + "`n"), [System.Text.UTF8Encoding]::new($false))
  $result | ConvertTo-Json -Depth 8
} catch {
  $blocked = [ordered]@{
    schema_version = 'sellerkit.alibaba.local-acceptance-receipt.v1'
    generated_at = [DateTime]::UtcNow.ToString('o')
    status = 'BLOCKED_LOCAL_ACCEPTANCE'
    trust_status = 'NONE'
    execution_authority = 'NONE'
    live_execution_status = 'BLOCKED_LIVE_ATTESTATION'
    error = $_.Exception.Message
    implementation_ready = $false
    final_write_enabled = $false
  }
  $blockedPath = Join-Path $resolvedOutput 'local_acceptance_blocked_receipt.json'
  [System.IO.File]::WriteAllText($blockedPath, (($blocked | ConvertTo-Json -Depth 6) + "`n"), [System.Text.UTF8Encoding]::new($false))
  throw
}
