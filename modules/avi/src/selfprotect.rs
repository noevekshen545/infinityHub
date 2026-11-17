//! Самозащита пользовательского режима: манифест контрольных сумм своих файлов,
//! страховочные копии, восстановление и NTFS-отказ на удаление.
//!
//! Потолок честности: процесс без ядра не может запретить удаление тому, кто
//! работает под тем же пользователем, — владелец всегда может переписать DACL.
//! Здесь это компенсируется скоростью: отказ в правах задерживает чужой процесс,
//! а детект и автовосстановление снимают сам эффект подмены.

use anyhow::{anyhow, Context, Result};
use ed25519_dalek::Signer;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{BufReader, Read, Write},
    path::{Path, PathBuf},
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

/// Имя манифеста внутри страховочного каталога.
pub const MANIFEST_NAME: &str = "guard_manifest.json";
/// Подкаталог, где лежат сами страховые копии.
pub const COPIES_DIR: &str = "files";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    /// Как файл назван в страховке.
    pub name: String,
    /// Где он должен стоять на самом деле.
    pub path: String,
    pub sha256: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub created: i64,
    pub files: Vec<Entry>,
}

#[derive(Debug, Default)]
pub struct Report {
    /// Защищаемый файл отсутствует.
    pub missing: Vec<Entry>,
    /// Защищаемый файл есть, но содержимое не совпало.
    pub changed: Vec<(Entry, String)>,
    /// Страховая копия отсутствует или не совпала с манифестом.
    pub copies_broken: Vec<String>,
    /// Что удалось восстановить.
    pub restored: Vec<PathBuf>,
}

impl Report {
    pub fn damaged(&self) -> bool {
        !self.missing.is_empty() || !self.changed.is_empty() || !self.copies_broken.is_empty()
    }
}

pub fn manifest_path(guard_dir: &Path) -> PathBuf {
    guard_dir.join(MANIFEST_NAME)
}

pub fn copies_dir(guard_dir: &Path) -> PathBuf {
    guard_dir.join(COPIES_DIR)
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Поточное чтение: страховка меряется на файлах в десятки мегабайт, и целиком
/// в память их тянуть нельзя.
pub fn sha256_of(path: &Path) -> Result<String> {
    let file = File::open(path)
        .with_context(|| format!("не открыть {} для хеширования", path.display()))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();

    let mut buf = [0u8; 128 * 1024];

    loop {
        let read = reader
            .read(&mut buf)
            .with_context(|| format!("сбой чтения {}", path.display()))?;

        if read == 0 {
            break;
        }

        hasher.update(&buf[..read]);
    }

    Ok(hex::encode(hasher.finalize()))
}

/// Имя копии для пути: каталог в имени не сохраняется, иначе раскладка
/// страховки повторяет за собой произвольный путь источника.
fn copy_name(path: &Path) -> String {
    let stem = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "unnamed".to_string());

    let hash = sha256_hex_of_stem(&stem);
    format!("{stem}.{hash}.copy")
}

fn sha256_hex_of_stem(text: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    hex::encode(hasher.finalize())[..12].to_string()
}

/// Запечатать страховку: копии файлов + манифест сумм.
///
/// Если передан секретный ключ, манифест подписывается — иначе подмена
/// манифеста была бы тривиальной.
pub fn seal(guard_dir: &Path, files: &[PathBuf], secret: Option<&Path>) -> Result<Manifest> {
    if files.is_empty() {
        return Err(anyhow!("нечего запечатывать: пустой список файлов"));
    }

    fs::create_dir_all(copies_dir(guard_dir))
        .with_context(|| format!("не создать страховочный каталог {}", guard_dir.display()))?;

    let mut manifest = Manifest {
        version: 1,
        created: now_secs(),
        files: Vec::new(),
    };

    for path in files {
        let size = fs::metadata(path)
            .with_context(|| format!("нет доступа к {}", path.display()))?
            .len();

        let sha256 = sha256_of(path)?;
        let name = copy_name(path);
        let target = copies_dir(guard_dir).join(&name);

        fs::copy(path, &target)
            .with_context(|| format!("не скопировать {} в страховку", path.display()))?;

        // Копия обязана совпадать с тем, что мы только что посчитали: диск или
        // антивирус мог подменить содержимое во время записи.
        let written = sha256_of(&target)?;

        if written != sha256 {
            return Err(anyhow!(
                "страховка не совпала с источником: {} (ждал {}, получил {})",
                path.display(),
                sha256,
                written
            ));
        }

        manifest.files.push(Entry {
            name,
            path: path.display().to_string(),
            sha256,
            size,
        });
    }

    let path = manifest_path(guard_dir);
    let mut file = File::create(&path)
        .with_context(|| format!("не записать манифест {}", path.display()))?;

    file.write_all(
        serde_json::to_string_pretty(&manifest)
            .context("манифест не сериализуется")?
            .as_bytes(),
    )?;

    if let Some(secret) = secret {
        sign_manifest(&path, secret)?;
    }

    Ok(manifest)
}

