//! sysmon.rs — системный монитор окружения Windows для сайдкара avi.
//!
//! Модуль только читает и только оповещает: ничего не удаляет, не правит и не
//! карантинит. Права обычного пользователя — осознанное ограничение v1: всё,
//! что нельзя прочитать без администратора, попадает в `restricted`, а не
//! выдаётся за проверенное.
//!
//! Снимок строится по шести областям: автозагрузка, службы, задачи, реестр и
//! настройки защиты, слушающие порты, журнал событий. Первый тик запоминает
//! базу и оповещает только о явно подозрительном; дальше каждое изменение
//! (появление, правка, исчезновение) становится событием в общей базе avi.
//!
//! Вывод `reg.exe`/`netstat.exe` приходит в OEM-кодировке консоли, поэтому
//! не-ASCII часть пути может выглядеть искажённой. Для сравнения снимков это
//! безопасно (искажение одинаково на каждом тике), а детали события остаются
//! информативными по ключу.

use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::Duration;
use sysinfo::System;
use walkdir::WalkDir;

const MAX_EVENTS_PER_TICK: usize = 60;
const EVENTLOG_KEEP_S: i64 = 7 * 86_400;

#[derive(Clone)]
struct Item {
    kind: &'static str,
    key: String,
    value: String,
    score: f64,
    detail: String,
}

fn now() -> i64 {
    crate::db_now()
}

