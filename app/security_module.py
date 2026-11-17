"""
Security module for ASIO Agent
Реальное шифрование для sudo паролей и чувствительных данных
"""

import os
import base64
import logging
from typing import Optional
import swallow

logger = logging.getLogger("infinity")

# Try to import cryptography, fallback to basic if not available
try:
    from cryptography.fernet import Fernet, InvalidToken
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    logger.warning("cryptography package not available — using basic encryption")
    Fernet = None
    InvalidToken = Exception

# === DIRECTORIES ===
AGENT_BASE = os.path.expanduser("~/.infinity/asio_agent")
ENCRYPTION_KEY_FILE = os.path.join(AGENT_BASE, "encryption.key")

os.makedirs(AGENT_BASE, exist_ok=True)


def _get_or_create_key() -> bytes:
    """Получает или создаёт ключ шифрования."""
    if os.path.exists(ENCRYPTION_KEY_FILE):
        with open(ENCRYPTION_KEY_FILE, "rb") as f:
            return f.read()
    
    # Создаём новый ключ
    if CRYPTOGRAPHY_AVAILABLE:
        key = Fernet.generate_key()
    else:
        # Fallback: генерируем простой ключ
        key = base64.urlsafe_b64encode(os.urandom(32))
    
    with open(ENCRYPTION_KEY_FILE, "wb") as f:
        f.write(key)
    
    # Устанавливаем безопасные права доступа (только для владельца)
    try:
        os.chmod(ENCRYPTION_KEY_FILE, 0o600)
    except OSError:
        swallow.note('security_module._get_or_create_key:L50')
        pass
    
    return key


def encrypt_data(data: str) -> str:
    """
    Шифрует строку с использованием Fernet encryption.
    
    Args:
        data: Строка для шифрования
        
    Returns:
        Зашифрованная строка (base64 encoded)
    """
    if not data:
        return ""
    
    key = _get_or_create_key()
    
    if CRYPTOGRAPHY_AVAILABLE:
        fernet = Fernet(key)
        encrypted = fernet.encrypt(data.encode("utf-8"))
        return base64.urlsafe_b64encode(encrypted).decode("utf-8")
    else:
        # Fallback: XOR encryption с ключом
        key_bytes = base64.urlsafe_b64decode(key)
        data_bytes = data.encode("utf-8")
        encrypted = bytes([d ^ key_bytes[i % len(key_bytes)] for i, d in enumerate(data_bytes)])
        return base64.urlsafe_b64encode(encrypted).decode("utf-8")


def decrypt_data(encrypted_data: str) -> Optional[str]:
    """
    Расшифровывает строку.
    
    Args:
        encrypted_data: Зашифрованная строка
        
    Returns:
        Расшифрованная строка или None при ошибке
    """
    if not encrypted_data:
        return None
    
    key = _get_or_create_key()
    
    try:
        if CRYPTOGRAPHY_AVAILABLE:
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode("utf-8"))
            fernet = Fernet(key)
            decrypted = fernet.decrypt(encrypted_bytes)
            return decrypted.decode("utf-8")
        else:
            # Fallback: XOR decryption
            key_bytes = base64.urlsafe_b64decode(key)
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode("utf-8"))
            decrypted = bytes([d ^ key_bytes[i % len(key_bytes)] for i, d in enumerate(encrypted_bytes)])
            return decrypted.decode("utf-8")
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return None


def save_sudo_password_secure(password: str) -> dict:
    """
    Безопасно сохраняет пароль для sudo.
    
    Args:
        password: Пароль sudo
        
    Returns:
        dict с результатом операции
    """
    if not password:
        return {"ok": False, "error": "Пароль не может быть пустым"}
    
    try:
        encrypted = encrypt_data(password)
        password_file = os.path.join(AGENT_BASE, "sudo_password.enc")
        
        with open(password_file, "w") as f:
            f.write(encrypted)
        
        # Устанавливаем безопасные права доступа
        try:
            os.chmod(password_file, 0o600)
        except OSError:
            swallow.note('security_module.save_sudo_password_secure:L139')
            pass
        
        logger.info("Sudo password saved securely")
        return {"ok": True}
    except Exception as e:
        logger.error(f"Failed to save sudo password: {e}")
        return {"ok": False, "error": str(e)}


