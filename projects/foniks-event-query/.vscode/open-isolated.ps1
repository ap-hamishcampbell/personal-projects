$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$isolatedRoot = Join-Path $env:LOCALAPPDATA "FonikEventQuery\VSCode"
$userDataDirectory = Join-Path $isolatedRoot "user-data"
$extensionsDirectory = Join-Path $isolatedRoot "extensions"
$codeCommand = (Get-Command code -ErrorAction Stop).Source
$extensions = @(
    "ms-python.python",
    "ms-python.vscode-python-envs",
    "ms-python.vscode-pylance",
    "ms-python.debugpy"
)

New-Item -ItemType Directory -Path $userDataDirectory, $extensionsDirectory -Force | Out-Null

foreach ($extension in $extensions) {
    & $codeCommand `
        --user-data-dir $userDataDirectory `
        --extensions-dir $extensionsDirectory `
        --install-extension $extension
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install extension $extension into the isolated VS Code data directory."
    }
}

& $codeCommand `
    --user-data-dir $userDataDirectory `
    --extensions-dir $extensionsDirectory `
    --new-window $projectRoot