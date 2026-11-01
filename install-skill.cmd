@echo off
setlocal DisableDelayedExpansion
set "SESSION_TRANSFER_ENTRY_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "$ErrorActionPreference='Stop'; try { & (Join-Path $env:SESSION_TRANSFER_ENTRY_ROOT 'scripts\install-skill.ps1'); exit $LASTEXITCODE } catch { [Console]::Error.WriteLine($_.Exception.Message); if ($_.Exception -is [System.Management.Automation.PSSecurityException]) { [Console]::Error.WriteLine('Windows blocked a downloaded script. Right-click the original ZIP, choose Properties, select Unblock, and click Apply. Then extract the ZIP again and rerun this launcher. If Unblock is unavailable, contact your administrator about the PowerShell policy.') }; exit 1 }"
set "SESSION_TRANSFER_EXIT=%ERRORLEVEL%"
pause
exit /b %SESSION_TRANSFER_EXIT%