/// Подпись манифеста тем же механизмом, что и определения обновлений:
/// `.sig` рядом с файлом, ed25519, hex.
fn sign_manifest(manifest: &Path, secret: &Path) -> Result<()> {
    let mut hex_secret = String::new();
    File::open(secret)
        .with_context(|| format!("нет секретного ключа {}", secret.display()))?
        .read_to_string(&mut hex_secret)?;

    let bytes = hex::decode(hex_secret.trim()).context("секретный ключ не в hex")?;
    let bytes: [u8; 32] = bytes
        .try_into()
        .map_err(|_| anyhow!("секретный ключ должен быть 32 байта"))?;

    let signing = ed25519_dalek::SigningKey::from_bytes(&bytes);
    let body = fs::read(manifest)?;
    let signature = signing.sign(&body);

    fs::write(
        manifest.with_extension("json.sig"),
        hex::encode(signature.to_bytes()),
    )
    .with_context(|| format!("не записать подпись для {}", manifest.display()))?;

    Ok(())
}

/// Проверка подписи манифеста: без неё подменённый манифест обнулил бы всю
/// самозащиту, поэтому отсутствующий pubkey означает «не проверено».
pub fn manifest_signature_ok(guard_dir: &Path, pubkey: Option<&Path>) -> Result<Option<bool>> {
    let Some(pubkey) = pubkey else {
        return Ok(None);
    };

    let manifest = manifest_path(guard_dir);
    let sig_path = manifest.with_extension("json.sig");

    let hex_key = fs::read_to_string(pubkey)
        .with_context(|| format!("нет открытого ключа {}", pubkey.display()))?;

    let key_bytes = hex::decode(hex_key.trim()).context("открытый ключ не в hex")?;
    let key_bytes: [u8; 32] = key_bytes
        .try_into()
        .map_err(|_| anyhow!("открытый ключ должен быть 32 байта"))?;

    let verifying = ed25519_dalek::VerifyingKey::from_bytes(&key_bytes)
        .map_err(|_| anyhow!("открытый ключ непригоден"))?;

    let body = fs::read(&manifest)?;
    let sig_hex = fs::read_to_string(&sig_path)
        .with_context(|| format!("нет подписи {}", sig_path.display()))?;

    let sig_bytes = hex::decode(sig_hex.trim()).context("подпись не в hex")?;
    let sig_bytes: [u8; 64] = sig_bytes
        .try_into()
        .map_err(|_| anyhow!("подпись должна быть 64 байта"))?;

    let signature = ed25519_dalek::Signature::from_bytes(&sig_bytes);

    Ok(Some(
        verifying
            .verify_strict(&body, &signature)
            .is_ok(),
    ))
}

pub fn load_manifest(guard_dir: &Path) -> Result<Manifest> {
    let path = manifest_path(guard_dir);
    let body = fs::read(&path)
        .with_context(|| format!("нет манифеста {}: не запечатана ли страховка?", path.display()))?;

    serde_json::from_slice(&body).context("манифест страховки не читается как JSON")
}

