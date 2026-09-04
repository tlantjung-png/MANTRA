$ErrorActionPreference = 'Stop'
# Anchor at the repo root so the relative python console.py invocation resolves.
Set-Location (Split-Path $PSScriptRoot -Parent)
# Copy the User-scope key into the process env; the live call fails without it.
$env:MODEL_API_KEY = [Environment]::GetEnvironmentVariable('MODEL_API_KEY', 'User')
# Fixed, reused temp workspace: a stale hello.py from a previous run can
# produce a false pass.
$ws = Join-Path $env:TEMP 'mantra-console-live'
$msg = 'Create a Python file hello.py that defines h() returning hi. Then verify your work by running a Python command that imports hello, calls h, and asserts the result equals hi. Report the exact command you ran and its output.'
python console.py --workspace $ws --once $msg
"exit=$LASTEXITCODE"
"hello_py_exists=$(Test-Path (Join-Path $ws 'hello.py'))"
