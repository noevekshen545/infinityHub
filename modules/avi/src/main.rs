use anyhow::{anyhow, bail, Context, Result};
use clap::{Args, Parser, Subcommand, ValueEnum};
use crossterm::{
    event::{self, Event as CrosstermEvent, KeyCode},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use goblin::pe::PE;
use notify::{Event as NotifyEvent, EventKind, RecursiveMode, Watcher};
use rand::rngs::OsRng;
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    widgets::{Block, Borders, Paragraph},
    Terminal,
};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env,
    ffi::OsStr,
    fs::{self, File},
    io::{Cursor, Read, Seek},
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Mutex, OnceLock},
    sync::atomic::{AtomicI64, AtomicU64, Ordering},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use sysinfo::System;
use walkdir::WalkDir;
use zip::ZipArchive;

mod rules;
mod selfprotect;
mod sysmon;

const EICAR: &[u8] =
    br#"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"#;

const FEATURE_COUNT: usize = 8;
const SAMPLE_MAX: usize = 4 * 1024 * 1024;
const YARA_MAX_SIZE: u64 = 20 * 1024 * 1024;

const MAX_ZIP_ENTRIES: usize = 1000;
const MAX_ZIP_TOTAL: u64 = 200 * 1024 * 1024;
const MAX_ZIP_ENTRY: u64 = 20 * 1024 * 1024;
const MAX_ARCHIVE_DEPTH: u32 = 2;

static TMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Parser)]
#[command(
    version,
    about = "MiniAV EDR: максимальный однофайловый прототип (не замена системному AV)"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Сканировать один или несколько путей
    Scan {
        #[command(flatten)]
        args: CommonArgs,
    },

    /// Мониторинг одного или нескольких путей
    Watch {
        #[command(flatten)]
        args: CommonArgs,
    },

    /// Защита одного файла: мониторинг, восстановление, read-only, immutable
    ProtectFile {
        /// Файл для защиты
        path: PathBuf,

        /// База данных
        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,

        /// Интервал проверки, сек
        #[arg(long, default_value_t = 2)]
        interval: u64,

        /// Файл резервной копии
        #[arg(long)]
        backup: Option<PathBuf>,

        /// Автоматически восстанавливать файл из резервной копии
        #[arg(long)]
        restore: bool,

        /// Сделать файл только для чтения
        #[arg(long)]
        read_only: bool,

        /// Попытаться поставить атрибут неизменяемости (Linux: chattr +i)
        #[arg(long)]
        immutable: bool,

        /// Куда помещать изменённый файл перед восстановлением
        #[arg(long)]
        quarantine: Option<PathBuf>,
    },

    /// Страховка своих файлов: манифест сумм, копии, восстановление, отказ на удаление
    Guard {
        /// Каталог страховки
        #[arg(long, default_value = "avi_guard")]
        guard: PathBuf,

        /// Что беречь; по умолчанию — сам запускаемый бинарь
        files: Vec<PathBuf>,

        /// Запечатать: копии + манифест контрольных сумм
        #[arg(long)]
        seal: bool,

        /// Сверить живые файлы и копии с манифестом
        #[arg(long)]
        check: bool,

        /// Восстановить повреждённое из копии
        #[arg(long)]
        restore: bool,

        /// Поставить NTFS-отказ на удаление и переналадку прав каталога страховки
        #[arg(long)]
        lock_acl: bool,

        /// Снять отказ
        #[arg(long)]
        unlock_acl: bool,

        /// Секретный ключ для подписи манифеста
        #[arg(long)]
        secret: Option<PathBuf>,

        /// Открытый ключ для проверки подписи манифеста
        #[arg(long)]
        pubkey: Option<PathBuf>,
    },

    /// Вердикт для обучения
    Verdict {
        path: PathBuf,

        #[arg(long, value_enum)]
        label: Label,

        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,
    },

    /// Диагностика подписи: нативный вердикт WinVerifyTrust по одному файлу
    SigCheck {
        /// Файл для проверки
        path: PathBuf,
    },

    /// Обучить модель
    Train {
        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,

        #[arg(long, default_value = "model.json")]
        model: PathBuf,

        #[arg(long, default_value_t = 3000)]
        epochs: usize,

        #[arg(long, default_value_t = 0.05)]
        lr: f64,
    },

    /// TUI
    Tui {
        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,
    },

    /// Добавить хеш в blacklist/allowlist
    AddHash {
        sha256: String,

        #[arg(long, value_enum)]
        list: HashList,

        #[arg(long, default_value = "manual")]
        source: String,

        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,
    },

    /// Сгенерировать ключи подписи обновлений
    Keygen {
        #[arg(long, default_value = "miniav.pub")]
        public: PathBuf,

        #[arg(long, default_value = "miniav.key")]
        secret: PathBuf,
    },

    /// Подписать файл
    SignFile {
        file: PathBuf,

        #[arg(long, default_value = "miniav.key")]
        secret: PathBuf,
    },

    /// Проверить подписанные обновления в папке
    VerifyUpdate {
        #[arg(long)]
        dir: PathBuf,

        #[arg(long, default_value = "miniav.pub")]
        public: PathBuf,
    },

    /// Системный монитор: снимок автозагрузки, служб, задач, реестра, сети и журнала
    Sysmon {
        #[arg(long, default_value = "miniav.db")]
        db: PathBuf,

        /// Сделать один снимок и напечатать состав
        #[arg(long)]
        once: bool,

        /// Интервал опроса, сек
        #[arg(long, default_value_t = 120)]
        interval: u64,
    },
}

#[derive(Args, Clone)]
struct CommonArgs {
    /// Пути для сканирования/мониторинга
    #[arg(required = true)]
    paths: Vec<PathBuf>,

    /// База данных
    #[arg(long, default_value = "miniav.db")]
    db: PathBuf,

    /// Папка карантина
    #[arg(long)]
    quarantine: Option<PathBuf>,

    /// Файл модели
    #[arg(long, default_value = "model.json")]
    model: PathBuf,

    /// Порог карантина
    #[arg(long, default_value_t = 0.80)]
    threshold: f64,

    /// Максимальный размер файла для полного хеширования, MB
    #[arg(long, default_value_t = 100)]
    max_mb: u64,

    /// Папка с YARA-правилами
    #[arg(long, default_value = "rules")]
    rules: PathBuf,

    /// Отключить YARA
    #[arg(long)]
    no_yara: bool,

    /// Отключить встроенный набор правил (останутся только файлы из --rules)
    #[arg(long)]
    no_builtin_rules: bool,

    /// Отключить PE-эвристики
    #[arg(long)]
    no_pe: bool,

    /// Отключить сканирование архивов
    #[arg(long)]
    no_archives: bool,

    /// Проверять подписи у каждого исполняемого файла, а не только у кандидатов
    /// в находки (дороже: каталожные подписи считаются внешним PowerShell)
    #[arg(long)]
    check_signatures: bool,

    /// Отключить проверку подписей; по умолчанию она включена, потому что без
    /// неё каждый подписанный системный EXE получает баллы за «опасное
    /// расширение» и становится находкой
    #[arg(long)]
    no_check_signatures: bool,

    /// Сколько файлов проверять одновременно в watch-режиме
    #[arg(long, default_value_t = 4)]
    workers: usize,

    /// Каталог страховки собственных файлов. Если манифест там есть, старт
    /// watch сверяет свой бинарь и отказывается запускаться на подмене
    #[arg(long, default_value = "avi_guard")]
    guard: PathBuf,

    /// Открытый ключ для проверки подписи манифеста страховки
    #[arg(long)]
    guard_pubkey: Option<PathBuf>,

    /// Не делать предстартовую самопроверку по страховке
    #[arg(long)]
    no_self_guard: bool,

    /// Не делать начальное сканирование в watch
    #[arg(long)]
    no_initial: bool,

    /// Ключ проверки подписанных обновлений
    #[arg(long)]
    update_pubkey: Option<PathBuf>,

    /// Включить поведенческий мониторинг процессов
    #[arg(long)]
    with_process_monitor: bool,

    /// Интервал опроса процессов, сек
    #[arg(long, default_value_t = 5)]
    process_interval: u64,

    /// Ключ облачного API
    #[arg(long)]
    cloud_api_key: Option<String>,

    /// Базовый URL облачной проверки хешей
    #[arg(long, default_value = "https://www.virustotal.com/api/v3/files")]
    cloud_url_base: String,

    /// Дополнительные исключения
    #[arg(long)]
    exclude: Vec<PathBuf>,

    /// Отключить встроенные исключения
    #[arg(long)]
    no_default_excludes: bool,

    /// Не доверять кэшу обхода: перечитать и переоценить каждый файл заново
    #[arg(long)]
    no_cache: bool,

    /// Системный монитор окружения Windows (автозагрузка, службы, задачи,
    /// реестр и настройки защиты, сеть, журнал событий)
    #[arg(long)]
    with_sysmon: bool,

    /// Интервал опроса системного монитора, сек
    #[arg(long, default_value_t = 120)]
    sysmon_interval: u64,
}

#[derive(ValueEnum, Clone, Copy)]
enum Label {
    Malicious,
    Clean,
}

#[derive(ValueEnum, Clone, Copy)]
enum HashList {
    Black,
    Allow,
}

#[derive(Serialize, Deserialize)]
struct Model {
    weights: Vec<f64>,
    bias: f64,
    feature_count: usize,
}

struct Analyzed {
    hash: String,
    sample: Vec<u8>,
    size: u64,
    mtime: i64,
    features: Vec<f64>,
}

struct Finding {
    path: PathBuf,
    hash: String,
    reason: String,
    score: f64,
}

/// Движок сигнатур. Правила разбираются и ищутся внутри процесса; внешний
/// `yara`, если он установлен, добавляется сверх этого, а не выключает детект
/// целиком, когда его нет (именно так раньше и происходило на этой машине).
struct YaraScanner {
    enabled: bool,
    engine: rules::Engine,
    external: Vec<PathBuf>,
    use_external: bool,
}

type RuleHit = rules::Hit;

const DB_SCHEMA: &str = r#"
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    sha256 TEXT,
    last_score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    fp INTEGER NOT NULL DEFAULT 0,
    last_scan INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    source TEXT NOT NULL,
    detail TEXT NOT NULL,
    score REAL NOT NULL,
    action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    original_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    reason TEXT,
    features_json TEXT
);

CREATE TABLE IF NOT EXISTS hashes_blacklist (
    sha256 TEXT PRIMARY KEY,
    source TEXT,
    added INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hashes_allowlist (
    sha256 TEXT PRIMARY KEY,
    source TEXT,
    added INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sysmon_state (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    seen INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
"#;

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.cmd {
        Cmd::Scan { args } => scan_cmd(args),
        Cmd::Watch { args } => watch_cmd(args),
        Cmd::Guard {
            guard,
            files,
            seal,
            check,
            restore,
            lock_acl,
            unlock_acl,
            secret,
            pubkey,
        } => guard_cmd(
            &guard,
            files,
            seal,
            check,
            restore,
            lock_acl,
            unlock_acl,
            secret,
            pubkey,
        ),

        Cmd::ProtectFile {
            path,
            db,
            interval,
            backup,
            restore,
            read_only,
            immutable,
            quarantine,
        } => protect_file_cmd(
            &path,
            &db,
            interval,
            backup,
            restore,
            read_only,
            immutable,
            quarantine,
        ),
        Cmd::Verdict { path, label, db } => verdict_cmd(&path, label, &db),
        Cmd::SigCheck { path } => sig_check_cmd(&path),
        Cmd::Train {
            db,
            model,
            epochs,
            lr,
        } => train_cmd(&db, &model, epochs, lr),
        Cmd::Tui { db } => tui_cmd(&db),
        Cmd::AddHash {
            sha256,
            list,
            source,
            db,
        } => add_hash_cmd(&sha256, list, &source, &db),
        Cmd::Keygen { public, secret } => keygen_cmd(&public, &secret),
        Cmd::SignFile { file, secret } => sign_file_cmd(&file, &secret),
        Cmd::VerifyUpdate { dir, public } => verify_update_cmd(&dir, &public),
        Cmd::Sysmon { db, once, interval } => sysmon_cmd(&db, once, interval),
    }
}

fn normalize_common(mut args: CommonArgs) -> CommonArgs {
    args.paths = args
        .paths
        .iter()
        .map(|p| fs::canonicalize(p).unwrap_or_else(|_| p.clone()))
        .collect();

    let mut excludes = if args.no_default_excludes {
        Vec::new()
    } else {
        default_excludes()
    };

    excludes.extend(args.exclude.clone());

    if let Some(q) = args.quarantine.clone() {
        excludes.push(q);
    }

    args.exclude = excludes
        .into_iter()
        .map(|p| fs::canonicalize(&p).unwrap_or(p))
        .collect();

    if let Some(q) = args.quarantine.clone() {
        let _ = fs::create_dir_all(&q);
    }

    if let Some(parent) = args.db.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = fs::create_dir_all(parent);
        }
    }

    // Отпечаток считаем здесь: этот путь проходят все команды, и воркеры,
    // получившие копию аргументов, пишут в базу ровно тот fp, по которому читают.
    set_cache_fingerprint(&args);

    args
}

