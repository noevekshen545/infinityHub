"""Опциональное шифрование медиафайлов «на диске».

Зачем: при включённой опции файл из проводника Windows открыть нельзя —
на диске лежит контейнер, а приложение расшифровывает его на лету при
воспроизведении через /api/media/stream. Ключ общий с security_module
(~/.infinity/asio_agent/encryption.key), поэтому файлы восстановимы только
на этой машине.

Формат контейнера: `INFMED1\\n` + 16 байт nonce + AES-CTR(plaintext).
CTR выбран из-за возможности читать произвольный диапазон (Range-запросы
<audio>/<video>) без расшифровки файла целиком. Заголовок — 24 байта.
"""

import os
import hashlib
import logging
import swallow

logger = logging.getLogger("infinity")

MAGIC = b"INFMED1\n"
HEADER = len(MAGIC) + 16
SUFFIX = ".infenc"
CHUNK = 256 * 1024

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _AES_OK = True
except Exception as e:  # pragma: no cover
    _AES_OK = False
    logger.warning(f"media_crypto: cryptography недоступна ({e})")


def available() -> bool:
    return _AES_OK


def key() -> bytes:
    """32-байтовый ключ, производный от ключа security_module."""
    import security_module as sec
    raw = sec._get_or_create_key()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(b"infinity-media-vault-v1" + raw).digest()


def encrypted_path(path: str) -> str:
    return path + SUFFIX


def has_encrypted_variant(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(encrypted_path(path))
    except OSError:
        swallow.note('media_crypto.has_encrypted_variant:L54')
        return False


def is_encrypted_container(path: str) -> bool:
    """Похож ли файл на наш контейнер (по заголовку)."""
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        swallow.note('media_crypto.is_encrypted_container:L66')
        return False


def plain_size(enc_path: str) -> int:
    return max(0, os.path.getsize(enc_path) - HEADER)


def _nonce(path: str) -> bytes:
    with open(path, "rb") as f:
        head = f.read(HEADER)
    if len(head) != HEADER or head[:len(MAGIC)] != MAGIC:
        raise ValueError("не контейнер Infinity")
    return head[len(MAGIC):]


def _decrypt_at(k: bytes, nonce: bytes, offset: int, data: bytes) -> bytes:
    """Расшифровать data, лежащие в контейнере по смещению offset."""
    block, skip = divmod(offset, 16)
    counter = (int.from_bytes(nonce, "big") + block) & ((1 << 128) - 1)
    dec = Cipher(algorithms.AES(k), modes.CTR(counter.to_bytes(16, "big"))).decryptor()
    out = dec.update(b"\x00" * skip + data) + dec.finalize()
    return out[skip:]


def iter_decrypted(enc_path: str, start: int = 0, length: int = None):
    """Поток расшифрованных байт из контейнера, начиная с start."""
    total = plain_size(enc_path)
    start = max(0, min(int(start or 0), total))
    if length is None:
        length = total - start
    length = max(0, min(int(length), total - start))
    k = key()
    nonce = _nonce(enc_path)
    with open(enc_path, "rb") as f:
        f.seek(HEADER + start)
        pos = start
        remaining = length
        while remaining > 0:
            data = f.read(min(CHUNK, remaining))
            if not data:
                break
            yield _decrypt_at(k, nonce, pos, data)
            pos += len(data)
            remaining -= len(data)


def encrypt_file(path: str) -> dict:
    """Зашифровать файл рядом (<имя>.infenc), оригинал удалить."""
    if not _AES_OK:
        raise RuntimeError("cryptography недоступна")
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    if path.lower().endswith(SUFFIX):
        raise ValueError("файл уже зашифрован")
    target = encrypted_path(path)
    if os.path.exists(target):
        raise FileExistsError("рядом уже есть .infenc")
    tmp = target + ".tmp"
    nonce = os.urandom(16)
    k = key()
    try:
        with open(path, "rb") as fi, open(tmp, "wb") as fo:
            fo.write(MAGIC + nonce)
            enc = Cipher(algorithms.AES(k), modes.CTR(nonce)).encryptor()
            while True:
                block = fi.read(CHUNK)
                if not block:
                    break
                fo.write(enc.update(block))
            fo.write(enc.finalize())
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            swallow.note('media_crypto.encrypt_file:L145')
            pass
        raise
    removed = True
    try:
        os.remove(path)
    except OSError as e:
        removed = False
        logger.warning(f"media_crypto: .infenc создан, оригинал не удалён ({path}): {e}")
    return {"path": path, "encrypted_path": target,
            "size": os.path.getsize(target), "original_removed": removed}


def decrypt_file(enc_path: str) -> dict:
    """Расшифровать <имя>.infenc обратно в исходное имя."""
    if not _AES_OK:
        raise RuntimeError("cryptography недоступна")
    if not enc_path or not os.path.isfile(enc_path):
        raise FileNotFoundError(enc_path)
    if not enc_path.lower().endswith(SUFFIX):
        raise ValueError("файл не зашифрован")
    original = enc_path[:-len(SUFFIX)]
    if os.path.exists(original):
        raise FileExistsError("файл с исходным именем уже существует")
    tmp = original + ".tmp"
    total = plain_size(enc_path)
    try:
        with open(tmp, "wb") as fo:
            for block in iter_decrypted(enc_path, 0, total):
                fo.write(block)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, original)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            swallow.note('media_crypto.decrypt_file:L183')
            pass
        raise
    os.remove(enc_path)
    return {"path": enc_path, "decrypted_path": original,
            "size": os.path.getsize(original)}
