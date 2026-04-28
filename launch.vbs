Set WshShell = CreateObject("WScript.Shell")

' Resolve the directory this script lives in
appDir  = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
psScript = appDir & "setup_and_start.ps1"

' Check if first-time setup has been completed
apiKey      = WshShell.ExpandEnvironmentStrings("%ANTHROPIC_API_KEY%")
bridgesRoot = WshShell.ExpandEnvironmentStrings("%SNBI_BRIDGES_ROOT%")

needsSetup = (apiKey = "%ANTHROPIC_API_KEY%") Or (bridgesRoot = "%SNBI_BRIDGES_ROOT%")

If needsSetup Then
    ' First-time setup — show the terminal so the user can type their API key and folder path
    WshShell.Run "powershell -ExecutionPolicy Bypass -File """ & psScript & """", 1, False
Else
    ' Already configured — start silently in the background, browser will open automatically
    WshShell.Run "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & psScript & """ -Silent", 0, False
End If
