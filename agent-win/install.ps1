# Gọi điện - Nhắn tin con for Windows: install/upgrade. Run as admin from the folder holding these files.
param([Parameter(Mandatory)][string]$Url, [Parameter(Mandatory)][string]$Token, [Parameter(Mandatory)][string]$Kid, [string]$Key = '')
$ErrorActionPreference = 'Stop'
$app = 'C:\Program Files\BoNhan\app'; $data = 'C:\ProgramData\BoNhan'
$py = 'C:\Program Files\BoNhan\Python\pythonw.exe'
New-Item -ItemType Directory -Force $app, $data, "$data\logs", "$data\audio\voice", "$data\shots", "$data\voices" | Out-Null
if (Get-ScheduledTask 'BoNhan Service' -ErrorAction SilentlyContinue) { Stop-ScheduledTask 'BoNhan Service' }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -match 'bo_win.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Copy-Item "$PSScriptRoot\bo_win.py", "$PSScriptRoot\kid.html", "$PSScriptRoot\call.html" $app -Force
# cryptography: end-to-end encryption. pycaw: raise the speakers. winrt: pause his music/videos while we talk.
& 'C:\Program Files\BoNhan\Python\python.exe' -m pip install -q --disable-pip-version-check --no-warn-script-location cryptography pycaw `
  winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections
# Windows Update: no restart between 06:00 and 24:00 (an update restart breaks messages and calls).
$wu = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
New-Item -Force $wu -ErrorAction SilentlyContinue | Out-Null
Set-ItemProperty $wu -Name SmartActiveHoursState -Type DWord -Value 0
Set-ItemProperty $wu -Name ActiveHoursStart -Type DWord -Value 6
Set-ItemProperty $wu -Name ActiveHoursEnd -Type DWord -Value 0
@{ url = $Url; token = $Token; kid_user = $Kid; key = $Key; port = 8765 } | ConvertTo-Json | Set-Content -Encoding utf8 "$data\agent.json"
# Rights: the child reads the app, audio, voices and helper.key, writes only shots; agent.json/state.db stay SYSTEM+admins.
# The account name alone can resolve to the computer when both share a name: always use the SID.
$kidSid = '*' + (Get-LocalUser $Kid).SID.Value
$kidFull = "$env:COMPUTERNAME\$Kid"
icacls $data /reset /T /Q | Out-Null
icacls $data /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null
foreach ($d in 'audio', 'voices') { icacls "$data\$d" /grant "${kidSid}:(OI)(CI)RX" | Out-Null }
icacls "$data\shots" /grant "${kidSid}:(OI)(CI)M" | Out-Null
icacls $data /grant "${kidSid}:(RX)" | Out-Null
if (-not (Test-Path "$data\helper.key")) { Set-Content -Encoding ascii "$data\helper.key" ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')) }
icacls "$data\helper.key" /grant "${kidSid}:(R)" | Out-Null
# Edge: mic + autoplay on the child's local page without asking.
$e = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
foreach ($k in 'AudioCaptureAllowedUrls', 'AutoplayAllowlist') { New-Item -Force "$e\$k" | Out-Null; Set-ItemProperty "$e\$k" -Name 1 -Value 'http://127.0.0.1:8765' }
# Service: SYSTEM at startup, restarts itself. Helper: the child's session at his logon.
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew
$act = New-ScheduledTaskAction -Execute $py -Argument "`"$app\bo_win.py`" service" -WorkingDirectory $app
Register-ScheduledTask 'BoNhan Service' -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest) -Settings $set -Force | Out-Null
$act = New-ScheduledTaskAction -Execute $py -Argument "`"$app\bo_win.py`" helper" -WorkingDirectory $app
Register-ScheduledTask 'BoNhan Helper' -Action $act -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $kidFull) -Principal (New-ScheduledTaskPrincipal -UserId $kidFull -LogonType Interactive) -Settings $set -Force | Out-Null
# Start menu + desktop shortcut "Gọi điện - Nhắn tin con" for the child page.
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$sh = New-Object -ComObject WScript.Shell
foreach ($l in "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Gọi điện - Nhắn tin con.lnk", "C:\Users\Public\Desktop\Gọi điện - Nhắn tin con.lnk") {
  # WScript.Shell cannot save to a path with Vietnamese letters: save under an ASCII name, then rename.
  $tmp = Join-Path (Split-Path $l) 'kids-talk.tmp.lnk'
  $s = $sh.CreateShortcut($tmp); $s.TargetPath = $edge; $s.Arguments = '--app=http://127.0.0.1:8765/'; $s.Save()
  Move-Item -LiteralPath $tmp -Destination $l -Force
}
Start-ScheduledTask 'BoNhan Service'
'installed'