fn default_excludes() -> Vec<PathBuf> {
    let mut excludes = Vec::new();

    #[cfg(unix)]
    {
        excludes.push(PathBuf::from("/proc"));
        excludes.push(PathBuf::from("/sys"));
        excludes.push(PathBuf::from("/dev"));
        excludes.push(PathBuf::from("/run"));
        excludes.push(PathBuf::from("/snap"));
        excludes.push(PathBuf::from("/var/lib/miniav"));
    }

    #[cfg(windows)]
    {
        if let Ok(sysroot) = env::var("SystemRoot") {
            let sysroot = PathBuf::from(sysroot);
            excludes.push(sysroot.join("WinSxS"));
            excludes.push(sysroot.join("Installer"));
            excludes.push(sysroot.join("System32").join("DriverStore"));
        }
    }

    excludes
}

fn is_excluded(path: &Path, excludes: &[PathBuf]) -> bool {
    excludes.iter().any(|ex| path.starts_with(ex))
}

fn scan_cmd(args: CommonArgs) -> Result<()> {
    let args = normalize_common(args);

    let conn = db_init(&args.db)?;
    let yara = YaraScanner::new(
        &args.rules,
        !args.no_yara,
        &args.update_pubkey,
        !args.no_builtin_rules,
    );
    let model = load_model(&args.model, &args.update_pubkey)?;

    let mut all_findings = Vec::new();

    for path in &args.paths {
        if !path.exists() {
            eprintln!("Путь не существует: {}", path.display());
            continue;
        }

        if is_excluded(path, &args.exclude) {
            println!("Пропущено исключение: {}", path.display());
            continue;
        }

        println!("Сканирование: {}", path.display());

        let findings = scan_path(&conn, path, &args, &yara, &model, "scan")?;
        all_findings.extend(findings);
    }

    print_findings(&all_findings);

    if let Some(q) = &args.quarantine {
        for f in &all_findings {
            if let Err(e) = quarantine_file(&conn, f, q) {
                eprintln!("Ошибка карантина: {e:#}");
            }
        }
    } else if !all_findings.is_empty() {
        println!("Режим отчёта: добавь --quarantine <папка>, чтобы перемещать находки.");
    }

    if !all_findings.is_empty() {
        std::process::exit(1);
    }

    Ok(())
}

fn sysmon_cmd(db: &Path, once: bool, interval: u64) -> Result<()> {
    if once {
        return sysmon::snapshot(db);
    }
    sysmon::loop_forever(db, interval)
}

fn watch_cmd(args: CommonArgs) -> Result<()> {
    let args = normalize_common(args);

    // Отказ старта на подмене. Если страховка запечатана, а собственный бинарь
    // не совпал с манифестом, вести надзор подменённым процессом бессмысленно:
    // он может делать вид, что проверяет, и не проверять ничего.
    if !args.no_self_guard && selfprotect::manifest_path(&args.guard).exists() {
        if let Some(pubkey) = &args.guard_pubkey {
            if selfprotect::manifest_signature_ok(&args.guard, Some(pubkey))? == Some(false) {
                eprintln!(
                    "Отказ старта: подпись манифеста страховки не сходится ({}). \
                     Не доверяйте этой сборке.",
                    args.guard.display()
                );
                std::process::exit(1);
            }
        }

        let exe = env::current_exe().context("не удалось узнать путь собственного бинаря")?;
        let report = selfprotect::check(&args.guard)
            .with_context(|| format!("не проверить страховку {}", args.guard.display()))?;

        let own_damaged = report
            .missing
            .iter()
            .chain(report.changed.iter().map(|(entry, _)| entry))
            .any(|entry| Path::new(&entry.path) == exe.as_path());

        if own_damaged {
            eprintln!(
                "Отказ старта: собственный бинарь {} не совпал с запечатанной страховкой. \
                 Запустите `avi guard --restore` либо перепечатайте манифест через --seal.",
                exe.display()
            );
            std::process::exit(1);
        }

        for entry in &report.missing {
            eprintln!("ПОД НАДЗОРОМ ПРОПАЛО: {}", entry.path);
        }

        for (entry, found) in &report.changed {
            eprintln!("ПОДМЕНА ДО СТАРТА: {} (найдено {})", entry.path, found);
        }
    }

    if args.with_process_monitor {
        let db = args.db.clone();
        let interval = args.process_interval;
        let threshold = args.threshold;

        thread::spawn(move || {
            if let Err(e) = process_monitor_loop(&db, interval, threshold) {
                eprintln!("Ошибка процессного монитора: {e:#}");
            }
        });
    }

    if args.with_sysmon {
        let db = args.db.clone();
        let interval = args.sysmon_interval;

        thread::spawn(move || {
            if let Err(e) = sysmon::loop_forever(&db, interval) {
                eprintln!("Ошибка системного монитора: {e:#}");
            }
        });
    }

    let yara = YaraScanner::new(
        &args.rules,
        !args.no_yara,
        &args.update_pubkey,
        !args.no_builtin_rules,
    );
    let model = load_model(&args.model, &args.update_pubkey)?;

    let args = Arc::new(args);
    let yara = Arc::new(yara);
    let model = Arc::new(model);

    let (tx, rx) = std::sync::mpsc::channel::<(PathBuf, bool)>();

    let mut watcher = notify::recommended_watcher(move |res: notify::Result<NotifyEvent>| {
        if let Ok(event) = res {
            // Исчезновение файла — тоже сигнал: шифровальщик и дроппер сначала
            // убирают то, что уже под надзором, а раньше на Remove не
            // реагировало ничего.
            let removed = matches!(event.kind, EventKind::Remove(_));

            if removed || matches!(event.kind, EventKind::Create(_) | EventKind::Modify(_)) {
                for path in event.paths {
                    let _ = tx.send((path, removed));
                }
            }
        }
    })?;

    for path in &args.paths {
        if !path.exists() {
            eprintln!("Путь не существует: {}", path.display());
            continue;
        }

        if is_excluded(path, &args.exclude) {
            println!("Пропущено исключение: {}", path.display());
            continue;
        }

        watcher.watch(path, RecursiveMode::Recursive)?;
        println!("Real-time мониторинг активен: {}", path.display());
    }

    println!("Ctrl+C для остановки.");

    // Начальный обход — отдельным потоком и уже после регистрации подписок.
    // Порядок был обратным: на корневом диске процесс час перечислял файлы, ещё
    // не слушая ни одного события, и всё это время надзор был слепым. Ошибка
    // обхода больше не роняет watch: один нечитаемый корень не стоит всего
    // реалтайм-покрытия.
    if !args.no_initial {
        let args = Arc::clone(&args);
        let yara = Arc::clone(&yara);
        let model = Arc::clone(&model);

        thread::spawn(move || {
            let conn = match db_init(&args.db) {
                Ok(conn) => conn,
                Err(e) => {
                    eprintln!("Начальный обход не открыл базу: {e:#}");
                    return;
                }
            };

            for path in &args.paths {
                if !path.exists() {
                    eprintln!("Путь не существует: {}", path.display());
                    continue;
                }

                if is_excluded(path, &args.exclude) {
                    println!("Пропущено исключение: {}", path.display());
                    continue;
                }

                println!("Начальное сканирование: {}", path.display());

                match scan_path(&conn, path, &args, &yara, &model, "scan") {
                    Ok(findings) => {
                        print_findings(&findings);

                        if let Some(q) = &args.quarantine {
                            for f in &findings {
                                if let Err(e) = quarantine_file(&conn, f, q) {
                                    eprintln!("Ошибка карантина: {e:#}");
                                }
                            }
                        }
                    }
                    Err(e) => eprintln!("Начальный обход {} не удался: {e:#}", path.display()),
                }
            }

            println!("Начальный обход завершён.");
        });
    }

    // Раньше watch был одним потоком: sleep(500ms) перед проверкой держал
    // очередь, и пачка из сотни файлов означала минуту молчания. Теперь поток
    // событий только раздаёт, а проверяют воркеры.
    let rx = Arc::new(Mutex::new(rx));
    let last_scan = Arc::new(Mutex::new(HashMap::<PathBuf, Instant>::new()));
    let workers = args.workers.clamp(1, 16);

    for _ in 0..workers {
        let args = Arc::clone(&args);
        let yara = Arc::clone(&yara);
        let model = Arc::clone(&model);
        let rx = Arc::clone(&rx);
        let last_scan = Arc::clone(&last_scan);
        let db = args.db.clone();

        thread::spawn(move || {
            let conn = match db_init(&db) {
                Ok(conn) => conn,
                Err(e) => {
                    eprintln!("Воркер не открыл базу: {e:#}");
                    return;
                }
            };

            loop {
                let event = match rx.lock() {
                    Ok(receiver) => receiver.recv(),
                    Err(_) => break,
                };

                let (path, removed) = match event {
                    Ok(event) => event,
                    Err(_) => break,
                };

                if is_excluded(&path, &args.exclude) {
                    continue;
                }

                if let Some(q) = &args.quarantine {
                    if path.starts_with(q) {
                        continue;
                    }
                }

                if removed {
                    let _ = db_insert_event(
                        &conn,
                        &path.display().to_string(),
                        "",
                        "remove",
                        "файл исчез из-под надзора",
                        0.0,
                        "info",
                    );

                    continue;
                }

                if !path.is_file() {
                    continue;
                }

                {
                    let mut seen = match last_scan.lock() {
                        Ok(seen) => seen,
                        Err(_) => continue,
                    };

                    // Окно дедупликации прежнее, но карта больше не растёт до
                    // конца жизни процесса.
                    if seen.len() > 50_000 {
                        seen.retain(|_, t| t.elapsed() < Duration::from_secs(600));
                    }

                    if let Some(t) = seen.get(&path) {
                        if t.elapsed() < Duration::from_secs(2) {
                            continue;
                        }
                    }

                    seen.insert(path.clone(), Instant::now());
                }

                // Окно «пусть допишут файл» нужно, но оно больше не блокирует
                // раздачу остальных событий.
                thread::sleep(Duration::from_millis(250));

                match scan_file(&conn, &path, &args, &yara, &model, "realtime") {
                    Ok(Some(f)) => {
                        println!(
                            "REALTIME ОБНАРУЖЕНО: {}\n  причина: {}\n  score: {:.3}\n  sha256: {}",
                            f.path.display(),
                            f.reason,
                            f.score,
                            f.hash
                        );

                        if let Some(q) = &args.quarantine {
                            if let Err(e) = quarantine_file(&conn, &f, q) {
                                eprintln!("Ошибка карантина: {e:#}");
                            }
                        }
                    }
                    Ok(None) => {}
                    Err(e) => {
                        eprintln!("Не удалось проверить {}: {e:#}", path.display());
                    }
                }
            }
        });
    }

    // Watcher живёт, пока живёт этот поток: держать канал событий больше
    // некому, поэтому цикл ниже не завершается.
    loop {
        thread::sleep(Duration::from_secs(3600));
    }
}

