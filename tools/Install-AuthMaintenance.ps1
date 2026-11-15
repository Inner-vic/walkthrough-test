param([Parameter(Mandatory=$true)][string]$ManifestPath)
$ErrorActionPreference = 'Stop'
$manifestFile = (Resolve-Path -LiteralPath $ManifestPath).Path
$m = Get-Content -LiteralPath $manifestFile -Raw -Encoding utf8 | ConvertFrom-Json
$root = (Resolve-Path -LiteralPath $m.workspace_root).Path
if ($m.project_id -notmatch '^[A-Za-z0-9_.-]+$' -or $m.active_mode -ne 'project_home') { throw 'Invalid isolated project.' }
$interval = [int]$m.auth_maintenance.check_interval_minutes
if ($interval -lt 60 -or $interval -gt 1440) { throw 'Use a 60-1440 minute interval.' }
$taskName = 'CC Connect Feishu Auth - ' + $m.project_id
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Existing task refused.' }
$script = Join-Path $root 'deployment/refresh-feishu-user-auth.ps1'
foreach ($path in @($root, $manifestFile, $script, [string]$m.python_executable)) {
    $item = Get-Item -LiteralPath $path
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked path refused.' }
        if ($item -is [IO.DirectoryInfo]) { $item = $item.Parent } else { $item = $item.Directory }
    }
}
$engine = (Get-Command powershell.exe -ErrorAction Stop).Source
$argsText = '-WindowStyle Hidden -NoProfile -NonInteractive -File "' + $script + '" -ManifestPath "' + $manifestFile + '"'
$action = New-ScheduledTaskAction -Execute $engine -Argument $argsText -WorkingDirectory $root
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $interval)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Output 'Project auth maintenance registered; first periodic run is in one minute. No vehicle job was started.'
