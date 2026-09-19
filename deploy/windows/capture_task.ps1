<#
deploy/windows/capture_task.ps1
---------------------------------------------------------------
Phase 25.9G -- run MarketLens intraday capture under Windows Task Scheduler.

    powershell -ExecutionPolicy Bypass -File deploy\windows\capture_task.ps1 install
    ... start | status | stop | remove

WHAT "install" REGISTERS
    Task      "MarketLens Intraday Capture" (current user only)
    Runs      pythonw.exe scripts\capture_supervisor.py, in the repository
    Triggers  at log-on of this user, and daily at 08:00 local time
              (a no-op when the supervisor is already running)
    Logon     Interactive: runs only while this user is logged on.
              NO PASSWORD IS STORED -- the task never asks for one and
              this script never passes one. That is deliberate: the
              IBKR Client Portal Gateway needs a human at a browser
              anyway, so a task that runs with nobody logged on would
              only ever wait for auth.
    Instances IgnoreNew: a second launch while one runs does nothing.
    Limits    no execution time limit; restarts up to 3 times, 5 min apart,
              if the supervisor process itself fails to launch.
    Rights    RunLevel Limited (no elevation).

WHAT IT NEVER DOES
    - store or pass a password, token, cookie or account secret
    - log in to IBKR or start the Client Portal Gateway for you
    - change power, firewall or any other system setting
    - enable ordering: the capture process cannot place, cancel or modify

STOP is graceful: it creates data\capture\STOP, which the running capture
sees within a second; the minute in progress is written as incomplete and
never archived. While STOP exists a scheduled trigger will not start
capture. START removes it.
#>

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("install", "start", "status", "stop", "remove")]
    [string]$Action,
    [string]$Python = "",
    [string]$DailyAt = "08:00"
)

$ErrorActionPreference = "Stop"
$TaskName = "MarketLens Intraday Capture"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$CaptureDir = Join-Path $Repo "data\capture"
$StopFile = Join-Path $CaptureDir "STOP"
$StateFile = Join-Path $CaptureDir "supervisor.json"

function Resolve-Pythonw {
    if ($Python -ne "") { return $Python }
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if ($null -eq $py) { throw "python was not found on PATH; pass -Python <path to pythonw.exe>" }
    $candidate = Join-Path (Split-Path $py.Source) "pythonw.exe"
    if (Test-Path $candidate) { return $candidate }
    return $py.Source
}

function Get-CaptureTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

switch ($Action) {
    "install" {
        if ($null -ne (Get-CaptureTask)) { throw "'$TaskName' is already registered; run 'remove' first" }
        New-Item -ItemType Directory -Force -Path (Join-Path $CaptureDir "logs") | Out-Null
        $exe = Resolve-Pythonw
        $user = "$env:USERDOMAIN\$env:USERNAME"
        # Not "$action": PowerShell names are case-insensitive, and that
        # would collide with the validated -Action parameter.
        $taskAction = New-ScheduledTaskAction -Execute $exe `
            -Argument "`"$Repo\scripts\capture_supervisor.py`"" -WorkingDirectory $Repo
        $triggers = @(
            (New-ScheduledTaskTrigger -AtLogOn -User $user),
            (New-ScheduledTaskTrigger -Daily -At $DailyAt)
        )
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $triggers `
            -Principal $principal -Settings $settings `
            -Description "MarketLens Phase 25.9G capture-only intraday market data. No orders; no stored credentials." | Out-Null
        Write-Output "Registered '$TaskName' for $user (interactive, no stored password)."
        Write-Output "Runs: $exe `"$Repo\scripts\capture_supervisor.py`""
        Write-Output "Next: 'start', then log in to the IBKR Client Portal Gateway in a browser."
    }
    "start" {
        if (Test-Path $StopFile) { Remove-Item $StopFile -Force }
        if ($null -eq (Get-CaptureTask)) { throw "'$TaskName' is not registered; run 'install' first" }
        Start-ScheduledTask -TaskName $TaskName
        Write-Output "Started '$TaskName'. Check with: capture_task.ps1 status"
    }
    "status" {
        $task = Get-CaptureTask
        if ($null -eq $task) { Write-Output "Task: not registered" }
        else {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName
            Write-Output ("Task: {0}; last run {1} (result {2}); next run {3}" -f `
                $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        }
        & python (Join-Path $Repo "scripts\capture_status.py")
        exit $LASTEXITCODE
    }
    "stop" {
        New-Item -ItemType Directory -Force -Path $CaptureDir | Out-Null
        Set-Content -Path $StopFile -Value ("stop requested " + (Get-Date).ToString("o")) -Encoding utf8
        Write-Output "STOP file written; waiting for capture to stop cleanly..."
        $deadline = (Get-Date).AddSeconds(120)
        $stopped = $false
        while ((Get-Date) -lt $deadline) {
            if (Test-Path $StateFile) {
                $state = (Get-Content $StateFile -Raw | ConvertFrom-Json).state
                if ($state -eq "STOPPED" -or $state -eq "MANUAL_ATTENTION") { $stopped = $true; break }
            }
            if ($null -ne (Get-CaptureTask) -and (Get-CaptureTask).State -ne "Running") { $stopped = $true; break }
            Start-Sleep -Seconds 2
        }
        if (-not $stopped -and $null -ne (Get-CaptureTask)) {
            Write-Output "Did not stop within 120s; ending the task."
            Stop-ScheduledTask -TaskName $TaskName
        }
        Write-Output "Stopped. The STOP file keeps scheduled triggers from restarting it until 'start'."
    }
    "remove" {
        if ($null -eq (Get-CaptureTask)) { Write-Output "'$TaskName' is not registered."; break }
        & $PSCommandPath stop
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed '$TaskName'. Captured data in data\capture is kept."
    }
}
