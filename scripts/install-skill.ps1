param(
    [string]$Destination,
    [switch]$CheckOnly,
    [AllowEmptyCollection()][string[]]$PythonCandidates
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-runtime.ps1')
$checkArguments = @{}
if ($PSBoundParameters.ContainsKey('PythonCandidates')) { $checkArguments.Candidates = $PythonCandidates }
$status = Find-SessionPython @checkArguments
if ($CheckOnly) { $status | ConvertTo-Json -Compress; exit 0 }
if (-not $status.Found) {
    Write-Host 'Python 3.11+ was not found. Run this command, then reopen the installer:'
    Write-Host $status.InstallCommand
    Write-Host ('If winget is unavailable, use: ' + $status.DownloadPage)
    exit 2
}
Write-Host ('Python ' + $status.Version + ' is ready. No Python installation is needed.')
$sourceSkill = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $Destination) {
    $skillHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $Destination = Join-Path $skillHome 'skills\codex-session-transfer'
}
$Destination = [IO.Path]::GetFullPath($Destination)
if ($Destination.TrimEnd('\') -eq $sourceSkill.TrimEnd('\')) {
    Write-Host ('Skill is already at: ' + $Destination)
    exit 0
}
if ($Destination.StartsWith($sourceSkill.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installation destination must not be inside the source skill folder.'
}
if (Test-Path -LiteralPath $Destination) {
    $destinationParent = Split-Path -Parent $Destination
    $backupParent = if ((Split-Path -Leaf $destinationParent) -eq 'skills') {
        Join-Path (Split-Path -Parent $destinationParent) 'skill-install-backups'
    } else {
        Join-Path $destinationParent 'skill-install-backups'
    }
    New-Item -ItemType Directory -Path $backupParent -Force -ErrorAction Stop | Out-Null
    $backup = Join-Path $backupParent ((Split-Path -Leaf $Destination) + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    Copy-Item -LiteralPath $Destination -Destination $backup -Recurse -ErrorAction Stop
    Write-Host ('Previous version backed up at: ' + $backup)
}
New-Item -ItemType Directory -Path $Destination -Force -ErrorAction Stop | Out-Null
foreach ($file in Get-ChildItem -LiteralPath $sourceSkill -Recurse -File) {
    if ($file.FullName -match '\\__pycache__\\' -or $file.Extension -eq '.pyc') { continue }
    $relative = $file.FullName.Substring($sourceSkill.Length).TrimStart('\')
    $target = Join-Path $Destination $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $file.FullName -Destination $target -Force -ErrorAction Stop
}
Write-Host ('Skill installed at: ' + $Destination)
Write-Host 'Restart Codex, then use: $codex-session-transfer'
