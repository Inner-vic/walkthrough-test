param([Parameter(Mandatory=$true)][string]$Workspace)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $Workspace).Path
$manifestPath = Join-Path $root 'deployment/walkthrough-test.installation.json'
$installed = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($installed.workspace_root -ne $root -or $installed.execution_enabled -ne $false) {
    throw 'Only a matching disabled candidate may be prepared.'
}
$taskName = 'CC Connect Walkthrough Dispatcher'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Existing dispatcher task must be reviewed; it will not be overwritten.'
}
$python = Join-Path $root 'runtime/wtv081/Scripts/python.exe'
$pythonw = Join-Path $root 'runtime/wtv081/Scripts/pythonw.exe'
$script = Join-Path $root 'deployment/walkthrough_dispatcher.pyw'
$metadataPath = Join-Path $root 'deployment/walkthrough-dispatcher.installation.json'
if ($installed.host_python -ne $python -or !(Test-Path -LiteralPath $pythonw) -or (Test-Path -LiteralPath $metadataPath)) {
    throw 'Candidate interpreter missing, or dispatcher metadata already exists.'
}
foreach ($target in @($root, $python, $pythonw, $script)) {
    $item = Get-Item -LiteralPath $target
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked candidate path refused.' }
        if ($item -is [IO.DirectoryInfo]) { $item = $item.Parent } else { $item = $item.Directory }
    }
}
$arguments = '-B "' + $script + '"'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $root
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$task = Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
$metadata = @{
    schema='walkthrough-dispatcher-installation-v1'; taskName=$taskName; workspace=$root;
    python=$python; actionExecutable=$pythonw; actionArguments=$arguments;
    dispatcherSha256=(Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash.ToLowerInvariant();
    installedAt=[DateTime]::UtcNow.ToString('o')
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
Write-Output 'Dispatcher registered. Candidate remains disabled; no task was started.'
