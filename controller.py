import telebot
import asyncio
import threading
import json
import os
import re
import logging
import shutil
from datetime import datetime
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

# Настройка логирования для бота
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from models import Account, Campaign, new_account_id, new_campaign_id, account_dict, campaign_dict, parse_proxy
from sender import TelegramSender
from security import (
    validate_filename, validate_file_path, check_session_file, check_txt_file,
    validate_api_credentials, validate_proxy_string, sanitize_filename,
    validate_campaign_data, validate_chat_identifier
)

# === Настройки бота ===
API_TOKEN = "8558704873:AAFux8GDq-q3dDM_VUJnGKS0zp-Xv7lah-s"
ADMIN_ID = 7895708340

# === Константы безопасности ===
MAX_MESSAGE_LENGTH = 4096  # Максимальная длина сообщения Telegram
MAX_TITLE_LENGTH = 200  # Максимальная длина названия кампании
MAX_FILENAME_LENGTH = 255  # Максимальная длина имени файла

# === Пути и директории ===
DATA_DIR = "data"
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
CAMPAIGNS_FILE = os.path.join(DATA_DIR, "campaigns.json")

# Создаём директорию для данных
os.makedirs(DATA_DIR, exist_ok=True)

bot = telebot.TeleBot(API_TOKEN, parse_mode="Markdown")

# Состояния и запущенные рассылки
user_states = {}  # {user_id: {"state": str, "data": dict}}
running_campaigns = {}  # {campaign_id: {"threads": list, "stop_flag": dict}}
auth_sessions = {}  # {user_id: {"client": TelegramClient, "phone": str, "api_id": int, "api_hash": str}}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def load_json(path: str, default):
    """
    Безопасная загрузка JSON файла
    """
    # Валидация пути
    is_valid, error_msg = validate_file_path(path)
    if not is_valid:
        logger.error(f"[SECURITY] Попытка загрузить файл по небезопасному пути: {path} - {error_msg}")
        return default
    
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"[SECURITY] Ошибка парсинга JSON файла {path}: {e}")
        return default
    except Exception as e:
        logger.error(f"[SECURITY] Ошибка чтения файла {path}: {e}")
        return default


def save_json(path: str, data):
    """
    Безопасное сохранение JSON файла
    """
    # Валидация пути
    is_valid, error_msg = validate_file_path(path)
    if not is_valid:
        logger.error(f"[SECURITY] Попытка сохранить файл по небезопасному пути: {path} - {error_msg}")
        raise ValueError(f"Небезопасный путь: {error_msg}")
    
    try:
        # Создаём директорию если её нет
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        
        # Сохраняем во временный файл, затем переименовываем (атомарная операция)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # Атомарное переименование
        if os.path.exists(path):
            os.replace(temp_path, path)
        else:
            os.rename(temp_path, path)
            
    except Exception as e:
        logger.error(f"[SECURITY] Ошибка сохранения файла {path}: {e}", exc_info=True)
        # Удаляем временный файл при ошибке
        temp_path = f"{path}.tmp"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass
        raise


def load_accounts() -> dict:
    return load_json(ACCOUNTS_FILE, {})


def save_accounts(data: dict):
    save_json(ACCOUNTS_FILE, data)


def load_campaigns() -> dict:
    return load_json(CAMPAIGNS_FILE, {})


def save_campaigns(data: dict):
    save_json(CAMPAIGNS_FILE, data)


def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📨 Рассылки"), KeyboardButton("👥 Аккаунты"))
    kb.add(KeyboardButton("📊 Логи и отчёты"))
    return kb


def back_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("⬅️ Назад"))
    return kb


def campaigns_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("➕ Создать рассылку"), KeyboardButton("📃 Список рассылок"))
    kb.add(KeyboardButton("⬅️ Назад"))
    return kb


def accounts_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("➕ Добавить .session"), KeyboardButton("➕ Добавить (логин)"))
    kb.add(KeyboardButton("📃 Список аккаунтов"), KeyboardButton("⬅️ Назад"))
    return kb


def campaign_actions_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🚀 Запустить"), KeyboardButton("⛔ Остановить"))
    kb.add(KeyboardButton("📊 Статистика"), KeyboardButton("📁 Логи"))
    kb.add(KeyboardButton("🗑 Удалить"), KeyboardButton("⬅️ Назад"))
    return kb


def account_actions_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("⚙️ Прокси"), KeyboardButton("🔑 API ключи"))
    kb.add(KeyboardButton("🗑 Удалить"), KeyboardButton("⬅️ Назад"))
    return kb


def yes_no_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("✅ Да"), KeyboardButton("❌ Нет"))
    return kb


def message_type_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("📝 Текст сообщения"))
    kb.add(KeyboardButton("📤 Пост с канала (пересылка)"))
    kb.add(KeyboardButton("⬅️ Назад"))
    return kb


def format_campaign_brief(cid: str, c: dict) -> str:
    status_emoji = {
        "idle": "⚪",
        "running": "🟢",
        "stopped": "⛔",
        "finished": "✅",
        "error": "❌"
    }
    emoji = status_emoji.get(c.get('status', 'idle'), '⚪')
    return f"{emoji} {cid[:8]} • {c.get('title','Без названия')}"


def set_state(user_id: int, state: str, data: dict = None):
    user_states[user_id] = {"state": state, "data": data or {}}


def get_state(user_id: int):
    return user_states.get(user_id, {"state": None, "data": {}})


def clear_state(user_id: int):
    user_states.pop(user_id, None)