fn guard_cmd(
    guard: &Path,
    files: Vec<PathBuf>,
    seal: bool,
    check: bool,
    restore: bool,
    lock_acl: bool,
    unlock_acl: bool,
    secret: Option<PathBuf>,
    pubkey: Option<PathBuf>,
) -> Result<()> {
    let mut damaged = false;

    if !seal && !check && !restore && !lock_acl && !unlock_acl {
        eprintln!(
            "Ничего не запрошено: добавьте --seal, --check, --restore, --lock-acl или --unlock-acl."
        );
        std::process::exit(2);
    }

    if unlock_acl {
        match selfprotect::unlock_acl(guard) {
            Ok(text) => println!("Отказ снят: {}", text.trim()),
            Err(e) => eprintln!("Не снять отказ: {e:#}"),
        }
    }

    if seal {
        let mut targets = files;

        if targets.is_empty() {
            targets.push(
                env::current_exe().context("не удалось узнать путь собственного бинаря")?,
            );
        }

        let manifest = selfprotect::seal(guard, &targets, secret.as_deref())
            .with_context(|| format!("не запечатать страховку в {}", guard.display()))?;

        println!(
            "Запечатано {} файл(ов) в {}",
            manifest.files.len(),
            guard.display()
        );

        for entry in &manifest.files {
            println!("  {} ← {}", entry.name, entry.path);
        }

        if secret.is_none() {
            println!(
                "ВНИМАНИЕ: манифест не подписан (--secret не передан), подмену манифеста \
                 обнаружить нельзя."
            );
        }
    }

    if let Some(pubkey) = &pubkey {
        match selfprotect::manifest_signature_ok(guard, Some(pubkey))? {
            Some(true) => println!("Подпись манифеста корректна."),
            Some(false) => {
                println!("ВНИМАНИЕ: подпись манифеста не сходится — страховке нельзя верить.");
                damaged = true;
            }
            None => {}
        }
    } else if check || restore {
        println!("ВНИМАНИЕ: открытый ключ не передан, подпись манифеста не проверялась.");
    }

    if restore {
        let report = selfprotect::restore(guard)
            .with_context(|| format!("не восстановить из {}", guard.display()))?;

        for path in &report.restored {
            println!("ВОССТАНОВЛЕНО: {}", path.display());
        }

        for broken in &report.copies_broken {
            println!("СТРАХОВКА НЕГОДНА: {broken}");
        }

        damaged |= !report.copies_broken.is_empty();
    }

    if check || restore {
        let report = selfprotect::check(guard)
            .with_context(|| format!("не проверить страховку {}", guard.display()))?;

        for entry in &report.missing {
            println!("ОТСУТСТВУЕТ: {}", entry.path);
        }

        for (entry, found) in &report.changed {
            println!(
                "ПОДМЕНА: {} (манифест ждал {}, нашёл {})",
                entry.path, entry.sha256, found
            );
        }

        for broken in &report.copies_broken {
            println!("СТРАХОВКА НЕГОДНА: {broken}");
        }

        damaged |= report.damaged();

        if !damaged {
            println!("Страховка цела: {} позиции сверены.", report_position_count(guard)?);
        }
    }

    if lock_acl {
        match selfprotect::lock_acl(guard) {
            Ok(text) => println!("NTFS-отказ поставлен: {}", text.trim()),
            Err(e) => eprintln!("Не поставить отказ: {e:#}"),
        }
    }

    if damaged {
        std::process::exit(1);
    }

    Ok(())
}

fn report_position_count(guard: &Path) -> Result<usize> {
    Ok(selfprotect::load_manifest(guard)?.files.len())
}

fn protect_file_cmd(
    path: &Path,
    db: &Path,
    interval: u64,
    backup: Option<PathBuf>,
    restore: bool,
    read_only: bool,
    immutable: bool,
    quarantine: Option<PathBuf>,
) -> Result<()> {
    let path = fs::canonicalize(path)
        .with_context(|| format!("не удалось канонизировать {}", path.display()))?;

    if !path.is_file() {
        bail!("Это не файл: {}", path.display());
    }

    let backup_path = backup.unwrap_or_else(|| PathBuf::from(format!("{}.miniav.bak", path.display())));

    let conn = db_init(db)?;

    if !backup_path.exists() {
        fs::copy(&path, &backup_path).with_context(|| {
            format!(
                "не удалось создать резервную копию {} -> {}",
                path.display(),
                backup_path.display()
            )
        })?;

        db_insert_event(
            &conn,
            &path.display().to_string(),
            "",
            "protect",
            "backup created",
            0.0,
            "info",
        )?;

        println!("Создана резервная копия: {}", backup_path.display());
    }

    let known_hash = sha256_file(&backup_path)?;

    if read_only {
        set_readonly(&path)?;
        set_readonly(&backup_path)?;
        println!("Файл и резервная копия переведены в read-only.");
    }

    if immutable {
        try_lock_immutable(&path);
        try_lock_immutable(&backup_path);
        println!("Попытка установить атрибут неизменяемости (на Linux нужен чатр +i и права).");
    }

    println!(
        "Защита файла запущена: {}\nРезервная копия: {}\nИнтервал: {} сек",
        path.display(),
        backup_path.display(),
        interval
    );

    loop {
        thread::sleep(Duration::from_secs(interval));

        match sha256_file(&backup_path) {
            Ok(current_backup_hash) => {
                if current_backup_hash != known_hash {
                    db_insert_event(
                        &conn,
                        &backup_path.display().to_string(),
                        &current_backup_hash,
                        "protect",
                        "backup hash changed",
                        1.0,
                        "alert",
                    )?;

                    println!(
                        "ВНИМАНИЕ: резервная копия изменилась: {}",
                        backup_path.display()
                    );
                }
            }
            Err(e) => {
                db_insert_event(
                    &conn,
                    &backup_path.display().to_string(),
                    "",
                    "protect",
                    &format!("backup read error: {e}"),
                    1.0,
                    "error",
                )?;

                eprintln!("Не удалось прочитать резервную копию: {e:#}");
            }
        }

        if !path.exists() {
            db_insert_event(
                &conn,
                &path.display().to_string(),
                "",
                "protect",
                "protected file missing",
                1.0,
                "alert",
            )?;

            println!("ВНИМАНИЕ: защищаемый файл отсутствует: {}", path.display());

            if restore {
                if immutable {
                    try_unlock_immutable(&path);
                }

                match fs::copy(&backup_path, &path) {
                    Ok(_) => {
                        if read_only {
                            let _ = set_readonly(&path);
                        }

                        if immutable {
                            try_lock_immutable(&path);
                        }

                        db_insert_event(
                            &conn,
                            &path.display().to_string(),
                            "",
                            "protect",
                            "file restored from backup",
                            1.0,
                            "restore",
                        )?;

                        println!("Файл восстановлен из резервной копии.");
                    }
                    Err(e) => {
                        db_insert_event(
                            &conn,
                            &path.display().to_string(),
                            "",
                            "protect",
                            &format!("restore failed: {e}"),
                            1.0,
                            "error",
                        )?;

                        eprintln!("Не удалось восстановить файл: {e:#}");
                    }
                }
            }

            continue;
        }

        let current_hash = match sha256_file(&path) {
            Ok(h) => h,
            Err(e) => {
                db_insert_event(
                    &conn,
                    &path.display().to_string(),
                    "",
                    "protect",
                    &format!("file read error: {e}"),
                    1.0,
                    "error",
                )?;

                eprintln!("Не удалось прочитать защищаемый файл: {e:#}");
                continue;
            }
        };

        if current_hash != known_hash {
            db_insert_event(
                &conn,
                &path.display().to_string(),
                &current_hash,
                "protect",
                "protected file changed",
                1.0,
                "alert",
            )?;

            println!("ВНИМАНИЕ: защищаемый файл изменён: {}", path.display());

            if immutable {
                try_unlock_immutable(&path);
            }

            if let Some(q) = &quarantine {
                let finding = Finding {
                    path: path.clone(),
                    hash: current_hash.clone(),
                    reason: "protected file changed".into(),
                    score: 1.0,
                };

                if let Err(e) = quarantine_file(&conn, &finding, q) {
                    eprintln!("Не удалось поместить изменённый файл в карантин: {e:#}");
                }
            }

            if restore {
                match fs::copy(&backup_path, &path) {
                    Ok(_) => {
                        if read_only {
                            let _ = set_readonly(&path);
                        }

                        if immutable {
                            try_lock_immutable(&path);
                        }

                        db_insert_event(
                            &conn,
                            &path.display().to_string(),
                            &known_hash,
                            "protect",
                            "file restored from backup",
                            1.0,
                            "restore",
                        )?;

                        println!("Файл восстановлен из резервной копии.");
                    }
                    Err(e) => {
                        db_insert_event(
                            &conn,
                            &path.display().to_string(),
                            &current_hash,
                            "protect",
                            &format!("restore failed: {e}"),
                            1.0,
                            "error",
                        )?;

                        eprintln!("Не удалось восстановить файл: {e:#}");
                    }
                }
            } else if immutable {
                try_lock_immutable(&path);
            }
        }
    }
}

fn scan_path(
    conn: &Connection,
    path: &Path,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    source: &str,
) -> Result<Vec<Finding>> {
    let mut findings = Vec::new();

    if is_excluded(path, &args.exclude) {
        return Ok(findings);
    }

    if let Some(q) = &args.quarantine {
        if path.starts_with(q) {
            return Ok(findings);
        }
    }

    if path.is_file() {
        if let Some(f) = scan_file(conn, path, args, yara, model, source)? {
            findings.push(f);
        }
        return Ok(findings);
    }

    // Один поток на обходе всего диска — это часы: проверку файла определяет
    // чтение + SHA-256 + правила, а диск их отдаёт тысячями. Пул воркеров уже
    // используется в надзоре, поэтому здесь та же форма: пути раздаёт главный
    // поток, проверяют воркеры, и каждый пишет в своё соединение с базой.
    let workers = args.workers.clamp(1, 16);

    if workers > 1 {
        return scan_tree_parallel(path, args, yara, model, source, workers);
    }

    scan_tree_serial(conn, path, args, yara, model, source)
}

/// Последовательный обход дерева: один поток и соединение заказчика.
fn scan_tree_serial(
    conn: &Connection,
    root: &Path,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    source: &str,
) -> Result<Vec<Finding>> {
    let mut findings = Vec::new();
    let mut it = WalkDir::new(root).follow_links(false).into_iter();

    while let Some(entry) = it.next() {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                eprintln!("Пропущено: {e}");
                continue;
            }
        };

        let p = entry.path();

        if is_excluded(p, &args.exclude) {
            if entry.file_type().is_dir() {
                it.skip_current_dir();
            }
            continue;
        }

        if let Some(q) = &args.quarantine {
            if p.starts_with(q) {
                if entry.file_type().is_dir() {
                    it.skip_current_dir();
                }
                continue;
            }
        }

        if !entry.file_type().is_file() {
            continue;
        }

        match scan_file(conn, p, args, yara, model, source) {
            Ok(Some(f)) => findings.push(f),
            Ok(None) => {}
            Err(e) => {
                eprintln!("Не удалось проверить {}: {e:#}", p.display());
            }
        }
    }

    Ok(findings)
}

/// Параллельный обход дерева тем же пулом, что и реалтайм-надзор.
fn scan_tree_parallel(
    root: &Path,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    source: &str,
    workers: usize,
) -> Result<Vec<Finding>> {
    let (tx, rx) = std::sync::mpsc::channel::<PathBuf>();
    let rx = std::sync::Mutex::new(rx);
    let (ftx, frx) = std::sync::mpsc::channel::<Finding>();

    thread::scope(|scope| {
        let mut pool = Vec::with_capacity(workers);

        for _ in 0..workers {
            let rx = &rx;
            let ftx = &ftx;
            let db = args.db.clone();

            pool.push(scope.spawn(move || {
                let conn = match db_init(&db) {
                    Ok(conn) => conn,
                    Err(e) => {
                        eprintln!("Воркер обхода не открыл базу: {e:#}");
                        return;
                    }
                };

                loop {
                    let path = match rx.lock() {
                        Ok(receiver) => receiver.recv(),
                        Err(_) => break,
                    };

                    let Ok(path) = path else { break };

                    match scan_file(&conn, &path, args, yara, model, source) {
                        Ok(Some(finding)) => {
                            let _ = ftx.send(finding);
                        }
                        Ok(None) => {}
                        Err(e) => {
                            eprintln!("Не удалось проверить {}: {e:#}", path.display());
                        }
                    }
                }
            }));
        }

        // Очередь закрывается вместе с tx: воркеры увидят disconnect и выйдут,
        // иначе scope ждал бы их вечно.
        {
            let mut it = WalkDir::new(root).follow_links(false).into_iter();
            drop(pool);

            while let Some(entry) = it.next() {
                let entry = match entry {
                    Ok(e) => e,
                    Err(e) => {
                        eprintln!("Пропущено: {e}");
                        continue;
                    }
                };

                let p = entry.path();

                if is_excluded(p, &args.exclude) {
                    if entry.file_type().is_dir() {
                        it.skip_current_dir();
                    }
                    continue;
                }

                if let Some(q) = &args.quarantine {
                    if p.starts_with(q) {
                        if entry.file_type().is_dir() {
                            it.skip_current_dir();
                        }
                        continue;
                    }
                }

                if !entry.file_type().is_file() {
                    continue;
                }

                if tx.send(p.to_path_buf()).is_err() {
                    break;
                }
            }
        }

        drop(tx);
    });

    drop(ftx);

    Ok(frx.iter().collect())
}

