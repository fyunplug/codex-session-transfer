function Find-SessionPython {
    param([AllowEmptyCollection()][string[]]$Candidates)
    if (-not $PSBoundParameters.ContainsKey('Candidates')) {
        $Candidates = @((Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
        foreach ($commandName in @('python.exe', 'python3.exe')) {
            $command = Get-Command $commandName -ErrorAction SilentlyContinue
            if ($command -and $command.Source -notmatch '\\Microsoft\\WindowsApps\\python[^\\]*\.exe$') {
                $Candidates += $command.Source
            }
        }
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            $launcherPaths = @()
            try { $launcherPaths = @(& $launcher.Source -0p 2>$null) } catch { }
            foreach ($line in $launcherPaths) {
                if ("$line" -match '([A-Za-z]:\\.*python(?:[0-9.]*)?\.exe)\s*$') { $Candidates += $Matches[1] }
            }
        }
        foreach ($registryRoot in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
            if (Test-Path -LiteralPath $registryRoot) {
                foreach ($versionKey in Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue) {
                    $installKey = Join-Path $versionKey.PSPath 'InstallPath'
                    if (Test-Path -LiteralPath $installKey) {
                        $key = Get-Item -LiteralPath $installKey -ErrorAction SilentlyContinue
                        if (-not $key) { continue }
                        $executable = $key.GetValue('ExecutablePath')
                        if (-not $executable -and $key.GetValue('')) { $executable = Join-Path $key.GetValue('') 'python.exe' }
                        if ($executable) { $Candidates += $executable }
                    }
                }
            }
        }
        $localPython = Join-Path $env:LOCALAPPDATA 'Programs\Python'
        if (Test-Path -LiteralPath $localPython) {
            foreach ($directory in Get-ChildItem -LiteralPath $localPython -Directory -ErrorAction SilentlyContinue) {
                $Candidates += Join-Path $directory.FullName 'python.exe'
            }
        }
    }
    foreach ($candidate in @($Candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            $probe = & $candidate -c 'import sys,json;print(json.dumps(dict(Executable=sys.executable,Version=sys.version.split()[0],Found=sys.version_info >= (3,11))))' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $status = ($probe -join "`n") | ConvertFrom-Json
                if ($status.Found) { return $status }
            }
        } catch { continue }
    }
    return [pscustomobject]@{
        Found = $false
        InstallCommand = 'winget install --exact --id Python.Python.3.13 --source winget'
        DownloadPage = 'https://www.python.org/downloads/windows/'
    }
}
