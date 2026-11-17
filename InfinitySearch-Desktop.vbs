' ============================================================
'  Infinity Search - silent desktop launcher (no console window)
'  ------------------------------------------------------------
'  Double-click this file to open the Infinity Search desktop.
'  It starts Electron directly (a GUI app - it creates NO console),
'  and Electron spawns the server with CREATE_NO_WINDOW, so the
'  whole launch is console-free.
'
'  NOTE: InfinitySearch.exe by itself is a CONSOLE build - running
'  it directly opens a terminal. Use this launcher (or the desktop
'  shortcut) instead for the graphical app.
' ============================================================
Option Explicit
Dim sh, fso, root, electron, desktop
Set sh = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
electron = root & "\desktop\node_modules\electron\dist\electron.exe"
desktop = root & "\desktop"
sh.CurrentDirectory = root
If Not fso.FileExists(electron) Then
  MsgBox "electron.exe not found:" & vbCrLf & electron, vbCritical, "Infinity Search"
  WScript.Quit 1
End If
' 1 = normal window (Electron shows its own GUI window; no console is
'     created because Electron is a windowed app). False = don't wait.
sh.Run """" & electron & """ """ & desktop & """", 1, False
