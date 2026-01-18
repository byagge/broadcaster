import asyncio
import random
import yaml
import os
import logging
from typing import Optional, Callable, Dict, Any, Tuple, List
from datetime import datetime, timedelta
from telethon import TelegramClient, errors
from telethon.tl.types import ChatBannedRights
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import FloodWaitError, ChatAdminRequiredError, UserNotParticipantError, InviteHashExpiredError

from models import Campaign, Account, parse_proxy

# Настройка логирования
def setup_logger(campaign_id=None):
    """Настройка логгера с возможностью отдельного лога для кампании"""
    logger = logging.getLogger(f"sender_{campaign_id}" if campaign_id else "sender")
    logger.setLevel(logging.INFO)
    
    # Удаляем старые обработчики
    logger.handlers.clear()
    
    handlers = [
        logging.FileHandler('sender.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
    
    # Добавляем отдельный лог для кампании
    if campaign_id:
        handlers.append(logging.FileHandler(f'campaign_{campaign_id}.log', encoding='utf-8'))
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    
    return logger

logger = setup_logger()

CONFIG_PATH = 'config.yaml'
CHATS_PATH = 'chats.txt'

class TelegramSender:
    """
    Базовый отправщик, который можно использовать как с config.yaml,
    так и с объектом Campaign (для управления из бота).
    """

    def __init__(
        self,
        stop_flag: Optional[Callable[[], bool]] = None,
        campaign: Optional[Campaign] = None,
        account: Optional[Account] = None,
        chats_path: Optional[str] = None,
    ):
        self.stop_flag = stop_flag
        self.client: Optional[TelegramClient] = None
        self.config: Optional[Dict[str, Any]] = None
        self.chats = []
        self.stats = {
            'sent': 0,
            'failed': 0,
            'skipped': 0,
            'joined': 0
        }
        self.campaign = campaign
        self.account = account
        self.chats_path = chats_path or CHATS_PATH
        
        # Настраиваем логгер для кампании
        if campaign:
            self.logger = setup_logger(campaign.id)
        else:
            self.logger = logger
    
    async def load_config(self):
        """
        Загрузка конфигурации.
        Если передана campaign, часть настроек берётся из неё.
        """
        if self.campaign and self.account:
            # Конфиг собираем из campaign/account
            self.config = {
                "api_id": self.account.api_id,
                "api_hash": self.account.api_hash,
                "session_name": self.account.session_name,
                "message": self.campaign.message_text or "",
                "parse_mode": "md",
                "disable_link_preview": True,
                "min_delay": self.campaign.min_delay,
                "max_delay": self.campaign.max_delay,
                "typing_delay": self.campaign.typing_delay,
                "typing_min": self.campaign.typing_min,
                "typing_max": self.campaign.typing_max,
            }
            self.logger.info("Конфигурация загружена из Campaign/Account")
            return

        # Старый режим через config.yaml
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
            self.logger.info("Конфигурация загружена из config.yaml")
        except Exception as e:
            self.logger.error(f"Ошибка загрузки конфига: {e}")
            raise
    
    async def load_chats(self):
        """
        Загрузка списка чатов.
        Поддержка формата:
        chat_or_link
        chat_or_link - доп_текст
        """
        try:
            self.logger.info(f"[SENDER] Загрузка чатов из файла: {self.chats_path}")
            if not os.path.exists(self.chats_path):
                self.logger.error(f"[SENDER] Файл {self.chats_path} не найден!")
                return []

            result = []
            with open(self.chats_path, 'r', encoding='utf-8') as f:
                for line_num, raw in enumerate(f, 1):
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue
                    # Разделяем по '-' только первый раз
                    if ' - ' in line:
                        chat, extra = line.split(' - ', 1)
                        result.append((chat.strip(), extra.strip()))
                        self.logger.info(f"[SENDER] Загружен чат: {chat.strip()} (доп. текст: {extra.strip()})")
                    else:
                        result.append((line, ""))
                        self.logger.info(f"[SENDER] Загружен чат: {line}")

            self.chats = result
            self.logger.info(f"[SENDER] Всего загружено {len(self.chats)} чатов")
            if len(self.chats) == 0:
                self.logger.warning(f"[SENDER] ВНИМАНИЕ: Не загружено ни одного чата!")
        except Exception as e:
            self.logger.error(f"[SENDER] Ошибка загрузки чатов: {e}", exc_info=True)
            return []
    
    async def connect(self):
        """Подключение к Telegram"""
        try:
            api_id = self.config['api_id']
            api_hash = self.config['api_hash']
            session_name = self.config['session_name']

            proxy = None
            if self.account and self.account.proxy:
                # Если proxy - строка, парсим её
                if isinstance(self.account.proxy, str):
                    proxy = parse_proxy(self.account.proxy)
                elif isinstance(self.account.proxy, dict):
                    proxy = self.account.proxy

            self.client = TelegramClient(session_name, api_id, api_hash, proxy=proxy)
            await self.client.start()

            me = await self.client.get_me()
            self.logger.info(f"Подключен как: {me.first_name} (@{me.username})")

        except Exception as e:
            self.logger.error(f"Ошибка подключения: {e}")
            raise
    
    async def check_chat_access(self, chat):
        """Проверка доступа к чату и вступление, если нужно"""
        try:
            entity = await self.client.get_entity(chat)
            
            # Проверяем, что это группа/канал
            if hasattr(entity, 'megagroup') or hasattr(entity, 'broadcast'):
                # Проверяем, участник ли мы
                try:
                    await self.client.get_participants(entity, limit=1)
                    return entity
                except (UserNotParticipantError, ValueError):
                    # Не участник, пытаемся вступить
                    self.logger.info(f"{chat}: Не участник, пытаемся вступить...")
                    try:
                        if hasattr(entity, 'username') and entity.username:
                            await self.client(JoinChannelRequest(entity))
                        elif 't.me/joinchat/' in str(chat) or 't.me/+' in str(chat):
                            # Приватная ссылка
                            invite_hash = str(chat).split('/')[-1]
                            await self.client(ImportChatInviteRequest(invite_hash))
                        else:
                            self.logger.warning(f"{chat}: Не удалось вступить (нет публичной ссылки)")
                            return None
                        
                        # Задержка после вступления
                        join_delay = random.uniform(3, 10)
                        await asyncio.sleep(join_delay)
                        self.logger.info(f"{chat}: Успешно вступили, ждали {join_delay:.1f} сек")
                        self.stats['joined'] = self.stats.get('joined', 0) + 1
                        return entity
                    except InviteHashExpiredError:
                        self.logger.warning(f"{chat}: Ссылка-приглашение истекла")
                        return None
                    except Exception as e:
                        self.logger.warning(f"{chat}: Ошибка вступления: {e}")
                        return None
            else:
                self.logger.warning(f"{chat}: Не группа/канал, пропускаем")
                return None
                
        except UserNotParticipantError:
            self.logger.warning(f"{chat}: Не участник группы")
            return None
        except Exception as e:
            self.logger.warning(f"{chat}: Ошибка доступа - {e}")
            return None
    
    async def _resolve_source_message(self) -> Optional[Any]:
        """
        Если указан source_link в campaign, пытаемся получить оригинальный пост.
        Иначе возвращаем None (будет использоваться обычный текст).
        """
        if not self.campaign or not self.campaign.source_link:
            return None

        try:
            # Парсим ссылку вида https://t.me/channel/123
            # или https://t.me/c/channel_id/123
            link = self.campaign.source_link
            if '/c/' in link:
                # Формат: https://t.me/c/1234567890/123
                parts = link.split('/c/')[1].split('/')
                channel_id = int(parts[0])
                msg_id = int(parts[1]) if len(parts) > 1 else None
                entity = await self.client.get_entity(channel_id)
                if msg_id:
                    msg = await self.client.get_messages(entity, ids=msg_id)
                else:
                    msgs = await self.client.get_messages(entity, limit=1)
                    msg = msgs[0] if msgs else None
            else:
                # Формат: https://t.me/channel/123
                parts = link.rstrip('/').split('/')
                channel_username = parts[-2] if len(parts) > 1 else None
                msg_id = int(parts[-1]) if parts[-1].isdigit() else None
                
                if channel_username:
                    entity = await self.client.get_entity(channel_username)
                    if msg_id:
                        msg = await self.client.get_messages(entity, ids=msg_id)
                    else:
                        msgs = await self.client.get_messages(entity, limit=1)
                        msg = msgs[0] if msgs else None
                else:
                    # Пробуем напрямую
                    msgs = await self.client.get_messages(link, limit=1)
                    msg = msgs[0] if msgs else None
            
            return msg
        except Exception as e:
            self.logger.error(f"Не удалось получить сообщение по ссылке {self.campaign.source_link}: {e}")
            return None

    async def send_message(self, chat_entity, extra_text: str = ""):
        """Отправка сообщения с имитацией человека и доп. текстом для чата"""
        try:
            parse_mode = self.config.get('parse_mode', 'md')
            disable_preview = self.config.get('disable_link_preview', True)

            # Имитация набора текста
            if self.config.get('typing_delay', True):
                typing_min = self.config.get('typing_min', 2)
                typing_max = self.config.get('typing_max', 5)
                typing_time = random.uniform(typing_min, typing_max)

                async with self.client.action(chat_entity, 'typing'):
                    await asyncio.sleep(typing_time)

            # Если нужно переслать пост
            if self.campaign and self.campaign.use_forward and self.campaign.source_link:
                source_msg = await self._resolve_source_message()
                if source_msg:
                    # Пересылаем пост (forward)
                    await self.client.forward_messages(
                        entity=chat_entity,
                        messages=source_msg
                    )
                else:
                    # Не удалось получить сообщение по ссылке
                    raise Exception(f"Не удалось получить сообщение по ссылке {self.campaign.source_link}")
            elif self.campaign and self.campaign.source_link:
                # Отправляем пост как обычное сообщение (копируем)
                source_msg = await self._resolve_source_message()
                if source_msg:
                    await self.client.send_message(
                        entity=chat_entity,
                        message=source_msg
                    )
                else:
                    # Не удалось получить, пробуем отправить текст
                    base_message = self.config.get('message', '')
                    if not base_message:
                        raise Exception(f"Не удалось получить сообщение по ссылке и нет текста для отправки")
                    if extra_text:
                        message = f"{base_message}\n{extra_text}"
                    else:
                        message = base_message
                    await self.client.send_message(
                        chat_entity,
                        message,
                        parse_mode=parse_mode,
                        link_preview=not disable_preview
                    )
            else:
                # Обычный текст
                base_message = self.config.get('message', '')
                if not base_message:
                    raise Exception("Не указан текст сообщения для отправки")
                if extra_text:
                    message = f"{base_message}\n{extra_text}"
                else:
                    message = base_message
                
                await self.client.send_message(
                    chat_entity,
                    message,
                    parse_mode=parse_mode,
                    link_preview=not disable_preview
                )

            self.stats['sent'] += 1
            self.logger.info(f"[OK] Отправлено в {getattr(chat_entity, 'title', None) or getattr(chat_entity, 'username', chat_entity)}")

        except FloodWaitError as e:
            wait_time = e.seconds
            self.logger.warning(f"[WAIT] FloodWait: ждём {wait_time} секунд")
            await asyncio.sleep(wait_time + 5)
            self.stats['skipped'] += 1

        except ChatAdminRequiredError:
            title = getattr(chat_entity, 'title', None) or getattr(chat_entity, 'username', chat_entity)
            self.logger.warning(f"[ERROR] {title}: Нет прав администратора")
            self.stats['failed'] += 1

        except Exception as e:
            title = getattr(chat_entity, 'title', None) or getattr(chat_entity, 'username', chat_entity)
            self.logger.error(f"[ERROR] Ошибка отправки в {title}: {e}")
            self.stats['failed'] += 1
    
    async def run(self):
        """Основной цикл рассылки с поддержкой бесконечного режима и ограничения по времени"""
        try:
            self.logger.info("=" * 50)
            self.logger.info("[SENDER] Начало выполнения рассылки")
            if self.campaign:
                self.logger.info(f"[SENDER] Campaign ID: {self.campaign.id}")
                self.logger.info(f"[SENDER] Campaign title: {self.campaign.title}")
                self.logger.info(f"[SENDER] Account IDs: {self.campaign.account_ids}")
                self.logger.info(f"[SENDER] Chats file: {self.chats_path}")
                self.logger.info(f"[SENDER] Message text: {bool(self.campaign.message_text)}")
                self.logger.info(f"[SENDER] Source link: {self.campaign.source_link}")
                self.logger.info(f"[SENDER] Use forward: {self.campaign.use_forward}")
            if self.account:
                self.logger.info(f"[SENDER] Account: {self.account.name} ({self.account.session_name})")
            self.logger.info("=" * 50)
            
            await self.load_config()
            await self.load_chats()
            await self.connect()

            if not self.chats:
                self.logger.error("Нет чатов для рассылки!")
                return

            # Определяем режим работы
            duration_minutes = None
            if self.campaign:
                duration_minutes = self.campaign.duration_minutes
            
            infinite_mode = duration_minutes is None or duration_minutes == -1
            end_time = None
            if not infinite_mode and duration_minutes > 0:
                end_time = datetime.now() + timedelta(minutes=duration_minutes)
                self.logger.info(f"[TIME] Режим ограничен по времени: {duration_minutes} минут, до {end_time}")
            else:
                self.logger.info("[TIME] Режим бесконечной рассылки (по кругу)")

            self.logger.info(f"[START] Начинаем рассылку в {len(self.chats)} чатов")
            iteration = 0

            while True:
                # Проверяем флаг остановки
                if self.stop_flag and self.stop_flag():
                    self.logger.info("[STOP] Рассылка остановлена пользователем")
                    break
                
                # Проверяем время окончания
                if end_time and datetime.now() >= end_time:
                    self.logger.info(f"[TIME] Время работы истекло ({duration_minutes} минут)")
                    break

                iteration += 1
                self.logger.info(f"[ITERATION {iteration}] Начинаем новый цикл по чатам")

                for i, (chat, extra_text) in enumerate(self.chats, 1):
                    # Проверяем флаг остановки
                    if self.stop_flag and self.stop_flag():
                        self.logger.info("[STOP] Рассылка остановлена пользователем")
                        break
                    
                    # Проверяем время окончания
                    if end_time and datetime.now() >= end_time:
                        self.logger.info(f"[TIME] Время работы истекло")
                        break
                    
                    self.logger.info(f"[{i}/{len(self.chats)}] Обрабатываем: {chat}")
                    
                    # Проверяем доступ к чату
                    chat_entity = await self.check_chat_access(chat)
                    if not chat_entity:
                        self.stats['skipped'] += 1
                        continue

                    # Отправляем сообщение
                    await self.send_message(chat_entity, extra_text=extra_text)

                    # Задержка между чатами
                    min_delay = self.config['min_delay']
                    max_delay = self.config['max_delay']
                    delay = random.uniform(min_delay, max_delay)
                    
                    self.logger.info(f"[DELAY] Ждём {delay:.1f} секунд...")
                    await asyncio.sleep(delay)

                # Если не бесконечный режим, выходим после первого прохода
                if not infinite_mode:
                    break
                
                # В бесконечном режиме делаем паузу перед новым циклом
                big_delay = None
                if self.campaign and self.campaign.big_delay_minutes:
                    big_delay = self.campaign.big_delay_minutes * 60  # Конвертируем минуты в секунды
                
                if big_delay and big_delay > 0:
                    self.logger.info(f"[LOOP] Завершили цикл, ждём {big_delay/60:.1f} минут перед новым циклом...")
                    await asyncio.sleep(big_delay)
                else:
                    self.logger.info("[LOOP] Завершили цикл, начинаем новый через 10 секунд...")
                    await asyncio.sleep(10)
            
            # Финальная статистика
            self.logger.info("[FINISH] Рассылка завершена!")
            self.logger.info(f"[STATS] Отправлено: {self.stats['sent']}")
            self.logger.info(f"[STATS] Ошибок: {self.stats['failed']}")
            self.logger.info(f"[STATS] Пропущено: {self.stats['skipped']}")
            self.logger.info(f"[STATS] Вступили в чаты: {self.stats.get('joined', 0)}")
            
            # Сохраняем статистику в кампанию, если она есть
            if self.campaign:
                try:
                    import json
                    campaigns_file = os.path.join("data", "campaigns.json")
                    if os.path.exists(campaigns_file):
                        with open(campaigns_file, "r", encoding="utf-8") as f:
                            campaigns = json.load(f)
                        if self.campaign.id in campaigns:
                            campaigns[self.campaign.id]["stats"] = self.stats
                            campaigns[self.campaign.id]["end_time"] = datetime.now().isoformat()
                            with open(campaigns_file, "w", encoding="utf-8") as f:
                                json.dump(campaigns, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    self.logger.error(f"Ошибка сохранения статистики: {e}")
            
        except Exception as e:
            self.logger.error(f"Критическая ошибка: {e}")
        finally:
            if self.client:
                await self.client.disconnect()
                self.logger.info("Отключен от Telegram")

async def main(stop_flag=None):
    """Главная функция"""
    sender = TelegramSender(stop_flag)
    await sender.run()

if __name__ == '__main__':
    asyncio.run(main()) 