/// Проверка одного файла.
///
/// `source` — откуда пришла проверка (`scan` обход либо `realtime` надзор):
/// панель модуля по этому полю судит, сработал ли реалтайм или файл поймал
/// только плановый обход.
fn scan_file(
    conn: &Connection,
    path: &Path,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    source: &str,
) -> Result<Option<Finding>> {
    let path_s = path.display().to_string();

    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            db_insert_event(
                conn,
                &path_s,
                "",
                source,
                &format!("metadata error: {e}"),
                0.0,
                "error",
            )?;
            return Ok(None);
        }
    };

    let max_bytes = args.max_mb.saturating_mul(1024 * 1024);

    if meta.len() > max_bytes {
        db_insert_event(conn, &path_s, "", source, "skipped: file too large", 0.0, "skip")?;
        return Ok(None);
    }

    let mtime = mtime_secs(&meta);

    // `--no-cache` — это кнопка «не верь прошлым вердиктам»: после смены правил
    // или обучения модели перечитать всё заново, а не жить старым кэшем.
    if !args.no_cache {
        if let Some((sha, score, status)) = db_get_cached(conn, &path_s, meta.len() as i64, mtime)?
        {
            if score >= args.threshold {
                return Ok(Some(Finding {
                    path: path.to_path_buf(),
                    hash: sha,
                    reason: format!("cached detection, status={status}"),
                    score,
                }));
            } else {
                return Ok(None);
            }
        }
    }

    let Some(analyzed) = analyze_file(path, max_bytes)? else {
        db_insert_event(conn, &path_s, "", source, "skipped: analyze returned none", 0.0, "skip")?;
        return Ok(None);
    };

    let mut finding = detect_analyzed(conn, path, &analyzed, args, yara, model, true, source)?;

    if finding.is_none() && !args.no_archives && analyzed.sample.starts_with(b"PK\x03\x04") {
        if let Some(archive_finding) =
            scan_zip_path(conn, path, &analyzed.hash, args, yara, model, 0)?
        {
            let status = if archive_finding.score >= args.threshold {
                "malicious"
            } else if archive_finding.score >= 0.5 {
                "suspicious"
            } else {
                "clean"
            };

            db_upsert_file(
                conn,
                &path_s,
                analyzed.size as i64,
                analyzed.mtime,
                &analyzed.hash,
                archive_finding.score,
                status,
            )?;

            db_insert_event(
                conn,
                &path_s,
                &analyzed.hash,
                "archive",
                &archive_finding.reason,
                archive_finding.score,
                status,
            )?;

            finding = Some(archive_finding);
        }
    }

    Ok(finding)
}

fn analyze_file(path: &Path, max_bytes: u64) -> Result<Option<Analyzed>> {
    let meta = fs::metadata(path).with_context(|| format!("metadata: {}", path.display()))?;

    if meta.len() > max_bytes {
        return Ok(None);
    }

    let mut file = File::open(path).with_context(|| format!("open: {}", path.display()))?;

    let mut hasher = Sha256::new();
    let mut sample = Vec::new();
    let mut buf = [0u8; 64 * 1024];

    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }

        hasher.update(&buf[..n]);

        if sample.len() < SAMPLE_MAX {
            let take = (SAMPLE_MAX - sample.len()).min(n);
            sample.extend_from_slice(&buf[..take]);
        }
    }

    let hash = hex::encode(hasher.finalize());
    let mtime = mtime_secs(&meta);
    let features = extract_features(path, &sample, meta.len());

    Ok(Some(Analyzed {
        hash,
        sample,
        size: meta.len(),
        mtime,
        features,
    }))
}

fn detect_analyzed(
    conn: &Connection,
    path: &Path,
    analyzed: &Analyzed,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    top_level: bool,
    source: &str,
) -> Result<Option<Finding>> {
    let path_s = path.display().to_string();

    if db_is_allowlisted(conn, &analyzed.hash)? {
        db_upsert_file(
            conn,
            &path_s,
            analyzed.size as i64,
            analyzed.mtime,
            &analyzed.hash,
            0.0,
            "allowed",
        )?;

        db_insert_event(conn, &path_s, &analyzed.hash, "allowlist", "file allowlisted", 0.0, "allow")?;

        return Ok(None);
    }

    if db_is_blacklisted(conn, &analyzed.hash)? {
        db_upsert_file(
            conn,
            &path_s,
            analyzed.size as i64,
            analyzed.mtime,
            &analyzed.hash,
            1.0,
            "malicious",
        )?;

        db_insert_event(conn, &path_s, &analyzed.hash, "blacklist", "SHA-256 blacklisted", 1.0, "alert")?;

        return Ok(Some(Finding {
            path: path.to_path_buf(),
            hash: analyzed.hash.clone(),
            reason: "SHA-256 в чёрном списке".into(),
            score: 1.0,
        }));
    }

    let mut reasons = Vec::new();
    let mut score: f64 = 0.0;

    if analyzed.sample.windows(EICAR.len()).any(|w| w == EICAR) {
        score = 1.0;
        reasons.push("EICAR-TEST".to_string());
    }

    if top_level {
        if let Some((cloud_score, cloud_reason)) = cloud_lookup_sha256(&analyzed.hash, args) {
            if cloud_score > score {
                score = cloud_score;
            }
            reasons.push(cloud_reason);
        }
    }

    if !args.no_yara && analyzed.size <= YARA_MAX_SIZE {
        apply_rule_hits(&mut score, &mut reasons, &yara.scan_path(path));
    }

    if !args.no_pe && analyzed.sample.starts_with(b"MZ") {
        let (pe_score, pe_reasons) = analyze_pe(&analyzed.sample);
        if pe_score > 0.0 {
            score = score.max(pe_score * 0.85);
            reasons.extend(pe_reasons);
        }

        // `--check-signatures` — придирчивый режим: проверять подпись у каждого
        // PE, а не только у кандидата в находки. Дороже (внешний powershell),
        // зато отсутствие подписи видно в отчёте по всем исполняемым файлам.
        if args.check_signatures && !args.no_check_signatures {
            let (signed_ok, sig_score, sig_reasons) = check_file_signature(path, &analyzed.hash);

            if signed_ok {
                record_signed_allow(conn, &path_s, analyzed, &sig_reasons)?;
                return Ok(None);
            }

            score = (score + sig_score).min(1.0);
            reasons.extend(sig_reasons);
        }
    }

    let ml_score = match model {
        Some(m) => model_score(m, &analyzed.features),
        None => heuristic_score(&analyzed.features),
    };

    if ml_score > 0.0 {
        score = score.max(ml_score * 0.90);
        reasons.push(format!("ML/heuristic score {ml_score:.2}"));
    }

    score = score.clamp(0.0, 1.0);

    let status = if score >= args.threshold {
        "malicious"
    } else if score >= 0.50 {
        "suspicious"
    } else {
        "clean"
    };

    // Подписанный издателем бинарник — не находка. По умолчанию проверка
    // берётся только с кандидатов в находки: каталожную подпись считает внешний
    // `powershell` (~2 с на файл), и проверять у каждого PE означало бы умножить
    // полное сканирование диска на эти секунды.
    if score >= args.threshold && !args.no_check_signatures && analyzed.sample.starts_with(b"MZ") {
        let (signed_ok, _, sig_reasons) = check_file_signature(path, &analyzed.hash);

        if signed_ok {
            record_signed_allow(conn, &path_s, analyzed, &sig_reasons)?;
            return Ok(None);
        }
    }

    db_upsert_file(
        conn,
        &path_s,
        analyzed.size as i64,
        analyzed.mtime,
        &analyzed.hash,
        score,
        status,
    )?;

    if top_level || score >= 0.10 {
        db_insert_event(
            conn,
            &path_s,
            &analyzed.hash,
            source,
            &reasons.join("; "),
            score,
            status,
        )?;
    }

    if score >= args.threshold {
        return Ok(Some(Finding {
            path: path.to_path_buf(),
            hash: analyzed.hash.clone(),
            reason: reasons.join("; "),
            score,
        }));
    }

    Ok(None)
}

fn detect_bytes(
    conn: &Connection,
    virtual_path: &Path,
    hash: &str,
    bytes: &[u8],
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    top_level: bool,
) -> Result<Option<Finding>> {
    let virtual_s = virtual_path.display().to_string();

    if db_is_allowlisted(conn, hash)? {
        if top_level {
            db_insert_event(conn, &virtual_s, hash, "allowlist", "bytes allowlisted", 0.0, "allow")?;
        }
        return Ok(None);
    }

    if db_is_blacklisted(conn, hash)? {
        if top_level {
            db_insert_event(conn, &virtual_s, hash, "blacklist", "SHA-256 blacklisted", 1.0, "alert")?;
        }

        return Ok(Some(Finding {
            path: virtual_path.to_path_buf(),
            hash: hash.to_string(),
            reason: "SHA-256 в чёрном списке".into(),
            score: 1.0,
        }));
    }

    let mut reasons = Vec::new();
    let mut score: f64 = 0.0;

    if bytes.windows(EICAR.len()).any(|w| w == EICAR) {
        score = 1.0;
        reasons.push("EICAR-TEST".to_string());
    }

    if top_level {
        if let Some((cloud_score, cloud_reason)) = cloud_lookup_sha256(hash, args) {
            if cloud_score > score {
                score = cloud_score;
            }
            reasons.push(cloud_reason);
        }
    }

    if !args.no_yara && bytes.len() as u64 <= YARA_MAX_SIZE {
        apply_rule_hits(&mut score, &mut reasons, &yara.scan_bytes(bytes));
    }

    if !args.no_pe && bytes.starts_with(b"MZ") {
        let (pe_score, pe_reasons) = analyze_pe(bytes);
        if pe_score > 0.0 {
            score = score.max(pe_score * 0.85);
            reasons.extend(pe_reasons);
        }
    }

    let features = extract_features(virtual_path, bytes, bytes.len() as u64);

    let ml_score = match model {
        Some(m) => model_score(m, &features),
        None => heuristic_score(&features),
    };

    if ml_score > 0.0 {
        score = score.max(ml_score * 0.90);
        reasons.push(format!("ML/heuristic score {ml_score:.2}"));
    }

    score = score.clamp(0.0, 1.0);

    let status = if score >= args.threshold {
        "malicious"
    } else if score >= 0.50 {
        "suspicious"
    } else {
        "clean"
    };

    if top_level || score >= 0.50 {
        db_insert_event(conn, &virtual_s, hash, "scanner", &reasons.join("; "), score, status)?;
    }

    if score >= args.threshold {
        return Ok(Some(Finding {
            path: virtual_path.to_path_buf(),
            hash: hash.to_string(),
            reason: reasons.join("; "),
            score,
        }));
    }

    Ok(None)
}

fn scan_bytes(
    conn: &Connection,
    virtual_path: &str,
    bytes: &[u8],
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    depth: u32,
    top_level: bool,
) -> Result<Option<Finding>> {
    if bytes.is_empty() {
        return Ok(None);
    }

    let hash = sha256_hex(bytes);
    let virtual_pathbuf = PathBuf::from(virtual_path);

    if let Some(f) = detect_bytes(
        conn,
        &virtual_pathbuf,
        &hash,
        bytes,
        args,
        yara,
        model,
        top_level,
    )? {
        return Ok(Some(f));
    }

    if !args.no_archives && depth < MAX_ARCHIVE_DEPTH && bytes.starts_with(b"PK\x03\x04") {
        let cursor = Cursor::new(bytes);
        let archive = ZipArchive::new(cursor)?;

        if let Some(f) = scan_zip_archive(
            conn,
            archive,
            virtual_path,
            &virtual_pathbuf,
            args,
            yara,
            model,
            depth + 1,
            &hash,
        )? {
            return Ok(Some(f));
        }
    }

    Ok(None)
}

fn scan_zip_path(
    conn: &Connection,
    path: &Path,
    archive_hash: &str,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    depth: u32,
) -> Result<Option<Finding>> {
    let file = File::open(path)?;
    let archive = ZipArchive::new(file)?;
    let archive_name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "archive.zip".into());

    scan_zip_archive(
        conn,
        archive,
        &archive_name,
        path,
        args,
        yara,
        model,
        depth,
        archive_hash,
    )
}

