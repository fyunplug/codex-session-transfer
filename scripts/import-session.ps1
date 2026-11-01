param([string]$Archive)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python-runtime.ps1')
$pythonStatus = Find-SessionPython
if (-not $pythonStatus.Found) {
    Write-Host 'Python 3.11+ was not found. Run this command, then retry:'
    Write-Host $pythonStatus.InstallCommand
    Write-Host ('If winget is unavailable, use: ' + $pythonStatus.DownloadPage)
    exit 2
}
$pythonExecutable = $pythonStatus.Executable
& $pythonExecutable -c 'import tkinter' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Python is installed, but tkinter is missing. Run the skill in Codex without --pick.' }
$codexCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
$codexExecutable = if ($codexCommand) { $codexCommand.Source } else { $null }
if (-not $codexExecutable) {
    $binaryRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    if (Test-Path -LiteralPath $binaryRoot) {
        $codexExecutable = Get-ChildItem -LiteralPath $binaryRoot -Recurse -Filter codex.exe -File |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $codexExecutable) { throw 'The installed Codex engine was not found.' }
$pythonArguments = @((Join-Path $PSScriptRoot 'native_import.py'), '--pick', '--codex', $codexExecutable)
if (-not [string]::IsNullOrWhiteSpace($Archive)) { $pythonArguments += $Archive }
& $pythonExecutable @pythonArguments
exit $LASTEXITCODE
