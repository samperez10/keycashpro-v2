param(
    [Parameter(Mandatory = $true)][string]$Binary
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dist = Join-Path $root "dist"
$package = Join-Path $dist "keycash-solver-windows-x86_64"
if (Test-Path $package) { throw "Release folder already exists: $package" }
New-Item -ItemType Directory -Force -Path (Join-Path $package "runtime\Lib\site-packages") | Out-Null
Copy-Item $Binary (Join-Path $package "keycash-solver.exe")
$pyVersion = python -c "import platform; print(platform.python_version())"
$embed = Join-Path $env:RUNNER_TEMP "python-embed.zip"
$url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
Invoke-WebRequest $url -OutFile $embed
Expand-Archive $embed -DestinationPath (Join-Path $package "runtime")
python -m pip install --disable-pip-version-check --no-compile --target (Join-Path $package "runtime\Lib\site-packages") -r (Join-Path $root "requirements.txt")
Copy-Item (Join-Path $package "runtime\python312.dll") $package
Copy-Item (Join-Path $package "runtime\python3.dll") $package
Copy-Item (Join-Path $root "scripts\install_windows.ps1") (Join-Path $package "install.ps1")
$cargo = Get-Content (Join-Path $root "Cargo.toml") -Raw
$version = [regex]::Match($cargo, "(?m)^version = `"([^`"]+)`"").Groups[1].Value
$build = [ordered]@{
    name = "keycash-solver"; version = $version; platform = "windows"
    architecture = "x86_64"; python_version = "3.12"
    python_source = "embedded"; python_runtime = "bundled"
    entrypoint = "keycash-solver.exe"
}
$build | ConvertTo-Json | Set-Content (Join-Path $package "build.json") -Encoding utf8NoBOM
$check = & (Join-Path $package "keycash-solver.exe") --root $package --self-test | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $check.embedded_python -ne "ok" -or $check.network_calls -ne 0) {
    throw "Portable Windows executable failed its offline self-test."
}
$hashes = Get-ChildItem $package -Recurse -File | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($package.Length + 1); sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() }
}
$hashes | ConvertTo-Json | Set-Content (Join-Path $package "SHA256SUMS.json") -Encoding utf8NoBOM
$archive = "$package.zip"
Compress-Archive -Path $package -DestinationPath $archive
$archiveHash = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
Set-Content "$archive.sha256" "$archiveHash  $(Split-Path $archive -Leaf)" -Encoding ascii
Write-Host "Created $archive"
