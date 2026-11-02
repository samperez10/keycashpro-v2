param(
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA "Programs\KeycashSolver")
)
$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$destination = [IO.Path]::GetFullPath($Prefix)
$data = Join-Path $destination ".state"
New-Item -ItemType Directory -Force -Path $destination, $data | Out-Null
Get-ChildItem $source | Where-Object Name -ne ".state" | ForEach-Object {
    Copy-Item $_.FullName -Destination $destination -Recurse -Force
}
$result = & (Join-Path $destination "keycash-solver.exe") --root $destination --self-test
if ($LASTEXITCODE -ne 0) { throw "Installed executable failed its offline self-test." }
$programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shortcutPath = Join-Path $programs "Keycash Solver.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $destination "keycash-solver.exe"
$shortcut.Arguments = "--root `"$destination`" --data-dir `"$data`""
$shortcut.WorkingDirectory = $destination
$shortcut.Save()
Write-Host "Installed Keycash Solver to $destination"
Write-Host "Start-menu shortcut: $shortcutPath"
Write-Host "Private data is preserved in $data"
