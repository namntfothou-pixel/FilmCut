# Print configuration using verified local paths; do not modify Codex settings.
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run this script on the Windows machine hosting FilmCut.'
}
$FilmCutRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$FilmCutPython = (Resolve-Path -LiteralPath (Join-Path $FilmCutRoot '.venv\Scripts\python.exe')).ProviderPath
$FilmCutServer = (Resolve-Path -LiteralPath (Join-Path $FilmCutRoot 'mcp_server.py')).ProviderPath
if (-not (Test-Path -LiteralPath $FilmCutPython -PathType Leaf)) { throw 'Virtual environment Python is missing.' }
if (-not (Test-Path -LiteralPath $FilmCutServer -PathType Leaf)) { throw 'MCP server is missing.' }
# JSON quoted strings are compatible with TOML basic strings for these paths.
$PythonLiteral = ConvertTo-Json -InputObject $FilmCutPython -Compress
$ServerLiteral = ConvertTo-Json -InputObject $FilmCutServer -Compress
$RootLiteral = ConvertTo-Json -InputObject $FilmCutRoot -Compress
@"
[mcp_servers.filmcut]
command = $PythonLiteral
args = [$ServerLiteral]
cwd = $RootLiteral
startup_timeout_sec = 20
tool_timeout_sec = 1800
"@
