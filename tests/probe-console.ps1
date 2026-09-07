$ErrorActionPreference = 'Stop'
# Anchor at the repo root so the relative python console.py invocation resolves.
Set-Location (Split-Path $PSScriptRoot -Parent)
# Copy the User-scope key into the process env; the live call fails without it.
$env:MODEL_API_KEY = [Environment]::GetEnvironmentVariable('MODEL_API_KEY', 'User')
# Reused temp workspace: remove any leftover hello.py first so a stale
# file from a previous run cannot produce a false pass.
$ws = Join-Path $env:TEMP 'mantra-console-live'
if (Test-Path $ws) { Remove-Item $ws -Recurse -Force }
$msg = 'Create a Python file hello.py that defines h() returning hi. Then verify your work by running a Python command that imports hello, calls h, and asserts the result equals hi. Report the exact command you ran and its output.'
$python = (Get-Command python).Source
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $python
$psi.Arguments = "console.py --workspace `"$ws`" --once `"$msg`""
$psi.WorkingDirectory = (Get-Location).Path
$psi.UseShellExecute = $false
$proc = [System.Diagnostics.Process]::Start($psi)
if (-not $proc.WaitForExit(300000)) {
    $proc.Kill()
    "exit=timeout"
    "hello_py_exists=$(Test-Path (Join-Path $ws 'hello.py'))"
    if (Test-Path $ws) { Remove-Item $ws -Recurse -Force }
    exit 1
}
"exit=$($proc.ExitCode)"
"hello_py_exists=$(Test-Path (Join-Path $ws 'hello.py'))"
if (Test-Path $ws) { Remove-Item $ws -Recurse -Force }
exit $proc.ExitCode
