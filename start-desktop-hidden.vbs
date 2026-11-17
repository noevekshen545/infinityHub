' Запуск Infinity Search без окна консоли.
' Ярлык ссылается на wscript.exe с этим файлом, поэтому чёрное окно
' не мигает. Приложение само поднимает сервер (desktop\main.js).
Option Explicit

Dim shell, fso, here, bat, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = here & "\start-desktop.bat"

If Not fso.FileExists(bat) Then
    MsgBox "Не найден файл запуска:" & vbCrLf & bat, 16, "Infinity Search"
    WScript.Quit 1
End If

cmd = """" & bat & """"
' 0 — скрыто окно; False — не ждать завершения, чтобы не блокировать сценарий
shell.Run cmd, 0, False