fn scan_zip_archive<R: Read + Seek>(
    conn: &Connection,
    mut archive: ZipArchive<R>,
    archive_name: &str,
    top_path: &Path,
    args: &CommonArgs,
    yara: &YaraScanner,
    model: &Option<Model>,
    depth: u32,
    archive_hash: &str,
) -> Result<Option<Finding>> {
    if depth > MAX_ARCHIVE_DEPTH {
        return Ok(None);
    }

    let mut total_uncompressed: u64 = 0;
    let entries = archive.len().min(MAX_ZIP_ENTRIES);

    for i in 0..entries {
        let mut entry = match archive.by_index(i) {
            Ok(e) => e,
            Err(_) => continue,
        };

        if entry.is_dir() {
            continue;
        }

        let name = entry.name().to_string();
        let virtual_name = format!("{archive_name}::{name}");

        let mut limited = Vec::new();
        let mut take = (&mut entry).take(MAX_ZIP_ENTRY);

        if take.read_to_end(&mut limited).is_err() {
            continue;
        }

        total_uncompressed = total_uncompressed.saturating_add(limited.len() as u64);

        if total_uncompressed > MAX_ZIP_TOTAL {
            break;
        }

        if let Some(entry_finding) = scan_bytes(
            conn,
            &virtual_name,
            &limited,
            args,
            yara,
            model,
            depth + 1,
            false,
        )? {
            return Ok(Some(Finding {
                path: top_path.to_path_buf(),
                hash: archive_hash.to_string(),
                reason: format!(
                    "archive entry {} [entry sha256={}]: {}",
                    name, entry_finding.hash, entry_finding.reason
                ),
                score: entry_finding.score.max(0.90),
            }));
        }
    }

    Ok(None)
}

fn cloud_lookup_sha256(sha256: &str, args: &CommonArgs) -> Option<(f64, String)> {
    let key = args.cloud_api_key.as_deref()?;

    let url = if args.cloud_url_base.ends_with('/') {
        format!("{}{}", args.cloud_url_base, sha256)
    } else {
        format!("{}/{}", args.cloud_url_base, sha256)
    };

    let resp = ureq::get(&url)
        .set("User-Agent", "miniav-edr-prototype")
        .set("x-apikey", key)
        .timeout(Duration::from_secs(8))
        .call()
        .ok()?;

    let json: serde_json::Value = resp.into_json().ok()?;

    let stats = &json["data"]["attributes"]["last_analysis_stats"];

    let malicious = stats["malicious"].as_u64().unwrap_or(0);
    let suspicious = stats["suspicious"].as_u64().unwrap_or(0);

    if malicious > 3 {
        Some((1.0, format!("cloud malicious={malicious}")))
    } else if malicious > 0 || suspicious > 2 {
        Some((0.60, format!("cloud malicious={malicious}, suspicious={suspicious}")))
    } else {
        Some((0.0, "cloud clean/unknown".into()))
    }
}

/// Проверка подлинности кода средствами самой Windows.
///
/// `Some(0)` — подпись корректна, `Some(code)` — код отказа, `None` — проверить
/// нельзя (файл не открылся). Отзыв (CRL/OCSP) не запрашивается: сетевой обход
/// добавляет секунды на файл и ломает реалтайм-надзор, а поддельный сертификат
/// отозванного издателя по-прежнему ловится эвристиками и правилами.
#[cfg(target_os = "windows")]
fn wintrust_signature_status(path: &Path) -> Option<i32> {
    use winapi::um::wintrust::{
        WTD_DISABLE_MD2_MD4, WTD_REVOCATION_CHECK_NONE, WTD_STATEACTION_VERIFY,
    };

    // SIP подаём ordinary-форму: канонализованный корень надзора даёт `\\?\C:\…`,
    // а на такой пути WinVerifyTrust отвечает TRUST_E_NOSIGNATURE даже для
    // подписанного файла.
    wintrust_status_with(
        &signature_path(path),
        WTD_STATEACTION_VERIFY,
        WTD_DISABLE_MD2_MD4 | WTD_REVOCATION_CHECK_NONE,
    )
}

/// Тот же вызов с выбранной парой флагов — нужно, чтобы подобрать корректную
/// комбинацию не гаданием, а матрицей статусов (`sig-check`).
#[cfg(target_os = "windows")]
fn wintrust_status_with(path: &Path, state_action: u32, prov_flags: u32) -> Option<i32> {
    use std::mem::size_of;
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::{null, null_mut};
    use winapi::ctypes::c_void;
    use winapi::shared::guiddef::GUID;
    use winapi::um::wintrust::{
        WinVerifyTrust, WINTRUST_DATA, WINTRUST_FILE_INFO, WTD_CHOICE_FILE, WTD_REVOKE_NONE,
        WTD_STATEACTION_CLOSE, WTD_STATEACTION_VERIFY, WTD_UI_NONE,
    };

    if !path.exists() {
        return None;
    }

    // WINTRUST_ACTION_GENERIC_VERIFY_V2 {00AAC56B-CD44-11d0-8CC2-006B992C3088}
    let mut action = GUID {
        Data1: 0x00AA_C56B,
        Data2: 0xCD44,
        Data3: 0x11D0,
        Data4: [0x8C, 0xC2, 0x00, 0x6B, 0x99, 0x2C, 0x30, 0x88],
    };

    let mut wide: Vec<u16> = path.as_os_str().encode_wide().collect();
    wide.push(0);

    let mut file_info = WINTRUST_FILE_INFO {
        cbStruct: size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: wide.as_ptr(),
        hFile: null_mut(),
        pgKnownSubject: null(),
    };

    let mut data = WINTRUST_DATA {
        cbStruct: size_of::<WINTRUST_DATA>() as u32,
        pPolicyCallbackData: null_mut(),
        pSIPClientData: null_mut(),
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_NONE,
        dwUnionChoice: WTD_CHOICE_FILE,
        // winapi делает объединение приватным кортеж-структуром: значение
        // поля ставится через pFile_mut(), поэтому здесь только нули.
        u: unsafe { std::mem::zeroed() },
        dwStateAction: state_action,
        hWVTStateData: null_mut(),
        pwszURLReference: null_mut(),
        dwProvFlags: prov_flags,
        dwUIContext: 0,
        pSignatureSettings: null_mut(),
    };

    unsafe { *data.u.pFile_mut() = &mut file_info };

    let status = unsafe {
        WinVerifyTrust(
            null_mut(),
            &mut action as *mut GUID,
            &mut data as *mut WINTRUST_DATA as *mut c_void,
        )
    };

    // Состояние VERIFY выделяется внутри WinVerifyTrust и обязано быть закрыто,
    // иначе надзор над всем диском утёк бы памятью на каждом файле.
    if state_action == WTD_STATEACTION_VERIFY {
        unsafe {
            data.dwStateAction = WTD_STATEACTION_CLOSE;
            data.dwUnionChoice = 0;
            *data.u.pFile_mut() = null_mut();
            WinVerifyTrust(
                null_mut(),
                &mut action as *mut GUID,
                &mut data as *mut WINTRUST_DATA as *mut c_void,
            );
        }
    }

    Some(status)
}

/// Матрица флагов для диагностики: какая комбинация действительно работает на
/// этой машине и влияет ли форма пути (`\\?\`) на вердикт SIP.
#[cfg(target_os = "windows")]
fn wintrust_probe(path: &Path) {
    use winapi::um::wintrust::{
        WTD_DISABLE_MD2_MD4, WTD_REVOCATION_CHECK_NONE, WTD_SAFER_FLAG, WTD_STATEACTION_IGNORE,
        WTD_STATEACTION_VERIFY,
    };

    let combos: [(&str, u32, u32); 5] = [
        ("VERIFY prov=0", WTD_STATEACTION_VERIFY, 0),
        (
            "VERIFY prov=DISABLE_MD2_MD4|REVOCATION_NONE",
            WTD_STATEACTION_VERIFY,
            WTD_DISABLE_MD2_MD4 | WTD_REVOCATION_CHECK_NONE,
        ),
        ("VERIFY prov=SAFER", WTD_STATEACTION_VERIFY, WTD_SAFER_FLAG),
        (
            "IGNORE prov=0",
            WTD_STATEACTION_IGNORE,
            0,
        ),
        (
            "IGNORE prov=DISABLE_MD2_MD4",
            WTD_STATEACTION_IGNORE,
            WTD_DISABLE_MD2_MD4,
        ),
    ];

    for (label, state, prov) in combos {
        let via_ordinary = wintrust_status_with(path, state, prov);
        let extended = wintrust_status_with(&fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf()), state, prov);
        println!(
            "  {label}: обычный={via_ordinary:?} (0x{:08x}) расширенный={extended:?}",
            via_ordinary.unwrap_or(0) as u32
        );
    }
}

/// Обычная форма пути для проверок подписи: расширенный путь канонализованного
/// корня (`\\?\C:\a`) Windows-проверки подписи не понимают и отвечают «подписи
/// нет», поэтому подаём ordinary-форму.
#[cfg(target_os = "windows")]
fn signature_path(path: &Path) -> PathBuf {
    let s = path.as_os_str().to_string_lossy();

    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        return std::path::PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = s.strip_prefix(r"\\?\") {
        return std::path::PathBuf::from(rest);
    }

    path.to_path_buf()
}

// `wintrust.lib` в линковке: winapi объявляет символы, но не подключает библиотеку.
#[cfg(windows)]
#[link(name = "wintrust")]
extern "system" {}

/// Оракул для каталожных подписей: `Get-AuthenticodeSignature` из PowerShell.
///
/// `Some(true)` — статус `Valid` (встроенная или каталожная подпись),
/// `Some(false)` — подписи нет либо она некорректна, `None` — проверка не
/// состоялась (powershell не запустился или ничего не ответил): такой исход
/// никогда не кэшируется, иначе временный отказ инструмента остался бы в кеше
/// как «подписи нет» и файл считался бы находкой до самой перезагрузки.
///
/// Медленно (замер на этой машине: ~0,7-2,1 с на процесс), поэтому вызывается
/// только для кандидата в находки, у которого быстрый нативный путь не сработал,
/// и только с кешем по SHA-256.
#[cfg(target_os = "windows")]
fn powershell_signature_valid(path: &Path) -> Option<bool> {
    use std::os::windows::process::CommandExt;

    // CREATE_NO_WINDOW: сайдкар живёт без консоли, и на каждой проверке мелькало
    // бы окно powershell.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    let literal = signature_path(path)
        .to_string_lossy()
        .replace('\'', "''");
    let command = format!("(Get-AuthenticodeSignature -LiteralPath '{literal}').Status");

    let output = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &command])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if stdout.is_empty() {
        return None;
    }

    Some(stdout == "Valid")
}

/// Диагностика: что именно отвечает WinVerifyTrust и какой из этого вердикт.
fn sig_check_cmd(path: &Path) -> Result<()> {
    println!("файл: {}", path.display());

    #[cfg(target_os = "windows")]
    {
        println!(
            "размеры: WINTRUST_DATA={}, WINTRUST_FILE_INFO={}",
            std::mem::size_of::<winapi::um::wintrust::WINTRUST_DATA>(),
            std::mem::size_of::<winapi::um::wintrust::WINTRUST_FILE_INFO>(),
        );
        wintrust_probe(path);
        println!("WinVerifyTrust: {:?}", wintrust_signature_status(path));
    }

    let (valid, score, reasons) = check_file_signature(path, "diag");
    println!("итог: подпись_корректна={valid} поправка={score} причины={reasons:?}");

    Ok(())
}

