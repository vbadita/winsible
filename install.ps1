# winsible installer: creates a venv with ansible-core + winsible's Windows layer.
#   .\install.ps1                      # venv in .\.venv, latest ansible-core
#   .\install.ps1 -AnsibleVersion 2.21.6 -Venv C:\tools\ansible
param(
    [string]$Venv = (Join-Path $PSScriptRoot '.venv'),
    [string]$AnsibleVersion = '',
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
# native tools write progress to stderr; judge them by exit code, not by stderr
function Invoke-Native([scriptblock]$Block, [string]$What) {
    $ErrorActionPreference = 'Continue'
    & $Block 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    Write-Host "Creating venv $Venv"
    Invoke-Native { & $Python -m venv $Venv } 'venv creation'
}
$py = Join-Path $Venv 'Scripts\python.exe'
$spec = 'ansible-core'
if ($AnsibleVersion) { $spec = "ansible-core==$AnsibleVersion" }

Invoke-Native { & $py -m pip install --upgrade --disable-pip-version-check $spec paramiko pywinrm } 'pip install'

$site = & $py -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
# 1) make the winsible package importable from this checkout
Set-Content -Path (Join-Path $site 'winsible-src.pth') -Value $PSScriptRoot -Encoding ASCII
# 2) activate the import hooks at interpreter start (cheap until 'ansible' is imported)
Set-Content -Path (Join-Path $site 'winsible.pth') -Value 'import winsible._bootstrap as _w; _w.install()' -Encoding ASCII

Invoke-Native { & (Join-Path $Venv 'Scripts\ansible.exe') --version } 'ansible --version'
Write-Host ''
Write-Host "Done. Add $(Join-Path $Venv 'Scripts') to PATH, or activate: $(Join-Path $Venv 'Scripts\Activate.ps1')"