/// Сверка живых файлов и самих копий с манифестом.
pub fn check(guard_dir: &Path) -> Result<Report> {
    let manifest = load_manifest(guard_dir)?;
    let mut report = Report::default();

    for entry in &manifest.files {
        let live = Path::new(&entry.path);

        if !live.exists() {
            report.missing.push(entry.clone());
            continue;
        }

        match sha256_of(live) {
            Ok(found) if found != entry.sha256 => report.changed.push((entry.clone(), found)),
            Ok(_) => {}
            // Файл есть, но не читается: это не «чисто», это отдельная поломка.
            Err(_) => report.changed.push((entry.clone(), String::new())),
        }

        let copy = copies_dir(guard_dir).join(&entry.name);

        match sha256_of(&copy) {
            Ok(found) if found == entry.sha256 => {}
            Ok(found) => report
                .copies_broken
                .push(format!("{}: копия изменена ({found})", entry.name)),
            Err(_) => report
                .copies_broken
                .push(format!("{}: копии нет", entry.name)),
        }
    }

    Ok(report)
}

/// Вернуть запечатанное на место. Работает и когда файл подменён, и когда его
/// нет; копия при этом сначала сверяется с манифестом.
pub fn restore(guard_dir: &Path) -> Result<Report> {
    let manifest = load_manifest(guard_dir)?;
    let mut report = Report::default();

    for entry in &manifest.files {
        let live = Path::new(&entry.path);
        let copy = copies_dir(guard_dir).join(&entry.name);

        let copy_hash = match sha256_of(&copy) {
            Ok(hash) => hash,
            Err(e) => {
                report.copies_broken.push(format!("{}: {e}", entry.name));
                continue;
            }
        };

        if copy_hash != entry.sha256 {
            report
                .copies_broken
                .push(format!("{}: копия не совпала с манифестом", entry.name));
            continue;
        }

        let healthy = live.exists() && sha256_of(live).map(|h| h == entry.sha256).unwrap_or(false);

        if healthy {
            continue;
        }

        if let Some(parent) = live.parent() {
            let _ = fs::create_dir_all(parent);
        }

        // Существующий файл замещается только если его удалось переписать:
        // снятие read-only пробует атрибут, отказ ACL — нет.
        let _ = fs::remove_file(live);

        match fs::copy(&copy, live) {
            Ok(_) => {
                let _ = crate::set_readonly(live);
                report.restored.push(live.to_path_buf());
            }
            Err(e) => report
                .copies_broken
                .push(format!("{}: не восстановить ({e})", entry.name)),
        }
    }

    Ok(report)
}

/// Отказ на удаление и изменение DACL для страховки. Это задержка, а не
/// броня: владелец объекта всегда может переписать права, поэтому смысл
/// в том, чтобы обычный процесс не снёс копии одним движением.
pub fn lock_acl(dir: &Path) -> Result<String> {
    run_icaccls(dir, &["/deny", "*S-1-1-0:(OI)(CI)(DE,DC,WD,AD)"])
}

/// То, чем на Windows без драйвера должен быть «immutable» для одного файла:
/// отказ на запись, смену DACL и удаление.
pub fn deny_write_acl(path: &Path) -> Result<String> {
    run_icaccls(path, &["/deny", "*S-1-1-0:(W,DC,DE)"])
}

pub fn clear_deny_acl(path: &Path) -> Result<String> {
    run_icaccls(path, &["/remove:d", "*S-1-1-0"])
}

pub fn unlock_acl(dir: &Path) -> Result<String> {
    run_icaccls(dir, &["/remove:d", "*S-1-1-0"])
}

#[cfg(windows)]
fn run_icaccls(dir: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("icacls")
        .arg(dir.as_os_str())
        .args(args)
        .output()
        .context("не удалось запустить icacls")?;

    let text = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();

    if !output.status.success() {
        return Err(anyhow!(
            "icacls отказал ({}): {}",
            output
                .status
                .code()
                .map(|c| c.to_string())
                .unwrap_or_else(|| "?".into()),
            if stderr.trim().is_empty() {
                text.trim()
            } else {
                stderr.trim()
            }
        ));
    }

    Ok(text)
}

#[cfg(not(windows))]
fn run_icaccls(_dir: &Path, _args: &[&str]) -> Result<String> {
    Err(anyhow!(
        "NTFS-отказ настроен только на Windows; на этой системе самозащита опирается на манифест и автовосстановление"
    ))
}
