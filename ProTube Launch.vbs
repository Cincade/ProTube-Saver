' Resolve cwd to the folder this .vbs lives in, then launch the entry script
' from src/. Without the explicit cwd, double-clicking the .vbs from anywhere
' (desktop shortcut, taskbar) would inherit that launcher's cwd and python
' would fail to find src\main.py.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = scriptDir
WshShell.Run "pythonw src\main.py", 0
Set WshShell = Nothing
Set fso = Nothing