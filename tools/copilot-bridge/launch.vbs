' Starts the Copilot bridge without a console window. Used by the scheduled task.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
node = dir & "\node-path.txt"
exe = "node"
If fso.FileExists(node) Then exe = Trim(fso.OpenTextFile(node).ReadLine())
port = dir & "\port.txt"
If fso.FileExists(port) Then sh.Environment("PROCESS")("COPILOT_BRIDGE_PORT") = Trim(fso.OpenTextFile(port).ReadLine())
sh.Run """" & exe & """ """ & dir & "\bridge.mjs""", 0, False