def start_campaign_thread(campaign_id: str):
    """Запуск кампании - каждый аккаунт в отдельном потоке."""
    try:
        logger.info("=" * 50)
        logger.info(f"[BOT] ===== START_CAMPAIGN_THREAD ВЫЗВАН =====")
        logger.info(f"[BOT] Campaign ID: {campaign_id}")
        logger.info(f"[BOT] Запуск рассылки {campaign_id}")
        campaigns = load_campaigns()
        logger.info(f"[BOT] Загружено кампаний: {len(campaigns)}")
        accounts = load_accounts()
        logger.info(f"[BOT] Загружено аккаунтов: {len(accounts)}")
        c = campaigns.get(campaign_id)
        if not c:
            logger.error(f"[BOT] Рассылка {campaign_id} не найдена")
            return
        logger.info(f"[BOT] Найдена кампания: {c.get('title', 'Без названия')}")

        account_ids = c.get("account_ids", [])
        if not account_ids:
            logger.error(f"[BOT] Нет аккаунтов для рассылки {campaign_id}")
            return

        logger.info(f"[BOT] Найдено {len(account_ids)} аккаунтов для рассылки")

        stop_flag = {"value": False}
        threads = []

        def run_sender(account_id: str):
            """Запуск отправщика для одного аккаунта"""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                logger.info(f"[BOT] Запуск отправщика для аккаунта {account_id}")
                acc = accounts.get(account_id)
                if not acc:
                    logger.error(f"[BOT] Аккаунт {account_id} не найден")
                    return
                
                logger.info(f"[BOT] Создание объектов Campaign и Account")
                campaign_obj = Campaign(**c)
                account_obj = Account(**acc)
                
                chats_file = c.get("chats_file", "chats.txt")
                logger.info(f"[BOT] Используется файл чатов: {chats_file}")
                
                sender = TelegramSender(
                    stop_flag=lambda: stop_flag["value"],
                    campaign=campaign_obj,
                    account=account_obj,
                    chats_path=chats_file,
                )
                logger.info(f"[BOT] Запуск рассылки для аккаунта {account_id}")
                loop.run_until_complete(sender.run())
                logger.info(f"[BOT] Рассылка завершена для аккаунта {account_id}")
                
                # Обновляем статистику в кампании
                campaigns = load_campaigns()
                if campaign_id in campaigns:
                    # Объединяем статистику (если несколько аккаунтов)
                    current_stats = campaigns[campaign_id].get("stats", {})
                    sender_stats = sender.stats
                    for key in ["sent", "failed", "skipped", "joined"]:
                        current_stats[key] = current_stats.get(key, 0) + sender_stats.get(key, 0)
                    campaigns[campaign_id]["stats"] = current_stats
                    save_campaigns(campaigns)
            except Exception as e:
                logger.error(f"[BOT] Ошибка в аккаунте {account_id}: {e}", exc_info=True)
                # Обновляем статус на error
                campaigns = load_campaigns()
                if campaign_id in campaigns:
                    campaigns[campaign_id]["status"] = "error"
                    campaigns[campaign_id]["error"] = str(e)
                    save_campaigns(campaigns)
            finally:
                loop.close()
                
                # Проверяем, все ли потоки завершились
                all_done = True
                if campaign_id in running_campaigns:
                    threads = running_campaigns[campaign_id].get("threads", [])
                    if any(t.is_alive() for t in threads):
                        all_done = False
                
                if all_done:
                    campaigns = load_campaigns()
                    if campaign_id in campaigns and campaigns[campaign_id].get("status") == "running":
                        campaigns[campaign_id]["status"] = "finished"
                        campaigns[campaign_id]["end_time"] = datetime.now().isoformat()
                        save_campaigns(campaigns)
                        logger.info(f"[BOT] Рассылка {campaign_id} полностью завершена")

        # Запускаем отдельный поток для каждого аккаунта
        for account_id in account_ids:
            logger.info(f"[BOT] Создание потока для аккаунта {account_id}")
            t = threading.Thread(target=run_sender, args=(account_id,), daemon=True)
            threads.append(t)
            logger.info(f"[BOT] Запуск потока для аккаунта {account_id}")
            t.start()
            logger.info(f"[BOT] Поток запущен для аккаунта {account_id}, активен: {t.is_alive()}")

        running_campaigns[campaign_id] = {"threads": threads, "stop_flag": stop_flag}
        campaigns[campaign_id]["status"] = "running"
        campaigns[campaign_id]["start_time"] = datetime.now().isoformat()
        save_campaigns(campaigns)
        logger.info(f"[BOT] Рассылка {campaign_id} запущена, статус обновлён")
        logger.info(f"[BOT] Всего потоков: {len(threads)}, активных: {sum(1 for t in threads if t.is_alive())}")
    except Exception as e:
        logger.error(f"[BOT] Критическая ошибка при запуске рассылки {campaign_id}: {e}", exc_info=True)


def stop_campaign(campaign_id: str):
    info = running_campaigns.get(campaign_id)
    if not info:
        return
    info["stop_flag"]["value"] = True
    campaigns = load_campaigns()
    if campaign_id in campaigns:
        campaigns[campaign_id]["status"] = "stopped"
        save_campaigns(campaigns)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Нет доступа.")
        return
    clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🤖 *Панель управления рассылками*\n\nВыберите действие:",
        reply_markup=main_menu_kb(),
    )


