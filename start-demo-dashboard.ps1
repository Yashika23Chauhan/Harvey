# Starts the Harvey dashboard and opens its sign-in page.
# Right-click > Run with PowerShell, then sign in at http://127.0.0.1:5555/
Set-Location $PSScriptRoot
$env:Path += ";$env:USERPROFILE\.local\bin"
Remove-Item Env:HARVEY_DASHBOARD_NO_TOKEN -ErrorAction SilentlyContinue
Start-Process "http://127.0.0.1:5555/"
& .\.venv\Scripts\harvey.exe dashboard