def get_sudo_password_secure() -> Optional[str]:
    """
    Безопасно получает пароль sudo.
    
    Returns:
        Пароль sudo или None
    """
    password_file = os.path.join(AGENT_BASE, "sudo_password.enc")
    
    if not os.path.exists(password_file):
        return None
    
    try:
        with open(password_file, "r") as f:
            encrypted = f.read().strip()
        
        return decrypt_data(encrypted)
    except Exception as e:
        logger.error(f"Failed to read sudo password: {e}")
        return None


def clear_sudo_password_secure() -> dict:
    """
    Удаляет сохранённый пароль sudo.
    
    Returns:
        dict с результатом операции
    """
    password_file = os.path.join(AGENT_BASE, "sudo_password.enc")
    
    if os.path.exists(password_file):
        try:
            os.remove(password_file)
            logger.info("Sudo password cleared")
            return {"ok": True}
        except Exception as e:
            logger.error(f"Failed to clear sudo password: {e}")
            return {"ok": False, "error": str(e)}
    
    return {"ok": True, "message": "Password file not found"}


def generate_sudo_command(cmd: str) -> Optional[str]:
    """
    Генерирует команду sudo с безопасной вставкой пароля.
    Использует stdin вместо аргументов командной строки.
    
    Args:
        cmd: Команда sudo (без 'sudo ')
        
    Returns:
        Полная команда с безопасной вставкой пароля или None
    """
    password = get_sudo_password_secure()
    if not password:
        return None
    
    # Безопасный способ: передаём пароль через stdin
    # Используем sudo -S для чтения из stdin
    safe_cmd = f'echo "{password}" | sudo -S -p "" {cmd.strip()}'
    return safe_cmd


# === БАЙТОВОЕ ШИФРОВАНИЕ (файловый менеджер) ===
# encrypt_data/decrypt_data работают со строками, а файлам нужны произвольные
# байты. Ключ тот же (encryption.key), отдельный заводить не нужно.
# Формат: 5-байтовый заголовок + данные. Заголовок позволяет отличить файл,
# зашифрованный здесь, от любого другого и корректно отказать при смене ключа.

_BYTES_FERNET = b"INFC1"
_BYTES_XOR = b"INFC0"


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])


def encrypt_bytes(data: bytes) -> bytes:
    """Зашифровать произвольные байты. Результат можно писать в файл."""
    # Пустые данные тоже шифруем (заголовок + токен), иначе пустой файл
    # после шифрования стал бы нулевым и расшифровка возвращала бы None.
    if data is None:
        data = b""
    key = _get_or_create_key()
    if CRYPTOGRAPHY_AVAILABLE:
        return _BYTES_FERNET + Fernet(key).encrypt(data)
    key_bytes = base64.urlsafe_b64decode(key)
    return _BYTES_XOR + base64.urlsafe_b64encode(_xor_bytes(data, key_bytes))


def decrypt_bytes(blob: bytes) -> Optional[bytes]:
    """Расшифровать то, что вернул encrypt_bytes(). None при ошибке."""
    if not blob:
        return None
    try:
        if blob.startswith(_BYTES_FERNET):
            if not CRYPTOGRAPHY_AVAILABLE:
                logger.error("decrypt_bytes: cryptography недоступна")
                return None
            return Fernet(_get_or_create_key()).decrypt(blob[len(_BYTES_FERNET):])
        if blob.startswith(_BYTES_XOR):
            key_bytes = base64.urlsafe_b64decode(_get_or_create_key())
            payload = base64.urlsafe_b64decode(blob[len(_BYTES_XOR):])
            return _xor_bytes(payload, key_bytes)
    except Exception as e:
        logger.error(f"decrypt_bytes failed: {e}")
    return None