@bot.message_handler(content_types=["text", "document"])
def handle_all_messages(message):
    if not is_admin(message.from_user.id):
        return

    # Логируем все входящие сообщения
    logger.info(f"[BOT] Получено сообщение: {message.text if message.text else 'document'} от {message.from_user.id}")

    # Обработка документов
    if message.content_type == "document":
        state = get_state(message.from_user.id)
        st = state["state"]
        data = state["data"]
        
        # Обработка файла chats.txt при создании рассылки
        if st == "campaign_create_chats_file":
            doc = message.document
            if doc.file_name.endswith('.txt'):
                # Валидация имени файла
                is_valid, error_msg = validate_filename(doc.file_name)
                if not is_valid:
                    logger.warning(f"[SECURITY] Попытка загрузки .txt файла с невалидным именем: {doc.file_name}")
                    bot.reply_to(message, f"❌ Ошибка безопасности: {error_msg}")
                    return
                
                try:
                    file_info = bot.get_file(doc.file_id)
                    downloaded = bot.download_file(file_info.file_path)
                    
                    # Проверка размера файла
                    if len(downloaded) == 0:
                        bot.reply_to(message, "❌ Файл пустой")
                        return
                    
                    # Проверка .txt файла на безопасность
                    is_safe, error_msg = check_txt_file(doc.file_name, downloaded)
                    if not is_safe:
                        logger.warning(f"[SECURITY] Обнаружен небезопасный .txt файл: {doc.file_name} - {error_msg}")
                        bot.reply_to(message, f"❌ Файл не прошёл проверку безопасности:\n`{error_msg}`")
                        return
                    
                    # Безопасное имя файла
                    chats_file = sanitize_filename(doc.file_name)
                    
                    # Валидация пути
                    is_valid_path, path_error = validate_file_path(chats_file)
                    if not is_valid_path:
                        logger.warning(f"[SECURITY] Небезопасный путь к файлу: {chats_file}")
                        bot.reply_to(message, f"❌ Ошибка безопасности: {path_error}")
                        return
                    
                    # Сохраняем файл
                    with open(chats_file, "wb") as f:
                        f.write(downloaded)
                    
                    logger.info(f"[SECURITY] Безопасный .txt файл загружен: {chats_file}")
                    
                    cid = data.get("campaign_id")
                    campaigns = load_campaigns()
                    if cid in campaigns:
                        campaigns[cid]["chats_file"] = chats_file
                        save_campaigns(campaigns)
                    
                    data["chats_file"] = chats_file
                    set_state(message.from_user.id, "campaign_create_min_delay", data)
                    bot.reply_to(message, f"✅ Файл `{chats_file}` сохранён!\n\n⏱ *Шаг 5/8*\nВведите минимальную задержку между отправками (в секундах, например: `30`):", reply_markup=back_kb())
                except Exception as e:
                    logger.error(f"[SECURITY] Ошибка при обработке .txt файла: {e}", exc_info=True)
                    bot.reply_to(message, f"❌ Ошибка при обработке файла: {e}")
                return
            else:
                bot.reply_to(message, "❌ Нужен файл .txt")
                return
        
        # Обработка .session файла при добавлении аккаунта
        if st == "account_add_session_name":
            doc = message.document
            if not doc.file_name.endswith('.session'):
                bot.reply_to(message, "❌ Нужен файл .session")
                return
            
            # Валидация имени файла
            is_valid, error_msg = validate_filename(doc.file_name)
            if not is_valid:
                logger.warning(f"[SECURITY] Попытка загрузки файла с невалидным именем: {doc.file_name}")
                bot.reply_to(message, f"❌ Ошибка безопасности: {error_msg}")
                return
            
            try:
                file_info = bot.get_file(doc.file_id)
                downloaded = bot.download_file(file_info.file_path)
                
                # Проверка размера файла перед проверкой безопасности
                if len(downloaded) == 0:
                    bot.reply_to(message, "❌ Файл пустой")
                    return
                
                # Проверка .session файла на безопасность
                is_safe, error_msg = check_session_file(doc.file_name, downloaded)
                if not is_safe:
                    logger.warning(f"[SECURITY] Обнаружен небезопасный .session файл: {doc.file_name} - {error_msg}")
                    bot.reply_to(message, f"❌ Файл не прошёл проверку безопасности:\n`{error_msg}`\n\n⚠️ Файл может содержать вредоносный код!")
                    return
                
                # Безопасное имя файла
                session_filename = sanitize_filename(doc.file_name)
                
                # Валидация пути
                is_valid_path, path_error = validate_file_path(session_filename)
                if not is_valid_path:
                    logger.warning(f"[SECURITY] Небезопасный путь к файлу: {session_filename}")
                    bot.reply_to(message, f"❌ Ошибка безопасности: {path_error}")
                    return
                
                # Сохранение файла
                with open(session_filename, "wb") as f:
                    f.write(downloaded)
                
                logger.info(f"[SECURITY] Безопасный .session файл загружен: {session_filename}")
                
                name = message.caption.strip() if message.caption else session_filename.replace('.session', '')
                # Очистка имени от опасных символов
                name = sanitize_filename(name) if name else session_filename.replace('.session', '')
                
                accounts = load_accounts()
                aid = new_account_id()
                accounts[aid] = account_dict(
                    Account(
                        id=aid,
                        name=name,
                        session_name=session_filename,
                        api_id=0,
                        api_hash="",
                        proxy=None,
                    )
                )
                save_accounts(accounts)
                clear_state(message.from_user.id)
                bot.reply_to(
                    message,
                    f"✅ Аккаунт сохранён!\n\n"
                    f"ID: `{aid[:8]}`\n"
                    f"Название: `{name}`\n"
                    f"Session: `{session_filename}`\n\n"
                    f"⚠️ Не забудьте указать API ID и API Hash в настройках аккаунта!\n\n"
                    f"💡 Отправьте ID аккаунта чтобы открыть его",
                    reply_markup=accounts_menu_kb()
                )
            except Exception as e:
                logger.error(f"[SECURITY] Ошибка при обработке .session файла: {e}", exc_info=True)
                bot.reply_to(message, f"❌ Ошибка при обработке файла: {e}")
            return
        
        # Если документ отправлен в неподходящий момент
        bot.reply_to(message, "❌ Файл не может быть обработан в данный момент")
        return

    # Обработка текстовых сообщений
    text = message.text
    if not text:
        return
    
    state = get_state(message.from_user.id)
    st = state["state"]
    data = state["data"]

    # === Главное меню ===
    if text == "📨 Рассылки":
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "📨 *Управление рассылками:*", reply_markup=campaigns_menu_kb())
        return

    if text == "👥 Аккаунты":
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "👥 *Управление аккаунтами:*", reply_markup=accounts_menu_kb())
        return

    if text == "📊 Логи и отчёты":
        if os.path.exists("sender.log"):
            try:
                with open("sender.log", "rb") as f:
                    bot.send_document(message.chat.id, f, caption="📋 Общий лог `sender.log`")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
        else:
            bot.reply_to(message, "📋 Логов пока нет")
        return

    if text == "⬅️ Назад":
        clear_state(message.from_user.id)
        bot.send_message(message.chat.id, "🤖 *Главное меню*", reply_markup=main_menu_kb())
        return

    # === Рассылки ===
    if text == "➕ Создать рассылку":
        set_state(message.from_user.id, "campaign_create_title", {})
        bot.send_message(message.chat.id, "📝 *Шаг 1/8*\nВведите название новой рассылки:", reply_markup=back_kb())
        return

    if text == "📃 Список рассылок":
        campaigns = load_campaigns()
        if not campaigns:
            bot.send_message(message.chat.id, "📨 Список рассылок пуст.", reply_markup=campaigns_menu_kb())
            return
        
        text_lines = ["📨 *Список рассылок:*\n"]
        for cid, c in campaigns.items():
            text_lines.append(format_campaign_brief(cid, c))
        
        bot.send_message(message.chat.id, "\n".join(text_lines), reply_markup=campaigns_menu_kb())
        bot.send_message(
            message.chat.id,
            "💡 Чтобы открыть рассылку, отправьте её ID (первые 8 символов)"
        )
        return

    # Открытие рассылки по ID (только если не в состоянии и текст похож на ID)
    if st is None and len(text) >= 6 and len(text) <= 10 and text.replace('-', '').replace('_', '').isalnum() and text not in ["📨 Рассылки", "👥 Аккаунты", "📊 Логи и отчёты", "⬅️ Назад"]:
        campaigns = load_campaigns()
        for cid, c in campaigns.items():
            if cid.startswith(text) or text in cid[:8]:
                data["campaign_id"] = cid
                set_state(message.from_user.id, "campaign_view", data)
                
                duration = c.get("duration_minutes")
                duration_str = "∞ (бесконечная)" if duration is None or duration == -1 else f"{duration} мин"
                big_delay = c.get("big_delay_minutes")
                big_delay_str = f"{big_delay} мин" if big_delay else "отключен"
                
                msg_text = (
                    f"*{c.get('title','Без названия')}*\n\n"
                    f"ID: `{cid}`\n"
                    f"Статус: `{c.get('status','idle')}`\n"
                    f"Аккаунтов: `{len(c.get('account_ids', []))}`\n"
                    f"Файл чатов: `{c.get('chats_file','chats.txt')}`\n"
                    f"Мин. задержка: `{c.get('min_delay',30)}` сек\n"
                    f"Макс. задержка: `{c.get('max_delay',60)}` сек\n"
                    f"Время работы: `{duration_str}`\n"
                    f"Крупный delay: `{big_delay_str}`\n"
                )
                bot.send_message(message.chat.id, msg_text, reply_markup=campaign_actions_kb())
                return

    # Действия с рассылкой
    if st == "campaign_view":
        cid = data.get("campaign_id")
        if not cid:
            bot.send_message(message.chat.id, "❌ Ошибка: не найден ID рассылки", reply_markup=campaigns_menu_kb())
            clear_state(message.from_user.id)
            return

        if text == "🚀 Запустить":
            logger.info("=" * 50)
            logger.info(f"[BOT] ===== ПОЛУЧЕНА КОМАНДА ЗАПУСКА =====")
            logger.info(f"[BOT] Campaign ID: {cid}")
            logger.info(f"[BOT] User ID: {message.from_user.id}")
            try:
                campaigns = load_campaigns()
                logger.info(f"[BOT] Загружено кампаний: {len(campaigns)}")
                if cid not in campaigns:
                    logger.error(f"[BOT] Рассылка {cid} не найдена в campaigns.json")
                    bot.reply_to(message, "❌ Рассылка не найдена")
                    return
                
                # Проверяем наличие аккаунтов
                c = campaigns.get(cid)
                account_ids = c.get("account_ids", [])
                logger.info(f"[BOT] Найдено аккаунтов: {len(account_ids)}")
                if not account_ids:
                    logger.error(f"[BOT] Нет аккаунтов для рассылки {cid}")
                    bot.reply_to(message, "❌ Не выбраны аккаунты для рассылки!")
                    return
                
                # Проверяем наличие файла чатов
                chats_file = c.get("chats_file", "chats.txt")
                logger.info(f"[BOT] Файл чатов: {chats_file}")
                if not os.path.exists(chats_file):
                    logger.error(f"[BOT] Файл {chats_file} не найден")
                    bot.reply_to(message, f"❌ Файл `{chats_file}` не найден!")
                    return
                
                # Проверяем наличие сообщения или ссылки
                message_text = c.get("message_text")
                source_link = c.get("source_link")
                logger.info(f"[BOT] message_text: {bool(message_text)}, source_link: {source_link}")
                if not message_text and not source_link:
                    logger.error(f"[BOT] Нет сообщения и ссылки для рассылки {cid}")
                    bot.reply_to(message, "❌ Не указано сообщение или ссылка на пост!")
                    return
                
                if cid in running_campaigns:
                    threads = running_campaigns[cid].get("threads", [])
                    if any(t.is_alive() for t in threads):
                        logger.warning(f"[BOT] Рассылка {cid} уже запущена")
                        bot.reply_to(message, "⚠️ Уже запущена")
                        return
                
                try:
                    logger.info(f"[BOT] Вызов start_campaign_thread для {cid}")
                    start_campaign_thread(cid)
                    logger.info(f"[BOT] start_campaign_thread завершён для {cid}")
                    bot.reply_to(message, "✅ Рассылка запущена!\n\n📊 Отслеживайте прогресс в логах.")
                except Exception as e:
                    logger.error(f"[BOT] Ошибка при запуске: {e}", exc_info=True)
                    bot.reply_to(message, f"❌ Ошибка при запуске: {e}")
            except Exception as e:
                logger.error(f"[BOT] Критическая ошибка в обработчике запуска: {e}", exc_info=True)
                bot.reply_to(message, f"❌ Критическая ошибка: {e}")
            return

        if text == "⛔ Остановить":
            stop_campaign(cid)
            bot.reply_to(message, "⛔ Остановка инициирована")
            return

        if text == "📊 Статистика":
            campaigns = load_campaigns()
            c = campaigns.get(cid)
            if not c:
                bot.reply_to(message, "❌ Рассылка не найдена")
                return
            st = c.get("stats", {})
            stats_text = (
                f"📊 *Статистика рассылки*\n\n"
                f"ID: `{cid}`\n"
                f"Отправлено: `{st.get('sent',0)}`\n"
                f"Ошибок: `{st.get('failed',0)}`\n"
                f"Пропущено: `{st.get('skipped',0)}`\n"
                f"Вступили в чаты: `{st.get('joined',0)}`\n"
            )
            bot.reply_to(message, stats_text)
            return

        if text == "📁 Логи":
            log_path = f"campaign_{cid}.log"
            if os.path.exists(log_path):
                with open(log_path, "rb") as f:
                    bot.send_document(message.chat.id, f, caption=f"📁 Лог кампании `{cid}`")
            else:
                bot.reply_to(message, "📁 Отдельного лога нет, смотрите sender.log")
            return

        if text == "🗑 Удалить":
            campaigns = load_campaigns()
            if cid in campaigns:
                campaigns.pop(cid)
                save_campaigns(campaigns)
            bot.reply_to(message, "✅ Рассылка удалена", reply_markup=campaigns_menu_kb())
            clear_state(message.from_user.id)
            return

        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "📨 *Управление рассылками*", reply_markup=campaigns_menu_kb())
            clear_state(message.from_user.id)
            return

    # === Создание рассылки ===
    if st == "campaign_create_title":
        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "📨 *Управление рассылками*", reply_markup=campaigns_menu_kb())
            clear_state(message.from_user.id)
            return
        
        title = text.strip()
        if not title:
            bot.reply_to(message, "❌ Название не может быть пустым")
            return
        
        # Валидация названия
        if len(title) > 200:
            bot.reply_to(message, "❌ Название слишком длинное (максимум 200 символов)")
            return
        
        # Проверка на подозрительные символы
        if re.search(r'[<>"\']', title):
            logger.warning(f"[SECURITY] Попытка создать кампанию с подозрительным названием: {title}")
            bot.reply_to(message, "❌ Название содержит недопустимые символы")
            return
        
        data["title"] = title
        set_state(message.from_user.id, "campaign_create_message_type", data)
        bot.send_message(message.chat.id, "📝 *Шаг 2/8*\nВыберите тип сообщения:", reply_markup=message_type_kb())
        return

    if st == "campaign_create_message_type":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_title", data)
            bot.send_message(message.chat.id, "📝 *Шаг 1/7*\nВведите название новой рассылки:", reply_markup=back_kb())
            return
        
        if text == "📝 Текст сообщения":
            data["message_type"] = "text"
            set_state(message.from_user.id, "campaign_create_message", data)
            bot.send_message(message.chat.id, "📝 *Шаг 2/8*\nОтправьте текст сообщения для рассылки:", reply_markup=back_kb())
            return

        if text == "📤 Пост с канала (пересылка)":
            data["message_type"] = "forward"
            set_state(message.from_user.id, "campaign_create_source_link", data)
            bot.send_message(message.chat.id, "📤 *Шаг 2/8*\nОтправьте ссылку на пост из канала (например: https://t.me/channel/123):", reply_markup=back_kb())
            return

    if st == "campaign_create_message":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_message_type", data)
            bot.send_message(message.chat.id, "📝 *Шаг 2/7*\nВыберите тип сообщения:", reply_markup=message_type_kb())
            return
        
        message_text = text.strip()
        if not message_text:
            bot.reply_to(message, "❌ Текст не может быть пустым")
            return
        data["message_text"] = message_text
        cid = new_campaign_id()
        data["campaign_id"] = cid
        
        campaigns = load_campaigns()
        campaigns[cid] = campaign_dict(
            Campaign(
                id=cid,
                title=data["title"],
                account_ids=[],
                chats_file="chats.txt",
                message_text=message_text,
            )
        )
        save_campaigns(campaigns)
        
        set_state(message.from_user.id, "campaign_create_accounts", data)
        bot.send_message(message.chat.id, f"✅ Сообщение сохранено!\n\n👥 *Шаг 3/8*\nВведите ID аккаунтов через запятую (например: `abc12345, def67890`)\nИли отправьте `список` чтобы увидеть все аккаунты:", reply_markup=back_kb())
        return

    if st == "campaign_create_source_link":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_message_type", data)
            bot.send_message(message.chat.id, "📝 *Шаг 2/7*\nВыберите тип сообщения:", reply_markup=message_type_kb())
            return
        
        source_link = text.strip()
        if not source_link.startswith("https://t.me/"):
            bot.reply_to(message, "❌ Неверный формат ссылки. Нужно: https://t.me/channel/123")
            return
        data["source_link"] = source_link
        cid = new_campaign_id()
        data["campaign_id"] = cid
        
        campaigns = load_campaigns()
        campaigns[cid] = campaign_dict(
            Campaign(
                id=cid,
                title=data["title"],
                account_ids=[],
                chats_file="chats.txt",
                source_link=source_link,
                use_forward=True,
            )
        )
        save_campaigns(campaigns)
        
        set_state(message.from_user.id, "campaign_create_accounts", data)
        bot.send_message(message.chat.id, f"✅ Ссылка сохранена!\n\n👥 *Шаг 3/8*\nВведите ID аккаунтов через запятую (например: `abc12345, def67890`)\nИли отправьте `список` чтобы увидеть все аккаунты:", reply_markup=back_kb())
        return

    if st == "campaign_create_accounts":
        if text == "⬅️ Назад":
            if data.get("message_type") == "text":
                set_state(message.from_user.id, "campaign_create_message", data)
                bot.send_message(message.chat.id, "📝 *Шаг 2/7*\nОтправьте текст сообщения для рассылки:", reply_markup=back_kb())
            else:
                set_state(message.from_user.id, "campaign_create_source_link", data)
                bot.send_message(message.chat.id, "📤 *Шаг 2/7*\nОтправьте ссылку на пост из канала:", reply_markup=back_kb())
            return
        
        if text.lower() == "список":
            accounts = load_accounts()
            if not accounts:
                bot.reply_to(message, "❌ Нет аккаунтов! Сначала добавьте аккаунты.")
                return
            
            text_lines = ["👥 *Доступные аккаунты:*\n"]
            for aid, acc in accounts.items():
                name = acc.get('name', '') or acc.get('session_name', aid[:8])
                text_lines.append(f"`{aid[:8]}` • {name}")
            bot.reply_to(message, "\n".join(text_lines))
            return
        
        account_ids = [aid.strip() for aid in text.split(',')]
        accounts = load_accounts()
        valid_ids = []
        for aid in account_ids:
            # Ищем по первым 8 символам
            for acc_id in accounts.keys():
                if acc_id.startswith(aid):
                    valid_ids.append(acc_id)
                    break
        
        if not valid_ids:
            bot.reply_to(message, "❌ Не найдено ни одного аккаунта. Проверьте ID или отправьте `список`")
            return
        
        data["selected_accounts"] = valid_ids
        cid = data.get("campaign_id")
        campaigns = load_campaigns()
        if cid in campaigns:
            campaigns[cid]["account_ids"] = valid_ids
            save_campaigns(campaigns)
        
        set_state(message.from_user.id, "campaign_create_chats_file", data)
        bot.send_message(message.chat.id, f"✅ Выбрано аккаунтов: {len(valid_ids)}\n\n📁 *Шаг 4/8*\nВведите имя файла с чатами (например: `chats.txt`)\nИли отправьте файл `chats.txt` как документ:", reply_markup=back_kb())
        return

    if st == "campaign_create_chats_file":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_accounts", data)
            bot.send_message(message.chat.id, "👥 *Шаг 3/8*\nВведите ID аккаунтов через запятую:", reply_markup=back_kb())
            return
        
        # Обработка текстового ввода имени файла
        chats_file = text.strip()
        if not chats_file:
            bot.reply_to(message, "❌ Укажите имя файла или отправьте файл .txt")
            return
        
        if not os.path.exists(chats_file):
            bot.reply_to(message, f"❌ Файл `{chats_file}` не найден!\n\n💡 Отправьте файл .txt как документ или убедитесь, что файл существует в директории бота.")
            return
        
        cid = data.get("campaign_id")
        campaigns = load_campaigns()
        if cid in campaigns:
            campaigns[cid]["chats_file"] = chats_file
            save_campaigns(campaigns)
        
        data["chats_file"] = chats_file
        set_state(message.from_user.id, "campaign_create_min_delay", data)
        bot.send_message(message.chat.id, f"✅ Файл `{chats_file}` выбран!\n\n⏱ *Шаг 5/8*\nВведите минимальную задержку между отправками (в секундах, например: `30`):", reply_markup=back_kb())
        return

    if st == "campaign_create_min_delay":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_chats_file", data)
            bot.send_message(message.chat.id, "📁 *Шаг 4/8*\nВведите имя файла с чатами:", reply_markup=back_kb())
            return
        
        try:
            min_delay = float(text.strip())
            if min_delay < 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Введите положительное число")
            return
        
        data["min_delay"] = min_delay
        set_state(message.from_user.id, "campaign_create_max_delay", data)
        bot.send_message(message.chat.id, f"⏱ *Шаг 5/8 (продолжение)*\nВведите максимальную задержку между отправками (в секундах, например: `60`):", reply_markup=back_kb())
        return

    if st == "campaign_create_max_delay":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_min_delay", data)
            bot.send_message(message.chat.id, "⏱ *Шаг 5/8*\nВведите минимальную задержку:", reply_markup=back_kb())
            return
        
        try:
            max_delay = float(text.strip())
            if max_delay < data.get("min_delay", 0):
                bot.reply_to(message, f"❌ Максимальная задержка должна быть больше минимальной ({data.get('min_delay')})")
                return
        except ValueError:
            bot.reply_to(message, "❌ Введите положительное число")
            return
        
        cid = data.get("campaign_id")
        campaigns = load_campaigns()
        if cid in campaigns:
            campaigns[cid]["min_delay"] = data["min_delay"]
            campaigns[cid]["max_delay"] = max_delay
            save_campaigns(campaigns)
        
        data["max_delay"] = max_delay
        set_state(message.from_user.id, "campaign_create_duration", data)
        bot.send_message(message.chat.id, "⏰ *Шаг 6/8*\nВведите время работы рассылки:\n• Число (минуты) - например `120`\n• `-` или `0` - бесконечный режим", reply_markup=back_kb())
        return

    if st == "campaign_create_duration":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_max_delay", data)
            bot.send_message(message.chat.id, "⏱ *Шаг 5/8*\nВведите максимальную задержку:", reply_markup=back_kb())
            return
        
        duration_str = text.strip()
        duration_minutes = None
        
        if duration_str in ["-", "0", "бесконечно", "inf"]:
            duration_minutes = -1
        else:
            try:
                duration_minutes = int(duration_str)
                if duration_minutes < 0:
                    duration_minutes = -1
            except ValueError:
                bot.reply_to(message, "❌ Введите число или `-` для бесконечного режима")
                return
        
        cid = data.get("campaign_id")
        campaigns = load_campaigns()
        if cid in campaigns:
            campaigns[cid]["duration_minutes"] = duration_minutes
            campaigns[cid]["account_ids"] = data.get("selected_accounts", [])
            save_campaigns(campaigns)
        
        data["duration_minutes"] = duration_minutes
        set_state(message.from_user.id, "campaign_create_big_delay", data)
        bot.send_message(
            message.chat.id,
            "⏸ *Шаг 7/8*\nВведите крупный delay между циклами (в минутах):\n"
            "• Число (минуты) - например `60` для 1 часа между циклами\n"
            "• `0` или `-` - без крупного delay (только 10 секунд между циклами)\n\n"
            "💡 Крупный delay применяется после завершения полного цикла по всем чатам",
            reply_markup=back_kb()
        )
        return

    if st == "campaign_create_big_delay":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "campaign_create_duration", data)
            bot.send_message(message.chat.id, "⏰ *Шаг 6/8*\nВведите время работы рассылки:", reply_markup=back_kb())
            return
        
        big_delay_str = text.strip()
        big_delay_minutes = None
        
        if big_delay_str in ["-", "0", "нет", "без"]:
            big_delay_minutes = None
        else:
            try:
                big_delay_minutes = float(big_delay_str)
                if big_delay_minutes < 0:
                    big_delay_minutes = None
            except ValueError:
                bot.reply_to(message, "❌ Введите число (минуты) или `0` для отключения")
                return
        
        cid = data.get("campaign_id")
        campaigns = load_campaigns()
        if cid in campaigns:
            campaigns[cid]["duration_minutes"] = data.get("duration_minutes")
            campaigns[cid]["big_delay_minutes"] = big_delay_minutes
            campaigns[cid]["account_ids"] = data.get("selected_accounts", [])
            
            # Валидация данных кампании перед сохранением
            campaign_data = {
                "title": campaigns[cid].get("title", ""),
                "min_delay": campaigns[cid].get("min_delay", 30),
                "max_delay": campaigns[cid].get("max_delay", 60),
                "duration_minutes": campaigns[cid].get("duration_minutes"),
                "big_delay_minutes": big_delay_minutes,
            }
            is_valid, error_msg = validate_campaign_data(campaign_data)
            if not is_valid:
                logger.warning(f"[SECURITY] Попытка создать кампанию с невалидными данными: {error_msg}")
                bot.reply_to(message, f"❌ Ошибка валидации данных: {error_msg}")
                return
            
            save_campaigns(campaigns)
            logger.info(f"[SECURITY] Кампания {cid[:8]} создана с валидными данными")
        
        clear_state(message.from_user.id)
        duration_text = "бесконечный режим" if data.get("duration_minutes") == -1 else f"{data.get('duration_minutes')} минут"
        big_delay_text = f"{big_delay_minutes} минут" if big_delay_minutes else "отключен"
        bot.send_message(
            message.chat.id,
            f"✅ *Рассылка создана!*\n\n"
            f"Название: `{data.get('title')}`\n"
            f"Аккаунтов: `{len(data.get('selected_accounts', []))}`\n"
            f"Файл чатов: `{data.get('chats_file', 'chats.txt')}`\n"
            f"Задержка: `{data.get('min_delay', 30)}-{data.get('max_delay', 60)}` сек\n"
            f"Время работы: `{duration_text}`\n"
            f"Крупный delay: `{big_delay_text}`\n\n"
            f"ID кампании: `{cid[:8]}`\n\n"
            f"💡 Отправьте ID кампании чтобы открыть её",
            reply_markup=campaigns_menu_kb()
        )
        return

    # === Аккаунты ===
    if text == "➕ Добавить .session":
        set_state(message.from_user.id, "account_add_session_name", {})
        bot.send_message(
            message.chat.id,
            "📎 Отправьте `.session` файл как документ.\nВ подписи укажите название аккаунта (или оставьте пустым).",
            reply_markup=back_kb()
        )
        return

    if text == "➕ Добавить (логин)":
        set_state(message.from_user.id, "account_add_login_api_id", {})
        bot.send_message(
            message.chat.id,
            "📱 *Авторизация по номеру телефона*\n\nШаг 1/4: Введите API ID (получите на https://my.telegram.org):",
            reply_markup=back_kb()
        )
        return

    if text == "📃 Список аккаунтов":
        accounts = load_accounts()
        if not accounts:
            bot.send_message(message.chat.id, "👥 Список аккаунтов пуст.", reply_markup=accounts_menu_kb())
            return
        
        text_lines = ["👥 *Аккаунты:*\n"]
        for aid, a in accounts.items():
            name = a.get('name', '') or a.get('session_name', aid[:8])
            proxy_info = "🔒" if a.get('proxy') else "🔓"
            api_info = "✅" if a.get('api_id') and a.get('api_hash') else "⚠️"
            text_lines.append(f"{proxy_info}{api_info} `{aid[:8]}` • {name}")
        
        bot.send_message(message.chat.id, "\n".join(text_lines), reply_markup=accounts_menu_kb())
        bot.send_message(
            message.chat.id,
            "💡 Чтобы открыть аккаунт, отправьте его ID (первые 8 символов)"
        )
        return

    # Открытие аккаунта по ID (только если не в состоянии и текст похож на ID)
    if st is None and len(text) >= 6 and len(text) <= 10 and text.replace('-', '').replace('_', '').isalnum() and text not in ["📨 Рассылки", "👥 Аккаунты", "📊 Логи и отчёты", "⬅️ Назад"]:
        accounts = load_accounts()
        for aid, acc in accounts.items():
            if aid.startswith(text) or text in aid[:8]:
                data["account_id"] = aid
                set_state(message.from_user.id, "account_view", data)
                
                proxy_info = acc.get('proxy', 'Не настроен')
                api_id = acc.get('api_id', 0)
                api_hash = acc.get('api_hash', '')
                
                msg_text = (
                    f"*{acc.get('name', 'Без названия')}*\n\n"
                    f"ID: `{aid}`\n"
                    f"Session: `{acc.get('session_name', '')}`\n"
                    f"API ID: `{api_id}`\n"
                    f"API Hash: `{'***' if api_hash else 'Не указан'}`\n"
                    f"Прокси: `{proxy_info if isinstance(proxy_info, str) else 'Настроен'}`\n"
                )
                bot.send_message(message.chat.id, msg_text, reply_markup=account_actions_kb())
                return

    # Действия с аккаунтом
    if st == "account_view":
        aid = data.get("account_id")
        if not aid:
            bot.send_message(message.chat.id, "❌ Ошибка: не найден ID аккаунта", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            return

        if text == "⚙️ Прокси":
            set_state(message.from_user.id, "account_set_proxy", data)
            bot.send_message(
                message.chat.id,
                "🔒 Введите прокси в формате:\n`login:password@ip:port`\n\nИли просто `ip:port` для прокси без авторизации.\nДля удаления прокси отправьте `-`",
                reply_markup=back_kb()
            )
            return

        if text == "🔑 API ключи":
            set_state(message.from_user.id, "account_set_api_id", data)
            bot.send_message(
                message.chat.id,
                "🔑 Введите API ID (число):",
                reply_markup=back_kb()
            )
            return

        if text == "🗑 Удалить":
            accounts = load_accounts()
            if aid in accounts:
                accounts.pop(aid)
                save_accounts(accounts)
            bot.send_message(message.chat.id, "✅ Аккаунт удалён", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            return

        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "👥 *Управление аккаунтами*", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            return

    # Настройка прокси
    if st == "account_set_proxy":
        aid = data.get("account_id")
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "account_view", data)
            accounts = load_accounts()
            acc = accounts.get(aid)
            if acc:
                proxy_info = acc.get('proxy', 'Не настроен')
                msg_text = f"*{acc.get('name', 'Без названия')}*\n\nПрокси: `{proxy_info if isinstance(proxy_info, str) else 'Настроен'}`\n"
                bot.send_message(message.chat.id, msg_text, reply_markup=account_actions_kb())
            return
        
        proxy_str = text.strip()
        
        if proxy_str == "-":
            accounts = load_accounts()
            if aid in accounts:
                accounts[aid]["proxy"] = None
                save_accounts(accounts)
            bot.send_message(message.chat.id, "✅ Прокси удалён", reply_markup=account_actions_kb())
            set_state(message.from_user.id, "account_view", data)
            return
        
        # Валидация прокси
        is_valid, error_msg = validate_proxy_string(proxy_str)
        if not is_valid:
            logger.warning(f"[SECURITY] Попытка установить невалидный прокси: {proxy_str}")
            bot.reply_to(message, f"❌ Ошибка валидации прокси: {error_msg}")
            return
        
        proxy_dict = parse_proxy(proxy_str)
        if not proxy_dict:
            bot.reply_to(message, "❌ Неверный формат прокси. Используйте: `login:password@ip:port`")
            return
        
        accounts = load_accounts()
        if aid in accounts:
            accounts[aid]["proxy"] = proxy_str
            save_accounts(accounts)
            logger.info(f"[SECURITY] Прокси установлен для аккаунта {aid[:8]}")
        
        bot.send_message(message.chat.id, f"✅ Прокси настроен: `{proxy_str}`", reply_markup=account_actions_kb())
        set_state(message.from_user.id, "account_view", data)
        return

    # Настройка API ID
    if st == "account_set_api_id":
        aid = data.get("account_id")
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "account_view", data)
            accounts = load_accounts()
            acc = accounts.get(aid)
            if acc:
                api_id = acc.get('api_id', 0)
                msg_text = f"*{acc.get('name', 'Без названия')}*\n\nAPI ID: `{api_id}`\n"
                bot.send_message(message.chat.id, msg_text, reply_markup=account_actions_kb())
            return
        
        try:
            api_id = int(text.strip())
        except ValueError:
            bot.reply_to(message, "❌ Введите число")
            return
        
        # Валидация API ID (базовая проверка перед сохранением)
        if api_id <= 0:
            bot.reply_to(message, "❌ API ID должен быть положительным числом")
            return
        
        if api_id > 999999999:
            bot.reply_to(message, "❌ API ID выглядит невалидным")
            return
        
        data["temp_api_id"] = api_id
        set_state(message.from_user.id, "account_set_api_hash", data)
        bot.send_message(message.chat.id, "🔑 Введите API Hash:", reply_markup=back_kb())
        return

    # Настройка API Hash
    if st == "account_set_api_hash":
        aid = data.get("account_id")
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "account_set_api_id", data)
            bot.send_message(message.chat.id, "🔑 Введите API ID:", reply_markup=back_kb())
            return
        
        api_hash = text.strip()
        if not api_hash:
            bot.reply_to(message, "❌ API Hash не может быть пустым")
            return
        
        # Валидация API credentials
        temp_api_id = data.get("temp_api_id", 0)
        is_valid, error_msg = validate_api_credentials(temp_api_id, api_hash)
        if not is_valid:
            logger.warning(f"[SECURITY] Попытка установить невалидные API credentials для аккаунта {aid[:8]}")
            bot.reply_to(message, f"❌ Ошибка валидации: {error_msg}")
            return
        
        accounts = load_accounts()
        if aid in accounts:
            accounts[aid]["api_id"] = temp_api_id
            accounts[aid]["api_hash"] = api_hash
            save_accounts(accounts)
            logger.info(f"[SECURITY] API credentials установлены для аккаунта {aid[:8]}")
        
        bot.send_message(message.chat.id, f"✅ API ключи сохранены!\n\nAPI ID: `{data.get('temp_api_id')}`\nAPI Hash: `***`", reply_markup=account_actions_kb())
        set_state(message.from_user.id, "account_view", data)
        return

    # Добавление аккаунта через .session (текстовый ввод не нужен, только файл)
    if st == "account_add_session_name":
        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "👥 *Управление аккаунтами*", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            return
        
        # Если пользователь ввёл текст вместо отправки файла
        bot.reply_to(message, "❌ Пожалуйста, отправьте `.session` файл как документ.\n\n💡 Нажмите на скрепку и выберите файл.")
        return

    # Авторизация по номеру (упрощённая версия - можно расширить позже)
    if st == "account_add_login_api_id":
        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "👥 *Управление аккаунтами*", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            return
        
        try:
            api_id = int(text.strip())
        except ValueError:
            bot.reply_to(message, "❌ Введите число")
            return
        
        data["api_id"] = api_id
        set_state(message.from_user.id, "account_add_login_api_hash", data)
        bot.send_message(message.chat.id, "📱 *Шаг 2/4*\nВведите API Hash:", reply_markup=back_kb())
        return

    if st == "account_add_login_api_hash":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "account_add_login_api_id", data)
            bot.send_message(message.chat.id, "📱 *Шаг 1/4*\nВведите API ID:", reply_markup=back_kb())
            return
        
        api_hash = text.strip()
        if not api_hash:
            bot.reply_to(message, "❌ API Hash не может быть пустым")
            return
        
        data["api_hash"] = api_hash
        set_state(message.from_user.id, "account_add_login_phone", data)
        bot.send_message(message.chat.id, "📱 *Шаг 3/4*\nВведите номер телефона в формате +79991234567:", reply_markup=back_kb())
        return

    if st == "account_add_login_phone":
        if text == "⬅️ Назад":
            set_state(message.from_user.id, "account_add_login_api_hash", data)
            bot.send_message(message.chat.id, "📱 *Шаг 2/4*\nВведите API Hash:", reply_markup=back_kb())
            return
        
        phone = text.strip()
        if not phone.startswith('+'):
            bot.reply_to(message, "❌ Номер должен начинаться с +")
            return
        
        data["phone"] = phone
        set_state(message.from_user.id, "account_add_login_code", data)
        
        # Запускаем авторизацию в отдельном потоке
        def auth_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            client = None
            try:
                logger.info(f"[AUTH] Начало авторизации для номера: {phone}")
                session_name = f"temp_auth_{message.from_user.id}"
                client = TelegramClient(session_name, data["api_id"], data["api_hash"])
                logger.info(f"[AUTH] Подключение к Telegram...")
                loop.run_until_complete(client.connect())
                logger.info(f"[AUTH] Подключено, проверка авторизации...")
                
                if not loop.run_until_complete(client.is_user_authorized()):
                    logger.info(f"[AUTH] Аккаунт не авторизован, запрос кода...")
                    sent_code = loop.run_until_complete(client.send_code_request(phone))
                    logger.info(f"[AUTH] Код отправлен на {phone}")
                    auth_sessions[message.from_user.id] = {
                        "session_name": session_name,          # было "client": client
                        "phone": phone,
                        "api_id": data["api_id"],
                        "api_hash": data["api_hash"],
                        "phone_code_hash": sent_code.phone_code_hash,
                    }
                    bot.send_message(
                        message.chat.id,
                        f"📱 *Шаг 4/4*\nКод отправлен на номер `{phone}`\nВведите код подтверждения:",
                        reply_markup=back_kb()
                    )
                else:
                    me = loop.run_until_complete(client.get_me())
                    logger.info(f"[AUTH] Аккаунт уже авторизован: {me.first_name} (@{me.username})")
                    phone_str = me.phone or "unknown"
                    session_name_final = f"{me.id}_{phone_str}.session"
                    
                    temp_session_path = f"{session_name}.session"
                    final_session_path = session_name_final
                    
                    if os.path.exists(temp_session_path):
                        # Если финальный файл уже существует, удаляем его
                        if os.path.exists(final_session_path):
                            os.remove(final_session_path)
                        try:
                            shutil.move(temp_session_path, final_session_path)
                            logger.info(f"[AUTH] Файл сессии переименован: {final_session_path}")
                        except Exception as e:
                            logger.error(f"[AUTH] Ошибка переименования файла: {e}")
                            # Пробуем скопировать
                            shutil.copy2(temp_session_path, final_session_path)
                            os.remove(temp_session_path)
                    
                    accounts = load_accounts()
                    aid = new_account_id()
                    accounts[aid] = account_dict(
                        Account(
                            id=aid,
                            name=f"{me.first_name} {me.last_name or ''}".strip() or phone_str,
                            session_name=session_name_final,
                            api_id=data["api_id"],
                            api_hash=data["api_hash"],
                            proxy=None,
                        )
                    )
                    save_accounts(accounts)
                    clear_state(message.from_user.id)
                    logger.info(f"[AUTH] Аккаунт сохранён с ID: {aid}")
                    bot.send_message(
                        message.chat.id,
                        f"✅ Аккаунт авторизован и сохранён!\n\nID: `{aid[:8]}`\nИмя: `{me.first_name}`",
                        reply_markup=accounts_menu_kb()
                    )
                    loop.run_until_complete(client.disconnect())
            except Exception as e:
                logger.error(f"[AUTH] Ошибка авторизации: {e}", exc_info=True)
                try:
                    bot.send_message(message.chat.id, f"❌ Ошибка авторизации: {e}\n\nПопробуйте ещё раз.", reply_markup=accounts_menu_kb())
                except:
                    pass
                clear_state(message.from_user.id)
                auth_sessions.pop(message.from_user.id, None)
            finally:
                try:
                    if client:
                        loop.run_until_complete(client.disconnect())
                except Exception as e:
                    logger.error(f"[AUTH] Ошибка при отключении: {e}")
                try:
                    loop.close()
                except:
                    pass
        
        threading.Thread(target=auth_thread, daemon=True).start()
        return

    if st == "account_add_login_code":
        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "👥 *Управление аккаунтами*", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            auth_sessions.pop(message.from_user.id, None)
            return
        
        code = text.strip()
        auth_info = auth_sessions.get(message.from_user.id)
        if not auth_info:
            bot.reply_to(message, "❌ Сессия авторизации не найдена. Начните заново.")
            clear_state(message.from_user.id)
            return
        
        def verify_code():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            client = None
            try:
                logger.info(f"[AUTH] Проверка кода для {auth_info.get('phone', 'unknown')}")

                # ВАЖНО: новый client в этом loop, без auth_info["client"]
                client = TelegramClient(
                    auth_info["session_name"],
                    auth_info["api_id"],
                    auth_info["api_hash"],
                )

                loop.run_until_complete(client.connect())

                try:
                    loop.run_until_complete(
                        client.sign_in(
                            auth_info["phone"],
                            code,
                            phone_code_hash=auth_info["phone_code_hash"],
                        )
                    )
                    logger.info("[AUTH] Код принят успешно")
                except SessionPasswordNeededError:
                    logger.info("[AUTH] Требуется пароль 2FA")
                    auth_sessions[message.from_user.id]["need_password"] = True
                    set_state(message.from_user.id, "account_add_login_password", {})
                    bot.send_message(
                        message.chat.id,
                        "🔐 Введите пароль двухфакторной аутентификации:",
                        reply_markup=back_kb(),
                    )
                    return

                me = loop.run_until_complete(client.get_me())
                logger.info(f"[AUTH] Успешная авторизация: {me.first_name} (@{me.username})")

                phone_str = me.phone or "unknown"
                session_name_final = f"{me.id}_{phone_str}.session"

                temp_session_path = f"{auth_info['session_name']}.session"
                final_session_path = session_name_final

                if os.path.exists(temp_session_path):
                    if os.path.exists(final_session_path):
                        os.remove(final_session_path)
                    try:
                        shutil.move(temp_session_path, final_session_path)
                        logger.info(f"[AUTH] Файл сессии переименован: {final_session_path}")
                    except Exception as e:
                        logger.error(f"[AUTH] Ошибка переименования файла: {e}")
                        shutil.copy2(temp_session_path, final_session_path)
                        os.remove(temp_session_path)

                accounts = load_accounts()
                aid = new_account_id()
                accounts[aid] = account_dict(
                    Account(
                        id=aid,
                        name=f"{me.first_name} {me.last_name or ''}".strip() or phone_str,
                        session_name=session_name_final,
                        api_id=auth_info["api_id"],
                        api_hash=auth_info["api_hash"],
                        proxy=None,
                    )
                )
                save_accounts(accounts)

                auth_sessions.pop(message.from_user.id, None)
                clear_state(message.from_user.id)

                logger.info(f"[AUTH] Аккаунт сохранён с ID: {aid}")
                bot.send_message(
                    message.chat.id,
                    f"✅ Аккаунт авторизован и сохранён!\n\nID: `{aid[:8]}`\nИмя: `{me.first_name}`",
                    reply_markup=accounts_menu_kb(),
                )

            except PhoneCodeInvalidError:
                logger.warning(f"[AUTH] Неверный код для {auth_info.get('phone', 'unknown')}")
                bot.send_message(message.chat.id, "❌ Неверный код. Попробуйте снова.")
            except Exception as e:
                logger.error(f"[AUTH] Ошибка при проверке кода: {e}", exc_info=True)
                bot.send_message(
                    message.chat.id,
                    f"❌ Ошибка: {e}\n\nПопробуйте начать заново.",
                    reply_markup=accounts_menu_kb(),
                )
                clear_state(message.from_user.id)
                auth_sessions.pop(message.from_user.id, None)
            finally:
                try:
                    if client:
                        loop.run_until_complete(client.disconnect())
                except Exception as e:
                    logger.error(f"[AUTH] Ошибка при отключении: {e}")
                try:
                    loop.close()
                except:
                    pass

        threading.Thread(target=verify_code, daemon=True).start()
        return


    if st == "account_add_login_password":
        if text == "⬅️ Назад":
            bot.send_message(message.chat.id, "👥 *Управление аккаунтами*", reply_markup=accounts_menu_kb())
            clear_state(message.from_user.id)
            auth_sessions.pop(message.from_user.id, None)
            return
        
        password = text.strip()
        auth_info = auth_sessions.get(message.from_user.id)
        if not auth_info:
            bot.reply_to(message, "❌ Сессия авторизации не найдена")
            clear_state(message.from_user.id)
            return
        
        def verify_password():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            client = None
            try:
                logger.info(f"[AUTH] Проверка пароля 2FA для {auth_info.get('phone', 'unknown')}")
                client = auth_info["client"]
                loop.run_until_complete(client.sign_in(password=password))
                logger.info(f"[AUTH] Пароль принят успешно")
                
                me = loop.run_until_complete(client.get_me())
                logger.info(f"[AUTH] Успешная авторизация с паролем: {me.first_name} (@{me.username})")
                phone_str = me.phone or "unknown"
                session_name_final = f"{me.id}_{phone_str}.session"
                
                temp_session_path = f"temp_auth_{message.from_user.id}.session"
                final_session_path = session_name_final
                
                if os.path.exists(temp_session_path):
                    # Если финальный файл уже существует, удаляем его
                    if os.path.exists(final_session_path):
                        os.remove(final_session_path)
                    try:
                        shutil.move(temp_session_path, final_session_path)
                        logger.info(f"[AUTH] Файл сессии переименован: {final_session_path}")
                    except Exception as e:
                        logger.error(f"[AUTH] Ошибка переименования файла: {e}")
                        # Пробуем скопировать
                        shutil.copy2(temp_session_path, final_session_path)
                        os.remove(temp_session_path)
                
                accounts = load_accounts()
                aid = new_account_id()
                accounts[aid] = account_dict(
                    Account(
                        id=aid,
                        name=f"{me.first_name} {me.last_name or ''}".strip() or phone_str,
                        session_name=session_name_final,
                        api_id=auth_info["api_id"],
                        api_hash=auth_info["api_hash"],
                        proxy=None,
                    )
                )
                save_accounts(accounts)
                auth_sessions.pop(message.from_user.id, None)
                clear_state(message.from_user.id)
                logger.info(f"[AUTH] Аккаунт сохранён с ID: {aid}")
                bot.send_message(
                    message.chat.id,
                    f"✅ Аккаунт авторизован и сохранён!\n\nID: `{aid[:8]}`\nИмя: `{me.first_name}`",
                    reply_markup=accounts_menu_kb()
                )
                loop.run_until_complete(client.disconnect())
            except Exception as e:
                logger.error(f"[AUTH] Ошибка при проверке пароля: {e}", exc_info=True)
                bot.send_message(message.chat.id, f"❌ Ошибка: {e}\n\nПопробуйте начать заново.", reply_markup=accounts_menu_kb())
                clear_state(message.from_user.id)
                auth_sessions.pop(message.from_user.id, None)
            finally:
                try:
                    if client:
                        loop.run_until_complete(client.disconnect())
                except Exception as e:
                    logger.error(f"[AUTH] Ошибка при отключении: {e}")
                try:
                    loop.close()
                except:
                    pass
        
        threading.Thread(target=verify_password, daemon=True).start()
        return