#[cfg(windows)]
fn run(cmd: &str, args: &[&str]) -> Result<String> {
    use std::os::windows::process::CommandExt;
    let out = Command::new(cmd)
        .args(args)
        .creation_flags(0x0800_0000)
        .output()
        .with_context(|| format!("не удалось запустить {cmd}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("{cmd} {}: {}", args.join(" "), err.trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout).to_string())
}

#[cfg(not(windows))]
fn run(cmd: &str, _args: &[&str]) -> Result<String> {
    anyhow::bail!("{cmd}: системный монитор доступен только в Windows")
}

fn is_denied(err: &str) -> bool {
    let e = err.to_lowercase();
    e.contains("отказано") || e.contains("denied") || e.contains("ошибка 5")
}

fn truncate(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    text.chars().take(limit).collect::<String>() + "…"
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

fn dword(raw: &str) -> Option<u64> {
    let value = raw.rsplit('|').next().unwrap_or(raw).trim();
    if let Some(hex) = value.strip_prefix("0x").or_else(|| value.strip_prefix("0X")) {
        u64::from_str_radix(hex, 16).ok()
    } else {
        value.parse().ok()
    }
}

/// Признаки того, что строка автозапуска/службы/задачи заслуживает внимания
/// уже при первом снимке. Это не вердикт, а повод показать запись человеку.
/// `rundll32`/`regsvr32` сами по себе — обычные системные хосты (Windows
/// запускает через них десятки штатных задач), поэтому подозрительны только
/// вместе с внешним кодом: `javascript:`, ссылкой или путём из Temp/AppData.
fn suspicious_text(text: &str) -> bool {
    let t = text.to_lowercase();
    const STRONG: &[&str] = &[
        "mshta", "wscript", "cscript", "frombase64string", "-encodedcommand",
        "\\appdata\\local\\temp\\", "\\windows\\temp\\", "\\downloads\\",
    ];
    if STRONG.iter().any(|needle| t.contains(needle)) {
        return true;
    }
    if t.contains("powershell")
        && (t.contains("-enc") || t.contains("encodedcommand") || t.contains("frombase64"))
    {
        return true;
    }
    if (t.contains("rundll32") || t.contains("regsvr32"))
        && (t.contains("javascript:") || t.contains("http://") || t.contains("https://"))
    {
        return true;
    }
    false
}

/// Разбор вывода `reg query`: заголовок ключа и строки `имя  тип  значение`.
/// Возвращает (ключ, имя значения, "тип|значение").
fn parse_reg(out: &str) -> Vec<(String, String, String)> {
    let mut cur = String::new();
    let mut rows = Vec::new();
    for line in out.lines() {
        if line.starts_with("HKEY_") {
            cur = line.trim().to_string();
            continue;
        }
        let trimmed = line.trim_start();
        if trimmed.is_empty() {
            continue;
        }
        let name_end = match trimmed.find(char::is_whitespace) {
            Some(i) => i,
            None => continue,
        };
        let name = &trimmed[..name_end];
        let rest = trimmed[name_end..].trim_start();
        let ty_end = match rest.find(char::is_whitespace) {
            Some(i) => i,
            None => continue,
        };
        let ty = &rest[..ty_end];
        if !ty.starts_with("REG_") {
            continue;
        }
        let value = rest[ty_end..].trim();
        rows.push((cur.clone(), name.to_string(), format!("{ty}|{value}")));
    }
    rows
}

fn reg_query(key: &str, recursive: bool, value: Option<&str>) -> Result<Vec<(String, String, String)>> {
    let mut args: Vec<&str> = vec!["query", key];
    if recursive {
        args.push("/s");
    }
    if let Some(name) = value {
        args.push("/v");
        args.push(name);
    }
    let out = run("reg", &args)?;
    Ok(parse_reg(&out))
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// Текст первого тега с учётом атрибутов (`<EventID Qualifiers="0">`).
fn tag_text(block: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}");
    let start = block.find(&open)?;
    let gt = block[start..].find('>')? + start + 1;
    let close = format!("</{tag}>");
    let end = block[gt..].find(&close)? + gt;
    Some(xml_unescape(block[gt..end].trim()))
}

fn status_path(db: &Path) -> PathBuf {
    db.with_file_name("sysmon_status.json")
}

#[derive(Serialize)]
struct Status {
    ts: i64,
    interval: u64,
    baseline: bool,
    counts: BTreeMap<String, usize>,
    alerts: usize,
    changes: usize,
    errors: Vec<String>,
    restricted: Vec<String>,
    last_error: String,
}

fn write_status(path: &Path, st: &Status) {
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Ok(data) = serde_json::to_vec_pretty(st) {
        let tmp = path.with_extension("json.tmp");
        if fs::write(&tmp, data).is_ok() {
            let _ = fs::rename(&tmp, path);
        }
    }
}

// ── Автозагрузка ──────────────────────────────────────────────────────────

const RUN_KEYS: &[&str] = &[
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce",
    r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run",
    r"HKLM\Software\Microsoft\Windows\CurrentVersion\RunOnce",
    r"HKLM\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run",
    r"HKCU\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run",
];

const WINLOGON_KEY: &str = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon";
const WINLOGON_VALUES: &[&str] = &["Shell", "Userinit", "Taskman", "AppSetup"];
const APPINIT_KEY: &str = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows";
const APPINIT_VALUES: &[&str] = &["AppInit_DLLs", "LoadAppInit_DLLs"];
const IFEO_KEY: &str = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options";

fn push_reg_items(
    items: &mut Vec<Item>,
    key: &str,
    recursive: bool,
    only: Option<&[&str]>,
) {
    let rows = match reg_query(key, recursive, None) {
        Ok(rows) => rows,
        // Отсутствие ключа — норма (политик может не быть). Локализованный
        // stderr `reg.exe` приходит в OEM-кодировке, поэтому различать причины
        // по тексту нельзя; молча пропускаем, а важные для прав области
        // (службы, исключения Defender) проверяются отдельно.
        Err(_) => return,
    };
    for (row_key, name, value) in rows {
        if let Some(allow) = only {
            if !allow.iter().any(|x| x.eq_ignore_ascii_case(&name)) {
                continue;
            }
        }
        if name.is_empty() || name.starts_with("(Default)") {
            continue;
        }
        let full = format!("{row_key}\\{name}");
        items.push(Item {
            kind: "autorun",
            key: full,
            value: value.clone(),
            score: 0.65,
            detail: format!("{name} = {value}"),
        });
    }
}

fn startup_dirs() -> Vec<PathBuf> {
    let mut out = Vec::new();
    for var in ["APPDATA", "ProgramData"] {
        if let Ok(base) = std::env::var(var) {
            out.push(
                Path::new(&base)
                    .join("Microsoft")
                    .join("Windows")
                    .join("Start Menu")
                    .join("Programs")
                    .join("Startup"),
            );
        }
    }
    out
}

fn collect_autorun(items: &mut Vec<Item>, restricted: &mut Vec<String>) {
    for key in RUN_KEYS {
        push_reg_items(items, key, false, None);
    }

    // Winlogon/AppInit читаются точечно: в тех же ключах лежит много шума,
    // а подмена Shell или Userinit — классический способ закрепиться.
    push_reg_items(items, WINLOGON_KEY, false, Some(WINLOGON_VALUES));
    push_reg_items(items, APPINIT_KEY, false, Some(APPINIT_VALUES));

    // IFEO: рекурсивный запрос находит все отладочные перехваты (`Debugger`).
    match reg_query(IFEO_KEY, true, Some("Debugger")) {
        Ok(rows) => {
            for (key, name, value) in rows {
                if !name.eq_ignore_ascii_case("Debugger") {
                    continue;
                }
                let exe = key.rsplit('\\').next().unwrap_or("").to_string();
                items.push(Item {
                    kind: "autorun",
                    key: format!("{key}\\{name}"),
                    value: value.clone(),
                    score: 0.8,
                    detail: format!("перехват запуска {exe} через {value}"),
                });
            }
        }
        // Значения `Debugger` может не быть ни у одного образа — это норма,
        // а не ошибка чтения. Отказ в доступе здесь неотличим от отсутствия.
        Err(_) => {}
    }

    for dir in startup_dirs() {
        match fs::read_dir(&dir) {
            Ok(entries) => {
                for entry in entries.flatten() {
                    let path = entry.path();
                    let meta = match entry.metadata() {
                        Ok(m) => m,
                        Err(_) => continue,
                    };
                    if !meta.is_file() {
                        continue;
                    }
                    let mtime = meta
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                        .map(|d| d.as_secs())
                        .unwrap_or(0);
                    items.push(Item {
                        kind: "autorun",
                        key: path.display().to_string(),
                        value: format!("{}|{mtime}", meta.len()),
                        score: 0.65,
                        detail: format!("файл автозагрузки {}", path.display()),
                    });
                }
            }
            Err(e) => {
                if is_denied(&e.to_string()) {
                    restricted.push(dir.display().to_string());
                } else if e.kind() != std::io::ErrorKind::NotFound {
                    restricted.push(format!("папка автозагрузки {}: {e}", dir.display()));
                }
            }
        }
    }
}

// ── Службы ────────────────────────────────────────────────────────────────

const SERVICES_KEY: &str = r"HKLM\SYSTEM\CurrentControlSet\Services";

fn collect_services(
    items: &mut Vec<Item>,
    errors: &mut Vec<String>,
    restricted: &mut Vec<String>,
) {
    let images = match reg_query(SERVICES_KEY, true, Some("ImagePath")) {
        Ok(rows) => rows,
        Err(e) => {
            let text = format!("{e:#}");
            if is_denied(&text) {
                restricted.push(SERVICES_KEY.to_string());
            } else {
                errors.push(format!("службы: {text}"));
            }
            return;
        }
    };

    let mut starts: HashMap<String, String> = HashMap::new();
    if let Ok(rows) = reg_query(SERVICES_KEY, true, Some("Start")) {
        for (key, name, value) in rows {
            if name.eq_ignore_ascii_case("Start") {
                if let Some(svc) = key.rsplit('\\').next() {
                    starts.insert(svc.to_string(), value);
                }
            }
        }
    }

    for (key, name, value) in images {
        if !name.eq_ignore_ascii_case("ImagePath") {
            continue;
        }
        let svc = match key.rsplit('\\').next() {
            Some(s) if !s.is_empty() => s.to_string(),
            _ => continue,
        };
        let start = starts.get(&svc).cloned().unwrap_or_default();
        items.push(Item {
            kind: "service",
            key: format!("{key}\\ImagePath"),
            value: format!("{value}|{start}"),
            score: 0.65,
            detail: format!("служба {svc}: {value} (Start {start})"),
        });
    }
}

// ── Задачи планировщика ───────────────────────────────────────────────────

fn tasks_dir() -> Option<PathBuf> {
    let root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".to_string());
    let dir = Path::new(&root).join("System32").join("Tasks");
    if dir.is_dir() {
        Some(dir)
    } else {
        None
    }
}

fn collect_tasks(
    items: &mut Vec<Item>,
    errors: &mut Vec<String>,
    restricted: &mut Vec<String>,
) {
    let dir = match tasks_dir() {
        Some(d) => d,
        None => {
            restricted.push(r"%SystemRoot%\System32\Tasks".to_string());
            return;
        }
    };

    let mut denied = 0usize;
    for entry in WalkDir::new(&dir).follow_links(false).into_iter().flatten() {
        if !entry.file_type().is_file() {
            continue;
        }
        let path = entry.path();
        let rel = match path.strip_prefix(&dir) {
            Ok(r) => r.display().to_string(),
            Err(_) => path.display().to_string(),
        };
        let data = match fs::read(path) {
            Ok(d) => d,
            Err(e) => {
                if is_denied(&e.to_string()) {
                    denied += 1;
                }
                continue;
            }
        };
        if data.len() > 2 * 1024 * 1024 {
            continue;
        }
        let text = String::from_utf8_lossy(&data);
        let command = tag_text(&text, "Command").unwrap_or_default();
        let args = tag_text(&text, "Arguments").unwrap_or_default();
        let detail = if command.is_empty() {
            format!("задача {rel}")
        } else {
            format!("задача {rel}: {command} {args}")
        };
        items.push(Item {
            kind: "task",
            key: rel,
            value: sha256_hex(&data),
            score: 0.6,
            detail,
        });
    }

    let collected = items.iter().filter(|i| i.kind == "task").count();
    if collected == 0 {
        // Каталог задач закрыт даже на чтение — обычное дело без админа.
        // `schtasks` отдаёт тот же список через планировщик; порядок колонок
        // стабилен, хотя заголовки локализованы.
        collect_tasks_cli(items, errors);
    } else if denied > 0 {
        restricted.push(format!("задачи: нет чтения у {denied} файлов"));
    }
    if items.iter().all(|i| i.kind != "task") {
        restricted.push(format!("задачи не прочитаны: {}", dir.display()));
    }
}

fn csv_fields(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_quotes = false;
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        if in_quotes {
            if c == '"' {
                if chars.peek() == Some(&'"') {
                    cur.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                cur.push(c);
            }
        } else if c == '"' {
            in_quotes = true;
        } else if c == ',' {
            out.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    out.push(cur);
    out
}

/// Запасной сбор задач через `schtasks`: строки `TaskName` и `Task To Run`
/// берутся по позиции (1 и 8), потому что заголовки локализованы.
fn collect_tasks_cli(items: &mut Vec<Item>, errors: &mut Vec<String>) {
    match run("schtasks", &["/query", "/fo", "csv", "/v", "/nh"]) {
        Ok(out) => {
            for line in out.lines() {
                if line.trim().is_empty() {
                    continue;
                }
                let fields = csv_fields(line);
                if fields.len() < 9 {
                    continue;
                }
                let name = fields[1].trim();
                let action = fields[8].trim();
                let author = fields.get(7).map(|s| s.trim()).unwrap_or("");
                if name.is_empty() {
                    continue;
                }
                items.push(Item {
                    kind: "task",
                    key: name.to_string(),
                    value: format!("{action}|{author}"),
                    score: 0.6,
                    detail: format!("задача {name}: {action}"),
                });
            }
        }
        Err(e) => errors.push(format!("задачи (schtasks): {e:#}")),
    }
}

// ── Реестр и настройки защиты ─────────────────────────────────────────────

const DEFENSE_VALUES: &[(&str, &[&str])] = &[
    (r"HKLM\SOFTWARE\Microsoft\Windows Defender\Features", &["TamperProtection"]),
    (r"HKLM\SOFTWARE\Policies\Microsoft\Windows Defender", &["DisableAntiSpyware", "DisableAntiVirus"]),
    (r"HKLM\SOFTWARE\Microsoft\Windows Defender", &["DisableAntiSpyware", "DisableAntiVirus", "ServiceStartType"]),
    (r"HKLM\SYSTEM\CurrentControlSet\Control\Lsa", &["RunAsPPL"]),
    (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", &["EnableLUA", "ConsentPromptBehaviorAdmin"]),
    (r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer", &["SmartScreenEnabled"]),
    (r"HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile", &["EnableFirewall"]),
    (r"HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\DomainProfile", &["EnableFirewall"]),
    (r"HKLM\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\PublicProfile", &["EnableFirewall"]),
];

const EXCLUSION_KEYS: &[&str] = &[
    r"HKLM\SOFTWARE\Microsoft\Windows Defender\Exclusions\Paths",
    r"HKLM\SOFTWARE\Microsoft\Windows Defender\Exclusions\Extensions",
    r"HKLM\SOFTWARE\Microsoft\Windows Defender\Exclusions\Processes",
];

fn defense_score(name: &str, value: &str) -> f64 {
    let v = value.to_lowercase();
    match name {
        "DisableAntiSpyware" | "DisableAntiVirus" => {
            if dword(value) == Some(1) { 0.9 } else { 0.55 }
        }
        "TamperProtection" => if dword(value) == Some(0) { 0.75 } else { 0.5 },
        "EnableLUA" => if dword(value) == Some(0) { 0.85 } else { 0.5 },
        "RunAsPPL" => if dword(value) == Some(0) { 0.6 } else { 0.5 },
        "EnableFirewall" => if dword(value) == Some(0) { 0.65 } else { 0.45 },
        "SmartScreenEnabled" => if v.contains("off") { 0.65 } else { 0.45 },
        "ConsentPromptBehaviorAdmin" => if dword(value) == Some(0) { 0.6 } else { 0.45 },
        _ => 0.5,
    }
}

fn collect_defense(items: &mut Vec<Item>, restricted: &mut Vec<String>) {
    for (key, names) in DEFENSE_VALUES {
        for name in names.iter() {
            match reg_query(key, false, Some(name)) {
                Ok(rows) => {
                    let found = rows
                        .into_iter()
                        .find(|(_, n, _)| n.eq_ignore_ascii_case(name));
                    if let Some((_, _, value)) = found {
                        items.push(Item {
                            kind: "defense",
                            key: format!("{key}\\{name}"),
                            value: value.clone(),
                            score: defense_score(name, &value),
                            detail: format!("{name} = {value}"),
                        });
                    }
                }
                // Значения политики может не быть — это норма, а не сбой.
                Err(_) => {}
            }
        }
    }

    for key in EXCLUSION_KEYS {
        match reg_query(key, false, None) {
            Ok(rows) => {
                for (_, name, value) in rows {
                    if name.is_empty() || name.starts_with("(Default)") {
                        continue;
                    }
                    items.push(Item {
                        kind: "defense",
                        key: format!("{key}\\{name}"),
                        value: value.clone(),
                        score: 0.85,
                        detail: format!("исключение Defender: {name}"),
                    });
                }
            }
            // Исключения Defender закрыты от обычного пользователя. Это
            // ограничение v1, а не сбой: честно показываем в `restricted`.
            Err(_) => restricted.push(key.to_string()),
        }
    }
}

// ── Сеть ──────────────────────────────────────────────────────────────────

fn process_meta() -> HashMap<u32, (String, String)> {
    let mut sys = System::new();
    sys.refresh_processes();
    sys.processes()
        .iter()
        .map(|(pid, p)| {
            (
                pid.as_u32(),
                (
                    p.name().to_string(),
                    p.exe().map(|e| e.display().to_string()).unwrap_or_default(),
                ),
            )
        })
        .collect()
}

fn collect_network(items: &mut Vec<Item>, errors: &mut Vec<String>) {
    let procs = process_meta();
    for proto in ["tcp", "udp"] {
        match run("netstat", &["-ano", "-p", proto]) {
            Ok(out) => {
                for line in out.lines() {
                    let cols: Vec<&str> = line.split_whitespace().collect();
                    let (local, pid_raw) = if proto == "tcp" {
                        if cols.len() < 5 || !cols[0].eq_ignore_ascii_case("tcp") {
                            continue;
                        }
                        if !cols[3].eq_ignore_ascii_case("LISTENING") {
                            continue;
                        }
                        (cols[1], cols[4])
                    } else {
                        if cols.len() < 4 || !cols[0].eq_ignore_ascii_case("udp") {
                            continue;
                        }
                        (cols[1], cols[3])
                    };
                    let pid: u32 = match pid_raw.parse() {
                        Ok(p) => p,
                        Err(_) => continue,
                    };
                    if pid == 0 {
                        continue;
                    }
                    let (name, exe) = procs.get(&pid).cloned().unwrap_or_default();
                    let port = local
                        .rsplit(':')
                        .next()
                        .and_then(|p| p.parse::<u16>().ok())
                        .unwrap_or(0);
                    // UDP-клиенты занимают эфемерные порты и меняют их каждые
                    // секунды; для снимка важны системные UDP-слушатели, а
                    // высокие порты дают только шум. TCP слушатели смотрим все.
                    if proto == "udp" && port >= 1024 {
                        continue;
                    }
                    let score = if suspicious_text(&exe) {
                        0.65
                    } else if port < 1024 {
                        0.25
                    } else {
                        0.35
                    };
                    items.push(Item {
                        kind: "listener",
                        key: format!("{proto}:{local}"),
                        value: format!("pid={pid} {name} {exe}"),
                        score,
                        detail: format!("{proto} {local} слушает {name} (pid {pid}) {exe}"),
                    });
                }
            }
            Err(e) => errors.push(format!("сеть ({proto}): {e:#}")),
        }
    }
}

// ── Журнал событий Windows ────────────────────────────────────────────────

const EVENT_CHANNELS: &[(&str, &str, bool)] = &[
    ("System", "*[System[(EventID=7045 or EventID=7040 or EventID=104)]]", false),
    (
        "Microsoft-Windows-Windows Defender/Operational",
        "*[System[(EventID=1116 or EventID=1117 or EventID=5001 or EventID=5007)]]",
        true,
    ),
    ("Security", "*[System[(EventID=1102 or EventID=4697 or EventID=4720)]]", true),
];

fn event_score(id: u32) -> (f64, &'static str) {
    match id {
        1102 => (0.95, "журнал безопасности очищен"),
        104 => (0.95, "журнал System очищен"),
        7045 => (0.7, "установлена новая служба"),
        7040 => (0.6, "изменён тип запуска службы"),
        4697 => (0.75, "установлена служба (Security)"),
        1116 | 1117 => (0.85, "Defender обнаружил угрозу"),
        5001 => (0.9, "реальная защита Defender выключена"),
        5007 => (0.75, "настройка Defender изменена"),
        4720 => (0.8, "создан пользователь"),
        _ => (0.4, "событие журнала"),
    }
}

fn collect_eventlog(
    items: &mut Vec<Item>,
    errors: &mut Vec<String>,
    restricted: &mut Vec<String>,
) {
    for (channel, query, needs_rights) in EVENT_CHANNELS {
        let q = format!("/q:{query}");
        match run(
            "wevtutil",
            &["qe", channel, &q, "/c:40", "/rd:true", "/f:xml"],
        ) {
            Ok(out) => {
                for block in out.split("</Event>") {
                    let record = match tag_text(block, "EventRecordID") {
                        Some(r) => r,
                        None => continue,
                    };
                    let id: u32 = tag_text(block, "EventID")
                        .and_then(|x| x.parse().ok())
                        .unwrap_or(0);
                    if id == 0 {
                        continue;
                    }
                    let data = truncate(&tag_text(block, "Data").unwrap_or_default(), 300);
                    let (score, what) = event_score(id);
                    items.push(Item {
                        kind: "eventlog",
                        key: format!("{channel}|{record}"),
                        value: format!("{id}|{data}"),
                        score,
                        detail: format!("{what} (EventID {id}) {data}"),
                    });
                }
            }
            Err(e) => {
                let text = format!("{e:#}");
                if *needs_rights || is_denied(&text) {
                    restricted.push(channel.to_string());
                } else {
                    errors.push(format!("журнал {channel}: {text}"));
                }
            }
        }
    }
}

fn collect() -> (Vec<Item>, Vec<String>, Vec<String>) {
    let mut items = Vec::new();
    let mut errors = Vec::new();
    let mut restricted = Vec::new();

    if !cfg!(windows) {
        return (items, errors, restricted);
    }

    collect_autorun(&mut items, &mut restricted);
    collect_services(&mut items, &mut errors, &mut restricted);
    collect_tasks(&mut items, &mut errors, &mut restricted);
    collect_defense(&mut items, &mut restricted);
    collect_network(&mut items, &mut errors);
    collect_eventlog(&mut items, &mut errors, &mut restricted);

    // Один и тот же адрес может слушать несколько процессов (SO_REUSEADDR).
    // Для снимка это одна запись: иначе один порт даёт пачку событий.
    let mut seen = HashSet::new();
    let items = items
        .into_iter()
        .filter(|item| seen.insert((item.kind, item.key.clone())))
        .collect();

    (items, errors, restricted)
}

// ── Снимок, сравнение, события ────────────────────────────────────────────

fn baseline_score(item: &Item) -> f64 {
    match item.kind {
        "autorun" => {
            if suspicious_text(&item.value) || suspicious_text(&item.detail) { 0.7 } else { 0.0 }
        }
        "service" => if suspicious_text(&item.value) { 0.65 } else { 0.0 },
        "task" => if suspicious_text(&item.detail) { 0.7 } else { 0.0 },
        "defense" => {
            if item.key.to_lowercase().contains("\\exclusions\\") {
                0.0
            } else {
                let name = item.key.rsplit('\\').next().unwrap_or("");
                let score = defense_score(name, &item.value);
                if score >= 0.6 { score } else { 0.0 }
            }
        }
        "listener" => if suspicious_text(&item.value) { 0.65 } else { 0.0 },
        _ => 0.0,
    }
}

fn removal_score(kind: &str) -> f64 {
    match kind {
        "autorun" => 0.45,
        "service" => 0.5,
        "task" => 0.35,
        "defense" => 0.4,
        "listener" => 0.25,
        _ => 0.2,
    }
}

fn is_stable_kind(kind: &str) -> bool {
    matches!(kind, "autorun" | "service" | "task" | "defense" | "listener")
}

fn has_baseline(conn: &Connection) -> Result<bool> {
    let exists = conn
        .prepare("SELECT 1 FROM sysmon_state WHERE kind = 'meta' AND key = 'baseline'")?
        .exists([])?;
    Ok(exists)
}

fn load_state(conn: &Connection) -> Result<HashMap<(String, String), String>> {
    let mut stmt = conn.prepare("SELECT kind, key, value FROM sysmon_state")?;
    let mut rows = stmt.query([])?;
    let mut out = HashMap::new();
    while let Some(row) = rows.next()? {
        let kind: String = row.get(0)?;
        let key: String = row.get(1)?;
        let value: String = row.get(2)?;
        out.insert((kind, key), value);
    }
    Ok(out)
}

fn upsert_state(conn: &Connection, kind: &str, key: &str, value: &str, seen: i64) -> Result<()> {
    conn.execute(
        "INSERT INTO sysmon_state(kind, key, value, seen) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(kind, key) DO UPDATE SET value = excluded.value, seen = excluded.seen",
        params![kind, key, value, seen],
    )?;
    Ok(())
}

fn delete_state(conn: &Connection, kind: &str, key: &str) -> Result<()> {
    conn.execute(
        "DELETE FROM sysmon_state WHERE kind = ?1 AND key = ?2",
        params![kind, key],
    )?;
    Ok(())
}

fn emit(conn: &Connection, item: &Item, prefix: &str, old: &str, alerts: &mut usize) -> Result<()> {
    if *alerts >= MAX_EVENTS_PER_TICK {
        return Ok(());
    }
    let detail = if old.is_empty() {
        format!("{prefix}: {}", item.detail)
    } else {
        format!("{prefix}: {} (было: {})", item.detail, truncate(old, 200))
    };
    let action = if item.score >= 0.6 { "alert" } else { "info" };
    crate::db_insert_event(
        conn,
        &truncate(&item.key, 400),
        "",
        &format!("sysmon:{}", item.kind),
        &truncate(&detail, 900),
        item.score,
        action,
    )?;
    println!(
        "SYSMON {} [{}] {}: {}",
        if action == "alert" { "!" } else { "·" },
        item.kind,
        truncate(&item.key, 180),
        truncate(&detail, 300)
    );
    *alerts += 1;
    Ok(())
}

fn emit_removed(
    conn: &Connection,
    kind: &str,
    key: &str,
    old: &str,
    score: f64,
    alerts: &mut usize,
) -> Result<()> {
    if *alerts >= MAX_EVENTS_PER_TICK {
        return Ok(());
    }
    let action = if score >= 0.6 { "alert" } else { "info" };
    let detail = format!("исчезло из снимка (было: {})", truncate(old, 200));
    crate::db_insert_event(
        conn,
        &truncate(key, 400),
        "",
        &format!("sysmon:{kind}"),
        &detail,
        score,
        action,
    )?;
    println!("SYSMON {} [{kind}] {}: {detail}", if action == "alert" { "!" } else { "·" }, truncate(key, 180));
    *alerts += 1;
    Ok(())
}

fn counts(items: &[Item]) -> BTreeMap<String, usize> {
    let mut out = BTreeMap::new();
    for item in items {
        *out.entry(item.kind.to_string()).or_insert(0) += 1;
    }
    out
}

fn summary(counts: &BTreeMap<String, usize>) -> String {
    counts
        .iter()
        .map(|(kind, count)| format!("{kind}={count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

struct Outcome {
    baseline: bool,
    alerts: usize,
    changes: usize,
    counts: BTreeMap<String, usize>,
    errors: Vec<String>,
    restricted: Vec<String>,
}

fn tick(
    conn: &Connection,
    status_file: &Path,
    interval: u64,
    first: &mut bool,
) -> Result<Outcome> {
    let (items, errors, restricted) = collect();
    let ts = now();
    let baseline = *first;
    let prev = load_state(conn)?;
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut alerts = 0usize;
    let mut changes = 0usize;

    for item in &items {
        let key = (item.kind.to_string(), item.key.clone());
        seen.insert(key.clone());
        match prev.get(&key) {
            Some(old) if *old != item.value => {
                changes += 1;
                if !baseline {
                    emit(conn, item, "изменено", old, &mut alerts)?;
                }
            }
            Some(_) => {}
            None => {
                if baseline {
                    let score = baseline_score(item);
                    if score >= 0.6 {
                        let mut copy = item.clone();
                        copy.score = score;
                        emit(conn, &copy, "при первом снимке", "", &mut alerts)?;
                    }
                } else {
                    changes += 1;
                    emit(conn, item, "появилось", "", &mut alerts)?;
                }
            }
        }
        upsert_state(conn, &key.0, &key.1, &item.value, ts)?;
    }

    for ((kind, key), old) in &prev {
        if !is_stable_kind(kind) {
            continue;
        }
        if seen.contains(&(kind.clone(), key.clone())) {
            continue;
        }
        if baseline {
            delete_state(conn, kind, key)?;
            continue;
        }
        changes += 1;
        emit_removed(conn, kind, key, old, removal_score(kind), &mut alerts)?;
        delete_state(conn, kind, key)?;
    }

    let _ = conn.execute(
        "DELETE FROM sysmon_state WHERE kind = 'eventlog' AND seen < ?1",
        params![ts - EVENTLOG_KEEP_S],
    );

    if alerts >= MAX_EVENTS_PER_TICK {
        let _ = crate::db_insert_event(
            conn,
            "sysmon",
            "",
            "sysmon:limit",
            &format!(
                "изменений больше лимита: записано {alerts}, всего в снимке {}",
                items.len()
            ),
            0.0,
            "info",
        );
    }

    let counts = counts(&items);
    let outcome = Outcome {
        baseline,
        alerts,
        changes,
        counts: counts.clone(),
        errors: errors.clone(),
        restricted: restricted.clone(),
    };

    if baseline {
        conn.execute(
            "INSERT OR REPLACE INTO sysmon_state(kind, key, value, seen)
             VALUES('meta', 'baseline', ?1, ?2)",
            params![ts.to_string(), ts],
        )?;
        *first = false;
        crate::db_insert_event(
            conn,
            "sysmon",
            "",
            "sysmon:baseline",
            &format!("первый снимок: {}", summary(&counts)),
            0.0,
            "info",
        )?;
    }

    write_status(
        status_file,
        &Status {
            ts,
            interval,
            baseline,
            counts,
            alerts,
            changes,
            errors,
            restricted,
            last_error: String::new(),
        },
    );
    Ok(outcome)
}

pub fn loop_forever(db: &Path, interval: u64) -> Result<()> {
    if !cfg!(windows) {
        return Ok(());
    }
    let conn = crate::db_init(db)?;
    let interval = interval.max(15);
    let status_file = status_path(db);
    let mut first = !has_baseline(&conn)?;
    println!(
        "Системный монитор: опрос раз в {interval} с, режим {}",
        if first { "первого снимка" } else { "сравнения" }
    );
    loop {
        match tick(&conn, &status_file, interval, &mut first) {
            Ok(o) => {
                if o.alerts > 0 || o.changes > 0 {
                    println!(
                        "SYSMON изменений {}, оповещений {}",
                        o.changes, o.alerts
                    );
                }
            }
            Err(e) => {
                eprintln!("Системный монитор: {e:#}");
                write_status(
                    &status_file,
                    &Status {
                        ts: now(),
                        interval,
                        baseline: false,
                        counts: BTreeMap::new(),
                        alerts: 0,
                        changes: 0,
                        errors: Vec::new(),
                        restricted: Vec::new(),
                        last_error: format!("{e:#}"),
                    },
                );
            }
        }
        thread::sleep(Duration::from_secs(interval));
    }
}

/// Один снимок для диагностики: печатает состав и запоминает базу.
pub fn snapshot(db: &Path) -> Result<()> {
    let conn = crate::db_init(db)?;
    let status_file = status_path(db);
    let mut first = !has_baseline(&conn)?;
    let outcome = tick(&conn, &status_file, 0, &mut first)?;
    println!(
        "Системный монитор: {}",
        if outcome.baseline { "первый снимок (база запомнена)" } else { "сравнение с прошлым снимком" }
    );
    for (kind, count) in &outcome.counts {
        println!("  {kind}: {count}");
    }
    println!(
        "  изменений: {}, оповещений: {}",
        outcome.changes, outcome.alerts
    );
    if !outcome.restricted.is_empty() {
        println!("  без прав (не проверено): {}", outcome.restricted.join(", "));
    }
    for error in outcome.errors.iter().take(10) {
        println!("  ошибка: {error}");
    }
    Ok(())
}



