param(
    [string]$TaskName = "Aviasales Price Agent",
    [int]$IntervalMinutes = 30,
    [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\..").Path
)

$ErrorActionPreference = "Stop"

$runner = Join-Path $ProjectDir "aviawatch.bat"

$action = New-ScheduledTaskAction `
    -Execute $runner `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Checks Aviasales cached prices every $IntervalMinutes minutes." `
    -Force

Write-Host "Registered task '$TaskName' in $ProjectDir. Interval: $IntervalMinutes minutes."