/// Проверка подписи: (корректна ли, поправка к счёту, причины).
///
/// Цена зависит от пути: встроенная подпись стоит единицы миллисекунд (нативный
/// `WinVerifyTrust`), каталожная — внешний PowerShell ~0,7-2,1 с. Поэтому вердикт
/// кэшируется по SHA-256: у одинакового содержимого одинаковая подпись, а без
/// кэша полное сканирование диска платит секундами на каждый неподписанный PE.
// Хвостовой возврат нужен только там, где нет WinVerifyTrust; на Windows все
// ветки расходятся, и компилятор считает хвост недостижимым.
#[cfg_attr(target_os = "windows", allow(unreachable_code))]
fn check_file_signature(path: &Path, hash: &str) -> (bool, f64, Vec<String>) {
    if let Some(valid) = signature_cache_get(hash) {
        return if valid {
            (true, 0.0, vec!["Authenticode signature valid".into()])
        } else {
            (false, 0.05, vec!["Authenticode signature invalid/unknown".into()])
        };
    }

    #[cfg(target_os = "windows")]
    {
        // 1) Быстрый нативный путь: встроенная подпись Authenticode. WinVerifyTrust
        // стоит единицы миллисекунд против ~0,7-2,1 с на внешний powershell.
        if let Some(0) = wintrust_signature_status(path) {
            signature_cache_put(hash, true);
            return (true, 0.0, vec!["Authenticode signature valid".into()]);
        }

        // 2) Оракул для каталожной подписи. Замер на этой машине: kernel32.dll,
        // urlmon.dll и notepad.exe — type=Catalog, встроенной подписи у них нет,
        // и WinVerifyTrust по пути отвечает TRUST_E_NOSIGNATURE (0x800b0001).
        // Без этой ветки каждая системная DLL стала бы «неподписанной», то есть
        // находкой — регресс, который дорого ловить глазами.
        if let Some(valid) = powershell_signature_valid(path) {
            signature_cache_put(hash, valid);

            return if valid {
                (true, 0.0, vec!["Authenticode/catalog signature valid".into()])
            } else {
                (false, 0.05, vec!["Authenticode signature invalid/unknown".into()])
            };
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(output) = Command::new("osslsigncode").arg("verify").arg(path).output() {
            let valid = output.status.success();
            signature_cache_put(hash, valid);

            if valid {
                return (true, 0.0, vec!["osslsigncode signature valid".into()]);
            }

            return (false, 0.05, vec!["osslsigncode signature invalid/unknown".into()]);
        }
    }

    // Проверка не состоялась — не кэшируем: временный отказ инструмента не
    // должен остаться в кеше как «подписи нет».
    (false, 0.0, vec!["signature check unavailable".into()])
}

static SIG_CACHE: OnceLock<Mutex<HashMap<String, bool>>> = OnceLock::new();

/// Файл с проверенной подписью издателя закрывается как «allowed». Запись в
/// кеш вердиктов обязательна: иначе следующая проверка того же содержимого
/// снова заплатит внешним powershell.
fn record_signed_allow(
    conn: &Connection,
    path_s: &str,
    analyzed: &Analyzed,
    reasons: &[String],
) -> Result<()> {
    db_upsert_file(
        conn,
        path_s,
        analyzed.size as i64,
        analyzed.mtime,
        &analyzed.hash,
        0.0,
        "allowed",
    )?;

    db_insert_event(
        conn,
        path_s,
        &analyzed.hash,
        "signature",
        &reasons.join("; "),
        0.0,
        "allow",
    )
}

fn signature_cache_get(hash: &str) -> Option<bool> {
    SIG_CACHE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .ok()?
        .get(hash)
        .copied()
}

fn signature_cache_put(hash: &str, valid: bool) {
    if let Ok(mut cache) = SIG_CACHE
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
    {
        if cache.len() >= 200_000 {
            cache.clear();
        }

        cache.insert(hash.to_string(), valid);
    }
}

fn extract_features(path: &Path, sample: &[u8], size: u64) -> Vec<f64> {
    let size_norm =
        ((size as f64 + 1.0).ln() / ((100.0f64 * 1024.0 * 1024.0) + 1.0).ln()).clamp(0.0, 1.0);

    let ent = entropy(sample);
    let printable = printable_ratio(sample);
    let ext = suspicious_extension(path);
    let dbl = double_extension(path);
    let pe = if sample.starts_with(b"MZ") { 1.0 } else { 0.0 };

    let script = if sample.starts_with(b"#!")
        || contains_bytes(sample, b"<?php")
        || contains_bytes(sample, b"<script")
    {
        1.0
    } else {
        0.0
    };

    let nulls = null_ratio(sample);

    vec![size_norm, ent, printable, ext, dbl, pe, script, nulls]
}

fn contains_bytes(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

fn entropy(data: &[u8]) -> f64 {
    if data.is_empty() {
        return 0.0;
    }

    let mut counts = [0u64; 256];
    for &b in data {
        counts[b as usize] += 1;
    }

    let len = data.len() as f64;
    let mut h = 0.0;

    for &c in counts.iter() {
        if c == 0 {
            continue;
        }

        let p = c as f64 / len;
        h -= p * p.log2();
    }

    (h / 8.0).clamp(0.0, 1.0)
}

fn printable_ratio(data: &[u8]) -> f64 {
    if data.is_empty() {
        return 1.0;
    }

    let count = data
        .iter()
        .filter(|&&b| (b >= 32 && b < 127) || matches!(b, b'\n' | b'\r' | b'\t'))
        .count();

    count as f64 / data.len() as f64
}

fn null_ratio(data: &[u8]) -> f64 {
    if data.is_empty() {
        return 0.0;
    }

    data.iter().filter(|&&b| b == 0).count() as f64 / data.len() as f64
}

fn suspicious_extension(path: &Path) -> f64 {
    path.extension()
        .and_then(|s| s.to_str())
        .map(|e| {
            if is_dangerous_ext(&e.to_lowercase()) {
                1.0
            } else {
                0.0
            }
        })
        .unwrap_or(0.0)
}

fn double_extension(path: &Path) -> f64 {
    let Some(name) = path.file_name().and_then(|s| s.to_str()) else {
        return 0.0;
    };

    let lower = name.to_lowercase();
    let parts: Vec<&str> = lower.split('.').collect();

    if parts.len() >= 3 {
        let outer = parts[parts.len() - 1];
        let inner = parts[parts.len() - 2];

        let benign_inner = [
            "pdf", "txt", "jpg", "jpeg", "png", "gif", "doc", "docx", "xls", "xlsx", "mp3", "mp4",
        ];

        if benign_inner.contains(&inner) && is_dangerous_ext(outer) {
            return 1.0;
        }
    }

    0.0
}

fn is_dangerous_ext(ext: &str) -> bool {
    matches!(
        ext,
        "exe" | "dll"
            | "sys"
            | "scr"
            | "bat"
            | "cmd"
            | "ps1"
            | "vbs"
            | "js"
            | "jse"
            | "hta"
            | "jar"
            | "msi"
            | "com"
            | "pif"
            | "lnk"
            | "docm"
            | "xlsm"
            | "pptm"
    )
}

fn heuristic_score(f: &[f64]) -> f64 {
    if f.len() < FEATURE_COUNT {
        return 0.0;
    }

    let entropy = f[1];
    let suspicious_ext = f[3];
    let double_ext = f[4];
    let pe = f[5];
    let script = f[6];
    let nulls = f[7];

    let mut score: f64 = 0.0;

    if pe > 0.5 {
        score += 0.25;
    }
    if suspicious_ext > 0.5 {
        score += 0.25;
    }
    if double_ext > 0.5 {
        score += 0.20;
    }
    if script > 0.5 {
        score += 0.10;
    }
    if entropy > 0.85 {
        score += 0.20;
    }
    if nulls > 0.40 {
        score += 0.10;
    }

    score.min(1.0)
}

fn analyze_pe(bytes: &[u8]) -> (f64, Vec<String>) {
    let mut score: f64 = 0.0;
    let mut reasons = Vec::new();

    let pe = match PE::parse(bytes) {
        Ok(pe) => pe,
        Err(_) => return (0.0, reasons),
    };

    let characteristics = pe.header.coff_header.characteristics;

    // IMAGE_FILE_EXECUTABLE_IMAGES = 0x0002
    if characteristics & 0x0002 == 0 {
        score += 0.20;
        reasons.push("PE without EXECUTABLE_IMAGES".into());
    }

    // IMAGE_FILE_DLL = 0x2000
    if characteristics & 0x2000 != 0 {
        reasons.push("PE is DLL".into());
    }

    if pe.sections.is_empty() {
        score += 0.40;
        reasons.push("PE has no sections".into());
    }

    const IMAGE_SCN_MEM_EXECUTE: u32 = 0x20000000;
    const IMAGE_SCN_MEM_WRITE: u32 = 0x80000000;

    for section in &pe.sections {
        let name_bytes = section.name;
        let end = name_bytes
            .iter()
            .position(|&b| b == 0)
            .unwrap_or(name_bytes.len());

        let name = String::from_utf8_lossy(&name_bytes[..end]).to_string();

        let writable = section.characteristics & IMAGE_SCN_MEM_WRITE != 0;
        let executable = section.characteristics & IMAGE_SCN_MEM_EXECUTE != 0;

        if writable && executable {
            score += 0.30;
            reasons.push(format!("section {name} W+X"));
        }

        let suspicious_names = [
            ".vmp0", ".vmp1", ".upx0", ".upx1", ".aspack", ".themida", ".enigma1", ".enigma2",
        ];

        if suspicious_names.contains(&name.as_str()) {
            score += 0.25;
            reasons.push(format!("section {name} looks packer-like"));
        }

        let start = section.pointer_to_raw_data as usize;
        let end = start.saturating_add(section.size_of_raw_data as usize);

        if end <= bytes.len() {
            let section_bytes = &bytes[start..end];
            let section_entropy = entropy(section_bytes);

            if section_entropy > 0.90 {
                score += 0.15;
                reasons.push(format!("section {name} entropy {section_entropy:.2}"));
            }
        }
    }

    (score.min(1.0), reasons)
}

fn model_score(model: &Model, features: &[f64]) -> f64 {
    if model.weights.len() != features.len() || model.feature_count != features.len() {
        return heuristic_score(features);
    }

    let z = model.bias
        + model
            .weights
            .iter()
            .zip(features.iter())
            .map(|(w, x)| w * x)
            .sum::<f64>();

    sigmoid(z)
}

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

fn load_model(path: &Path, pubkey: &Option<PathBuf>) -> Result<Option<Model>> {
    if !path.exists() {
        return Ok(None);
    }

    if let Some(pubkey_path) = pubkey {
        if !verify_file(pubkey_path, path)? {
            bail!("Модель не прошла проверку подписи: {}", path.display());
        }
    }

    let text = fs::read_to_string(path)
        .with_context(|| format!("reading model: {}", path.display()))?;

    match serde_json::from_str(&text) {
        Ok(model) => Ok(Some(model)),
        Err(e) => {
            eprintln!("Не удалось прочитать модель, использую эвристики: {e}");
            Ok(None)
        }
    }
}

fn print_findings(findings: &[Finding]) {
    for f in findings {
        println!(
            "ОБНАРУЖЕНО: {}\n  причина: {}\n  score: {:.3}\n  sha256: {}\n",
            f.path.display(),
            f.reason,
            f.score,
            f.hash
        );
    }

    println!("Всего находок: {}", findings.len());
}

fn quarantine_file(conn: &Connection, f: &Finding, dir: &Path) -> Result<()> {
    fs::create_dir_all(dir)?;

    let ts = now_ms();
    let name = f
        .path
        .file_name()
        .unwrap_or(OsStr::new("file"))
        .to_string_lossy();

    let short_hash = &f.hash[..12];
    let dest = dir.join(format!("{ts}_{name}_{short_hash}"));
    let meta = dir.join(format!("{ts}_{name}_{short_hash}.meta.txt"));

    fs::rename(&f.path, &dest).with_context(|| {
        format!(
            "rename {} -> {} (карантин должен быть на том же томе)",
            f.path.display(),
            dest.display()
        )
    })?;

    fs::write(
        &meta,
        format!(
            "original={}\nsha256={}\nreason={}\nscore={}\n",
            f.path.display(),
            f.hash,
            f.reason,
            f.score
        ),
    )?;

    db_insert_quarantine(
        conn,
        &f.path.display().to_string(),
        &dest.display().to_string(),
        &f.hash,
        &f.reason,
        f.score,
    )?;

    println!("  КАРАНТИН -> {}", dest.display());

    Ok(())
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

fn mtime_secs(meta: &fs::Metadata) -> i64 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("open: {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];

    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }

    Ok(hex::encode(hasher.finalize()))
}

fn set_readonly(path: &Path) -> Result<()> {
    let meta = fs::metadata(path)?;
    let mut perms = meta.permissions();
    perms.set_readonly(true);
    fs::set_permissions(path, perms)?;
    Ok(())
}

#[cfg(unix)]
fn try_lock_immutable(path: &Path) {
    let _ = Command::new("chattr").arg("+i").arg(path).output();
}

#[cfg(unix)]
fn try_unlock_immutable(path: &Path) {
    let _ = Command::new("chattr").arg("-i").arg(path).output();
}

#[cfg(not(unix))]
fn try_lock_immutable(path: &Path) {
    // «Неизменяемость» пользовательского режима на Windows — это NTFS-отказ на
    // запись, смену DACL и удаление плюс атрибут read-only. Владелец файла всё
    // ещё может это снять, поэтому защита задерживает, а не делает невозможным.
    if let Err(e) = selfprotect::deny_write_acl(path) {
        eprintln!("Не удалось поставить отказ на {}: {e:#}", path.display());
    }

    let _ = set_readonly(path);
}

#[cfg(not(unix))]
fn try_unlock_immutable(path: &Path) {
    if let Err(e) = selfprotect::clear_deny_acl(path) {
        eprintln!("Не удалось снять отказ на {}: {e:#}", path.display());
    }

    if let Ok(meta) = fs::metadata(path) {
        let mut perms = meta.permissions();
        perms.set_readonly(false);
        let _ = fs::set_permissions(path, perms);
    }
}

impl YaraScanner {
    fn new(rules_dir: &Path, enabled: bool, pubkey: &Option<PathBuf>, builtin: bool) -> Self {
        if !enabled {
            return Self {
                enabled: false,
                engine: rules::Engine::empty(),
                external: Vec::new(),
                use_external: false,
            };
        }

        let mut engine = rules::Engine::empty();

        if builtin {
            engine.add_source("встроенные правила", include_str!("../rules_builtin.yar"));
        }

        let mut rule_files = Vec::new();

        if enabled && rules_dir.exists() {
            if let Ok(entries) = fs::read_dir(rules_dir) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if path.extension().and_then(|s| s.to_str()) == Some("yar")
                        || path.extension().and_then(|s| s.to_str()) == Some("yara")
                    {
                        rule_files.push(path);
                    }
                }
            }
        }

        let mut valid_rules = Vec::new();

        for rule in rule_files {
            if let Some(pubkey_path) = pubkey {
                match verify_file(pubkey_path, &rule) {
                    Ok(true) => valid_rules.push(rule),
                    Ok(false) => {
                        eprintln!(
                            "YARA правило отклонено, нет корректной подписи: {}",
                            rule.display()
                        );
                    }
                    Err(e) => {
                        eprintln!(
                            "Ошибка проверки подписи YARA правила {}: {e:#}",
                            rule.display()
                        );
                    }
                }
            } else {
                valid_rules.push(rule);
            }
        }

        for rule in &valid_rules {
            if let Err(e) = engine.add_file(rule) {
                eprintln!("{e:#}");
            }
        }

        let use_external = Command::new("yara")
            .arg("--version")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);

        if !engine.unsupported.is_empty() {
            eprintln!(
                "Не разобрано конструкций в правилах: {} (первые {}: {})",
                engine.unsupported.len(),
                engine.unsupported.len().min(5),
                engine.unsupported.iter().take(5).cloned().collect::<Vec<_>>().join("; ")
            );
        }

        println!(
            "Сигнатурный движок: {} правил в процессе, файлов из {}={}, внешний yara={}",
            engine.rule_count(),
            rules_dir.display(),
            valid_rules.len(),
            if use_external { "найден" } else { "не найден" }
        );

        Self {
            enabled: true,
            engine,
            external: valid_rules,
            use_external,
        }
    }

    /// Только внешний `yara`: синтаксис шире, но бинарь должен быть установлен.
    fn external_hits(&self, path: &Path) -> Vec<RuleHit> {
        let mut hits = Vec::new();

        for rule in &self.external {
            let Ok(output) = Command::new("yara").arg(rule).arg(path).output() else {
                continue;
            };

            for line in String::from_utf8_lossy(&output.stdout).lines() {
                let name = line.split_whitespace().next().unwrap_or("").trim();
                if name.is_empty() {
                    continue;
                }
                hits.push(RuleHit {
                    rule: name.to_string(),
                    score: 0.75,
                    description: String::new(),
                    matched: Vec::new(),
                });
            }
        }

        hits
    }

    fn scan_hits(&self, bytes: &[u8]) -> Vec<RuleHit> {
        self.engine.scan(bytes)
    }

    fn scan_path(&self, path: &Path) -> Vec<RuleHit> {
        if !self.enabled {
            return Vec::new();
        }

        let mut hits = if let Some(bytes) = read_bounded(path, YARA_MAX_SIZE) {
            self.scan_hits(&bytes)
        } else {
            Vec::new()
        };

        if self.use_external {
            hits.extend(self.external_hits(path));
        }

        hits.sort_by(|a, b| a.rule.cmp(&b.rule));
        hits.dedup_by(|a, b| a.rule == b.rule);
        hits
    }

    fn scan_bytes(&self, bytes: &[u8]) -> Vec<RuleHit> {
        if !self.enabled {
            return Vec::new();
        }

        let mut hits = self.scan_hits(bytes);

        if self.use_external {
            let tmp = std::env::temp_dir().join(format!(
                "miniav-yara-{}-{}.tmp",
                std::process::id(),
                TMP_COUNTER.fetch_add(1, Ordering::Relaxed)
            ));

            if fs::write(&tmp, bytes).is_ok() {
                hits.extend(self.external_hits(&tmp));
                let _ = fs::remove_file(&tmp);
            }
        }

        hits.sort_by(|a, b| a.rule.cmp(&b.rule));
        hits.dedup_by(|a, b| a.rule == b.rule);
        hits
    }
}

