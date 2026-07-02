' Launches any command with a fully hidden window (no taskbar entry).
' Usage: wscript run_hidden.vbs "C:\path\to\watchdog.bat"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """" & WScript.Arguments(0) & """", 0, False
