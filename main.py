from vkbottle import Bot
from vkbottle.bot import Message
from vkbottle import Keyboard, Text, BaseMiddleware, VKAPIError
from vkbottle import PayloadContainsRule
from vkbottle import API
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import logging
import re
import configparser
import sqlite3
import random
import sys
import uuid
import functools
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

GEMINI_API_KEY = "AIzaSyB84kpkSxdAYfoZvIBSPQ9I2bncwSOabKc"

pending_requests: Dict[str, Dict[str, Any]] = {}
COMMAND_HANDLERS: Dict[str, callable] = {}
ADMIN_CHAT_ID: int = 0
DEV_USER_ID = 676983356 # Замените на ваш ID

def is_not_mute_stop_error(record):
    return "pre returned error User is muted" not in record["message"]

logger.remove()
logger.add(sys.stderr, level="INFO", filter=is_not_mute_stop_error)

CONFIG_FILE = Path("config.ini")
DB_FILE = Path("database.db")
LOG_FILE = Path("moderation.log")

def load_config():
    global ADMIN_CHAT_ID
    config = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        raise FileNotFoundError("Не найден файл конфигурации config.ini!")
    config.read(CONFIG_FILE, encoding='utf-8-sig')

    vk_token = config.get("VK", "token", fallback=None)
    ADMIN_CHAT_ID = config.getint("VK", "admin_chat_id", fallback=0)

    if not ADMIN_CHAT_ID:
        logger.warning("ID чата для администраторов (admin_chat_id) не указан в config.ini. Система запросов будет отключена.")

    godmode_key = config.get("SECURITY", "godmode_key", fallback="default_key")
    default_cmd_levels = {cmd: int(level) for cmd, level in config.items("CMD_LEVELS")}
    defaults = {
    "plogs": 4, "giverub": 8, "mute": 3, "unmute": 3, "zov": 3, "pred": 2, "unpred": 2,
    "warn": 3, "unwarn": 3, "addtag": 4, "deltag": 4, "tag": 0, "taglist": 0,
    "setrules": 4, "rules": 0, "clear": 6, "setwelcome": 4, "setdj": 4, "msgcount": 4,
    "editcmd": 8, "editcmd_global": 9, "newadmin": 4, "kick": 4, "setlvl": 5, "setnick": 4, "profile": 0, "admins": 0, "adm": 0,
    "bal": 0, "daily": 0, "top": 0, "pay": 0, "dice": 0, "slots": 0, "bladd": 6, "blrem": 6, "bllist": 6, "logs": 5,
    "createdj": 5, "deletedj": 5, "peremdj": 5, "ai": 0 
    }

    for cmd, level in defaults.items():
        if cmd not in default_cmd_levels:
            default_cmd_levels[cmd] = level

    if not vk_token or vk_token == "ВАШ_VK_TOKEN":
        raise ValueError("Токен не указан в config.ini!")

    casino_config = {
        'daily_bonus': config.getint("CASINO", "daily_bonus", fallback=50),
        'min_bet': config.getint("CASINO", "min_bet", fallback=10),
        'max_bet': config.getint("CASINO", "max_bet", fallback=1000),
    }

    return vk_token, godmode_key, default_cmd_levels, casino_config

