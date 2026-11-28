$ErrorActionPreference = 'Stop'

function Import-Function([string]$path, [string]$name) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors) { throw ($errors | Out-String) }
    $node = $ast.Find({
        param($item)
        $item -is [Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $name
    }, $true)
    if (-not $node) { throw "Missing function $name" }
    return [scriptblock]::Create($node.Extent.Text)
}

. (Import-Function (Join-Path $PSScriptRoot 'Install-AutoUpdate.ps1') 'Resolve-UpdateRunLevel')
. (Import-Function (Join-Path $PSScriptRoot 'Update-HandyRemote.ps1') 'Assert-LaunchContext')
. (Import-Function (Join-Path $PSScriptRoot 'Update-HandyRemote.ps1') 'Test-AppLaunchHealth')

foreach ($case in @(
    @($false, '', 'Limited'),
    @($false, 'Limited', 'Limited'),
    @($false, 'Highest', 'Highest'),
    @($true, 'Limited', 'Highest')
)) {
    $actual = Resolve-UpdateRunLevel $case[0] $case[1]
    if ($actual -ne $case[2]) { throw "Wrong run level: $actual, expected $($case[2])" }
}

$TaskName = 'HandyRemote-PolicyFixture'
$script:level = $null
$script:admin = $false
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($TaskName -ne 'HandyRemote-PolicyFixture') { throw 'Wrong task policy queried' }
    if ($script:level) { [pscustomobject]@{ Principal = [pscustomobject]@{ RunLevel = $script:level } } }
}
function Test-Administrator { return $script:admin }

Assert-LaunchContext
$script:level = 'Limited'
Assert-LaunchContext
$script:level = 'Highest'
$rejected = $false
try { Assert-LaunchContext } catch {
    if ($_.Exception.Message -notlike '*refusing to restart Handy*') { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'An unelevated updater accepted an elevated task, even with no app running' }
$script:admin = $true
Assert-LaunchContext

$script:processes = @()
$script:elevatedProcess = $false
$script:tokenError = $false
function Get-AppProcesses { param($inst) return $script:processes }
function Get-AppElevation {
    param($processId)
    if ($script:tokenError) { throw (New-Object ComponentModel.Win32Exception(5)) }
    return $script:elevatedProcess
}
function Log { param($message) }
if (Test-AppLaunchHealth $null) { throw 'A missing process passed the launch health check' }
$script:processes = @([pscustomobject]@{ Id = 123 })
if (Test-AppLaunchHealth $null) { throw 'A medium-integrity app passed an elevated launch health check' }
$script:elevatedProcess = $true
if (-not (Test-AppLaunchHealth $null)) { throw 'An elevated app failed an elevated launch health check' }
$script:tokenError = $true
if (Test-AppLaunchHealth $null) { throw 'An unreadable process token passed the launch health check' }
$script:tokenError = $false
$script:level = 'Limited'
$script:elevatedProcess = $false
if (-not (Test-AppLaunchHealth $null)) { throw 'A normal app failed a normal launch health check' }
Write-Output 'PASS: 13 launch-policy cases; no tasks registered or applications started.'
