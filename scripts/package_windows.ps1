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
$payload = Join-Path $env:RUNNER_TEMP "keycash-portable-payload.zip"
Compress-Archive -Path (Join-Path $package "*") -DestinationPath $payload
$env:KEYCASH_PORTABLE_PAYLOAD = $payload
$env:KEYCASH_PAYLOAD_ID = (Get-FileHash $payload -Algorithm SHA256).Hash.Substring(0, 16).ToLower()
cargo build --release --locked --manifest-path (Join-Path $root "portable-launcher\Cargo.toml")
if ($LASTEXITCODE -ne 0) { throw "Portable launcher compilation failed." }
$portable = Join-Path $dist "KeycashSolver-Portable-x86_64.exe"
Copy-Item (Join-Path $root "portable-launcher\target\release\keycash-portable.exe") $portable
$portableCheck = & $portable --self-test | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $portableCheck.embedded_python -ne "ok" -or $portableCheck.network_calls -ne 0) {
    throw "Single-file portable executable failed its offline self-test."
}
$portableHash = (Get-FileHash $portable -Algorithm SHA256).Hash.ToLower()
Set-Content "$portable.sha256" "$portableHash  $(Split-Path $portable -Leaf)" -Encoding ascii
Write-Host "Created $portable"