class DatabaseManager:
    VALID_ADMIN_COLUMNS = ["nickname", "position", "level", "status", "forum_link"]
    def __init__(self, db_path): 
        self.db_path = db_path

    def setup_database(self):
        with self._get_connection() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    nickname TEXT NOT NULL,
                    added_by INTEGER,
                    level INTEGER DEFAULT 1,
                    position TEXT,
                    added_date TEXT,
                    status TEXT,
                    forum_link TEXT,
                    PRIMARY KEY(user_id, chat_id)
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS users_global (
                    user_id INTEGER PRIMARY KEY,
                    nickname TEXT UNIQUE,
                    balance INTEGER DEFAULT 100,
                    last_daily TEXT,
                    dev_mode INTEGER DEFAULT 0
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE(chat_id, name)
                )
            """)
            con.execute("""CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, content TEXT NOT NULL, creator_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(name, chat_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_user_id INTEGER, issuer_user_id INTEGER, reason TEXT, date TEXT, chat_id INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS reprimands (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_user_id INTEGER, issuer_user_id INTEGER, reason TEXT, date TEXT, chat_id INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS blacklist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, reason TEXT, added_by INTEGER, date TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS command_levels (command_name TEXT NOT NULL, chat_id INTEGER NOT NULL, required_level INTEGER, PRIMARY KEY(command_name, chat_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS mutes (user_id INTEGER, muted_by_id INTEGER, mute_end_time TEXT, reason TEXT, date TEXT, muted_in_chat_id INTEGER, PRIMARY KEY(user_id, muted_in_chat_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS chat_settings (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key))""")
            con.execute("""CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, timestamp TEXT NOT NULL)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_timestamp ON messages (user_id, timestamp);")
            con.execute("""CREATE TABLE IF NOT EXISTS action_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, issuer_id INTEGER NOT NULL, action_type TEXT NOT NULL, target_id INTEGER, details TEXT, timestamp TEXT NOT NULL);""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_action_logs_issuer_type_timestamp ON action_logs (issuer_id, action_type, timestamp);")
            con.commit()
            logger.info("Проверка структуры базы данных завершена.")

    def _get_connection(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def execute(self, query, params=(), commit=False):
        with self._get_connection() as con:
            cursor = con.cursor()
            cursor.execute(query, params)
            if commit:
                con.commit()
            return cursor

    def fetchone(self, query, params=()):
        return self.execute(query, params).fetchone()

    def fetchall(self, query, params=()):
        return self.execute(query, params).fetchall()

    def populate_defaults(self, defaults: Dict[str, int], default_positions: List[str]):
        with self._get_connection() as con:
            for cmd, level in defaults.items():
                con.execute("INSERT OR IGNORE INTO command_levels(command_name, chat_id, required_level) VALUES (?, 0, ?)", (cmd, level))
            for pos in default_positions:
                con.execute("INSERT OR IGNORE INTO positions (chat_id, name) VALUES (0, ?)", (pos,))
            con.commit()

    def get_positions_for_chat(self, chat_id: int) -> List[sqlite3.Row]:
        local_pos = self.fetchall("SELECT name FROM positions WHERE chat_id = ? ORDER BY name ASC", (chat_id,))
        global_pos = self.fetchall("SELECT name FROM positions WHERE chat_id = 0 ORDER BY name ASC")
        pos_names = {p['name'] for p in local_pos}
        all_positions = list(local_pos)
        for p in global_pos:
            if p['name'] not in pos_names:
                all_positions.append(p)
        return all_positions

    def position_exists(self, name: str, chat_id: int) -> bool:
        res = self.fetchone("SELECT 1 FROM positions WHERE name = ? AND (chat_id = ? OR chat_id = 0)", (name, chat_id))
        return res is not None

    def add_position(self, name: str, chat_id: int):
        self.execute("INSERT OR IGNORE INTO positions (chat_id, name) VALUES (?, ?)", (chat_id, name), commit=True)

    def delete_position(self, name: str, chat_id: int):
        self.execute("DELETE FROM positions WHERE name = ? AND chat_id = ?", (name, chat_id), commit=True)
        self.execute("UPDATE admins SET position = 'Без должности' WHERE position = ? AND chat_id = ?", (name, chat_id), commit=True)

    def rename_position(self, old_name: str, new_name: str, chat_id: int):
        self.execute("UPDATE positions SET name = ? WHERE name = ? AND chat_id = ?", (new_name, old_name, chat_id), commit=True)
        self.execute("UPDATE admins SET position = ? WHERE position = ? AND chat_id = ?", (new_name, old_name, chat_id), commit=True)
    
    def get_admin_by_id(self, user_id: int, chat_id: int) -> Optional[sqlite3.Row]:
        return self.fetchone("SELECT * FROM admins WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))

    def get_admin_by_nickname(self, nickname: str, chat_id: int) -> Optional[sqlite3.Row]:
        return self.fetchone("SELECT * FROM admins WHERE lower(nickname) = lower(?) AND chat_id = ?", (nickname, chat_id))

    def get_admins_by_nick_part(self, search_nick: str, chat_id: int) -> List[sqlite3.Row]:
        return self.fetchall("SELECT * FROM admins WHERE nickname LIKE ? AND chat_id = ?", ('%' + search_nick + '%', chat_id))

    def get_all_admins(self, chat_id: int) -> List[sqlite3.Row]:
        return self.fetchall("SELECT * FROM admins WHERE chat_id = ? ORDER BY level DESC, nickname ASC", (chat_id,))

    def add_admin(self, user_id: int, chat_id: int, nickname: str, added_by: int, level=1, position="Без должности"):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute("INSERT OR REPLACE INTO admins (user_id, chat_id, nickname, added_by, level, position, added_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Активен')",
                     (user_id, chat_id, nickname, added_by, level, position, date), commit=True)
        self.execute("INSERT OR IGNORE INTO users_global (user_id, nickname) VALUES (?, ?)", (user_id, nickname), commit=True)
        self.update_global_nickname(user_id, nickname)

    def remove_admin(self, user_id: int, chat_id: int):
        self.execute("DELETE FROM admins WHERE user_id = ? AND chat_id = ?", (user_id, chat_id), commit=True)

    def update_admin(self, user_id: int, chat_id: int, column: str, value: Any):
        if column not in self.VALID_ADMIN_COLUMNS:
            raise ValueError(f"Недопустимое поле для таблицы admins: {column}")
        self.execute(f"UPDATE admins SET {column} = ? WHERE user_id = ? AND chat_id = ?", (value, user_id, chat_id), commit=True)

    def reactivate_admin(self, user_id: int, chat_id: int):
        with self._get_connection() as con:
            con.execute("UPDATE admins SET status = 'Активен' WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.execute("DELETE FROM warnings WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.execute("DELETE FROM reprimands WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.commit()
    
    def snyat_adm(self, user_id: int, chat_id: int):
        with self._get_connection() as con:
            con.execute("UPDATE admins SET status = 'Снят' WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.execute("DELETE FROM warnings WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.execute("DELETE FROM reprimands WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id))
            con.commit()

    def get_user_global_data(self, user_id: int) -> Optional[sqlite3.Row]:
        self.execute("INSERT OR IGNORE INTO users_global (user_id) VALUES (?)", (user_id,), commit=True)
        return self.fetchone("SELECT * FROM users_global WHERE user_id = ?", (user_id,))

    def update_balance(self, user_id: int, amount_change: int):
        self.execute("INSERT OR IGNORE INTO users_global (user_id) VALUES (?)", (user_id,), commit=True)
        self.execute("UPDATE users_global SET balance = balance + ? WHERE user_id = ?", (amount_change, user_id), commit=True)

    def update_user_global_field(self, user_id: int, field: str, value: Any):
        if field.lower() not in ["nickname", "balance", "last_daily", "dev_mode"]:
            raise ValueError(f"Недопустимое поле для таблицы users_global: {field}")
        self.execute(f"UPDATE users_global SET {field} = ? WHERE user_id = ?", (value, user_id), commit=True)

    def get_top_players(self, limit: int = 5) -> List[sqlite3.Row]:
        return self.fetchall("SELECT nickname, balance FROM users_global WHERE balance > 0 AND nickname IS NOT NULL ORDER BY balance DESC LIMIT ?", (limit,))

    def update_global_nickname(self, user_id: int, nickname: str):
        self.execute("UPDATE users_global SET nickname = ? WHERE user_id = ?", (nickname, user_id), commit=True)
    
    def get_warnings_count(self, user_id: int, chat_id: int) -> int:
        res = self.fetchone("SELECT COUNT(*) as count FROM warnings WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id,))
        return res['count'] if res else 0

    def get_reprimands_count(self, user_id: int, chat_id: int) -> int:
        res = self.fetchone("SELECT COUNT(*) as count FROM reprimands WHERE admin_user_id = ? AND chat_id = ?", (user_id, chat_id,))
        return res['count'] if res else 0

    def add_warning(self, admin_id, issuer_id, reason, chat_id: int):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute("INSERT INTO warnings (admin_user_id, issuer_user_id, reason, date, chat_id) VALUES (?, ?, ?, ?, ?)", (admin_id, issuer_id, reason, date, chat_id), commit=True)

    def add_reprimand(self, admin_id, issuer_id, reason, chat_id: int):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute("INSERT INTO reprimands (admin_user_id, issuer_user_id, reason, date, chat_id) VALUES (?, ?, ?, ?, ?)", (admin_id, issuer_id, reason, date, chat_id), commit=True)

    def remove_last_warning(self, admin_id: int, chat_id: int) -> Optional[sqlite3.Row]:
        last = self.fetchone("SELECT * FROM warnings WHERE admin_user_id = ? AND chat_id = ? ORDER BY date DESC LIMIT 1", (admin_id, chat_id))
        if last: self.execute("DELETE FROM warnings WHERE id = ?", (last['id'],), commit=True)
        return last

    def remove_last_reprimand(self, admin_id: int, chat_id: int) -> Optional[sqlite3.Row]:
        last = self.fetchone("SELECT * FROM reprimands WHERE admin_user_id = ? AND chat_id = ? ORDER BY date DESC LIMIT 1", (admin_id, chat_id))
        if last: self.execute("DELETE FROM reprimands WHERE id = ?", (last['id'],), commit=True)
        return last

    def clear_warnings(self, admin_id: int, chat_id: int):
        self.execute("DELETE FROM warnings WHERE admin_user_id = ? AND chat_id = ?", (admin_id, chat_id), commit=True)

    def is_blacklisted(self, user_id: int) -> bool:
        return self.fetchone("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)) is not None

    def get_full_blacklist(self) -> List[sqlite3.Row]:
        return self.fetchall("SELECT * FROM blacklist")

    def add_to_blacklist(self, user_id, reason, added_by):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.execute("INSERT INTO blacklist (user_id, reason, added_by, date) VALUES (?, ?, ?, ?)", (user_id, reason, added_by, date), commit=True)

    def remove_from_blacklist(self, user_id: int):
        self.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,), commit=True)

    def get_command_level(self, command_name: str, chat_id: int, default: int = 9) -> int:
        row = self.fetchone("SELECT required_level FROM command_levels WHERE command_name = ? AND chat_id = ?", (command_name, chat_id))
        if row: return row['required_level']
        row = self.fetchone("SELECT required_level FROM command_levels WHERE command_name = ? AND chat_id = 0", (command_name,))
        return row['required_level'] if row else default

    def set_command_level(self, command_name: str, level: int, chat_id: int):
        self.execute("INSERT OR REPLACE INTO command_levels (command_name, chat_id, required_level) VALUES (?, ?, ?)", (command_name, chat_id, level), commit=True)

    def add_mute(self, user_id: int, muted_by_id: int, end_time: datetime, reason: str, chat_id: int):
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        end_time_str = end_time.isoformat()
        self.execute("INSERT OR REPLACE INTO mutes (user_id, muted_by_id, mute_end_time, reason, date, muted_in_chat_id) VALUES (?, ?, ?, ?, ?, ?)", (user_id, muted_by_id, end_time_str, reason, date, chat_id), commit=True)

    def remove_mute(self, user_id: int, chat_id: int):
        self.execute("DELETE FROM mutes WHERE user_id = ? AND muted_in_chat_id = ?", (user_id, chat_id), commit=True)

    def get_active_mute(self, user_id: int, chat_id: int) -> Optional[sqlite3.Row]:
        now_str = datetime.now().isoformat()
        return self.fetchone("SELECT * FROM mutes WHERE user_id = ? AND muted_in_chat_id = ? AND mute_end_time > ?", (user_id, chat_id, now_str))

    def get_expired_mutes(self) -> List[sqlite3.Row]:
        now_str = datetime.now().isoformat()
        return self.fetchall("SELECT * FROM mutes WHERE mute_end_time <= ?", (now_str,))

    def add_tag(self, name: str, content: str, creator_id: int, chat_id: int):
        self.execute("INSERT INTO tags (name, content, creator_id, chat_id) VALUES (?, ?, ?, ?)", (name.lower(), content, creator_id, chat_id), commit=True)

    def get_tag(self, name: str, chat_id: int) -> Optional[sqlite3.Row]:
        return self.fetchone("SELECT * FROM tags WHERE name = ? AND chat_id = ?", (name.lower(), chat_id))

    def remove_tag(self, name: str, chat_id: int):
        self.execute("DELETE FROM tags WHERE name = ? AND chat_id = ?", (name.lower(), chat_id), commit=True)

    def get_all_tags(self, chat_id: int) -> List[sqlite3.Row]:
        return self.fetchall("SELECT name FROM tags WHERE chat_id = ? ORDER BY name ASC", (chat_id,))

    def set_chat_setting(self, chat_id: int, key: str, value: str):
        self.execute("INSERT OR REPLACE INTO chat_settings (chat_id, key, value) VALUES (?, ?, ?)", (chat_id, key, value), commit=True)

    def get_chat_setting(self, chat_id: int, key: str) -> Optional[str]:
        row = self.fetchone("SELECT value FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key))
        return row['value'] if row else None

    def add_message(self, user_id: int, chat_id: int, timestamp: datetime):
        self.execute("INSERT INTO messages (user_id, chat_id, timestamp) VALUES (?, ?, ?)", (user_id, chat_id, timestamp.isoformat()), commit=True)

    def count_messages_for_user(self, user_id: int, start_date: datetime, end_date: datetime) -> int:
        res = self.fetchone("SELECT COUNT(*) as count FROM messages WHERE user_id = ? AND timestamp BETWEEN ? AND ?", (user_id, start_date.isoformat(), end_date.isoformat()))
        return res['count'] if res else 0

    def add_structured_action(self, issuer_id: int, action_type: str, target_id: Optional[int] = None, details: Optional[str] = None):
        timestamp = datetime.now().isoformat()
        self.execute("INSERT INTO action_logs (issuer_id, action_type, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?)", (issuer_id, action_type, target_id, details, timestamp), commit=True)

    def count_actions_for_user(self, user_id: int, action_type: str, start_date: datetime, end_date: datetime) -> int:
        res = self.fetchone("SELECT COUNT(*) as count FROM action_logs WHERE issuer_id = ? AND action_type = ? AND timestamp BETWEEN ? AND ?", (user_id, action_type, start_date.isoformat(), end_date.isoformat()))
        return res['count'] if res else 0

VK_TOKEN, GODMODE_KEY, DEFAULT_CMD_LEVELS, CASINO_CONFIG = load_config()
vk_api = API(token=VK_TOKEN)
bot = Bot(token=VK_TOKEN)
db = DatabaseManager(DB_FILE)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

moderation_logger = logging.getLogger('moderation_bot'); moderation_logger.setLevel(logging.INFO)
handler = logging.FileHandler(LOG_FILE, encoding='utf-8'); formatter = logging.Formatter('%(asctime)s - %(message)s')
handler.setFormatter(formatter); moderation_logger.addHandler(handler); moderation_logger.propagate = False

EMOJI = { "warning": "⚠️", "error": "❌", "success": "✅", "info": "ℹ️", "admin": "👑", "user": "👤", "command": "📌", "settings": "⚙️", "ban": "🔨", "warn": "⚠️", "time": "⏰", "list": "📋", "help": "❓", "crown": "👑", "star": "⭐", "lock": "🔒", "unlock": "🔓", "up": "⬆️", "down": "⬇️", "ok": "🆗", "blacklist": "⚫", "search": "🔎", "money": "💰", "game_die": "🎲", "slot_machine": "🎰", "megaphone": "📢", "request": "📩", "tag": "🏷️", "activity": "📊", "messages": "💬", "new_admin": "🧑‍💻", "kick": "🚪" }
POSITIONS = [ "Владелец", "Заместитель Владельца", "Разработчик", "Спец. Администратор", "Главный администратор", "Заместитель ГА", "Куратор", "Администратор 3-го уровня", "Администратор 2-го уровня", "Администратор 1-го уровня", "Без должности" ]
POSITION_ALIASES: Dict[str, str] = {}

def parse_mention(text: str) -> Optional[int]:
    match = re.search(r'\[id(\d+)\|', text)
    return int(match.group(1)) if match else None

def get_admin_by_mention_or_nick(text: str, chat_id: int) -> Optional[sqlite3.Row]:
    user_id = parse_mention(text)
    return db.get_admin_by_id(user_id, chat_id) if user_id else db.get_admin_by_nickname(text.strip().lower(), chat_id)

async def parse_target_and_args(message: Message) -> Tuple[Optional[int], Optional[sqlite3.Row], Optional[str]]:
    chat_id = message.peer_id
    full_args_text = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""
    
    if message.reply_message:
        target_id = message.reply_message.from_id
        target_admin = db.get_admin_by_id(target_id, chat_id)
        return target_id, target_admin, full_args_text
    
    if not full_args_text:
        return None, None, None
        
    parts = full_args_text.split(maxsplit=1)
    target_str, args_text = parts[0], parts[1] if len(parts) > 1 else ""
    
    target_admin = get_admin_by_mention_or_nick(target_str, chat_id)
    
    if target_admin:
        return target_admin['user_id'], target_admin, args_text
        
    target_id = parse_mention(target_str)
    if target_id:
        return target_id, db.get_admin_by_id(target_id, chat_id), args_text
        
    all_admins = db.get_all_admins(chat_id)
    possible_targets = sorted(
        [admin for admin in all_admins if full_args_text.lower().startswith(admin['nickname'].lower())],
        key=lambda x: len(x['nickname']),
        reverse=True
    )
    
    if possible_targets:
        best_match = possible_targets[0]
        args_text = full_args_text[len(best_match['nickname']):].strip()
        return best_match['user_id'], best_match, args_text
        
    return None, None, full_args_text

def find_position_by_alias(alias: str) -> Optional[str]:
    return POSITION_ALIASES.get(alias.lower().strip())

def parse_duration(time_str: str) -> Optional[timedelta]:
    match = re.match(r"(\d+)([smhd])", time_str.lower())
    if not match: return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 's': return timedelta(seconds=value)
    if unit == 'm': return timedelta(minutes=value)
    if unit == 'h': return timedelta(hours=value)
    if unit == 'd': return timedelta(days=value)
    return None

def format_profile(admin_local: sqlite3.Row, user_global: sqlite3.Row, chat_id: int) -> str:
    added_by_admin = db.get_admin_by_id(admin_local['added_by'], chat_id)
    added_by_name = added_by_admin['nickname'] if added_by_admin else "Неизвестно"
    return (f"{EMOJI['admin']} Профиль администратора (в этом чате) {EMOJI['admin']}\n\n"
            f"{EMOJI['user']} Ник: {admin_local['nickname']}\n"
            f"{EMOJI['crown']} Должность: {admin_local['position']}\n"
            f"{EMOJI['star']} Уровень: {admin_local['level']}\n"
            f"{EMOJI['money']} Глобальный баланс: {user_global['balance'] if user_global else 100} фишек\n"
            f"{EMOJI['time']} Дата добавления (в этот чат): {admin_local['added_date']}\n"
            f"{EMOJI['admin']} Добавил: {added_by_name}\n"
            f"{EMOJI['info']} Статус: {admin_local['status']}\n"
            f"{EMOJI['warn']} Предупреждений (в этом чате): {db.get_warnings_count(admin_local['user_id'], chat_id)}/2\n"
            f"{EMOJI['ban']} Выговоров (в этом чате): {db.get_reprimands_count(admin_local['user_id'], chat_id)}/3")

def log_action(user_id: int, action: str, target_id: Optional[int] = None, details: Optional[str] = None):
    if user_id == 0:
        user_nick = "СИСТЕМА"
    else:
        user_global = db.get_user_global_data(user_id)
        user_nick = user_global['nickname'] if user_global and user_global['nickname'] else f"ID{user_id}"
    
    target_info = ""
    if target_id:
        target_global = db.get_user_global_data(target_id)
        target_nick = target_global['nickname'] if target_global and target_global['nickname'] else f'ID{target_id}'
        target_info = f" [id{target_id}|{target_nick}]"
        
    details_info = f" ({details})" if details else ""
    moderation_logger.info(f"{user_nick} выполнил: {action}{target_info}{details_info}")

async def send_warning_notification(target_id: int, warning_type: str, reason: str, count: int, limit: int, chat_id: int):
    try:
        chat_info = await vk_api.messages.get_conversations_by_id(peer_ids=chat_id)
        chat_title = chat_info.items[0].chat_settings.title if chat_info.items else f"чате {chat_id}"
        await vk_api.messages.send(user_id=target_id, message=(f"{EMOJI['warn']} Вы получили {warning_type} в чате «{chat_title}»!\n{EMOJI['info']} Причина: {reason}\n{EMOJI['warning']} Текущее количество в этом чате: {count}/{limit}"), random_id=0)
    except Exception as e: logger.warning(f"Не удалось отправить уведомление: {e}")

async def deactivate_admin(admin: sqlite3.Row, peer_id: int):
    user_id_to_remove, nickname_to_remove = admin['user_id'], admin['nickname']
    db.snyat_adm(user_id_to_remove, peer_id)
    log_action(0, "автоматически снял админа (лимит нарушений)", user_id_to_remove, f"в чате {peer_id}")
    try: await vk_api.messages.remove_chat_user(chat_id=peer_id - 2000000000, user_id=user_id_to_remove)
    except Exception as e: logger.warning(f"Не удалось исключить из беседы {peer_id} пользователя {user_id_to_remove}: {e}")
    await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['ban']} Администратор {nickname_to_remove} автоматически снят с поста в этом чате!\n{EMOJI['info']} Причина: достигнут лимит нарушений.", random_id=0)

async def check_permission(message: Message, command_name: str) -> bool:
    if db.is_blacklisted(message.from_id): return False
    command_name, required_level = command_name.lower(), db.get_command_level(command_name, message.peer_id, 9)
    if required_level == 0: return True
    admin = db.get_admin_by_id(message.from_id, message.peer_id)
    if not admin:
        await message.answer(f"{EMOJI['error']} У вас нет прав администратора в этом чате.")
        return False
    if admin['level'] >= required_level: return True
    if ADMIN_CHAT_ID != 0 and command_name in COMMAND_HANDLERS:
        request_id = str(uuid.uuid4())[:8]
        pending_requests[request_id] = { "requester_id": message.from_id, "requester_level": admin['level'], "requester_nick": admin['nickname'], "chat_id": message.peer_id, "command_name": command_name, "command_text": message.text, "args_text": message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else None, }
        keyboard = Keyboard(inline=True).add(Text("✅ Отправить", payload={"action": "req_confirm", "id": request_id})).add(Text("❌ Отмена", payload={"action": "req_cancel", "id": request_id}))
        await message.answer(f"{EMOJI['warning']} Ваш уровень ({admin['level']}) ниже требуемого ({required_level}). Хотите отправить запрос на выполнение команды?", keyboard=keyboard.get_json())
    else:
        await message.answer(f"{EMOJI['error']} Недостаточно прав! (Требуется: {required_level}, ваш: {admin['level']})")
    return False

# Middleware и Планировщик
class MuteCheckMiddleware(BaseMiddleware[Message]):
    async def pre(self):
        if self.event.from_id < 0 or not self.event.peer_id: 
            return

        issuer_admin_local = db.get_admin_by_id(self.event.from_id, self.event.peer_id)
        issuer_global_data = db.get_user_global_data(self.event.from_id)

        if issuer_admin_local and (issuer_admin_local['level'] >= 8 or (issuer_global_data and issuer_global_data['dev_mode'])):
            return

        mute_info = db.get_active_mute(self.event.from_id, self.event.peer_id)
        if mute_info:
            try:
                await vk_api.messages.delete(peer_id=self.event.peer_id, cmids=[self.event.conversation_message_id], delete_for_all=1)
            except VKAPIError as e:
                if e.code == 925:
                    logger.warning(f"Не удалось удалить сообщение от {self.event.from_id}: Бот не админ.")
                else:
                    logger.error(f"Ошибка API при удалении сообщения от {self.event.from_id}: {e}")
            except Exception as e:
                logger.error(f"Не удалось удалить сообщение от замученного {self.event.from_id}: {e}")
            self.stop("User is muted.")

class MessageLoggingMiddleware(BaseMiddleware[Message]):
    async def pre(self):
        if self.event.from_id > 0 and self.event.peer_id:
            try: db.add_message(self.event.from_id, self.event.peer_id, datetime.now())
            except Exception as e: logger.error(f"Ошибка при логировании сообщения: {e}")

bot.labeler.message_view.register_middleware(MuteCheckMiddleware)
bot.labeler.message_view.register_middleware(MessageLoggingMiddleware)

async def check_expired_mutes():
    expired_mutes = db.get_expired_mutes()
    if not expired_mutes: return
    logger.info(f"Найдено {len(expired_mutes)} истекших мутов. Обработка...")
    for mute in expired_mutes:
        user_id, chat_id = mute['user_id'], mute['muted_in_chat_id']
        db.remove_mute(user_id, chat_id)
        log_action(0, "автоматически снял мут", user_id, f"время истекло в чате {chat_id}")
        if chat_id:
            try:
                user_info = (await vk_api.users.get(user_ids=[user_id]))[0]
                await vk_api.messages.send(peer_id=chat_id, message=f"{EMOJI['unlock']} С пользователя [id{user_id}|{user_info.first_name}] автоматически снят мут.", random_id=0, disable_mentions=1)
            except Exception as e: logger.error(f"Не удалось отправить уведомление о снятии мута в чат {chat_id}: {e}")

async def startup_task():
    scheduler.add_job(check_expired_mutes, 'interval', seconds=30)
    scheduler.start()
    logger.info("Планировщик задач запущен.")

# Система запросов
@bot.on.message(PayloadContainsRule({"action": "req_cancel"}))
async def handle_request_cancel(message: Message):
    payload = message.get_payload_json(); request_id = payload.get("id")
    if request_id in pending_requests: del pending_requests[request_id]
    try: await bot.api.messages.edit(peer_id=message.peer_id, conversation_message_id=message.conversation_message_id, message=f"{EMOJI['info']} Запрос отменен.")
    except VKAPIError: await message.answer(f"{EMOJI['info']} Запрос отменен.")
@bot.on.message(PayloadContainsRule({"action": "req_confirm"}))
async def handle_request_confirm(message: Message):
    payload = message.get_payload_json(); request_id = payload.get("id"); request_data = pending_requests.get(request_id)
    if not request_data or not ADMIN_CHAT_ID: return await message.answer("❌ Запрос устарел или не настроен чат для администраторов.")
    admin_keyboard = Keyboard(inline=True).add(Text("✅ Одобрить", payload={"action": "req_approve", "id": request_id})).add(Text("❌ Отклонить", payload={"action": "req_deny", "id": request_id}))
    try:
        await bot.api.messages.send(peer_id=ADMIN_CHAT_ID, message=(f"{EMOJI['request']} Новый запрос!\nID: `{request_id}`\nОт: [id{request_data['requester_id']}|{request_data['requester_nick']}] (Ур: {request_data['requester_level']})\nКоманда: `{request_data['command_text']}`"), keyboard=admin_keyboard.get_json(), random_id=0)
        await bot.api.messages.edit(peer_id=message.peer_id, conversation_message_id=message.conversation_message_id, message=f"{EMOJI['success']} Ваш запрос с ID `{request_id}` отправлен на рассмотрение.")
    except VKAPIError as e: logger.error(f"Не удалось отправить запрос в рукво-чат: {e}")
@bot.on.message(PayloadContainsRule({"action": "req_approve"}))
@bot.on.message(PayloadContainsRule({"action": "req_deny"}))
async def handle_request_decision(message: Message):
    payload = message.get_payload_json(); request_id = payload.get("id"); decision = "approve" if payload['action'] == "req_approve" else "deny"
    try: await bot.api.messages.delete(peer_id=message.peer_id, conversation_message_ids=[message.conversation_message_id], delete_for_all=1)
    except VKAPIError: logger.warning("Не удалось удалить сообщение с кнопками в рукво-чат.")
    await process_decision(message.from_id, request_id, decision)
async def process_decision(approver_id: int, request_id: str, decision: str):
    request_data = pending_requests.get(request_id)
    if not request_data: return
    chat_id = request_data['chat_id']
    approver_admin = db.get_admin_by_id(approver_id, chat_id)
    if not approver_admin: return 
    requester_id, command_name, command_text, args_text = request_data['requester_id'], request_data['command_name'], request_data['command_text'], request_data['args_text']
    required_level = db.get_command_level(command_name, chat_id)
    if approver_admin['level'] < required_level: return await bot.api.messages.send(peer_id=ADMIN_CHAT_ID, message=f"{EMOJI['error']} У вас недостаточно прав для этого действия.", random_id=0)
    if decision == "approve":
        handler = COMMAND_HANDLERS.get(command_name)
        if handler:
            await handler(requester_id, chat_id, args_text)
            await bot.api.messages.send(peer_id=ADMIN_CHAT_ID, message=f"✅ Запрос `{request_id}` на `{command_text}` одобрен [id{approver_id}|{approver_admin['nickname']}]", random_id=0)
            log_action(approver_id, f"одобрил запрос от {request_data['requester_nick']}", details=f"ID {request_id}: {command_text}")
    else:
        await bot.api.messages.send(peer_id=ADMIN_CHAT_ID, message=f"❌ Запрос `{request_id}` на `{command_text}` отклонен [id{approver_id}|{approver_admin['nickname']}]", random_id=0)
        log_action(approver_id, f"отклонил запрос от {request_data['requester_nick']}", details=f"ID {request_id}: {command_text}")
    if request_id in pending_requests: del pending_requests[request_id]

# Основные команды
@bot.on.message(text="/help")
async def help_cmd(message: Message):
    if not await check_permission(message, "help"): return
    help_text = f"""{EMOJI['help']} Список команд бота {EMOJI['help']}

{EMOJI['command']} Основные команды
/admins - Список всех администраторов.
/profile [@упом/ник] - Профиль и статистика активности.
.adm <часть_ника> - Найти ВК админа по нику.
/zov <текст> - Оповестить всех участников чата.
/test - Проверить работоспособность бота.

{EMOJI['settings']} Управление чатом
/rules - Показать правила чата.
/setrules <текст> - (Адм) Установить правила.
/setwelcome <текст> - (Адм) Установить приветствие.
/clear <число> - (Адм) Удалить последние сообщения.

{EMOJI['admin']} Админские команды
/newadmin @упом ник - Добавить админа.
/kick @упом/ник - Снять админа и исключить.
/reactivate @упом/ник - Восстановить снятого админа.
/setdj @упом/ник [должность] - Установить должность.
/setnick @упом/ник Новый_ник - Изменить ник.
/setlvl @упом/ник уровень - Изменить уровень (0-9).
/editcmd <команда> <уровень> - Изменить доступ к команде.

{EMOJI['ban']} Наказания
/warn @упом/ник [причина] - Выдать выговор (лимит 3).
/unwarn @упом/ник [причина] - Снять выговор.
/pred @упом/ник [причина] - Выдать предупреждение (лимит 2).
/unpred @упом/ник [причина] - Снять предупреждение.
/mute @упом <время> [причина] - Выдать мут (10s, 5m, 2h, 1d).
/unmute @упом - Снять мут.

{EMOJI['list']} Логи, ЧС и Активность
/logs [@упом/ник] - Показать логи действий.
/msgcount [@упом/ник] [с ДД.ММ.ГГГГ] [по ДД.ММ.ГГГГ] - (Адм) Сообщения админа.
/bladd @упом [причина] - Добавить в ЧС бота.
/blrem @упом - Убрать из ЧС.
/bllist - Показать черный список.

{EMOJI['slot_machine']} Казино
/bal - Показать ваш баланс фишек.
/daily - Получить ежедневный бонус.
/top - Топ-5 самых богатых игроков.
/pay @упом <сумма> - Перевести фишки другому.
/giverub @упом <сумма> - (Адм) Выдать фишки.
/dice <ставка> - Сыграть в кости.
/slots <ставка> - Сыграть в игровой автомат.

{EMOJI['tag']} Система тегов (FAQ)
/tag <название> - Показать информацию из тега.
/taglist - Показать список всех тегов.
/addtag <название> <текст> - (Адм) Создать тег.
/deltag <название> - (Адм) Удалить тег.
"""
    await message.answer(help_text)
@bot.on.message(text="/test")
async def test_cmd(message: Message): await message.answer(f"{EMOJI['success']} Бот работает! Peer ID: {message.peer_id}")

@bot.on.message(text=["/ai", "/ai <text>"])
async def ai_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "ai"):
        return
    if not text:
        return await message.answer(f"{EMOJI['error']} Пожалуйста, введите ваш вопрос после команды.\nПример: /ai Что такое черная дыра?")

    api_url = ""

    payload = {
        "contents": [{"parts": [{"text": text}]}]
    }
    
    headers = {
        'Content-Type': 'application/json',
        'X-goog-api-key': GEMINI_API_KEY
    }

    processing_message = await message.answer("🧠 Думаю...")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(api_url, json=payload, headers=headers)
            response.raise_for_status()  
            
            data = response.json()
            
            if 'candidates' in data and data['candidates']:
                content = data['candidates'][0].get('content', {})
                if 'parts' in content and content['parts']:
                    result_text = content['parts'][0].get('text', '')
                    if result_text:
                        final_response = f"🤖 Ответ от Gemini:\n\n{result_text}"
                        await bot.api.messages.edit(
                            peer_id=message.peer_id,
                            conversation_message_id=processing_message.conversation_message_id,
                            message=final_response[:4096] 
                        )
                        return
            
            await bot.api.messages.edit(
                peer_id=message.peer_id,
                conversation_message_id=processing_message.conversation_message_id,
                message=f"{EMOJI['warning']} Не удалось получить ответ. Возможно, ваш запрос был заблокирован из-за правил безопасности."
            )

    except httpx.HTTPStatusError as e:
        logger.error(f"AI command failed with HTTP status error: {e.response.status_code} - {e.response.text}")
        try:
            error_details = e.response.json().get("error", {}).get("message", "Нет деталей")
        except:
            error_details = e.response.text
        await bot.api.messages.edit(
            peer_id=message.peer_id,
            conversation_message_id=processing_message.conversation_message_id,
            message=f"{EMOJI['error']} Ошибка API ({e.response.status_code}): {error_details}"
        )
    except httpx.RequestError as e:
        logger.error(f"AI command failed with request error: {e}")
        await bot.api.messages.edit(
            peer_id=message.peer_id,
            conversation_message_id=processing_message.conversation_message_id,
            message=f"{EMOJI['error']} Произошла ошибка сети при обращении к AI. Попробуйте позже."
        )
    except Exception as e:
        logger.critical(f"An unexpected error occurred in AI command: {e}")
        await bot.api.messages.edit(
            peer_id=message.peer_id,
            conversation_message_id=processing_message.conversation_message_id,
            message=f"{EMOJI['error']} Произошла непредвиденная ошибка при обработке вашего запроса."
        )
@bot.on.message(text=["/profile", "/profile <text>"])
async def profile_cmd(message: Message, text: Optional[str] = None):
    
    
    admin_to_show = None
    target_id = message.from_id
    
    if message.reply_message: 
        target_id = message.reply_message.from_id
    elif text:
        parsed_id, parsed_admin, _ = await parse_target_and_args(message)
        if parsed_admin:
            target_id = parsed_admin['user_id']
    
    admin_to_show = db.get_admin_by_id(target_id, message.peer_id)
    if not admin_to_show: 
        return await message.answer(f"{EMOJI['error']} Пользователь не является администратором в этом чате!")

    if not await check_permission(message, "profile"): return
    
    user_global_data = db.get_user_global_data(target_id)
    
    keyboard = Keyboard(inline=True).add(
        Text("Последние действия", payload={"cmd": "plogs", "user_id": admin_to_show['user_id'], "chat_id": message.peer_id})
    ).row().add(
        Text("Активность", payload={"cmd": "activity", "user_id": admin_to_show['user_id'], "chat_id": message.peer_id})
    )
    
    await message.answer(format_profile(admin_to_show, user_global_data, message.peer_id), keyboard=keyboard.get_json())
@bot.on.message(text="/admins")
async def admins_cmd(message: Message):
    if not await check_permission(message, "admins"): return
    all_admins = db.get_all_admins(message.peer_id)
    if not all_admins: return await message.answer(f"{EMOJI['list']} Список администраторов этого чата пуст.")
    admin_list = "\n".join(f"{i+1}. [id{a['user_id']}|{a['nickname']}] ({a['position']}, ур: {a['level']})" for i, a in enumerate(all_admins))
    await message.answer(f"{EMOJI['list']} Администраторы этого чата:\n\n{admin_list}", disable_mentions=1)

@bot.on.message(text=[".adm", ".adm <search_nick>"])
async def adm_search_cmd(message: Message, search_nick: Optional[str] = None):
    if not await check_permission(message, "adm"): return
    if not search_nick: return await message.answer(f"{EMOJI['error']} Неверный формат! Правильно: .adm <часть_ника>")
    found_admins = db.get_admins_by_nick_part(search_nick, message.peer_id)
    if not found_admins: return await message.answer(f"{EMOJI['error']} Администратор с ником, содержащим '{search_nick}', не найден в этом чате.")
    if len(found_admins) == 1:
        admin = found_admins[0]
        return await message.answer(f"{EMOJI['success']} Найден: {admin['nickname']}\n{EMOJI['user']} ВК: https://vk.com/id{admin['user_id']}")
    response_text = f"{EMOJI['warning']} Найдено несколько:\n\n" + "\n".join(f"{i}. {admin['nickname']} - https://vk.com/id{admin['user_id']}" for i, admin in enumerate(found_admins[:10], 1))
    await message.answer(response_text)

# Система тегов (FAQ)
@bot.on.message(text=["/addtag", "/addtag <text>"])
async def addtag_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "addtag"): return
    if not text or len(text.split(maxsplit=1)) < 2: 
        return await message.answer(f"{EMOJI['error']} Формат: /addtag <название> <текст тега>")
        
    name, content = text.split(maxsplit=1)
    if ' ' in name: 
        return await message.answer(f"{EMOJI['error']} Название тега не должно содержать пробелов.")
        
    if db.get_tag(name, message.peer_id): 
        return await message.answer(f"{EMOJI['error']} Тег с названием '{name.lower()}' уже существует в этом чате.")
        
    db.add_tag(name, content, message.from_id, message.peer_id)
    log_action(message.from_id, "создал тег", details=f"название: {name.lower()} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Тег '{name.lower()}' успешно создан для этого чата!")

@bot.on.message(text=["/deltag", "/deltag <name>"])
async def deltag_cmd(message: Message, name: Optional[str] = None):
    if not await check_permission(message, "deltag"): return
    if not name: 
        return await message.answer(f"{EMOJI['error']} Формат: /deltag <название>")
        
    tag = db.get_tag(name, message.peer_id)
    if not tag: 
        return await message.answer(f"{EMOJI['error']} Тег '{name.lower()}' не найден в этом чате.")
        
    db.remove_tag(name, message.peer_id)
    log_action(message.from_id, "удалил тег", details=f"название: {name.lower()} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Тег '{name.lower()}' удален.")

@bot.on.message(text=["/tag", "/tag <name>"])
async def tag_cmd(message: Message, name: Optional[str] = None):
    if not await check_permission(message, "tag"): return
    if not name: 
        return await message.answer(f"{EMOJI['error']} Формат: /tag <название>")
        
    tag = db.get_tag(name, message.peer_id)
    if not tag: 
        return await message.answer(f"{EMOJI['error']} Тег '{name.lower()}' не найден в этом чате. Посмотрите список тегов: /taglist")
        
    creator = db.get_admin_by_id(tag['creator_id'], message.peer_id)
    creator_info = f"[id{creator['user_id']}|{creator['nickname']}]" if creator else "Неизвестно"
    await message.answer(f"{EMOJI['tag']} Тег: {tag['name']}\nАвтор: {creator_info}\n\n{tag['content']}")

@bot.on.message(text="/taglist")
async def taglist_cmd(message: Message):
    if not await check_permission(message, "taglist"): return
    
    all_tags = db.get_all_tags(message.peer_id)
    
    if not all_tags: 
        return await message.answer(f"{EMOJI['info']} Список тегов для этого чата пуст. Создайте первый с помощью /addtag.")
        
    tag_names = ", ".join([tag['name'] for tag in all_tags])
    await message.answer(f"{EMOJI['list']} Доступные теги в этом чате:\n{tag_names}")

# Команды администрирования
@bot.on.message(text=["/newadmin", "/newadmin <text>"])
async def newadmin_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "newadmin"): return
    if not text or len(text.split(maxsplit=1)) < 2: return await message.answer(f"{EMOJI['error']} Формат: /newadmin @упом Ник")
    mention, nickname = text.split(maxsplit=1)
    user_id = parse_mention(mention)
    if not user_id: return await message.answer(f"{EMOJI['error']} Укажите корректное упоминание!")
    if db.get_admin_by_id(user_id, message.peer_id): return await message.answer(f"{EMOJI['error']} Этот пользователь уже является администратором в этом чате!")
    
    db.add_admin(user_id, message.peer_id, nickname, message.from_id)
    log_action(message.from_id, "добавил администратора", user_id, f"ник: {nickname} в чате {message.peer_id}")
    db.add_structured_action(message.from_id, 'add_admin', user_id, details=f"chat_id:{message.peer_id}")
    await message.answer(f"{EMOJI['success']} Администратор [id{user_id}|{nickname}] успешно добавлен в этом чате!")

@bot.on.message(text=["/createdj", "/createdj <name>"])
async def createdj_cmd(message: Message, name: Optional[str] = None):
    if not await check_permission(message, "createdj"): return 
    if not name:
        return await message.answer(f"{EMOJI['error']} Формат: /createdj <Название должности>")

    if db.position_exists(name, message.peer_id):
        return await message.answer(f"{EMOJI['error']} Должность '{name}' уже существует в этом чате (или является глобальной).")
    
    db.add_position(name, message.peer_id)
    log_action(message.from_id, "создал должность", details=f"'{name}' в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Должность '{name}' успешно создана для этого чата!")

@bot.on.message(text=["/deletedj", "/deletedj <name>"])
async def deletedj_cmd(message: Message, name: Optional[str] = None):
    if not await check_permission(message, "deletedj"): return
    if not name:
        return await message.answer(f"{EMOJI['error']} Формат: /deletedj <Название должности>")

    local_pos = db.fetchone("SELECT 1 FROM positions WHERE name = ? AND chat_id = ?", (name, message.peer_id))
    if not local_pos:
        return await message.answer(f"{EMOJI['error']} Должность '{name}' не найдена в этом чате или является глобальной (глобальные должности удалять нельзя).")
    
    db.delete_position(name, message.peer_id)
    log_action(message.from_id, "удалил должность", details=f"'{name}' в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Должность '{name}' удалена из этого чата. У администраторов с этой должностью она будет сброшена на 'Без должности'.")

@bot.on.message(text=["/peremdj", "/peremdj <text>"])
async def peremdj_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "peremdj"): return
    if not text or '|' not in text:
        return await message.answer(f"{EMOJI['error']} Формат: /peremdj <Старое название> | <Новое название>")

    parts = text.split('|', 1)
    old_name, new_name = parts[0].strip(), parts[1].strip()

    if not old_name or not new_name:
        return await message.answer(f"{EMOJI['error']} Оба названия (старое и новое) должны быть указаны.")
    
    local_pos = db.fetchone("SELECT 1 FROM positions WHERE name = ? AND chat_id = ?", (old_name, message.peer_id))
    if not local_pos:
        return await message.answer(f"{EMOJI['error']} Должность '{old_name}' не найдена в этом чате или является глобальной (глобальные должности переименовывать нельзя).")

    if db.position_exists(new_name, message.peer_id):
        return await message.answer(f"{EMOJI['error']} Должность '{new_name}' уже существует.")

    db.rename_position(old_name, new_name, message.peer_id)
    log_action(message.from_id, "переименовал должность", details=f"'{old_name}' -> '{new_name}' в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Должность '{old_name}' переименована в '{new_name}'. У всех администраторов в этом чате должность также обновлена.")

@bot.on.message(text=["/setdj <text>", "/setdj"])
async def setdj_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "setdj"): return
    
    if not text and not message.reply_message:
        positions_rows = db.get_positions_for_chat(message.peer_id)
        positions_list = "\n".join([f"• {p['name']}" for p in positions_rows])
        return await message.answer(f"{EMOJI['error']} Формат: /setdj @упом/ник Должность\n\n{EMOJI['list']} Доступные должности в этом чате:\n{positions_list}")

    target_id, admin, new_position_input = await parse_target_and_args(message)
    if not admin: 
        return await message.answer(f"{EMOJI['error']} Администратор не найден в этом чате.")

    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1 and message.from_id != admin['user_id']: 
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому администратору.")

    if not new_position_input: 
        return await message.answer(f"{EMOJI['error']} Не указана новая должность!")
        
    # if not db.position_exists(new_position_input.strip(), message.peer_id):
    #    return await message.answer(f"{EMOJI['error']} Должность '{new_position_input.strip()}' не найдена в этом чате! Проверьте список доступных должностей командой /setdj.")
        
    db.update_admin(admin['user_id'], message.peer_id, 'position', new_position_input.strip())
    log_action(message.from_id, "изменил должность для", admin['user_id'], f"новое: {new_position_input.strip()} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Должность администратора {admin['nickname']} изменена на '{new_position_input.strip()}'!")
    
@bot.on.message(text=["/setnick <text>", "/setnick"])
async def setnick_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "setnick"): return
    
    target_id, admin, new_value = await parse_target_and_args(message)
    if not admin: 
        return await message.answer(f"{EMOJI['error']} Администратор не найден в этом чате.")
    
    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1 and message.from_id != admin['user_id']: 
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому администратору.")

    if not new_value: 
        return await message.answer(f"{EMOJI['error']} Не указано новое значение!")
        
    if db.get_admin_by_nickname(new_value, message.peer_id): 
        return await message.answer(f"{EMOJI['error']} Ник '{new_value}' уже занят в этом чате!")
    
    old_nick = admin['nickname']
    db.update_admin(admin['user_id'], message.peer_id, 'nickname', new_value)
    db.update_global_nickname(admin['user_id'], new_value)
    
    log_action(message.from_id, f"изменил ник для", admin['user_id'], f"старый: {old_nick}, новый: {new_value} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Ник изменен с {old_nick} на {new_value}!")
    
@bot.on.message(text=["/setlvl <text>", "/setlvl"])
async def setlvl_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "setlvl"): return
    
    target_id, admin, value = await parse_target_and_args(message)
    if not admin: 
        return await message.answer(f"{EMOJI['error']} Администратор не найден в этом чате.")
    
    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1 and message.from_id != admin['user_id']: 
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому администратору.")
        
    if not value: 
        return await message.answer(f"{EMOJI['error']} Не указан уровень!")
        
    try: 
        level = int(value)
        assert 0 <= level <= 9
    except: 
        return await message.answer(f"{EMOJI['error']} Уровень должен быть числом от 0 до 9!")
        
    issuer = db.get_admin_by_id(message.from_id, message.peer_id)
    if admin['level'] >= issuer['level'] and message.from_id != admin['user_id']: 
        return await message.answer(f"{EMOJI['error']} Нельзя менять уровень админа с равным/большим уровнем!")
        
    db.update_admin(admin['user_id'], message.peer_id, 'level', level)
    log_action(message.from_id, "изменил уровень для", admin['user_id'], f"новое: {level} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Уровень {admin['nickname']} изменен на {level}!")
    
@bot.on.message(text=["/kick", "/kick <text>"])
async def kick_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "kick"): return
    
    target_id, target_admin, _ = await parse_target_and_args(message)
    if not target_admin: 
        return await message.answer(f"{EMOJI['error']} Администратор не найден в этом чате.")
    
    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1: 
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому администратору.")
        
    issuer = db.get_admin_by_id(message.from_id, message.peer_id)
    if not issuer: 
        return await message.answer(f"{EMOJI['error']} Не удалось определить ваши права администратора.")

    if target_admin['level'] >= issuer['level']: 
        return await message.answer(f"{EMOJI['error']} Нельзя снять админа с равным/большим уровнем!")
    
    db.snyat_adm(target_admin['user_id'], message.peer_id)
    db.add_structured_action(message.from_id, 'kick_admin', target_admin['user_id'], details=f"chat_id:{message.peer_id}")

    try:
        await bot.api.messages.remove_chat_user(
            chat_id=message.peer_id - 2000000000, 
            user_id=target_admin['user_id']
        )
        log_action(message.from_id, "снял с поста и исключил из чата", target_admin['user_id'], f"в чате {message.peer_id}")
        await message.answer(f"{EMOJI['kick']} Пользователь [id{target_admin['user_id']}|{target_admin['nickname']}] снят с поста и исключен из чата!")

    except VKAPIError as e:
        logger.warning(f"Не удалось исключить {target_admin['user_id']} из чата {message.peer_id}: {e}")
        log_action(message.from_id, "снял с поста админа (не удалось исключить)", target_admin['user_id'], f"в чате {message.peer_id}")
        await message.answer(f"{EMOJI['ban']} Пользователь [id{target_admin['user_id']}|{target_admin['nickname']}] снят с поста администратора, "
                             f"но не удалось исключить из чата (Ошибка API: {e.code}). Пожалуйста, сделайте это вручную.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при исключении пользователя: {e}")
        await message.answer(f"{EMOJI['ban']} Пользователь [id{target_admin['user_id']}|{target_admin['nickname']}] снят с поста, "
                             f"но при исключении из чата произошла неизвестная ошибка.")
@bot.on.message(text=["/reactivate", "/reactivate <text>"])
async def reactivate_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "reactivate"): return
    target_id, admin, _ = await parse_target_and_args(message)
    if not admin: return await message.answer(f"{EMOJI['error']} Администратор не найден.")
    if admin['status'] != "Снят": return await message.answer(f"{EMOJI['error']} Администратор не снят!")
    db.reactivate_admin(admin['user_id'])
    log_action(message.from_id, "восстановил администратора", admin['user_id'])
    try: await vk_api.messages.send(user_id=admin['user_id'], message=f"{EMOJI['success']} Вы восстановлены!", random_id=0)
    except Exception as e: logger.warning(f"Уведомление о восстановлении не отправлено: {e}")
    await message.answer(f"{EMOJI['success']} Администратор [id{admin['user_id']}|{admin['nickname']}] восстановлен!")

# Команды управления чатом
@bot.on.chat_message(action="chat_invite_user")
async def welcome_new_user(message: Message):
    if not message.action or message.action.member_id <= 0: return
    chat_id, new_user_id = message.peer_id, message.action.member_id
    welcome_text_template = db.get_chat_setting(chat_id, 'welcome_text') or ("Добро пожаловать в наш чат, {user}!\n" "Пожалуйста, ознакомьтесь с правилами командой /rules.")
    try:
        user_info = (await vk_api.users.get(user_ids=[new_user_id]))[0]
        user_mention = f"[id{user_info.id}|{user_info.first_name} {user_info.last_name}]"
        final_message = welcome_text_template.replace('{user}', user_mention)
        await message.answer(final_message)
        logger.info(f"В чат {chat_id} присоединился новый пользователь {user_mention}, отправлено приветствие.")
    except Exception as e: logger.error(f"Не удалось отправить приветствие в чат {chat_id}: {e}")
@bot.on.message(text=["/setwelcome", "/setwelcome <text>"])
async def set_welcome_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "setwelcome"): return
    if not text: return await message.answer(f"{EMOJI['error']} Неверный формат! Используйте:\n/setwelcome <текст приветствия>\n\n{EMOJI['info']} Вы можете использовать {{user}} для упоминания нового участника.\nПример: /setwelcome Привет, {{user}}! Рады тебя видеть. ")
    db.set_chat_setting(message.peer_id, 'welcome_text', text)
    log_action(message.from_id, "установил приветственное сообщение", details=f"в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Приветственное сообщение для этого чата установлено!")
@bot.on.message(text=["/setrules", "/setrules <text>"])
async def set_rules_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "setrules"): return
    if not text: return await message.answer(f"{EMOJI['error']} Неверный формат! Используйте: /setrules <текст правил>")
    db.set_chat_setting(message.peer_id, 'rules', text)
    log_action(message.from_id, "установил правила", details=f"в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Правила для этого чата успешно установлены!")
@bot.on.message(text="/rules")
async def rules_cmd(message: Message):
    if not await check_permission(message, "rules"): return
    rules_text = db.get_chat_setting(message.peer_id, 'rules')
    if rules_text: await message.answer(f"{EMOJI['list']} Правила чата:\n\n{rules_text}")
    else: await message.answer(f"{EMOJI['info']} Правила для этого чата еще не установлены. Администратор может сделать это командой /setrules <текст>. ")
@bot.on.message(text=["/clear", "/clear <count_str>"])
async def clear_cmd(message: Message, count_str: Optional[str] = None):
    if not await check_permission(message, "clear"): return
    if not count_str or not count_str.isdigit(): return await message.answer(f"{EMOJI['error']} Формат: /clear <число от 1 до 100>")
    count = int(count_str)
    if not 1 <= count <= 100: return await message.answer(f"{EMOJI['error']} Укажите число от 1 до 100.")
    try:
        history = await bot.api.messages.get_history(peer_id=message.peer_id, count=count + 1)
        cmids_to_delete = [msg.conversation_message_id for msg in history.items if msg.conversation_message_id > 0]
        if not cmids_to_delete: return await message.answer(f"{EMOJI['info']} Не найдено сообщений для удаления.")
        await bot.api.messages.delete(peer_id=message.peer_id, cmids=cmids_to_delete, delete_for_all=1)
        log_action(message.from_id, f"очистил чат", details=f"удалил {len(cmids_to_delete)} сообщ. в чате {message.peer_id}")
    except VKAPIError as e:
        if e.code == 917: await message.answer(f"{EMOJI['error']} Ошибка: я не являюсь администратором в этом чате и не могу удалять сообщения.")
        elif e.code == 924: await message.answer(f"{EMOJI['error']} Ошибка: не могу удалить некоторые сообщения (возможно, они старше 24 часов).")
        else: logger.error(f"Ошибка API при очистке чата {message.peer_id}: {e}"); await message.answer(f"{EMOJI['error']} Произошла неизвестная ошибка API при попытке удаления. {e}")
    except Exception as e: logger.error(f"Неизвестная ошибка при очистке чата {message.peer_id}: {e}"); await message.answer(f"{EMOJI['error']} Произошла внутренняя ошибка.")

# Команды наказаний
async def internal_punishment_handler(issuer_id: int, peer_id: int, message: Message, cmd: str, is_add: bool):
    target_id, admin, reason = await parse_target_and_args(message)
    if not admin: 
        return await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['error']} Администратор не найден.", random_id=0)
    
    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1: 
        return await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['lock']} Действие не может быть применено к этому администратору.", random_id=0)
        
    reason = reason or "Не указана"
    issuer = db.get_admin_by_id(issuer_id, peer_id)
    if admin['level'] >= issuer['level'] and admin['user_id'] != issuer['user_id']: 
        return await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['error']} Нельзя взаимодействовать с админом равного/большего уровня!", random_id=0)
    
    if is_add:
        if "pred" in cmd:
            db.add_warning(admin['user_id'], issuer['user_id'], reason, peer_id)
            warn_count = db.get_warnings_count(admin['user_id'], peer_id)
            log_action(issuer['user_id'], "выдал предупреждение", admin['user_id'], f"причина: {reason} в чате {peer_id}")
            db.add_structured_action(issuer['user_id'], 'issue_pred', admin['user_id'], details=reason)
            await send_warning_notification(admin['user_id'], "предупреждение", reason, warn_count, 2, peer_id)
            await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['warn']} {admin['nickname']} выдано предупреждение! (в этом чате: {warn_count}/2)", random_id=0)
            if warn_count >= 2:
                db.clear_warnings(admin['user_id'], peer_id)
                db.add_reprimand(admin['user_id'], 0, "Автоматически за 2/2 предупреждения", peer_id)
                reprimand_count = db.get_reprimands_count(admin['user_id'], peer_id)
                log_action(0, "автоматически выдал выговор (2/2 пред.)", admin['user_id'], f"в чате {peer_id}")
                db.add_structured_action(0, 'issue_warn', admin['user_id'], details="Автоматически за 2/2 пред.")
                await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['info']} {admin['nickname']} набрал 2/2 предупреждения в этом чате! Они сброшены и конвертированы в +1 выговор. Текущее кол-во выговоров в этом чате: {reprimand_count}/3.", random_id=0)
                await send_warning_notification(admin['user_id'], "выговор", "Автоматически за 2/2 предупреждения", reprimand_count, 3, peer_id)
                if reprimand_count >= 3: await deactivate_admin(admin, peer_id)
        else: # "warn"
            db.add_reprimand(admin['user_id'], issuer['user_id'], reason, peer_id)
            count = db.get_reprimands_count(admin['user_id'], peer_id)
            log_action(issuer['user_id'], "выдал выговор", admin['user_id'], f"причина: {reason} в чате {peer_id}")
            db.add_structured_action(issuer['user_id'], 'issue_warn', admin['user_id'], details=reason)
            await send_warning_notification(admin['user_id'], "выговор", reason, count, 3, peer_id)
            await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['ban']} {admin['nickname']} выдан выговор! (в этом чате: {count}/3)", random_id=0)
            if count >= 3: await deactivate_admin(admin, peer_id)
    else: # Снятие наказания
        last_punishment = db.remove_last_warning(admin['user_id'], peer_id) if "pred" in cmd else db.remove_last_reprimand(admin['user_id'], peer_id)
        if not last_punishment: 
            msg = "предупреждений" if "pred" in cmd else "выговоров"
            return await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['error']} У админа нет {msg} в этом чате!", random_id=0)
        
        issuer_last_global = db.get_user_global_data(last_punishment['issuer_user_id'])
        issuer_last_local = db.get_admin_by_id(last_punishment['issuer_user_id'], peer_id)
        
        if issuer_last_local and issuer_last_local['level'] >= issuer['level'] and issuer_last_local['user_id'] != issuer['user_id']:
            if "pred" in cmd: db.add_warning(admin['user_id'], issuer_last_local['user_id'], last_punishment['reason'], peer_id)
            else: db.add_reprimand(admin['user_id'], issuer_last_local['user_id'], last_punishment['reason'], peer_id)
            return await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['error']} Нельзя снять наказание от админа с равным/большим уровнем! ({issuer_last_global['nickname']})", random_id=0)

        count = db.get_warnings_count(admin['user_id'], peer_id) if "pred" in cmd else db.get_reprimands_count(admin['user_id'], peer_id)
        limit, msg = (2, "предупреждение") if "pred" in cmd else (3, "выговор")
        log_action(issuer['user_id'], f"снял {msg}", admin['user_id'], f"причина: {reason} в чате {peer_id}")
        await bot.api.messages.send(peer_id=peer_id, message=f"{EMOJI['success']} С {admin['nickname']} снят {msg}! (в этом чате: {count}/{limit})", random_id=0)
        
@bot.on.message(text=["/pred <text>", "/pred"])
async def pred_cmd(message: Message, text: Optional[str] = None):
    if await check_permission(message, "pred"): await internal_punishment_handler(message.from_id, message.peer_id, message, "pred", True)
@bot.on.message(text=["/unpred <text>", "/unpred"])
async def unpred_cmd(message: Message, text: Optional[str] = None):
    if await check_permission(message, "unpred"): await internal_punishment_handler(message.from_id, message.peer_id, message, "unpred", False)
@bot.on.message(text=["/warn <text>", "/warn"])
async def warn_cmd(message: Message, text: Optional[str] = None):
    if await check_permission(message, "warn"): await internal_punishment_handler(message.from_id, message.peer_id, message, "warn", True)
@bot.on.message(text=["/unwarn <text>", "/unwarn"])
async def unwarn_cmd(message: Message, text: Optional[str] = None):
    if await check_permission(message, "unwarn"): await internal_punishment_handler(message.from_id, message.peer_id, message, "unwarn", False)
@bot.on.message(text=["/mute", "/mute <text>"])
async def mute_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "mute"): return
    
    target_id, target_admin, args_text = await parse_target_and_args(message)
    if not target_id: 
        return await message.answer(f"{EMOJI['error']} Цель не указана. Ответьте на сообщение или используйте @упом/ник.")

    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1: 
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому пользователю.")
        
    if not args_text: 
        return await message.answer(f"{EMOJI['error']} Формат: /mute <время> [причина]")
        
    parts = args_text.split()
    time_str, *reason_parts = parts
    reason = " ".join(reason_parts) if reason_parts else "Не указана"
    
    if target_admin:
        issuer = db.get_admin_by_id(message.from_id, message.peer_id)
        if target_admin['level'] >= issuer['level'] and target_admin['user_id'] != issuer['user_id']: 
            return await message.answer(f"{EMOJI['error']} Нельзя замутить админа с равным/большим уровнем!")
            
    duration = parse_duration(time_str)
    if not duration: 
        return await message.answer(f"{EMOJI['error']} Неверный формат времени! (10s, 5m, 2h, 1d)")
        
    end_time = datetime.now() + duration
    db.add_mute(target_id, message.from_id, end_time, reason, message.peer_id)
    log_action(message.from_id, "выдал мут", target_id, f"до {end_time.strftime('%Y-%m-%d %H:%M')}, причина: {reason}")
    
    try: 
        user_info = (await vk_api.users.get(user_ids=[target_id]))[0]
        name = user_info.first_name
    except: 
        name = target_global_data['nickname'] if target_global_data and target_global_data['nickname'] else f"Пользователь {target_id}"
        
    await message.answer(f"{EMOJI['lock']} [id{target_id}|{name}] получил мут на {time_str}.\nПричина: {reason}")
@bot.on.message(text=["/unmute", "/unmute <text>"])
async def unmute_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "unmute"): return
    target_id, target_admin, _ = await parse_target_and_args(message)
    if not target_id: return await message.answer(f"{EMOJI['error']} Цель не указана. Ответьте на сообщение или используйте @упом/ник.")
    mute_info = db.get_active_mute(target_id)
    if not mute_info: return await message.answer(f"{EMOJI['error']} У этого пользователя нет активного мута.")
    issuer = db.get_admin_by_id(message.from_id)
    muted_by_admin = db.get_admin_by_id(mute_info['muted_by_id'])
    if muted_by_admin and muted_by_admin['level'] >= issuer['level'] and muted_by_admin['user_id'] != issuer['user_id']: return await message.answer(f"{EMOJI['error']} Нельзя снять мут от админа с равным/большим уровнем ({muted_by_admin['nickname']})!")
    db.remove_mute(target_id)
    log_action(message.from_id, "снял мут", target_id)
    try: user_info = (await vk_api.users.get(user_ids=[target_id]))[0]; name = user_info.first_name
    except: name = target_admin['nickname'] if target_admin else f"Пользователь {target_id}"
    await message.answer(f"{EMOJI['unlock']} С пользователя [id{target_id}|{name}] снят мут.")

# Логи, ЧС, Активность и Системные команды
@bot.on.message(text=["/editcmd", "/editcmd <command> <level>"])
async def editcmd_cmd(message: Message, command: Optional[str] = None, level: Optional[str] = None):
    if not await check_permission(message, "editcmd"): return
    if not command or not level: return await message.answer(f"{EMOJI['error']} Формат: /editcmd <команда> <уровень>")
    clean_command = command.lower().lstrip('/')
    try: new_level = int(level); assert 0 <= new_level <= 9
    except: return await message.answer(f"{EMOJI['error']} Уровень должен быть числом от 0 до 9!")
    
    db.set_command_level(clean_command, new_level, message.peer_id)
    log_action(message.from_id, "изменил уровень доступа к команде", details=f"/{clean_command} -> {new_level} в чате {message.peer_id}")
    await message.answer(f"{EMOJI['success']} Уровень для /{clean_command} изменен на {new_level} для этого чата!")

# команда для установки глобального уровня
@bot.on.message(text=["/editcmd_global <command> <level>", "/editcmd_global"])
async def editcmd_global_cmd(message: Message, command: Optional[str] = None, level: Optional[str] = None):
    if not await check_permission(message, "editcmd_global"): return
    if not command or not level: return await message.answer(f"{EMOJI['error']} Формат: /editcmd_global <команда> <уровень>")
    clean_command = command.lower().lstrip('/')
    try: new_level = int(level); assert 0 <= new_level <= 9
    except: return await message.answer(f"{EMOJI['error']} Уровень должен быть числом от 0 до 9!")

@bot.on.message(text=["/bladd", "/bladd <text>"])
async def blacklist_add_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "bladd"): return
    
    target_id, target_admin, reason = await parse_target_and_args(message)
    reason = reason or "Не указана"
    
    if not target_id: 
        return await message.answer(f"{EMOJI['error']} Цель не указана. Ответьте на сообщение или используйте @упом.")

    target_global_data = db.get_user_global_data(target_id)
    if target_global_data and target_global_data['dev_mode'] == 1:
        return await message.answer(f"{EMOJI['lock']} Действие не может быть применено к этому пользователю.")

    if target_id == message.from_id: 
        return await message.answer(f"{EMOJI['error']} Нельзя добавить в ЧС самого себя.")
        
    if db.is_blacklisted(target_id): 
        return await message.answer(f"{EMOJI['error']} Этот пользователь уже в ЧС.")
        
    issuer = db.get_admin_by_id(message.from_id, message.peer_id)
    if target_admin and target_admin['level'] >= issuer['level']: 
        return await message.answer(f"{EMOJI['error']} Нельзя добавить в ЧС админа с равным/большим уровнем!")
        
    db.add_to_blacklist(target_id, reason, message.from_id)
    log_action(message.from_id, "добавил в ЧС", target_id, f"причина: {reason}")
    
    try: 
        user_info = (await vk_api.users.get(user_ids=[target_id]))[0]
        name = user_info.first_name
    except Exception: 
        name = "Пользователь"
        
    await message.answer(f"{EMOJI['blacklist']} [id{target_id}|{name}] добавлен в ЧС.\nПричина: {reason}")
@bot.on.message(text=["/blrem", "/blrem <text>"])
async def blacklist_remove_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "blrem"): return
    target_id, _, __ = await parse_target_and_args(message)
    if not target_id: return await message.answer(f"{EMOJI['error']} Цель не указана. Ответьте на сообщение или используйте @упом.")
    if not db.is_blacklisted(target_id): return await message.answer(f"{EMOJI['error']} Этот пользователь не в ЧС.")
    db.remove_from_blacklist(target_id)
    log_action(message.from_id, "убрал из ЧС", target_id)
    try: user_info = (await vk_api.users.get(user_ids=[target_id]))[0]; name = user_info.first_name
    except Exception: name = "Пользователь"
    await message.answer(f"{EMOJI['success']} [id{target_id}|{name}] удален из ЧС.")
@bot.on.message(text="/bllist")
async def blacklist_list_cmd(message: Message):
    if not await check_permission(message, "bllist"): return
    blacklist_entries = db.get_full_blacklist()
    if not blacklist_entries: return await message.answer(f"{EMOJI['list']} Черный список пуст.")
    text = f"{EMOJI['blacklist']} Черный список:\n\n"
    user_ids = [entry['user_id'] for entry in blacklist_entries]
    try: users_info = await vk_api.users.get(user_ids=user_ids); users_map = {u.id: f"{u.first_name} {u.last_name}" for u in users_info}
    except Exception: users_map = {}
    for i, entry in enumerate(blacklist_entries, 1):
        user_name = users_map.get(entry['user_id'], f"ID{entry['user_id']}")
        added_by_admin = db.get_admin_by_id(entry['added_by'])
        added_by_info = f"[id{added_by_admin['user_id']}|{added_by_admin['nickname']}]" if added_by_admin else "Неизвестно"
        text += (f"{i}. [id{entry['user_id']}|{user_name}]\n - Причина: {entry['reason']}\n - Добавил: {added_by_info}\n\n")
    await message.answer(text)
@bot.on.message(PayloadContainsRule({"cmd": "plogs"}))
async def profile_logs_handler(message: Message):
    if not await check_permission(message, "plogs"): return
    try:
        payload = message.get_payload_json()
        target_id = int(payload["user_id"])
        # Извлекаем chat_id из payload
        chat_id = int(payload["chat_id"])
    except (ValueError, KeyError, TypeError):
        return await message.answer(f"{EMOJI['error']} Некорректный или устаревший payload кнопки.")
    
    await show_user_logs(message, target_id, chat_id)
@bot.on.message(text=["/logs", "/logs <text>"])
async def logs_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "logs"): return
    if not LOG_FILE.exists(): return await message.answer(f"{EMOJI['info']} Файл логов пуст.")
    
    target_id, _, __ = await parse_target_and_args(message)
    
    if target_id: 
        return await show_user_logs(message, target_id, message.peer_id)
        
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f: all_lines = f.readlines()
    except Exception as e: return await message.answer(f"{EMOJI['error']} Не удалось прочитать файл логов: {e}")
    
    header = f"{EMOJI['list']} Последние 20 действий:\n\n"
    if not all_lines: return await message.answer(f"{EMOJI['info']} Логи пусты.")
    response_text = header + "\n".join([l.strip() for l in all_lines][-20:])
    await message.answer(response_text[:4096])
async def show_user_logs(message: Message, user_id: int, chat_id: int):
    target_admin = db.get_admin_by_id(user_id, chat_id)
    if not target_admin:
        target_global = db.get_user_global_data(user_id)
        target_nick = target_global['nickname'] if target_global else f"ID{user_id}"
    else:
        target_nick = target_admin['nickname']

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            user_logs = [line.strip() for line in f if target_nick in line or f"[id{user_id}|" in line]
    except Exception as e: 
        return await message.answer(f"{EMOJI['error']} Не удалось прочитать файл логов: {e}")
        
    if not user_logs: 
        return await message.answer(f"{EMOJI['info']} Не найдено действий для {target_nick}.")
        
    header = f"{EMOJI['list']} Последние 20 действий для {target_nick}:\n\n"
    response_text = header + "\n".join(user_logs[-20:])
    await message.answer(response_text[:4096])

@bot.on.message(PayloadContainsRule({"cmd": "activity"}))
async def show_activity_summary(message: Message):
    try:
        payload = message.get_payload_json()
        target_id = int(payload["user_id"])
        # Извлекаем chat_id из payload
        chat_id = int(payload["chat_id"])
        target_admin = db.get_admin_by_id(target_id, chat_id)
        if not target_admin: 
            return await message.answer(f"{EMOJI['error']} Администратор не найден в указанном чате.")
    except (ValueError, KeyError, TypeError): 
        return await message.answer(f"{EMOJI['error']} Некорректный или устаревший payload кнопки.")
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    msg_count = db.count_messages_for_user(target_id, start_date, end_date)
    # global stats
    admins_added = db.count_actions_for_user(target_id, 'add_admin', start_date, end_date)
    preds_issued = db.count_actions_for_user(target_id, 'issue_pred', start_date, end_date)
    warns_issued = db.count_actions_for_user(target_id, 'issue_warn', start_date, end_date)
    admins_kicked = db.count_actions_for_user(target_id, 'kick_admin', start_date, end_date)

    response = (f"{EMOJI['activity']} Статистика {target_admin['nickname']} за 7 дней:\n\n"
                f"{EMOJI['messages']} Сообщений отправлено (во всех чатах): {msg_count}\n"
                f"{EMOJI['list']} Админов назначено (во всех чатах): {admins_added}\n"
                f"{EMOJI['kick']} Админов снято (во всех чатах): {admins_kicked}\n"
                f"{EMOJI['warn']} Предупреждений выдано (во всех чатах): {preds_issued}\n"
                f"{EMOJI['ban']} Выговоров выдано (во всех чатах): {warns_issued}\n\n"
                f"{EMOJI['info']} Примечание: Указанная статистика является приблизительной. При большой нагрузке некоторые сообщения могут не быть учтены в реальном времени, однако это происходит крайне редко. Наш бот старается обрабатывать каждое ваше сообщение.")
    
    await message.answer(response, disable_mentions=1)

@bot.on.chat_message(action=["chat_leave_user", "chat_kick_user"])
async def handle_user_departure(message: Message):
    logger.info(f"Сработало событие ухода из чата: {message.action.type}. Peer ID: {message.peer_id}")

    if not message.action or not message.action.member_id:
        logger.warning(f"Событие ухода из чата не содержит member_id: {message.action}")
        return

    departed_user_id = message.action.member_id
    chat_id = message.peer_id

    if departed_user_id < 0:
        logger.info(f"Игнорируем уход из чата бота/группы с ID {departed_user_id}")
        return
        
    admin_record = db.get_admin_by_id(departed_user_id, chat_id)

    if not admin_record:
        logger.info(f"Пользователь {departed_user_id} покинул чат {chat_id}, но не был администратором.")
        return

    db.snyat_adm(departed_user_id, chat_id)
    
    user_global_data = db.get_user_global_data(departed_user_id)
    nickname = user_global_data['nickname'] if user_global_data and user_global_data['nickname'] else f"ID {departed_user_id}"

    action_type_str = str(message.action.type)

    if "leave_user" in action_type_str:
        log_action(0, "автоматически снял с поста (пользователь сам вышел из чата)", departed_user_id, f"в чате {chat_id}")
        await message.answer(
            f"{EMOJI['info']} Администратор [id{departed_user_id}|{nickname}] покинул чат и был автоматически снят с поста."
        )
    elif "kick_user" in action_type_str:
        if departed_user_id == message.from_id:
            return
            
        kicker_global_data = db.get_user_global_data(message.from_id)
        kicker_nick = kicker_global_data['nickname'] if kicker_global_data and kicker_global_data['nickname'] else f"ID {message.from_id}"
        
        log_action(message.from_id, f"исключил админа, что привело к снятию с поста", departed_user_id, f"в чате {chat_id}")
        await message.answer(
            f"{EMOJI['kick']} Пользователь [id{message.from_id}|{kicker_nick}] исключил администратора [id{departed_user_id}|{nickname}] из чата. Пост администратора был автоматически снят."
        )

@bot.on.message(text=["/msgcount", "/msgcount <text>"])
async def msgcount_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "msgcount"): return
    target_id, target_admin, args_text = await parse_target_and_args(message)
    if not target_admin:
        if text: return await message.answer(f"{EMOJI['error']} Администратор не найден. Укажите @упом/ник или ответьте на сообщение.")
        target_admin = db.get_admin_by_id(message.from_id)
        args_text = ""
    if not target_admin: return await message.answer(f"{EMOJI['error']} Ваш профиль администратора не найден.")

    args = args_text.split() if args_text else []
    start_date_str = args[0] if len(args) > 0 else None
    end_date_str = args[1] if len(args) > 1 else None

    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)

    try:
        if start_date_str: start_date = datetime.strptime(start_date_str, "%d.%m.%Y")
        if end_date_str: end_date = datetime.strptime(end_date_str, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
    except ValueError: return await message.answer(f"{EMOJI['error']} Неверный формат даты! Используйте ДД.ММ.ГГГГ.")

    count = db.count_messages_for_user(target_admin['user_id'], start_date, end_date)
    await message.answer(f"{EMOJI['messages']} Администратор {target_admin['nickname']} отправил {count} сообщений с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}.")

@bot.on.message(text=["/godmode", "/godmode <key>", "/godmode <key> <user_id_str>"])
async def godmode_cmd(message: Message, key: Optional[str] = None, user_id_str: Optional[str] = None):
    if not key or not user_id_str: return await message.answer(f"{EMOJI['error']} Формат: /godmode <ключ> @упом/ID")
    if key != GODMODE_KEY: return await message.answer(f"{EMOJI['error']} Неверный ключ!")
    target_id = parse_mention(user_id_str) or (int(user_id_str) if user_id_str.isdigit() else None)
    if not target_id: return await message.answer(f"{EMOJI['error']} Укажите корректный ID/упоминание!")
    try: user_info = (await vk_api.users.get(user_ids=[target_id]))[0]; nickname = user_info.first_name
    except Exception: nickname = f"Пользователь_{target_id}"
    if not db.get_admin_by_id(target_id): db.add_admin(target_id, nickname, message.from_id, 9, "Владелец")
    else: db.update_admin(target_id, 'level', 9); db.update_admin(target_id, 'position', "Владелец")
    log_action(message.from_id, "активировал GODMODE для", target_id)
    await message.answer(f"{EMOJI['success']} Администратор [id{target_id}|{nickname}] получил FULL ACCESS!")

@bot.on.message(text=[".dev <mode>", ".dev"])
async def dev_mode_cmd(message: Message, mode: Optional[str] = None):
    if message.from_id != DEV_USER_ID: 
        return await message.answer(f"{EMOJI['error']} У вас нет прав для использования этой команды.")
    else:
        print(message.from_id, 'Попытался использовать команду .dev')
    dev_global_data = db.get_user_global_data(DEV_USER_ID)
    if not dev_global_data:
        db.execute("INSERT OR IGNORE INTO users_global (user_id, nickname) VALUES (?, 'DevUser')", (DEV_USER_ID,), commit=True)
        dev_global_data = db.get_user_global_data(DEV_USER_ID)

    if not mode or mode.lower() not in ["on", "off"]: 
        return await message.answer(f"{EMOJI['error']} Неверный формат. Используйте: .dev <on/off>\n"
                                    f"Текущий глобальный статус Dev-режима: {'Включен' if dev_global_data['dev_mode'] else 'Выключен'}")

    dev_admin_local = db.get_admin_by_id(DEV_USER_ID, message.peer_id)

    if mode.lower() == "on":
        db.update_user_global_field(DEV_USER_ID, "dev_mode", 1) 
        log_action(DEV_USER_ID, "включил dev-режим (глобально)")

        if not dev_admin_local:
            try:
                user_info = (await vk_api.users.get(user_ids=[DEV_USER_ID]))[0]
                nickname = user_info.first_name + " " + user_info.last_name
            except Exception:
                nickname = f"DevUser_{DEV_USER_ID}"
            db.add_admin(DEV_USER_ID, message.peer_id, nickname, DEV_USER_ID, 9, "Владелец")
            dev_admin_local = db.get_admin_by_id(DEV_USER_ID, message.peer_id) 
            log_action(DEV_USER_ID, "добавил себя как админа в dev-режиме", DEV_USER_ID, f"в чате {message.peer_id}")
        
        db.update_admin(DEV_USER_ID, message.peer_id, "level", 9)
        db.update_admin(DEV_USER_ID, message.peer_id, "position", "Владелец")
        db.update_admin(DEV_USER_ID, message.peer_id, "status", "Активен") 
        log_action(DEV_USER_ID, "получил FULL ACCESS в dev-режиме", DEV_USER_ID, f"в чате {message.peer_id}")

        await message.answer(f"{EMOJI['success']} Dev-режим включен. Вы получили уровень 9 и должность 'Владелец' в этом чате!")
    else: # off
        db.update_user_global_field(DEV_USER_ID, "dev_mode", 0)
        log_action(DEV_USER_ID, "выключил dev-режим (глобально)")

        if dev_admin_local:
            db.update_admin(DEV_USER_ID, message.peer_id, "level", 1) # Можно выбрать другой дефолтный уровень
            db.update_admin(DEV_USER_ID, message.peer_id, "position", "Без должности")
            log_action(DEV_USER_ID, "сбросил привилегии в dev-режиме", DEV_USER_ID, f"в чате {message.peer_id}")
            await message.answer(f"{EMOJI['error']} Dev-режим выключен. Ваши привилегии в этом чате сброшены.")
        else:
            await message.answer(f"{EMOJI['error']} Dev-режим выключен. Вы не были администратором в этом чате, поэтому сбрасывать нечего.")

# Команды казино
@bot.on.message(text=["/bal", "/balance"])
async def balance_cmd(message: Message):
    user_data = db.get_user_global_data(message.from_id)
    balance = user_data['balance'] if user_data else 100
    await message.answer(f"{EMOJI['money']} Ваш глобальный баланс: {balance} фишек.")
@bot.on.message(text="/daily")
async def daily_cmd(message: Message):
    user_data = db.get_user_global_data(message.from_id)
    if not user_data:
        db.update_balance(message.from_id, 0) # Создаст запись с балансом по умолчанию
        user_data = db.get_user_global_data(message.from_id)

    now = datetime.now()
    if user_data['last_daily']:
        last_daily_dt = datetime.fromisoformat(user_data['last_daily'])
        if now - last_daily_dt < timedelta(hours=24):
            time_left = timedelta(hours=24) - (now - last_daily_dt)
            hours, rem = divmod(int(time_left.total_seconds()), 3600); mins, _ = divmod(rem, 60)
            return await message.answer(f"{EMOJI['time']} Вы уже получали бонус. Следующий через: {hours} ч. {mins} мин.")
            
    bonus = CASINO_CONFIG['daily_bonus']
    db.update_balance(message.from_id, bonus)
    db.update_user_global_field(message.from_id, 'last_daily', now.isoformat())
    new_balance = user_data['balance'] + bonus
    log_action(message.from_id, "получил ежедневный бонус", details=f"+{bonus} фишек")
    await message.answer(f"{EMOJI['success']} Вы получили {bonus} фишек!\n{EMOJI['money']} Ваш новый баланс: {new_balance} фишек.")
@bot.on.message(text="/top")
async def top_cmd(message: Message):
    if not await check_permission(message, "top"): return
    top_players = db.get_top_players(5)
    if not top_players: return await message.answer(f"{EMOJI['list']} Пока нет игроков с фишками.")
    response = f"{EMOJI['crown']} Топ-5 богачей:\n\n"
    for i, p in enumerate(top_players, 1): response += f"{i}. {p['nickname']} - {p['balance']} фишек {EMOJI['money']}\n"
    await message.answer(response)
@bot.on.message(text=["/pay", "/pay <text>"])
async def pay_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "pay"): return
    receiver_id, receiver, amount_str = await parse_target_and_args(message)
    if not receiver: return await message.answer(f"{EMOJI['error']} Получатель не найден. Ответьте на сообщение или используйте @упом/ник.")
    if not amount_str: return await message.answer(f"{EMOJI['error']} Не указана сумма для перевода.")
    try: amount = int(amount_str); assert amount > 0
    except: return await message.answer(f"{EMOJI['error']} Сумма должна быть положительным числом.")
    sender = db.get_admin_by_id(message.from_id)
    if (sender['balance'] or 0) < amount: return await message.answer(f"{EMOJI['error']} У вас недостаточно фишек! (Баланс: {sender['balance'] or 0})")
    if receiver['user_id'] == sender['user_id']: return await message.answer(f"{EMOJI['error']} Нельзя перевести фишки самому себе.")
    db.update_balance(sender['user_id'], -amount); db.update_balance(receiver['user_id'], amount)
    log_action(sender['user_id'], "перевел фишки", receiver['user_id'], f"{amount} фишек")
    await message.answer(f"{EMOJI['success']} Вы успешно перевели {amount} фишек игроку {receiver['nickname']}!")
@bot.on.message(text=["/giverub", "/giverub <text>"])
async def giverub_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "giverub"): return
    target_id, target, amount_str = await parse_target_and_args(message)
    if not target: return await message.answer(f"{EMOJI['error']} Цель не найдена. Ответьте на сообщение или используйте @упом/ник.")
    if not amount_str: return await message.answer(f"{EMOJI['error']} Не указана сумма.")
    try: amount = int(amount_str)
    except ValueError: return await message.answer(f"{EMOJI['error']} Сумма должна быть числом.")
    db.update_balance(target['user_id'], amount)
    log_action(message.from_id, "выдал фишки", target['user_id'], f"{amount} фишек")
    await message.answer(f"{EMOJI['success']} Вы успешно выдали {amount} фишек игроку {target['nickname']}!")
@bot.on.message(text=["/dice", "/dice <bet_str>"])
async def dice_cmd(message: Message, bet_str: Optional[str] = None):
    if not await check_permission(message, "dice"): return
    admin = db.get_admin_by_id(message.from_id); min_bet, max_bet = CASINO_CONFIG['min_bet'], CASINO_CONFIG['max_bet']
    if not bet_str: return await message.answer(f"{EMOJI['error']} Укажите ставку! /dice <ставка>")
    try: bet = int(bet_str)
    except ValueError: return await message.answer(f"{EMOJI['error']} Ставка должна быть числом.")
    if not (min_bet <= bet <= max_bet): return await message.answer(f"{EMOJI['error']} Ставка от {min_bet} до {max_bet} фишек.")
    if (admin['balance'] or 0) < bet: return await message.answer(f"{EMOJI['error']} У вас недостаточно фишек. (Баланс: {admin['balance'] or 0})")
    player_roll, bot_roll = random.randint(2, 12), random.randint(2, 12)
    result_text = f"{EMOJI['game_die']} Ваши кости: {player_roll}\n{EMOJI['game_die']} Кости бота: {bot_roll}\n\n"
    if player_roll > bot_roll:
        db.update_balance(message.from_id, bet)
        log_action(message.from_id, "выиграл в кости", details=f"ставка {bet}, +{bet} фишек")
        await message.answer(result_text + f"{EMOJI['success']} Победа! Выигрыш: {bet} фишек.\n{EMOJI['money']} Баланс: {(admin['balance'] or 0) + bet}")
    elif bot_roll > player_roll:
        db.update_balance(message.from_id, -bet)
        log_action(message.from_id, "проиграл в кости", details=f"ставка {bet}, -{bet} фишек")
        await message.answer(result_text + f"{EMOJI['error']} Проигрыш! Потеряно: {bet} фишек.\n{EMOJI['money']} Баланс: {(admin['balance'] or 0) - bet}")
    else:
        log_action(message.from_id, "сыграл вничью в кости", details=f"ставка {bet}")
        await message.answer(result_text + f"{EMOJI['info']} Ничья! Ваша ставка возвращена.")
@bot.on.message(text=["/slots", "/slots <bet_str>"])
async def slots_cmd(message: Message, bet_str: Optional[str] = None):
    if not await check_permission(message, "slots"): return
    admin = db.get_admin_by_id(message.from_id); min_bet, max_bet = CASINO_CONFIG['min_bet'], CASINO_CONFIG['max_bet']
    if not bet_str: return await message.answer(f"{EMOJI['error']} Укажите ставку! /slots <ставка>")
    try: bet = int(bet_str)
    except ValueError: return await message.answer(f"{EMOJI['error']} Ставка должна быть числом.")
    if not (min_bet <= bet <= max_bet): return await message.answer(f"{EMOJI['error']} Ставка от {min_bet} до {max_bet} фишек.")
    if (admin['balance'] or 0) < bet: return await message.answer(f"{EMOJI['error']} У вас недостаточно фишек. (Баланс: {admin['balance'] or 0})")
    reels = ['🍒', '🍋', '🔔', '💎', '💰', '🎰']; weights = [25, 25, 20, 15, 10, 5] 
    roll = random.choices(reels, weights=weights, k=3); result_text = f"{EMOJI['slot_machine']} | {' '.join(roll)} | {EMOJI['slot_machine']}\n\n"; change = -bet
    if roll[0] == roll[1] == roll[2]:
        winnings = bet * (50 if roll[0] == '🎰' else 10); change += winnings
        result_text += f"{'🎉 ДЖЕКПОТ! 🎉' if roll[0] == '🎰' else EMOJI['success'] + ' Три в ряд!'}\nВыигрыш: {winnings} фишек!"
    elif roll[0] == roll[1] or roll[1] == roll[2]:
        winnings = bet * 2; change += winnings
        result_text += f"{EMOJI['success']} Два в ряд! Выигрыш: {winnings} фишек!"
    else: result_text += f"{EMOJI['error']} Вы проиграли. Попробуйте еще раз!"
    db.update_balance(message.from_id, change)
    log_action(message.from_id, "сыграл в слоты", details=f"ставка {bet}, изменение баланса: {change}")
    await message.answer(result_text + f"\n{EMOJI['money']} Ваш новый баланс: {(admin['balance'] or 0) + change}")
@bot.on.message(text=["/zov", "/zov <text>"])
async def zov_cmd(message: Message, text: Optional[str] = None):
    if not await check_permission(message, "zov"): return
    if not text: return await message.answer(f"{EMOJI['error']} Укажите текст для оповещения! /zov <текст>")
    try:
        members_response = await bot.api.messages.get_conversation_members(peer_id=message.peer_id)
        member_ids = [m.member_id for m in members_response.items if m.member_id > 0 and m.member_id != message.from_id]
        if not member_ids: return await message.answer(f"{EMOJI['info']} Некого оповещать в этом чате.")
        mentions = "".join([f"[id{uid}|\u200b]" for uid in member_ids])
    except VKAPIError as e:
        if e.code == 917: return await message.answer(f"{EMOJI['error']} Я не администратор в этом чате.")
        else: logger.error(f"Ошибка API при вызове /zov: {e}"); return await message.answer(f"{EMOJI['error']} Произошла ошибка API.")
    caller_admin = db.get_admin_by_id(message.from_id)
    caller_name = caller_admin['nickname'] if caller_admin else "Пользователь"
    final_message = (f"{EMOJI['megaphone']} Вы были вызваны Администратором [id{message.from_id}|{caller_name}]!\n\n" f"Сообщение: {text}\n\n{mentions}")
    if len(final_message) > 4096: return await message.answer(f"{EMOJI['error']} Сообщение слишком длинное.")
    await message.answer(final_message, disable_mentions=0)
    log_action(message.from_id, "использовал /zov", details=f"в чате {message.peer_id}")

# Запуск бота
def register_requestable_commands():
    COMMAND_HANDLERS.clear() 
    COMMAND_HANDLERS["pred"] = functools.partial(internal_punishment_handler, cmd="pred", is_add=True)
    COMMAND_HANDLERS["unpred"] = functools.partial(internal_punishment_handler, cmd="unpred", is_add=False)
    COMMAND_HANDLERS["warn"] = functools.partial(internal_punishment_handler, cmd="warn", is_add=True)
    COMMAND_HANDLERS["unwarn"] = functools.partial(internal_punishment_handler, cmd="unwarn", is_add=False)
    COMMAND_HANDLERS["mute"], COMMAND_HANDLERS["unmute"] = mute_cmd, unmute_cmd
    COMMAND_HANDLERS["kick"], COMMAND_HANDLERS["setlvl"] = kick_cmd, setlvl_cmd
    COMMAND_HANDLERS["bladd"], COMMAND_HANDLERS["blrem"] = blacklist_add_cmd, blacklist_remove_cmd
    logger.info(f"Зарегистрировано {len(COMMAND_HANDLERS)} команд для системы запросов.")

if __name__ == "__main__":
    try:
        if not DB_FILE.exists(): logger.warning(f"Файл базы данных '{DB_FILE.name}' не найден! Создаю новый...")
        db.setup_database()
        db.populate_defaults(DEFAULT_CMD_LEVELS, POSITIONS)
        
        logger.info(f"ВАЖНО: Убедитесь, что бот является участником всех необходимых бесед и имеет права администратора.")
        bot.loop_wrapper.on_startup.append(startup_task())
        logger.success(f"Бот запущен! Работа с базой данных '{DB_FILE.name}'.")
        bot.run_forever()
    except (FileNotFoundError, ValueError) as e: logger.critical(f"Критическая ошибка при запуске: {e}")
    except Exception as e:
        logger.critical(f"Непредвиденная ошибка: {e}")
        raise
