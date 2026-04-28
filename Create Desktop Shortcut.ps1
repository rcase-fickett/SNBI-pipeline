# Creates an "SNBI Review" shortcut on the Desktop.
# Run this once on any machine that needs the app icon.

$appDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbsPath   = Join-Path $appDir "launch.vbs"
$shortcut  = Join-Path ([Environment]::GetFolderPath("Desktop")) "SNBI Review.lnk"

$wsh  = New-Object -ComObject WScript.Shell
$lnk  = $wsh.CreateShortcut($shortcut)

$lnk.TargetPath       = "wscript.exe"
$lnk.Arguments        = "`"$vbsPath`""
$lnk.WorkingDirectory = $appDir
$lnk.Description      = "SNBI Review App"
$lnk.IconLocation     = "$env:SystemRoot\System32\shell32.dll,13"

$lnk.Save()

Write-Host ""
Write-Host "  Shortcut created on your Desktop: SNBI Review" -ForegroundColor Green
Write-Host "  You can move it anywhere — it will still work." -ForegroundColor White
Write-Host ""
Write-Host "  To change the icon: right-click the shortcut > Properties > Change Icon" -ForegroundColor Gray
Write-Host ""
