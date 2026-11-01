param([string]$Archive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-runtime.ps1')
$status = Find-SessionPython
if (-not $status.Found) {
    Write-Host 'Python 3.11+ was not found. Run this command, then repeat this check:'
    Write-Host $status.InstallCommand
    Write-Host ('If winget is unavailable, use: ' + $status.DownloadPage)
    exit 2
}
$arguments = @((Join-Path $PSScriptRoot 'session_transfer.py'), 'doctor')
if (-not [string]::IsNullOrWhiteSpace($Archive)) { $arguments += @('--archive', $Archive) }
& $status.Executable @arguments
exit $LASTEXITCODE
