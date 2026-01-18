import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime


@dataclass
class Account:
    id: str
    name: str
    session_name: str
    api_id: int
    api_hash: str
    proxy: Optional[Dict[str, Any]] = None


@dataclass
class Campaign:
    id: str
    title: str
    account_ids: List[str] = field(default_factory=list)  # Список ID аккаунтов
    chats_file: str = "chats.txt"
    message_text: Optional[str] = None
    source_link: Optional[str] = None
    use_forward: bool = False  # Пересылать пост или отправлять текст
    min_delay: float = 30.0
    max_delay: float = 60.0
    typing_min: float = 2.0
    typing_max: float = 5.0
    typing_delay: bool = True
    duration_minutes: Optional[int] = None  # None или -1 = бесконечная, иначе минуты
    big_delay_minutes: Optional[float] = None  # Крупный delay между циклами (в минутах), None = без задержки
    status: str = "idle"  # idle, running, stopped, finished, error
    stats: Dict[str, int] = field(default_factory=lambda: {
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "joined": 0,  # Сколько чатов вступили
    })
    start_time: Optional[str] = None  # ISO формат времени старта
    end_time: Optional[str] = None  # ISO формат времени окончания


def account_dict(account: Account) -> dict:
    return asdict(account)


def campaign_dict(campaign: Campaign) -> dict:
    return asdict(campaign)


def new_account_id() -> str:
    return str(uuid.uuid4())


def new_campaign_id() -> str:
    return str(uuid.uuid4())


def parse_proxy(proxy_str: str) -> Optional[Dict[str, Any]]:
    """
    Парсит прокси из формата login:password@ip:port
    Возвращает словарь для Telethon или None
    """
    if not proxy_str or not proxy_str.strip():
        return None
    
    try:
        # Формат: login:password@ip:port
        if '@' in proxy_str:
            auth_part, addr_part = proxy_str.split('@', 1)
            if ':' in auth_part:
                login, password = auth_part.split(':', 1)
            else:
                login, password = auth_part, ""
            
            if ':' in addr_part:
                ip, port = addr_part.rsplit(':', 1)
                port = int(port)
            else:
                ip, port = addr_part, 1080
        else:
            # Просто ip:port без авторизации
            if ':' in proxy_str:
                ip, port = proxy_str.rsplit(':', 1)
                port = int(port)
                login, password = "", ""
            else:
                return None
        
        return {
            "proxy_type": "socks5",  # По умолчанию SOCKS5
            "addr": ip.strip(),
            "port": port,
            "username": login.strip() if login else None,
            "password": password.strip() if password else None,
        }
    except Exception as e:
        print(f"Ошибка парсинга прокси {proxy_str}: {e}")
        return None


