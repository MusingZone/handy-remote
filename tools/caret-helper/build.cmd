@echo off
rem Builds caret_helper.exe with the C# compiler that ships with .NET Framework 4 (no SDK needed).
set FW=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319
"%FW%\csc.exe" /nologo /target:winexe /optimize+ /out:"%~dp0caret_helper.exe" /lib:"%FW%\WPF" /r:Accessibility.dll /r:UIAutomationClient.dll /r:UIAutomationTypes.dll /r:WindowsBase.dll "%~dp0caret_helper.cs"
if errorlevel 1 (echo BUILD-FAILED & exit /b 1)
echo BUILD-OK
