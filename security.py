"""
Модуль безопасности для проверки файлов и валидации данных
"""
import os
import re
import hashlib
import logging
from typing import Tuple, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# Максимальные размеры файлов (в байтах)
MAX_SESSION_SIZE = 100 * 1024  # 100 KB - максимальный размер .session файла
MAX_TXT_SIZE = 10 * 1024 * 1024  # 10 MB - максимальный размер .txt файла
MIN_SESSION_SIZE = 1 * 1024  # 1 KB - минимальный размер .session файла

# Подозрительные паттерны в файлах (стилеры, трояны)
SUSPICIOUS_PATTERNS = [
    # Паттерны стилеров
    b'stealer',
    b'keylogger',
    b'password',
    b'wallet',
    b'crypto',
    b'bitcoin',
    b'ethereum',
    b'private_key',
    b'seed',
    b'mnemonic',
    
    # Паттерны троянов
    b'backdoor',
    b'trojan',
    b'virus',
    b'malware',
    b'exploit',
    b'shellcode',
    
    # Подозрительные URL и IP
    b'http://',
    b'https://',
    b'ftp://',
    b'@',
    
    # Подозрительные команды
    b'exec(',
    b'eval(',
    b'__import__',
    b'subprocess',
    b'os.system',
    b'shell=True',
    
    # Подозрительные расширения
    b'.exe',
    b'.dll',
    b'.bat',
    b'.cmd',
    b'.ps1',
    b'.sh',
    b'.pyc',
    b'.pyo',
]

# Разрешенные символы в именах файлов
ALLOWED_FILENAME_CHARS = re.compile(r'^[a-zA-Z0-9._-]+$')

# Разрешенные пути (защита от path traversal)
ALLOWED_DIRS = [
    '.',  # Текущая директория
    'data',
]


