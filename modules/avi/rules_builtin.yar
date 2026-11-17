// Встроенный набор правил Avi.
//
// Файл compiled в бинарь (include_str!), поэтому он закрыт той же страховкой
// манифеста, что и сам exe: подменить правила, не подменив бинарь, нельзя.
// Папка --rules читается ДОПОЛНИТЕЛЬНО к этому набору, а не вместо него.
//
// Подмножество синтаксиса: текстовые и hex-строки (без регулярных выражений),
// условия any/all/N of, and/or/not. Правила, которые не разбираются,
// оглашаются при старте, а не молча игнорируются.

rule EicarTestFile {
    meta:
        score = 1.00
        description = "EICAR-тест: файл проверки антивирусных конвейеров"
    strings:
        $a = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    condition:
        $a
}

rule AmsiBypass {
    meta:
        score = 0.95
        description = "Обход AMSI через amsiInitFailed"
    strings:
        $a = "AmsiUtils" nocase
        $b = "amsiInitFailed" nocase
        $c = "AmsiScanBuffer" nocase
    condition:
        all of ($a, $b) or ($c and $a)
}

rule MimikatzStrings {
    meta:
        score = 0.95
        description = "Строки mimikatz/sekurlsa"
    strings:
        $a = "sekurlsa::logonpasswords" nocase
        $b = "mimikatz" nocase
        $c = "kerberos::golden" nocase
    condition:
        any of them
}

rule LsassDumpViaComsvcs {
    meta:
        score = 0.90
        description = "Дамп lsass через comsvcs!MiniDump"
    strings:
        $a = "comsvcs.dll" nocase
        $b = "MiniDump" nocase
        $c = "procdump" nocase
        $d = "lsass" nocase
    condition:
        ($a and $b) or ($c and $d)
}

rule WebshellCreateObject {
    meta:
        score = 0.85
        description = "JScript/VBS-вебшелл: WScript.Shell + сетевой клиент"
    strings:
        $a = "WScript.Shell" nocase
        $b = "MSXML2.XMLHTTP" nocase
        $c = "WinHttp.WinHttpRequest" nocase
    condition:
        $a and any of ($b, $c)
}

rule FileCryptorNote {
    meta:
        score = 0.85
        description = "Текст записки вымогателя"
    strings:
        $a = "files have been encrypted" nocase
        $b = "decrypt" nocase
        $c = "bitcoin" nocase
        $d = "restore your files" nocase
    condition:
        $a or ($b and $c and $d)
}

rule HiddenPowerShellDownload {
    meta:
        score = 0.80
        description = "PowerShell скачивает и исполняет в памяти"
    strings:
        $a = "powershell" nocase
        $b = "DownloadString" nocase
        $c = "Invoke-Expression" nocase
        $d = "-w hidden" nocase
        $e = "-WindowStyle Hidden" nocase
    condition:
        $a and $b and any of ($c, $d, $e)
}

rule EncodedPowerShell {
    meta:
        score = 0.70
        description = "Base64-команда в PowerShell"
    strings:
        $a = "powershell" nocase
        $b = "-enc" nocase
        $c = "EncodedCommand" nocase
        $d = "FromBase64String" nocase
    condition:
        $a and any of ($b, $c, $d)
}

rule RunKeyPersistence {
    meta:
        score = 0.70
        description = "Автозапуск через CurrentVersion\\Run"
    strings:
        $a = "CurrentVersion\\Run" nocase
        $b = "reg add" nocase
        $c = "New-ItemProperty" nocase
    condition:
        $a and any of ($b, $c)
}

rule ScheduledTaskElevated {
    meta:
        score = 0.70
        description = "Плановая задача с наивысшими привилегиями"
    strings:
        $a = "schtasks" nocase
        $b = "/create" nocase
        $c = "/rl highest" nocase
    condition:
        $a and all of ($b, $c)
}

rule CertutilDecodeDrop {
    meta:
        score = 0.65
        description = "certutil раскодировывает полезную нагрузку"
    strings:
        $a = "certutil" nocase
        $b = "-decode" nocase
        $c = "-encode" nocase
        $d = "urlcache" nocase
    condition:
        $a and any of ($b, $c, $d)
}

rule MshtaScriptlet {
    meta:
        score = 0.80
        description = "mshta с inline-скриптом"
    strings:
        $a = "mshta" nocase
        $b = "vbscript:execscript" nocase
        $c = "javascript:" nocase
        $d = "about:" nocase
    condition:
        $a and any of ($b, $c, $d)
}

rule ServiceInstallFromScript {
    meta:
        score = 0.60
        description = "Установка службы из скрипта"
    strings:
        $a = "sc create" nocase
        $b = "sc.exe" nocase
        $c = "binPath=" nocase
        $d = "New-Service" nocase
    condition:
        ($a and $c) or ($b and $c) or $d
}