if __name__ == "__main__":
    import time
    import telebot.apihelper as apihelper

    apihelper.READ_TIMEOUT = 90
    apihelper.CONNECT_TIMEOUT = 30
    apihelper.SESSION_TIME_TO_LIVE = 5 * 60

    logger.info("=" * 50)
    logger.info("🤖 Telegram Sender Bot запускается...")

    try:
        bot_info = bot.get_me()
        logger.info(f"👤 Администратор: {ADMIN_ID}")
        logger.info(f"🔗 Username бота: @{bot_info.username}")
        logger.info("📝 Управление через кнопки, команда /start")
        logger.info("=" * 50)

        print("🤖 Telegram Sender Bot запущен...")
        print(f"👤 Администратор: {ADMIN_ID}")
        print(f"🔗 Username бота: @{bot_info.username}")
        print("📝 Управление через кнопки, команда /start")
        print("📋 Логи пишутся в bot.log")

    except Exception as e:
        logger.error(f"[BOT] Ошибка инициализации: {e}", exc_info=True)

    while True:
        try:
            bot.polling(
                none_stop=True,
                interval=0,
                timeout=60,
                long_polling_timeout=50,
            )
        except Exception as e:
            logger.exception(f"[BOT] polling упал, перезапуск через 5 сек: {e}")
            time.sleep(5)