/// Читает файл целиком, но не больше `max` байт: правила ищут по всему
/// содержимому, а выделять гигабайт под архив-контейнер нельзя.
fn read_bounded(path: &Path, max: u64) -> Option<Vec<u8>> {
    let meta = fs::metadata(path).ok()?;
    if !meta.is_file() || meta.len() > max {
        return None;
    }
    fs::read(path).ok()
}

/// Срабатывания правил в общий счёт: берётся максимальная оценка среди
/// угодивших правил, а не одна константа, иначе правило с `score = 0.6` и
/// правило с `score = 0.95` давали бы одинаковый вердикт.
fn apply_rule_hits(score: &mut f64, reasons: &mut Vec<String>, hits: &[RuleHit]) {
    if hits.is_empty() {
        return;
    }

    let best = hits.iter().map(|h| h.score).fold(0.0f64, f64::max);
    if best > *score {
        *score = best;
    }

    let names: Vec<String> = hits
        .iter()
        .map(|h| {
            let what = if h.matched.is_empty() {
                String::new()
            } else {
                format!(" [{}]", h.matched.join("+"))
            };
            if h.description.is_empty() {
                format!("{}{}", h.rule, what)
            } else {
                format!("{} — {}{}", h.rule, h.description, what)
            }
        })
        .collect();

    reasons.push(format!("правила: {}", names.join(", ")));
}

fn process_monitor_loop(db: &Path, interval: u64, threshold: f64) -> Result<()> {
    let conn = db_init(db)?;
    let mut sys = System::new();
    let mut seen: HashMap<u32, ()> = HashMap::new();

    // Всё, что крутилось до старта надзора, — не «новые» процессы: без этого
    // шага первый же тик пишет алерт по каждому системному бинарнику.
    sys.refresh_processes();

    for pid in sys.processes().keys() {
        seen.insert(pid.as_u32(), ());
    }

    println!(
        "Процессный мониторинг запущен (опрос раз в {interval} с, за старые принято {} уже идущих процесса).",
        seen.len()
    );

    loop {
        sys.refresh_processes();

        // Номер процесса переиспользуется: «виденные» обязаны жить ровно
        // столько, сколько жив процесс, иначе программа, получившая освобождённый
        // pid, не будет проверена никогда.
        let alive: HashSet<u32> = sys.processes().keys().map(|p| p.as_u32()).collect();
        seen.retain(|pid, _| alive.contains(pid));

        for (pid, process) in sys.processes() {
            let pid_u32 = pid.as_u32();

            if seen.contains_key(&pid_u32) {
                continue;
            }

            seen.insert(pid_u32, ());

            let cmdline = process.cmd().join(" ");
            let exe = process
                .exe()
                .map(|p| p.display().to_string())
                .unwrap_or_default();

            let (score, reasons) = behavior_score(&cmdline, &exe);

            if score >= 0.10 {
                let action = if score >= threshold { "alert" } else { "info" };

                db_insert_event(
                    &conn,
                    &format!("pid:{pid_u32} {exe}"),
                    "",
                    "process",
                    &format!("cmd={} | reasons={}", cmdline, reasons.join("; ")),
                    score,
                    action,
                )?;

                println!(
                    "PROCESS ALERT pid={} score={:.2} exe={} cmd={}",
                    pid_u32,
                    score,
                    exe,
                    cmdline
                );
            }
        }

        thread::sleep(Duration::from_secs(interval));
    }
}

fn behavior_score(cmdline: &str, exe: &str) -> (f64, Vec<String>) {
    let mut score: f64 = 0.0;
    let mut reasons = Vec::new();

    let cmd = cmdline.to_lowercase();
    let exe = exe.to_lowercase();

    if cmd.contains("powershell") && (cmd.contains("-enc") || cmd.contains("encodedcommand")) {
        score += 0.80;
        reasons.push("PowerShell encoded command".to_string());
    }

    if cmd.contains("vssadmin") && cmd.contains("delete shadows") {
        score += 0.95;
        reasons.push("vssadmin delete shadows".to_string());
    }

    if cmd.contains("wmic") && cmd.contains("shadowcopy") && cmd.contains("delete") {
        score += 0.95;
        reasons.push("wmic shadowcopy delete".to_string());
    }

    if cmd.contains("reg add") && cmd.contains("currentversion\\run") {
        score += 0.70;
        reasons.push("registry autorun modification".to_string());
    }

    if cmd.contains("rundll32") && cmd.contains("javascript") {
        score += 0.80;
        reasons.push("rundll32 javascript suspicious".to_string());
    }

    if exe.contains("temp") || exe.contains("appdata") {
        if cmd.contains("cmd") || cmd.contains("powershell") {
            score += 0.40;
            reasons.push("executable from temp/appdata with shell".to_string());
        }
    }

    if cmd.contains("schtasks") && cmd.contains("/create") {
        score += 0.60;
        reasons.push("scheduled task creation".to_string());
    }

    if cmd.contains("net user") && cmd.contains("/add") {
        score += 0.80;
        reasons.push("local user creation".to_string());
    }

    (score.min(1.0), reasons)
}

fn db_init(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)
        .with_context(|| format!("не удалось открыть SQLite DB: {}", path.display()))?;

    // События в watch разбирают несколько потоков через одну базу: без
    // busy_timeout и WAL первая же конкуренция за блокировку отдаёт SQLITE_BUSY.
    conn.busy_timeout(Duration::from_secs(5))?;
    let _ = conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;");

    conn.execute_batch(DB_SCHEMA)?;

    // Кэш обхода живёт в таблице `files`, и в уже существующей базе колонки
    // `fp` не было: CREATE TABLE её не добавит, нужен явный миграционный шаг.
    let has_fp: bool = conn
        .prepare("SELECT 1 FROM pragma_table_info('files') WHERE name = 'fp'")?
        .exists([])?;
    if !has_fp {
        conn.execute("ALTER TABLE files ADD COLUMN fp INTEGER NOT NULL DEFAULT 0", [])?;
    }

    Ok(conn)
}

/// Отпечаток того, ЧЕМ мы оцениваем, а не кого: модель, набор правил, режимы
/// разбора. Кэш по path+size+mtime годится ровно до тех пор, пока годится
/// анализатор; после обучения модели или смены правил старый вердикт — мусор,
/// и молчать об этом нельзя.
static CACHE_FP: AtomicI64 = AtomicI64::new(0);

fn cache_fp() -> i64 {
    CACHE_FP.load(Ordering::Relaxed)
}

fn set_cache_fingerprint(args: &CommonArgs) {
    // FNV-1a на u64: константы не лезут в i64, а в базу кладём те же байты.
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;

    let mut mix = |part: &str| {
        for byte in part.as_bytes() {
            hash ^= *byte as u64;
            hash = hash.wrapping_mul(0x100_0000_01b3);
        }
        hash ^= 0x7f;
    };

    mix(env!("CARGO_PKG_VERSION"));
    mix(if args.no_yara { "yara-off" } else { "yara-on" });
    mix(if args.no_builtin_rules { "builtin-off" } else { "builtin-on" });
    mix(if args.no_pe { "pe-off" } else { "pe-on" });
    mix(if args.no_archives { "zip-off" } else { "zip-on" });
    mix(if args.check_signatures { "sig-on" } else { "sig-off" });
    mix(&format!("max_mb={}", args.max_mb));
    mix(&format!("rules={}", args.rules.display()));

    // Модель: только метаданные файла, читать её целиком ради хеша дороже кэша.
    if let Ok(meta) = fs::metadata(&args.model) {
        mix(&format!("model={}|{}", meta.len(), mtime_secs(&meta)));
    } else {
        mix("model=none");
    }

    CACHE_FP.store(hash as i64, Ordering::Relaxed);
}