def validate_filename(filename: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация имени файла
    Возвращает (is_valid, error_message)
    """
    if not filename:
        return False, "Имя файла не может быть пустым"
    
    if len(filename) > 255:
        return False, "Имя файла слишком длинное"
    
    # Проверка на path traversal
    if '..' in filename or '/' in filename or '\\' in filename:
        return False, "Имя файла содержит недопустимые символы"
    
    # Проверка разрешенных символов
    if not ALLOWED_FILENAME_CHARS.match(filename):
        return False, "Имя файла содержит недопустимые символы"
    
    # Проверка на зарезервированные имена (Windows)
    reserved_names = ['CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 
                     'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3',
                     'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9']
    if filename.upper().split('.')[0] in reserved_names:
        return False, "Имя файла зарезервировано системой"
    
    return True, None


def validate_file_path(filepath: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация пути к файлу (защита от path traversal)
    Возвращает (is_valid, error_message)
    """
    try:
        # Нормализуем путь
        normalized = os.path.normpath(filepath)
        
        # Проверяем, что путь не выходит за пределы разрешенных директорий
        abs_path = os.path.abspath(normalized)
        current_dir = os.path.abspath('.')
        
        # Проверяем, что файл находится в текущей директории или поддиректориях
        if not abs_path.startswith(current_dir):
            return False, "Путь к файлу выходит за пределы разрешенной директории"
        
        return True, None
    except Exception as e:
        logger.error(f"Ошибка валидации пути: {e}")
        return False, f"Ошибка валидации пути: {e}"


def check_session_file(file_path: str, file_content: bytes) -> Tuple[bool, Optional[str]]:
    """
    Проверка .session файла на безопасность
    Возвращает (is_safe, error_message)
    """
    try:
        # Проверка размера
        file_size = len(file_content)
        if file_size < MIN_SESSION_SIZE:
            return False, f"Файл слишком маленький ({file_size} байт). Возможно, это не валидный .session файл"
        
        if file_size > MAX_SESSION_SIZE:
            return False, f"Файл слишком большой ({file_size} байт). Максимальный размер: {MAX_SESSION_SIZE} байт"
        
        # Проверка на подозрительные паттерны
        file_lower = file_content.lower()
        for pattern in SUSPICIOUS_PATTERNS:
            if pattern.lower() in file_lower:
                logger.warning(f"Обнаружен подозрительный паттерн в файле: {pattern}")
                return False, f"Файл содержит подозрительный контент (паттерн: {pattern.decode('utf-8', errors='ignore')})"
        
        # Проверка структуры .session файла
        # Telethon session файлы обычно начинаются с определенных байтов
        # Проверяем, что файл не является исполняемым
        if file_content.startswith(b'MZ'):  # PE executable
            return False, "Файл является исполняемым (PE формат). Это не .session файл"
        
        if file_content.startswith(b'\x7fELF'):  # ELF executable
            return False, "Файл является исполняемым (ELF формат). Это не .session файл"
        
        # Проверка на наличие Python байт-кода (может быть вредоносным)
        if b'\x03\xf3\r\n' in file_content[:100]:  # Python magic number
            return False, "Файл содержит Python байт-код. Это не валидный .session файл"
        
        # Проверка на наличие только текстовых символов (подозрительно для бинарного файла)
        # .session файлы Telethon - это бинарные файлы, но они могут содержать некоторые текстовые данные
        # Проверяем, что файл не является чистым текстом (может быть скриптом)
        text_ratio = sum(1 for b in file_content[:1000] if 32 <= b <= 126) / min(1000, len(file_content))
        if text_ratio > 0.9 and file_size < 5000:
            return False, "Файл выглядит как текстовый скрипт, а не .session файл"
        
        # Проверка хеша файла (можно добавить черный список известных вредоносных файлов)
        file_hash = hashlib.sha256(file_content).hexdigest()
        logger.info(f"[SECURITY] Хеш .session файла: {file_hash[:16]}...")
        
        return True, None
        
    except Exception as e:
        logger.error(f"Ошибка проверки .session файла: {e}", exc_info=True)
        return False, f"Ошибка проверки файла: {e}"


def check_txt_file(file_path: str, file_content: bytes) -> Tuple[bool, Optional[str]]:
    """
    Проверка .txt файла на безопасность
    Возвращает (is_safe, error_message)
    """
    try:
        # Проверка размера
        file_size = len(file_content)
        if file_size > MAX_TXT_SIZE:
            return False, f"Файл слишком большой ({file_size} байт). Максимальный размер: {MAX_TXT_SIZE} байт"
        
        # Проверка на подозрительные паттерны
        file_lower = file_content.lower()
        suspicious_patterns_txt = [
            b'<script',
            b'javascript:',
            b'onerror=',
            b'onload=',
            b'eval(',
            b'exec(',
            b'__import__',
            b'subprocess',
            b'os.system',
        ]
        
        for pattern in suspicious_patterns_txt:
            if pattern.lower() in file_lower:
                logger.warning(f"Обнаружен подозрительный паттерн в .txt файле: {pattern}")
                return False, f"Файл содержит подозрительный контент"
        
        # Проверка кодировки
        try:
            file_content.decode('utf-8')
        except UnicodeDecodeError:
            # Пробуем другие кодировки
            try:
                file_content.decode('latin-1')
            except UnicodeDecodeError:
                return False, "Файл содержит недопустимые символы или бинарные данные"
        
        return True, None
        
    except Exception as e:
        logger.error(f"Ошибка проверки .txt файла: {e}", exc_info=True)
        return False, f"Ошибка проверки файла: {e}"


def validate_api_credentials(api_id: int, api_hash: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация API credentials
    Возвращает (is_valid, error_message)
    """
    try:
        # Проверка API ID
        if not isinstance(api_id, int) or api_id <= 0:
            return False, "API ID должен быть положительным числом"
        
        if api_id > 999999999:  # Telegram API ID обычно меньше
            return False, "API ID выглядит невалидным"
        
        # Проверка API Hash
        if not isinstance(api_hash, str) or not api_hash:
            return False, "API Hash не может быть пустым"
        
        if len(api_hash) < 20 or len(api_hash) > 50:
            return False, "API Hash имеет неверную длину"
        
        # Проверка формата API Hash (обычно hex)
        if not re.match(r'^[a-f0-9]+$', api_hash.lower()):
            return False, "API Hash содержит недопустимые символы"
        
        return True, None
        
    except Exception as e:
        logger.error(f"Ошибка валидации API credentials: {e}")
        return False, f"Ошибка валидации: {e}"


def validate_proxy_string(proxy_str: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация строки прокси
    Возвращает (is_valid, error_message)
    """
    if not proxy_str or not proxy_str.strip():
        return True, None  # Пустой прокси - это нормально
    
    # Проверка длины
    if len(proxy_str) > 200:
        return False, "Строка прокси слишком длинная"
    
    # Проверка на подозрительные символы
    if re.search(r'[<>"\']', proxy_str):
        return False, "Строка прокси содержит недопустимые символы"
    
    # Проверка формата (базовая)
    if '@' in proxy_str:
        parts = proxy_str.split('@')
        if len(parts) != 2:
            return False, "Неверный формат прокси"
        
        # Проверка части с логином/паролем
        if ':' not in parts[0]:
            return False, "Неверный формат логина/пароля в прокси"
    
    # Проверка IP/порта
    if ':' in proxy_str:
        addr_part = proxy_str.split('@')[-1] if '@' in proxy_str else proxy_str
        if ':' in addr_part:
            ip, port = addr_part.rsplit(':', 1)
            try:
                port_num = int(port)
                if port_num < 1 or port_num > 65535:
                    return False, "Порт прокси должен быть в диапазоне 1-65535"
            except ValueError:
                return False, "Порт прокси должен быть числом"
    
    return True, None


def sanitize_filename(filename: str) -> str:
    """
    Очистка имени файла от опасных символов
    """
    # Удаляем опасные символы
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Удаляем пробелы в начале и конце
    filename = filename.strip()
    # Ограничиваем длину
    if len(filename) > 255:
        filename = filename[:255]
    return filename


def validate_campaign_data(campaign_data: dict) -> Tuple[bool, Optional[str]]:
    """
    Валидация данных кампании
    Возвращает (is_valid, error_message)
    """
    try:
        # Проверка названия
        title = campaign_data.get('title', '')
        if not title or len(title) > 200:
            return False, "Название кампании не может быть пустым или длиннее 200 символов"
        
        # Проверка задержек
        min_delay = campaign_data.get('min_delay', 0)
        max_delay = campaign_data.get('max_delay', 0)
        if min_delay < 0 or max_delay < 0:
            return False, "Задержки не могут быть отрицательными"
        if min_delay > max_delay:
            return False, "Минимальная задержка не может быть больше максимальной"
        if max_delay > 3600:  # 1 час
            return False, "Максимальная задержка не может быть больше 1 часа"
        
        # Проверка времени работы
        duration = campaign_data.get('duration_minutes')
        if duration is not None and duration != -1:
            if duration < 0:
                return False, "Время работы не может быть отрицательным"
            if duration > 10080:  # 7 дней
                return False, "Время работы не может быть больше 7 дней"
        
        # Проверка крупного delay
        big_delay = campaign_data.get('big_delay_minutes')
        if big_delay is not None:
            if big_delay < 0:
                return False, "Крупный delay не может быть отрицательным"
            if big_delay > 10080:  # 7 дней
                return False, "Крупный delay не может быть больше 7 дней"
        
        return True, None
        
    except Exception as e:
        logger.error(f"Ошибка валидации данных кампании: {e}")
        return False, f"Ошибка валидации: {e}"


def validate_chat_identifier(chat_id: str) -> Tuple[bool, Optional[str]]:
    """
    Валидация идентификатора чата
    Возвращает (is_valid, error_message)
    """
    if not chat_id or not chat_id.strip():
        return False, "Идентификатор чата не может быть пустым"
    
    # Проверка длины
    if len(chat_id) > 200:
        return False, "Идентификатор чата слишком длинный"
    
    # Проверка на подозрительные символы
    if re.search(r'[<>"\']', chat_id):
        return False, "Идентификатор чата содержит недопустимые символы"
    
    return True, None

