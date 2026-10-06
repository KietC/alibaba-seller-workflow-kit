[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Model,

    [Parameter(Mandatory = $true)]
    [string]$ProductMaster,

    [Parameter(Mandatory = $true)]
    [string]$DispositionManifest,

    [string]$AssetRoot,

    [string]$ArchiveRoot,

    [string]$ProductId,

    [string]$Python = 'python',

    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$packageRoot = Split-Path -Parent $PSScriptRoot
$DispositionManifest = (Resolve-Path -LiteralPath $DispositionManifest -ErrorAction Stop).Path
if ($ArchiveRoot) {
    $ArchiveRoot = (Resolve-Path -LiteralPath $ArchiveRoot -ErrorAction Stop).Path
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $packageRoot 'outputs'
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$assets = Join-Path $OutputDirectory "${Model}_assets.json"
$draft = Join-Path $OutputDirectory "${Model}_publish_draft.json"
$rag = Join-Path $OutputDirectory 'rag_preflight.json'

$assetArguments = @(
    (Join-Path $PSScriptRoot 'alibaba_asset_resolver.py'),
    '--model', $Model,
    '--output', $assets,
    '--disposition-manifest', $DispositionManifest
)
if ($AssetRoot) {
    $assetArguments += @('--root', $AssetRoot)
}
if ($ArchiveRoot) {
    $assetArguments += @('--archive-root', $ArchiveRoot)
}
if ($ProductId) {
    $assetArguments += @('--product-id', $ProductId)
}
& $Python @assetArguments
if ($LASTEXITCODE -ne 0) { throw "Asset resolver failed with exit code $LASTEXITCODE" }

$ragArguments = @(
    (Join-Path $PSScriptRoot 'alibaba_rag_preflight.py'),
    '--output', $rag,
    '--disposition-manifest', $DispositionManifest
)
if ($ArchiveRoot) {
    $ragArguments += @('--archive-root', $ArchiveRoot)
}
& $Python @ragArguments
if ($LASTEXITCODE -ne 0) { throw "RAG preflight failed with exit code $LASTEXITCODE" }

& $Python (Join-Path $PSScriptRoot 'build_publish_draft.py') --product-master $ProductMaster --assets $assets --disposition-manifest $DispositionManifest --output $draft
if ($LASTEXITCODE -ne 0) { throw "Draft builder failed with exit code $LASTEXITCODE" }

# The generic builder already validates its policy contract; the company-only deliverable verifier is excluded.
# 通用构建器已校验其策略合同；企业专属交付物校验器不在公开包中。

[pscustomobject]@{
    Model = $Model
    Assets = $assets
    Draft = $draft
    DraftMarkdown = [System.IO.Path]::ChangeExtension($draft, '.md')
    RagPreflight = $rag
    DispositionManifest = $DispositionManifest
    DispositionGateEnforced = $true
    ExternalWriteEnabled = $false
}
