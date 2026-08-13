Option Explicit

Dim shell, fileSystem, baseFolder, appScript, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
baseFolder = fileSystem.GetParentFolderName(WScript.ScriptFullName)
appScript = fileSystem.BuildPath(baseFolder, "BG3ModBridge.pyw")
command = "pyw.exe -3 " & Chr(34) & appScript & Chr(34)

On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "pythonw.exe " & Chr(34) & appScript & Chr(34), 0, False
End If
On Error GoTo 0
