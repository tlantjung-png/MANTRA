param(
    [switch]$Live
)
$ErrorActionPreference = 'Stop'
# This probe spends real credentials and network access: require an opt-in.
if (-not $Live) {
    "usage: probe-console.ps1 -Live   (spends real credentials and network access)"
    exit 2
}
# Anchor at the repo root so the module form resolves core/console.py.
Set-Location (Split-Path $PSScriptRoot -Parent)
# Copy the User-scope key into the process env; the live call fails without it.
$env:MODEL_API_KEY = [Environment]::GetEnvironmentVariable('MODEL_API_KEY', 'User')
# Unique per-run workspace: concurrent runs cannot delete each other's files.
$ws = Join-Path $env:TEMP ("mantra-console-live-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $ws -Force | Out-Null
$msg = 'Create a Python file hello.py that defines h() returning hi. Then verify your work by running a Python command that imports hello, calls h, and asserts the result equals hi. Report the exact command you ran and its output.'
$python = (Get-Command python).Source
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $python
$psi.Arguments = "-m core.console --workspace `"$ws`" --once `"$msg`""
$psi.WorkingDirectory = (Get-Location).Path
$psi.UseShellExecute = $false
$exitCode = 1
try {
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit(300000)) {
        $proc.Kill()
        "exit=timeout"
        $exitCode = 1
    } else {
        "exit=$($proc.ExitCode)"
        $exitCode = $proc.ExitCode
    }
    "hello_py_exists=$(Test-Path (Join-Path $ws 'hello.py'))"
} finally {
    if (Test-Path $ws) { Remove-Item $ws -Recurse -Force }
}
exit $exitCode
