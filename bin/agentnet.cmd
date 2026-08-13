@echo off
REM AgentNet launcher for cmd.exe / PowerShell.
REM
REM Paired with the extensionless "agentnet" for POSIX shells -- both are needed,
REM see the comments in that file.
REM
REM ASCII ONLY in this file: cmd.exe decodes .cmd with the OEM codepage, so UTF-8
REM text in comments gets mojibaked and can emit bytes that split a REM line into
REM a bogus command. (Learned the hard way.)
if "%AGENTNET_HOME%"=="" set "AGENTNET_HOME=%USERPROFILE%\.agentnet"
python "%AGENTNET_HOME%\scripts\agentnet.py" %*
