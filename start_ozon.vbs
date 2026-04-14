Set WinScriptHost = CreateObject("WScript.Shell")
' Запускаем скрипт, указывая путь в кавычках на случай пробелов
WinScriptHost.Run "python.exe ""bot.py""", 0
Set WinScriptHost = Nothing