fn db_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn db_get_cached(
    conn: &Connection,
    path: &str,
    size: i64,
    mtime: i64,
) -> Result<Option<(String, f64, String)>> {
    let mut stmt = conn.prepare(
        "SELECT sha256, last_score, status
         FROM files
         WHERE path = ?1 AND size = ?2 AND mtime = ?3 AND fp = ?4",
    )?;

    let mut rows = stmt.query(params![path, size, mtime, cache_fp()])?;

    if let Some(row) = rows.next()? {
        Ok(Some((
            row.get::<_, Option<String>>(0)?.unwrap_or_default(),
            row.get(1)?,
            row.get(2)?,
        )))
    } else {
        Ok(None)
    }
}

fn db_upsert_file(
    conn: &Connection,
    path: &str,
    size: i64,
    mtime: i64,
    sha256: &str,
    score: f64,
    status: &str,
) -> Result<()> {
    conn.execute(
        "
        INSERT INTO files(path, size, mtime, sha256, last_score, status, fp, last_scan)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
        ON CONFLICT(path) DO UPDATE SET
            size = excluded.size,
            mtime = excluded.mtime,
            sha256 = excluded.sha256,
            last_score = excluded.last_score,
            status = excluded.status,
            fp = excluded.fp,
            last_scan = excluded.last_scan
        ",
        params![path, size, mtime, sha256, score, status, cache_fp(), db_now()],
    )?;

    Ok(())
}

fn db_insert_event(
    conn: &Connection,
    path: &str,
    sha256: &str,
    source: &str,
    detail: &str,
    score: f64,
    action: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO events(ts, path, sha256, source, detail, score, action)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![db_now(), path, sha256, source, detail, score, action],
    )?;

    Ok(())
}

fn db_insert_quarantine(
    conn: &Connection,
    original_path: &str,
    quarantine_path: &str,
    sha256: &str,
    reason: &str,
    score: f64,
) -> Result<()> {
    conn.execute(
        "INSERT INTO quarantine(ts, original_path, quarantine_path, sha256, reason, score)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            db_now(),
            original_path,
            quarantine_path,
            sha256,
            reason,
            score
        ],
    )?;

    Ok(())
}

fn db_is_blacklisted(conn: &Connection, sha256: &str) -> Result<bool> {
    let mut stmt = conn.prepare("SELECT 1 FROM hashes_blacklist WHERE sha256 = ?1")?;
    Ok(stmt.exists(params![sha256])?)
}

fn db_is_allowlisted(conn: &Connection, sha256: &str) -> Result<bool> {
    let mut stmt = conn.prepare("SELECT 1 FROM hashes_allowlist WHERE sha256 = ?1")?;
    Ok(stmt.exists(params![sha256])?)
}

fn db_add_hash(conn: &Connection, sha256: &str, list: HashList, source: &str) -> Result<()> {
    let table = match list {
        HashList::Black => "hashes_blacklist",
        HashList::Allow => "hashes_allowlist",
    };

    let sql = format!(
        "INSERT OR REPLACE INTO {}(sha256, source, added) VALUES (?1, ?2, ?3)",
        table
    );

    conn.execute(&sql, params![sha256, source, db_now()])?;

    Ok(())
}

fn add_hash_cmd(sha256: &str, list: HashList, source: &str, db: &Path) -> Result<()> {
    let sha256 = sha256.trim().to_lowercase();

    if sha256.len() != 64 || !sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("SHA-256 должен быть 64 шестнадцатеричных символа");
    }

    let conn = db_init(db)?;
    db_add_hash(&conn, &sha256, list, source)?;

    println!(
        "Хеш {} добавлен в {}",
        sha256,
        match list {
            HashList::Black => "blacklist",
            HashList::Allow => "allowlist",
        }
    );

    Ok(())
}

fn verdict_cmd(path: &Path, label: Label, db: &Path) -> Result<()> {
    let conn = db_init(db)?;

    let max_bytes = 100 * 1024 * 1024;

    let Some(analyzed) = analyze_file(path, max_bytes)? else {
        bail!("Файл слишком большой или недоступен: {}", path.display());
    };

    let label_s = match label {
        Label::Malicious => "malicious",
        Label::Clean => "clean",
    };

    let features_json = serde_json::to_string(&analyzed.features)?;

    conn.execute(
        "INSERT INTO verdicts(ts, sha256, path, label, reason, features_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            db_now(),
            analyzed.hash,
            path.display().to_string(),
            label_s,
            "manual verdict",
            features_json
        ],
    )?;

    println!("Вердикт сохранён: {} -> {}", path.display(), label_s);

    Ok(())
}

fn train_cmd(db: &Path, model_path: &Path, epochs: usize, lr: f64) -> Result<()> {
    let conn = db_init(db)?;

    let mut xs: Vec<Vec<f64>> = Vec::new();
    let mut ys: Vec<f64> = Vec::new();

    let mut stmt = conn.prepare("SELECT features_json, label FROM verdicts")?;

    let rows = stmt.query_map(params![], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;

    for row in rows {
        let (features_json, label) = row?;

        let features: Vec<f64> = match serde_json::from_str(&features_json) {
            Ok(f) => f,
            Err(_) => continue,
        };

        if features.len() != FEATURE_COUNT {
            continue;
        }

        let y = if label == "malicious" { 1.0 } else { 0.0 };

        xs.push(features);
        ys.push(y);
    }

    if xs.len() < 10 {
        bail!("Слишком мало вердиктов. Нужно минимум 10 образцов.");
    }

    let positives = ys.iter().filter(|&&y| y > 0.5).count();
    let negatives = ys.len() - positives;

    if positives == 0 || negatives == 0 {
        bail!("Нужны оба класса: вредоносные и чистые примеры.");
    }

    println!(
        "Обучение: всего {} образцов, вредоносных {}, чистых {}",
        xs.len(),
        positives,
        negatives
    );

    let mut weights = vec![0.0; FEATURE_COUNT];
    let mut bias = 0.0;

    let pos_weight = xs.len() as f64 / (2.0 * positives as f64);
    let neg_weight = xs.len() as f64 / (2.0 * negatives as f64);

    for _ in 0..epochs {
        let mut grad_w = vec![0.0; FEATURE_COUNT];
        let mut grad_b = 0.0;

        for (x, y) in xs.iter().zip(ys.iter()) {
            let z = bias
                + weights
                    .iter()
                    .zip(x.iter())
                    .map(|(w, xi)| w * xi)
                    .sum::<f64>();

            let p = sigmoid(z);

            let class_weight = if *y > 0.5 { pos_weight } else { neg_weight };
            let err = (p - y) * class_weight;

            for (g, xi) in grad_w.iter_mut().zip(x.iter()) {
                *g += err * xi;
            }

            grad_b += err;
        }

        let n = xs.len() as f64;

        for (w, g) in weights.iter_mut().zip(grad_w.iter()) {
            *w -= lr * (g / n + 0.0001 * *w);
        }

        bias -= lr * grad_b / n;
    }

    let model = Model {
        weights,
        bias,
        feature_count: FEATURE_COUNT,
    };

    if let Some(parent) = model_path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }

    fs::write(model_path, serde_json::to_string_pretty(&model)?)?;

    println!("Модель сохранена: {}", model_path.display());

    Ok(())
}

fn tui_cmd(db: &Path) -> Result<()> {
    let conn = db_init(db)?;

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen)?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    loop {
        let text = db_fetch_event_text(&conn, 200)?;

        terminal.draw(|f| {
            let chunks = Layout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Min(1), Constraint::Length(1)])
                .split(f.size());

            let events = Paragraph::new(text)
                .block(Block::default().borders(Borders::ALL).title("MiniAV events"));

            let help = Paragraph::new("q: quit");

            f.render_widget(events, chunks[0]);
            f.render_widget(help, chunks[1]);
        })?;

        if event::poll(Duration::from_millis(300))? {
            if let CrosstermEvent::Key(key) = event::read()? {
                if key.code == KeyCode::Char('q') {
                    break;
                }
            }
        }
    }

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;

    Ok(())
}

fn db_fetch_event_text(conn: &Connection, limit: i64) -> Result<String> {
    let mut stmt = conn.prepare(
        "SELECT ts, path, source, score, action
         FROM events
         ORDER BY id DESC
         LIMIT ?1",
    )?;

    let rows = stmt.query_map(params![limit], |row| {
        Ok(format!(
            "{} | {} | {} | {:.2} | {}",
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, f64>(3)?,
            row.get::<_, String>(4)?
        ))
    })?;

    let mut lines = Vec::new();

    for row in rows {
        lines.push(row?);
    }

    Ok(lines.join("\n"))
}

fn keygen_cmd(public: &Path, secret: &Path) -> Result<()> {
    let mut rng = OsRng;
    let signing_key = SigningKey::generate(&mut rng);

    let verifying_key = signing_key.verifying_key();

    fs::write(secret, hex::encode(signing_key.to_bytes()))?;
    fs::write(public, hex::encode(verifying_key.to_bytes()))?;

    println!("Ключи созданы:");
    println!("  private: {}", secret.display());
    println!("  public:  {}", public.display());
    println!("Храни приватный ключ отдельно и не клади его в папку с обновлениями.");

    Ok(())
}

fn sign_file_cmd(file: &Path, secret: &Path) -> Result<()> {
    let secret_hex = fs::read_to_string(secret)
        .with_context(|| format!("не удалось прочитать {}", secret.display()))?;

    let secret_bytes = hex::decode(secret_hex.trim())?;

    let secret_arr: [u8; 32] = secret_bytes
        .try_into()
        .map_err(|_| anyhow!("Неверная длина приватного ключа"))?;

    let signing_key = SigningKey::from_bytes(&secret_arr);

    let msg = fs::read(file).with_context(|| format!("не удалось прочитать {}", file.display()))?;

    let signature = signing_key.sign(&msg);

    let sig_path = PathBuf::from(format!("{}.sig", file.display()));

    fs::write(&sig_path, hex::encode(signature.to_bytes()))?;

    println!("Подпись сохранена: {}", sig_path.display());

    Ok(())
}

fn verify_update_cmd(dir: &Path, public: &Path) -> Result<()> {
    if !dir.exists() {
        bail!("Папка обновлений не существует: {}", dir.display());
    }

    if !public.exists() {
        bail!("Публичный ключ не найден: {}", public.display());
    }

    let mut checked = 0;

    for entry in WalkDir::new(dir).into_iter() {
        let entry = entry?;
        let path = entry.path();

        if !path.is_file() {
            continue;
        }

        let file_name = path.file_name().unwrap_or_default().to_string_lossy();

        if file_name.ends_with(".sig") {
            continue;
        }

        if path == public {
            continue;
        }

        if verify_file(public, path)? {
            println!("OK: {}", path.display());
            checked += 1;
        } else {
            bail!("ПРОВЕРКА ПРОВАЛЕНА: {}", path.display());
        }
    }

    if checked == 0 {
        bail!("В папке не найдено файлов для проверки");
    }

    println!("Все проверенные файлы имеют корректные подписи.");

    Ok(())
}

fn load_public_key(path: &Path) -> Result<VerifyingKey> {
    let hex_str = fs::read_to_string(path)
        .with_context(|| format!("не удалось прочитать {}", path.display()))?;

    let bytes = hex::decode(hex_str.trim())?;

    let arr: [u8; 32] = bytes
        .try_into()
        .map_err(|_| anyhow!("Неверная длина публичного ключа"))?;

    Ok(VerifyingKey::from_bytes(&arr)?)
}

fn verify_file(public_key_path: &Path, file_path: &Path) -> Result<bool> {
    let sig_path = PathBuf::from(format!("{}.sig", file_path.display()));

    if !sig_path.exists() {
        return Ok(false);
    }

    let pubkey = load_public_key(public_key_path)?;

    let msg = fs::read(file_path)?;

    let sig_hex = fs::read_to_string(sig_path)?;
    let sig_bytes = hex::decode(sig_hex.trim())?;

    let signature = Signature::from_slice(&sig_bytes)?;

    Ok(pubkey.verify(&msg, &signature).is_ok())
}