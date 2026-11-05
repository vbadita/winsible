# winsible test runner.
#   .\tests\run_tests.ps1                         # controller-only tests, both venvs found
#   .\tests\run_tests.ps1 -Remote                 # + end-to-end and parallel tests vs tests\remote\inventory.ini
#   .\tests\run_tests.ps1 -Venv D:\tools\ansible  # a specific venv only
param([string[]]$Venv, [switch]$Remote)
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
if (-not $Venv) { $Venv = @('.venv', '.venv216') | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ } }

$failed = 0
function Run-Case($name, $exe, [string[]]$arguments, $dir) {
    Push-Location $dir
    $t = Measure-Command { $out = & $exe @arguments 2>&1 | ForEach-Object { "$_" } }
    $code = $LASTEXITCODE
    Pop-Location
    $ok = ($code -eq 0)
    $status = 'FAIL'
    if ($ok) { $status = 'PASS' }
    Write-Host ('{0,-4} {1,-45} {2,6:n1}s' -f $status, $name, $t.TotalSeconds)
    if (-not $ok) { $script:failed++; $out | Select-Object -Last 25 | ForEach-Object { Write-Host "     $_" } }
}

foreach ($v in $Venv) {
    $s = Join-Path $v 'Scripts'
    $ver = (& (Join-Path $s 'python.exe') -c "import ansible.release as r; print(r.__version__)")
    Write-Host "== ansible-core $ver ($v)"
    Run-Case 'controller playbook (8 hosts)' (Join-Path $s 'ansible-playbook.exe') @('-i', 'inventory.ini', 'play.yml') (Join-Path $PSScriptRoot 'basic')
    Run-Case 'ansible-inventory --graph' (Join-Path $s 'ansible-inventory.exe') @('-i', 'inventory.ini', '--graph') (Join-Path $PSScriptRoot 'basic')
    Run-Case 'ansible-doc ping' (Join-Path $s 'ansible-doc.exe') @('ping') $PSScriptRoot
    if ($Remote) {
        Run-Case 'end-to-end modules vs remote host' (Join-Path $s 'ansible-playbook.exe') @('-i', 'inventory.ini', 'site.yml') (Join-Path $PSScriptRoot 'remote')
        Run-Case 'parallel hosts vs remote host' (Join-Path $s 'ansible-playbook.exe') @('-i', 'parallel.ini', 'parallel.yml') (Join-Path $PSScriptRoot 'remote')
    }
}
if ($failed) { Write-Host "$failed test(s) failed"; exit 1 }
Write-Host 'all tests passed'
