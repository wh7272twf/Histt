# -*- coding: utf-8 -*-
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import logging
import threading
import re
import sys
import atexit
import requests
from flask import Flask
from threading import Thread

# ============================================================
#  CONFIG  ⚠️ ROTATE THE TOKEN VIA @BotFather IMMEDIATELY ⚠️
# ============================================================
TOKEN = os.environ.get("BOT_TOKEN", "8690308551:AAGdtSZe47xHVc2hyJGs8KA436qEYSyQf1g")
OWNER_ID = 7811364879
ADMIN_ID = 7811364879
YOUR_USERNAME = '@STRONXIUM'
UPDATE_CHANNEL = 'https://t.me/telegram'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

FREE_USER_LIMIT = 10000
SUBSCRIBED_USER_LIMIT = 40000
ADMIN_LIMIT = 100000
OWNER_LIMIT = float('inf')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================
#  HELPERS
# ============================================================
def esc_md(text):
    if text is None:
        return ""
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def fmt_limit(v):
    return "∞" if v == float('inf') else str(v)


# ============================================================
#  FLASK KEEP-ALIVE
# ============================================================
app = Flask('__name__')

@app.route('/')
def home():
    return "I'am STRIKERxMAGNUS File Host"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()
    print("✓ Flask Keep-Alive started")


# ============================================================
#  KEYBOARD LAYOUTS
# ============================================================
USER_KEYBOARD = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["📞 Contact Owner"],
]

ADMIN_KEYBOARD = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💳 Subscriptions", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["👑 Admin Panel", "📞 Contact Owner"],
]


# ============================================================
#  DATABASE
# ============================================================
def init_db():
    logger.info(f"Initializing DB: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expiry TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS user_files '
                  '(user_id INTEGER, file_name TEXT, file_type TEXT, PRIMARY KEY (user_id, file_name))')
        c.execute('CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        conn.commit()
        conn.close()
        logger.info("✓ Database ready")
    except Exception as e:
        logger.error(f"❌ DB init error: {e}", exc_info=True)


def load_data():
    logger.info("Loading data...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        c.execute('SELECT user_id, expiry FROM subscriptions')
        for uid, exp in c.fetchall():
            try:
                user_subscriptions[uid] = {'expiry': datetime.fromisoformat(exp)}
            except ValueError:
                logger.warning(f"Bad expiry for {uid}")

        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for uid, fn, ft in c.fetchall():
            user_files.setdefault(uid, []).append((fn, ft))

        c.execute('SELECT user_id FROM active_users')
        active_users.update(u for (u,) in c.fetchall())

        c.execute('SELECT user_id FROM admins')
        admin_ids.update(u for (u,) in c.fetchall())

        conn.close()
        logger.info(f"✓ Loaded: {len(active_users)} users, {len(user_subscriptions)} subs, {len(admin_ids)} admins")
    except Exception as e:
        logger.error(f"❌ Load error: {e}", exc_info=True)


# ============================================================
#  GENERIC HELPERS
# ============================================================
def get_user_folder(user_id):
    p = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(p, exist_ok=True)
    return p


def get_user_file_limit(user_id):
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    sub = user_subscriptions.get(user_id)
    if sub and sub['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT


def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))


def get_user_status_text(user_id):
    if user_id == OWNER_ID:
        return "👑 Owner"
    if user_id in admin_ids:
        return "🛡️ Admin"
    sub = user_subscriptions.get(user_id)
    if sub:
        exp = sub.get('expiry')
        if exp and exp > datetime.now():
            return f"⭐ Premium ({max(0, (exp - datetime.now()).days)}d left)"
        remove_subscription_db(user_id)
    return "🆓 Free"


def is_bot_running(owner_id, file_name):
    key = f"{owner_id}_{file_name}"
    info = bot_scripts.get(key)
    if not info or not info.get('process'):
        return False
    try:
        proc = psutil.Process(info['process'].pid)
        alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        if not alive:
            _cleanup_script_entry(key)
        return alive
    except psutil.NoSuchProcess:
        _cleanup_script_entry(key)
        return False
    except Exception as e:
        logger.error(f"is_bot_running error {key}: {e}")
        return False


def _cleanup_script_entry(key):
    info = bot_scripts.get(key)
    if not info:
        return
    lf = info.get('log_file')
    if lf and hasattr(lf, 'close') and not lf.closed:
        try:
            lf.close()
        except Exception:
            pass
    bot_scripts.pop(key, None)


def kill_process_tree(process_info):
    key = process_info.get('script_key', 'N/A')
    lf = process_info.get('log_file')
    if lf and hasattr(lf, 'close') and not lf.closed:
        try:
            lf.close()
        except Exception as e:
            logger.error(f"Close log ({key}): {e}")

    proc = process_info.get('process')
    if not proc or not getattr(proc, 'pid', None):
        return
    pid = proc.pid
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
        try:
            parent.terminate()
            try:
                parent.wait(timeout=1)
            except psutil.TimeoutExpired:
                parent.kill()
        except psutil.NoSuchProcess:
            pass
        logger.info(f"✓ Killed tree {key} (PID {pid})")
    except psutil.NoSuchProcess:
        logger.warning(f"PID {pid} already gone ({key})")
    except Exception as e:
        logger.error(f"Kill tree error {key}: {e}", exc_info=True)


# ============================================================
#  DB WRITE OPERATIONS
# ============================================================
DB_LOCK = threading.Lock()


def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)',
                      (user_id, file_name, file_type))
            conn.commit()
            conn.close()
            lst = [x for x in user_files.get(user_id, []) if x[0] != file_name]
            lst.append((file_name, file_type))
            user_files[user_id] = lst
        except Exception as e:
            logger.error(f"save_user_file error: {e}")


def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (user_id, file_name))
            conn.commit()
            conn.close()
            if user_id in user_files:
                user_files[user_id] = [x for x in user_files[user_id] if x[0] != file_name]
                if not user_files[user_id]:
                    del user_files[user_id]
        except Exception as e:
            logger.error(f"remove_user_file_db error: {e}")


def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"add_active_user error: {e}")


def save_subscription(user_id, expiry):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                      (user_id, expiry.isoformat()))
            conn.commit()
            conn.close()
            user_subscriptions[user_id] = {'expiry': expiry}
        except Exception as e:
            logger.error(f"save_subscription error: {e}")


def remove_subscription_db(user_id):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('DELETE FROM subscriptions WHERE user_id=?', (user_id,))
            conn.commit()
            conn.close()
            user_subscriptions.pop(user_id, None)
        except Exception as e:
            logger.error(f"remove_subscription_db error: {e}")


def add_admin_db(admin_id):
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (admin_id,))
            conn.commit()
            conn.close()
            admin_ids.add(admin_id)
        except Exception as e:
            logger.error(f"add_admin_db error: {e}")


def remove_admin_db(admin_id):
    if admin_id == OWNER_ID:
        return False
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('DELETE FROM admins WHERE user_id=?', (admin_id,))
            conn.commit()
            removed = c.rowcount > 0
            conn.close()
            admin_ids.discard(admin_id)
            return removed
        except Exception as e:
            logger.error(f"remove_admin_db error: {e}")
            return False


init_db()
load_data()


# ============================================================
#  SCRIPT RUNNERS
# ============================================================
TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'python_telegram_bot': 'python-telegram-bot',
    'aiogram': 'aiogram', 'pyrogram': 'pyrogram', 'telethon': 'telethon',
    'telepot': 'telepot', 'pytg': 'pytg', 'tgcrypto': 'tgcrypto',
    'bs4': 'beautifulsoup4', 'requests': 'requests', 'pillow': 'Pillow',
    'cv2': 'opencv-python', 'yaml': 'PyYAML', 'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil', 'pandas': 'pandas', 'numpy': 'numpy',
    'flask': 'Flask', 'django': 'Django', 'sqlalchemy': 'SQLAlchemy',
    'psutil': 'psutil',
    'asyncio': None, 'json': None, 'datetime': None, 'os': None, 'sys': None,
    're': None, 'time': None, 'math': None, 'random': None, 'logging': None,
    'threading': None, 'subprocess': None, 'zipfile': None, 'tempfile': None,
    'shutil': None, 'sqlite3': None, 'atexit': None,
}


def attempt_install_pip(module_name, message):
    package_name = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    if package_name is None:
        return False
    try:
        bot.reply_to(message, f"🐍 Installing `{package_name}`...", parse_mode='Markdown')
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', package_name],
            capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore'
        )
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Installed `{package_name}`.", parse_mode='Markdown')
            return True
        err = (result.stderr or result.stdout or "")[:3500]
        bot.reply_to(message, f"❌ Install failed `{package_name}`:\n```\n{err}\n```", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"pip install error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ pip error: {e}")
    return False


def attempt_install_npm(module_name, user_folder, message):
    try:
        bot.reply_to(message, f"🟠 Installing Node `{module_name}`...", parse_mode='Markdown')
        result = subprocess.run(['npm', 'install', module_name],
                                capture_output=True, text=True, check=False,
                                cwd=user_folder, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Installed `{module_name}`.", parse_mode='Markdown')
            return True
        err = (result.stderr or result.stdout or "")[:3500]
        bot.reply_to(message, f"❌ npm failed `{module_name}`:\n```\n{err}\n```", parse_mode='Markdown')
    except FileNotFoundError:
        bot.reply_to(message, "❌ 'npm' not found")
    except Exception as e:
        logger.error(f"npm install error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ npm error: {e}")
    return False


def _start_long_run(cmd_prefix, script_path, owner_id, user_folder, file_name, message_obj, script_type):
    key = f"{owner_id}_{file_name}"
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
    except Exception as e:
        bot.reply_to(message_obj, f"❌ Cannot open log: {e}")
        return

    startupinfo, creationflags = None, 0
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    try:
        process = subprocess.Popen(
            cmd_prefix + [script_path], cwd=user_folder,
            stdout=log_file, stderr=log_file, stdin=subprocess.PIPE,
            startupinfo=startupinfo, creationflags=creationflags,
            encoding='utf-8', errors='ignore'
        )
        bot_scripts[key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'chat_id': message_obj.chat.id, 'script_owner_id': owner_id,
            'start_time': datetime.now(), 'user_folder': user_folder,
            'type': script_type, 'script_key': key,
        }
        bot.reply_to(
            message_obj,
            f"✅ *{script_type.upper()} script started*\n"
            f"📄 File: `{file_name}`\n"
            f"👤 Owner: `{owner_id}`\n"
            f"🆔 PID: `{process.pid}`",
            parse_mode='Markdown'
        )
    except FileNotFoundError:
        log_file.close()
        bot.reply_to(message_obj, f"❌ `{cmd_prefix[0]}` not found")
    except Exception as e:
        log_file.close()
        logger.error(f"start_long_run error: {e}", exc_info=True)
        bot.reply_to(message_obj, f"❌ Launch error: {e}")


def run_script(script_path, owner_id, user_folder, file_name, message_obj, attempt=1):
    if attempt > 2:
        bot.reply_to(message_obj, f"❌ Giving up on `{file_name}` after 2 attempts.")
        return
    if not os.path.exists(script_path):
        bot.reply_to(message_obj, f"❌ File `{file_name}` missing")
        remove_user_file_db(owner_id, file_name)
        return

    if attempt == 1:
        try:
            cp = subprocess.Popen(
                [sys.executable, script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore'
            )
            _, stderr = cp.communicate(timeout=5)
            rc = cp.returncode
            if rc != 0 and stderr:
                m = re.search(r"ModuleNotFoundError: No module named '(.+?)'", stderr)
                if m:
                    mod = m.group(1).strip("'\"")
                    if attempt_install_pip(mod, message_obj):
                        time.sleep(1.5)
                        threading.Thread(
                            target=run_script,
                            args=(script_path, owner_id, user_folder, file_name, message_obj, attempt + 1)
                        ).start()
                        return
                bot.reply_to(
                    message_obj,
                    f"❌ Script error in `{file_name}`:\n```\n{stderr[:800]}\n```",
                    parse_mode='Markdown'
                )
                return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            bot.reply_to(message_obj, "❌ Python interpreter not found")
            return
        except Exception as e:
            logger.error(f"precheck error: {e}")
            bot.reply_to(message_obj, f"❌ Precheck failed: {e}")
            return

    _start_long_run([sys.executable], script_path, owner_id, user_folder, file_name, message_obj, 'py')


def run_js_script(script_path, owner_id, user_folder, file_name, message_obj, attempt=1):
    if attempt > 2:
        bot.reply_to(message_obj, f"❌ Giving up on `{file_name}` after 2 attempts.")
        return
    if not os.path.exists(script_path):
        bot.reply_to(message_obj, f"❌ File `{file_name}` missing")
        remove_user_file_db(owner_id, file_name)
        return

    if attempt == 1:
        try:
            cp = subprocess.Popen(
                ['node', script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='ignore'
            )
            _, stderr = cp.communicate(timeout=5)
            rc = cp.returncode
            if rc != 0 and stderr:
                m = re.search(r"Cannot find module '(.+?)'", stderr)
                if m:
                    mod = m.group(1).strip("'\"")
                    if not mod.startswith('.') and not mod.startswith('/'):
                        if attempt_install_npm(mod, user_folder, message_obj):
                            time.sleep(1.5)
                            threading.Thread(
                                target=run_js_script,
                                args=(script_path, owner_id, user_folder, file_name, message_obj, attempt + 1)
                            ).start()
                            return
                bot.reply_to(
                    message_obj,
                    f"❌ JS error in `{file_name}`:\n```\n{stderr[:800]}\n```",
                    parse_mode='Markdown'
                )
                return
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            bot.reply_to(message_obj, "❌ 'node' not found")
            return
        except Exception as e:
            logger.error(f"js precheck error: {e}")
            bot.reply_to(message_obj, f"❌ Precheck failed: {e}")
            return

    _start_long_run(['node'], script_path, owner_id, user_folder, file_name, message_obj, 'js')


# ============================================================
#  FILE HANDLING
# ============================================================
def handle_zip_file(content, zip_name, message):
    user_id = message.from_user.id
    user_folder = get_user_folder(user_id)
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix=f"u{user_id}_zip_")
        zpath = os.path.join(temp_dir, zip_name)
        with open(zpath, 'wb') as f:
            f.write(content)
        with zipfile.ZipFile(zpath, 'r') as zf:
            for member in zf.infolist():
                mp = os.path.abspath(os.path.join(temp_dir, member.filename))
                if not mp.startswith(os.path.abspath(temp_dir)):
                    raise zipfile.BadZipFile(f"Unsafe path: {member.filename}")
            zf.extractall(temp_dir)

        items = os.listdir(temp_dir)
        py_files = [f for f in items if f.endswith('.py')]
        js_files = [f for f in items if f.endswith('.js')]

        if 'requirements.txt' in items:
            bot.reply_to(message, "🔄 Installing `requirements.txt`...", parse_mode='Markdown')
            try:
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', os.path.join(temp_dir, 'requirements.txt')],
                    capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore'
                )
                bot.reply_to(message, "✅ Python deps installed")
            except subprocess.CalledProcessError as e:
                err = (e.stderr or e.stdout or "")[:3000]
                bot.reply_to(message, f"❌ pip reqs failed:\n```\n{err}\n```", parse_mode='Markdown')
                return

        if 'package.json' in items:
            bot.reply_to(message, "🔄 Installing `package.json`...", parse_mode='Markdown')
            try:
                subprocess.run(['npm', 'install'], capture_output=True, text=True, check=True,
                               cwd=temp_dir, encoding='utf-8', errors='ignore')
                bot.reply_to(message, "✅ Node deps installed")
            except FileNotFoundError:
                bot.reply_to(message, "❌ 'npm' not found")
                return
            except subprocess.CalledProcessError as e:
                err = (e.stderr or e.stdout or "")[:3000]
                bot.reply_to(message, f"❌ npm failed:\n```\n{err}\n```", parse_mode='Markdown')
                return

        main, ftype = None, None
        for p in ('main.py', 'bot.py', 'app.py'):
            if p in py_files:
                main, ftype = p, 'py'
                break
        if not main:
            for p in ('index.js', 'main.js', 'bot.js', 'app.js'):
                if p in js_files:
                    main, ftype = p, 'js'
                    break
        if not main:
            if py_files:
                main, ftype = py_files[0], 'py'
            elif js_files:
                main, ftype = js_files[0], 'js'
        if not main:
            bot.reply_to(message, "❌ No `.py` or `.js` found in archive")
            return

        for item in os.listdir(temp_dir):
            src = os.path.join(temp_dir, item)
            dst = os.path.join(user_folder, item)
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            elif os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)

        save_user_file(user_id, main, ftype)
        bot.reply_to(
            message,
            f"✅ Extracted. Main script: `{main}` ({ftype})",
            parse_mode='Markdown'
        )
        main_path = os.path.join(user_folder, main)
        if ftype == 'py':
            threading.Thread(target=run_script,
                             args=(main_path, user_id, user_folder, main, message)).start()
        else:
            threading.Thread(target=run_js_script,
                             args=(main_path, user_id, user_folder, main, message)).start()

    except zipfile.BadZipFile as e:
        bot.reply_to(message, f"❌ Invalid ZIP: {e}")
    except Exception as e:
        logger.error(f"zip error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ ZIP error: {e}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# ============================================================
#  MENUS
# ============================================================
def create_main_menu_inline(user_id):
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(types.InlineKeyboardButton('📢 Updates', url=UPDATE_CHANNEL))
    mk.add(types.InlineKeyboardButton('📤 Upload', callback_data='upload'),
           types.InlineKeyboardButton('📂 My Files', callback_data='check_files'))
    mk.add(types.InlineKeyboardButton('⚡ Speed', callback_data='speed'),
           types.InlineKeyboardButton('📊 Stats', callback_data='stats'))

    if user_id in admin_ids:
        mk.add(types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscription'),
               types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast'))
        lock_label = '🔓 Unlock Bot' if bot_locked else '🔒 Lock Bot'
        lock_data = 'unlock_bot' if bot_locked else 'lock_bot'
        mk.add(types.InlineKeyboardButton(lock_label, callback_data=lock_data),
               types.InlineKeyboardButton('🟢 Run All', callback_data='run_all_scripts'))
        mk.add(types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'))

    mk.add(types.InlineKeyboardButton('📞 Contact Owner',
                                      url=f'https://t.me/{YOUR_USERNAME.lstrip("@")}'))
    return mk


def create_reply_keyboard_main_menu(user_id):
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for row in (ADMIN_KEYBOARD if user_id in admin_ids else USER_KEYBOARD):
        mk.add(*[types.KeyboardButton(t) for t in row])
    return mk


def create_control_buttons(owner_id, file_name, running):
    mk = types.InlineKeyboardMarkup(row_width=2)
    if running:
        mk.row(
            types.InlineKeyboardButton("🔴 Stop", callback_data=f'stop_{owner_id}_{file_name}'),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f'restart_{owner_id}_{file_name}'),
        )
    else:
        mk.row(
            types.InlineKeyboardButton("🟢 Start", callback_data=f'start_{owner_id}_{file_name}'),
        )
    mk.row(
        types.InlineKeyboardButton("📜 Logs", callback_data=f'logs_{owner_id}_{file_name}'),
        types.InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{owner_id}_{file_name}'),
    )
    mk.add(types.InlineKeyboardButton("🔙 Back", callback_data='check_files'))
    return mk


def create_admin_panel():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.row(
        types.InlineKeyboardButton('➕ Add Admin', callback_data='add_admin'),
        types.InlineKeyboardButton('➖ Remove Admin', callback_data='remove_admin'),
    )
    mk.add(types.InlineKeyboardButton('📋 List Admins', callback_data='list_admins'))
    mk.add(types.InlineKeyboardButton('🔙 Back', callback_data='back_to_main'))
    return mk


def create_subscription_menu():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.row(
        types.InlineKeyboardButton('➕ Add', callback_data='add_subscription'),
        types.InlineKeyboardButton('➖ Remove', callback_data='remove_subscription'),
    )
    mk.add(types.InlineKeyboardButton('🔍 Check', callback_data='check_subscription'))
    mk.add(types.InlineKeyboardButton('🔙 Back', callback_data='back_to_main'))
    return mk


# ============================================================
#  STATS
# ============================================================
def build_stats_text(user_id):
    total_users = len(active_users)
    total_files = sum(len(v) for v in user_files.values())
    active = 0
    mine = 0
    for key in list(bot_scripts.keys()):
        try:
            oid_str, _ = key.split('_', 1)
            oid = int(oid_str)
            info = bot_scripts.get(key)
            if info and is_bot_running(oid, info['file_name']):
                active += 1
                if oid == user_id:
                    mine += 1
        except Exception:
            continue

    text = (
        "📊 *Bot Statistics*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 *Total Users*     : `{total_users}`\n"
        f"📂 *File Records*    : `{total_files}`\n"
        f"🟢 *Running Bots*    : `{active}`\n"
        f"🤖 *Your Bots*       : `{mine}`\n"
    )
    if user_id in admin_ids:
        text += f"🔐 *Lock State*      : {'🔴 Locked' if bot_locked else '🟢 Unlocked'}\n"
    text += f"💎 *Your Status*     : {get_user_status_text(user_id)}\n"
    text += "━━━━━━━━━━━━━━━━━━"
    return text


# ============================================================
#  WELCOME
# ============================================================
def _logic_send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    first_name = message.from_user.first_name or "User"
    username = message.from_user.username

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, "🔒 *Bot is locked by admin. Please try later.*", parse_mode='Markdown')
        return

    if user_id not in active_users:
        add_active_user(user_id)
        try:
            notif = (
                f"🎉 *New User Joined*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 Name: {esc_md(first_name)}\n"
                f"✳️ Username: @{esc_md(username or 'N/A')}\n"
                f"🆔 ID: `{user_id}`"
            )
            bot.send_message(OWNER_ID, notif, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"notify owner: {e}")

    limit = get_user_file_limit(user_id)
    files_now = get_user_file_count(user_id)
    status = get_user_status_text(user_id)

    text = (
        f"👋 *Welcome, {esc_md(first_name)}!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 *ID*      : `{user_id}`\n"
        f"✳️ *User*    : @{esc_md(username or 'not set')}\n"
        f"💎 *Status*  : {status}\n"
        f"📁 *Files*   : `{files_now} / {fmt_limit(limit)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Upload `.py`, `.js`, or `.zip` to host.\n"
        f"👇 Use the buttons below or type a command."
    )
    try:
        bot.send_message(chat_id, text, reply_markup=create_reply_keyboard_main_menu(user_id),
                         parse_mode='Markdown')
    except Exception as e:
        logger.error(f"welcome error: {e}")


# ============================================================
#  LOGIC HANDLERS
# ============================================================
def _logic_updates_channel(message):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton('📢 Open Channel', url=UPDATE_CHANNEL))
    bot.reply_to(message, "📢 *Our Updates Channel*\nTap below to join:", reply_markup=mk, parse_mode='Markdown')


def _logic_upload_file(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "🔒 Bot is locked, cannot accept files.")
        return
    limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= limit:
        bot.reply_to(message, f"⚠️ File limit reached (`{get_user_file_count(user_id)}/{fmt_limit(limit)}`).\n"
                              f"Delete files first via 📂 *Check Files*.", parse_mode='Markdown')
        return
    bot.reply_to(
        message,
        "📤 *Send me your file*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📄 Supported: `.py`, `.js`, `.zip`\n"
        "📦 ZIP: include `requirements.txt` or `package.json` for auto-deps",
        parse_mode='Markdown'
    )


def _logic_check_files(message):
    user_id = message.from_user.id
    lst = user_files.get(user_id, [])
    if not lst:
        bot.reply_to(message, "📂 *Your Files*\n\n_(No files uploaded yet)_", parse_mode='Markdown')
        return
    mk = types.InlineKeyboardMarkup(row_width=1)
    for fn, ft in sorted(lst):
        icon = "🟢" if is_bot_running(user_id, fn) else "🔴"
        mk.add(types.InlineKeyboardButton(f"{icon} {fn} ({ft})",
                                          callback_data=f'file_{user_id}_{fn}'))
    bot.reply_to(message, "📂 *Your Files*\nTap to manage:", reply_markup=mk, parse_mode='Markdown')


def _logic_bot_speed(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    t0 = time.time()
    msg = bot.reply_to(message, "🏃 Testing speed...")
    try:
        bot.send_chat_action(chat_id, 'typing')
        ms = round((time.time() - t0) * 1000, 2)
        text = (
            "⚡ *Bot Speed*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ Latency : `{ms} ms`\n"
            f"🚦 Status  : {'🔴 Locked' if bot_locked else '🟢 Unlocked'}\n"
            f"💎 Level   : {get_user_status_text(user_id)}"
        )
        bot.edit_message_text(text, chat_id, msg.message_id, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"speed error: {e}")
        bot.edit_message_text("❌ Speed test failed", chat_id, msg.message_id)


def _logic_contact_owner(message):
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton('📞 Contact Owner',
                                      url=f'https://t.me/{YOUR_USERNAME.lstrip("@")}'))
    bot.reply_to(message, "📞 *Contact Owner*", reply_markup=mk, parse_mode='Markdown')


def _logic_subscriptions_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin only.")
        return
    bot.reply_to(message, "💳 *Subscription Management*\nSelect an action:",
                 reply_markup=create_subscription_menu(), parse_mode='Markdown')


def _logic_statistics(message):
    bot.reply_to(message, build_stats_text(message.from_user.id), parse_mode='Markdown')


def _logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin only.")
        return
    msg = bot.reply_to(message, "📢 Send the message to broadcast (text/photo/video).\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)


def _logic_toggle_lock_bot(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin only.")
        return
    global bot_locked
    bot_locked = not bot_locked
    logger.warning(f"Bot {'locked' if bot_locked else 'unlocked'} by {message.from_user.id}")
    bot.reply_to(message, f"{'🔒 Locked' if bot_locked else '🔓 Unlocked'}")


def _logic_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin only.")
        return
    bot.reply_to(message, "👑 *Admin Panel*",
                 reply_markup=create_admin_panel(), parse_mode='Markdown')


def _logic_run_all_scripts(message_or_call):
    if isinstance(message_or_call, telebot.types.Message):
        admin_id = message_or_call.from_user.id
        chat_id = message_or_call.chat.id
        msg_obj = message_or_call
    elif isinstance(message_or_call, telebot.types.CallbackQuery):
        admin_id = message_or_call.from_user.id
        chat_id = message_or_call.message.chat.id
        bot.answer_callback_query(message_or_call.id)
        msg_obj = message_or_call.message
    else:
        return

    if admin_id not in admin_ids:
        bot.send_message(chat_id, "⚠️ Admin only.")
        return

    bot.send_message(chat_id, "🚀 Starting all user scripts in background...")

    def _worker():
        started = 0
        users_proc = 0
        errors = 0
        snapshot = dict(user_files)
        for uid, files in snapshot.items():
            if not files:
                continue
            users_proc += 1
            folder = get_user_folder(uid)
            for fn, ft in files:
                if is_bot_running(uid, fn):
                    continue
                path = os.path.join(folder, fn)
                if not os.path.exists(path):
                    errors += 1
                    continue
                try:
                    if ft == 'py':
                        threading.Thread(target=run_script,
                                         args=(path, uid, folder, fn, msg_obj)).start()
                    elif ft == 'js':
                        threading.Thread(target=run_js_script,
                                         args=(path, uid, folder, fn, msg_obj)).start()
                    else:
                        errors += 1
                        continue
                    started += 1
                    time.sleep(0.6)
                except Exception as e:
                    logger.error(f"run all err: {e}")
                    errors += 1
        summary = (
            "✅ *Run-All Complete*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"▶️ Started  : `{started}`\n"
            f"👥 Users    : `{users_proc}`\n"
            f"⚠️ Errors   : `{errors}`"
        )
        try:
            bot.send_message(chat_id, summary, parse_mode='Markdown')
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# ============================================================
#  COMMAND / TEXT / DOCUMENT HANDLERS
# ============================================================
@bot.message_handler(commands=['start', 'help', 'menu'])
def cmd_start(m):
    _logic_send_welcome(m)


@bot.message_handler(commands=['status', 'statistics'])
def cmd_stats(m):
    _logic_statistics(m)


BUTTON_MAP = {
    "📢 Updates Channel": _logic_updates_channel,
    "📤 Upload File": _logic_upload_file,
    "📂 Check Files": _logic_check_files,
    "⚡ Bot Speed": _logic_bot_speed,
    "📞 Contact Owner": _logic_contact_owner,
    "📊 Statistics": _logic_statistics,
    "💳 Subscriptions": _logic_subscriptions_panel,
    "📢 Broadcast": _logic_broadcast_init,
    "🔒 Lock Bot": _logic_toggle_lock_bot,
    "🟢 Running All Code": _logic_run_all_scripts,
    "👑 Admin Panel": _logic_admin_panel,
}


@bot.message_handler(func=lambda m: m.text in BUTTON_MAP)
def handle_button(m):
    fn = BUTTON_MAP.get(m.text)
    if fn:
        fn(m)


@bot.message_handler(commands=['updateschannel'])
def cmd_upd(m):
    _logic_updates_channel(m)


@bot.message_handler(commands=['uploadfile'])
def cmd_up(m):
    _logic_upload_file(m)


@bot.message_handler(commands=['checkfiles'])
def cmd_cf(m):
    _logic_check_files(m)


@bot.message_handler(commands=['botspeed'])
def cmd_bs(m):
    _logic_bot_speed(m)


@bot.message_handler(commands=['contactowner'])
def cmd_co(m):
    _logic_contact_owner(m)


@bot.message_handler(commands=['subscriptions'])
def cmd_sub(m):
    _logic_subscriptions_panel(m)


@bot.message_handler(commands=['broadcast'])
def cmd_bc(m):
    _logic_broadcast_init(m)


@bot.message_handler(commands=['lockbot'])
def cmd_lock(m):
    _logic_toggle_lock_bot(m)


@bot.message_handler(commands=['adminpanel'])
def cmd_ap(m):
    _logic_admin_panel(m)


@bot.message_handler(commands=['runningallcode'])
def cmd_ra(m):
    _logic_run_all_scripts(m)


@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    t0 = time.time()
    msg = bot.reply_to(m, "🏓 Pong!")
    latency = round((time.time() - t0) * 1000, 2)
    bot.edit_message_text(f"🏓 *Pong!*\n⏱️ Latency: `{latency} ms`",
                          m.chat.id, msg.message_id, parse_mode='Markdown')


@bot.message_handler(content_types=['document'])
def handle_doc(m):
    user_id = m.from_user.id
    chat_id = m.chat.id
    doc = m.document
    logger.info(f"Doc from {user_id}: {doc.file_name}")

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(m, "🔒 Bot locked.")
        return

    limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= limit:
        bot.reply_to(m, f"⚠️ File limit reached (`{get_user_file_count(user_id)}/{fmt_limit(limit)}`).",
                     parse_mode='Markdown')
        return

    fn = doc.file_name
    if not fn:
        bot.reply_to(m, "⚠️ No file name")
        return
    ext = os.path.splitext(fn)[1].lower()
    if ext not in ('.py', '.js', '.zip'):
        bot.reply_to(m, "⚠️ Only `.py`, `.js`, `.zip` accepted", parse_mode='Markdown')
        return
    if doc.file_size > 20 * 1024 * 1024:
        bot.reply_to(m, "⚠️ Max file size is 20 MB")
        return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, m.message_id)
            bot.send_message(OWNER_ID,
                             f"⬆️ From {esc_md(m.from_user.first_name)} (`{user_id}`): `{fn}`",
                             parse_mode='Markdown')
        except Exception:
            pass

        wait = bot.reply_to(m, f"⏳ Downloading `{fn}`...", parse_mode='Markdown')
        tg_file = bot.get_file(doc.file_id)
        data = bot.download_file(tg_file.file_path)
        bot.edit_message_text(f"📦 Processing `{fn}`...", chat_id, wait.message_id, parse_mode='Markdown')

        folder = get_user_folder(user_id)

        if ext == '.zip':
            handle_zip_file(data, fn, m)
        else:
            path = os.path.join(folder, fn)
            with open(path, 'wb') as f:
                f.write(data)
            save_user_file(user_id, fn, 'py' if ext == '.py' else 'js')
            if ext == '.py':
                threading.Thread(target=run_script,
                                 args=(path, user_id, folder, fn, m)).start()
            else:
                threading.Thread(target=run_js_script,
                                 args=(path, user_id, folder, fn, m)).start()

    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"tg api error: {e}")
        if "file is too big" in str(e).lower():
            bot.reply_to(m, "❌ File too large to download")
        else:
            bot.reply_to(m, f"❌ Telegram error: {e}")
    except Exception as e:
        logger.error(f"doc handler error: {e}", exc_info=True)
        bot.reply_to(m, f"❌ Error: {e}")


# ============================================================
#  CALLBACK HANDLERS
# ============================================================
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data

    if bot_locked and user_id not in admin_ids and data not in ('back_to_main', 'speed', 'stats'):
        bot.answer_callback_query(call.id, "🔒 Bot is locked", show_alert=True)
        return

    try:
        if data == 'upload':
            _cb_upload(call)
        elif data == 'check_files':
            _cb_check_files(call)
        elif data.startswith('file_'):
            _cb_file_control(call)
        elif data.startswith('start_'):
            _cb_start(call)
        elif data.startswith('stop_'):
            _cb_stop(call)
        elif data.startswith('restart_'):
            _cb_restart(call)
        elif data.startswith('delete_'):
            _cb_delete(call)
        elif data.startswith('logs_'):
            _cb_logs(call)
        elif data == 'speed':
            _cb_speed(call)
        elif data == 'back_to_main':
            _cb_back(call)
        elif data == 'stats':
            _cb_stats(call)
        elif data.startswith('confirm_broadcast_'):
            _cb_confirm_bc(call)
        elif data == 'cancel_broadcast':
            _cb_cancel_bc(call)
        elif data == 'subscription':
            _admin_only(call, _cb_subs_menu)
        elif data == 'lock_bot':
            _admin_only(call, _cb_lock)
        elif data == 'unlock_bot':
            _admin_only(call, _cb_unlock)
        elif data == 'run_all_scripts':
            _admin_only(call, lambda c: _logic_run_all_scripts(c))
        elif data == 'broadcast':
            _admin_only(call, _cb_bc_init)
        elif data == 'admin_panel':
            _admin_only(call, _cb_admin_panel)
        elif data == 'add_admin':
            _owner_only(call, _cb_add_admin_init)
        elif data == 'remove_admin':
            _owner_only(call, _cb_remove_admin_init)
        elif data == 'list_admins':
            _admin_only(call, _cb_list_admins)
        elif data == 'add_subscription':
            _admin_only(call, _cb_add_sub_init)
        elif data == 'remove_subscription':
            _admin_only(call, _cb_remove_sub_init)
        elif data == 'check_subscription':
            _admin_only(call, _cb_check_sub_init)
        else:
            bot.answer_callback_query(call.id, "Unknown action")
    except Exception as e:
        logger.error(f"cb error [{data}]: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "❌ Error")
        except Exception:
            pass


def _admin_only(call, fn):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only", show_alert=True)
        return
    fn(call)


def _owner_only(call, fn):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⚠️ Owner only", show_alert=True)
        return
    fn(call)


def _cb_upload(call):
    uid = call.from_user.id
    if get_user_file_count(uid) >= get_user_file_limit(uid):
        bot.answer_callback_query(call.id, "⚠️ File limit reached", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
                     "📤 Send your `.py`, `.js`, or `.zip` file.",
                     parse_mode='Markdown')


def _cb_check_files(call):
    uid = call.from_user.id
    lst = user_files.get(uid, [])
    mk = types.InlineKeyboardMarkup(row_width=1)
    if lst:
        for fn, ft in sorted(lst):
            icon = "🟢" if is_bot_running(uid, fn) else "🔴"
            mk.add(types.InlineKeyboardButton(f"{icon} {fn} ({ft})",
                                              callback_data=f'file_{uid}_{fn}'))
    else:
        mk.add(types.InlineKeyboardButton("📂 No files", callback_data='noop'))
    mk.add(types.InlineKeyboardButton("🔙 Back", callback_data='back_to_main'))
    try:
        bot.edit_message_text("📂 *Your Files*\nTap to manage:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=mk, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e):
            logger.error(f"check_files cb: {e}")
    bot.answer_callback_query(call.id)


def _cb_file_control(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        lst = user_files.get(oid, [])
        ftype = next((f[1] for f in lst if f[0] == fn), None)
        if not ftype:
            bot.answer_callback_query(call.id, "⚠️ File not found", show_alert=True)
            _cb_check_files(call)
            return
        running = is_bot_running(oid, fn)
        status = "🟢 Running" if running else "🔴 Stopped"
        try:
            bot.edit_message_text(
                f"⚙️ *Controls*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📄 File  : `{fn}`\n"
                f"📁 Type  : `{ftype}`\n"
                f"👤 Owner : `{oid}`\n"
                f"🚦 Status: {status}",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_control_buttons(oid, fn, running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" not in str(e):
                raise
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"file control: {e}")
        bot.answer_callback_query(call.id, "❌ Error", show_alert=True)


def _cb_start(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        lst = user_files.get(oid, [])
        ftype = next((f[1] for f in lst if f[0] == fn), None)
        if not ftype:
            bot.answer_callback_query(call.id, "⚠️ File missing", show_alert=True)
            return
        folder = get_user_folder(oid)
        path = os.path.join(folder, fn)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "⚠️ File not on disk", show_alert=True)
            remove_user_file_db(oid, fn)
            return
        if is_bot_running(oid, fn):
            bot.answer_callback_query(call.id, "⚠️ Already running", show_alert=True)
            return
        bot.answer_callback_query(call.id, "⏳ Starting...")
        if ftype == 'py':
            threading.Thread(target=run_script, args=(path, oid, folder, fn, call.message)).start()
        else:
            threading.Thread(target=run_js_script, args=(path, oid, folder, fn, call.message)).start()
    except Exception as e:
        logger.error(f"start cb: {e}", exc_info=True)


def _cb_stop(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        key = f"{oid}_{fn}"
        if not is_bot_running(oid, fn):
            bot.answer_callback_query(call.id, "Already stopped", show_alert=True)
            return
        info = bot_scripts.get(key)
        if info:
            kill_process_tree(info)
            bot_scripts.pop(key, None)
        bot.answer_callback_query(call.id, "🔴 Stopped")
        _cb_file_control(call)
    except Exception as e:
        logger.error(f"stop cb: {e}", exc_info=True)


def _cb_restart(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        lst = user_files.get(oid, [])
        ftype = next((f[1] for f in lst if f[0] == fn), None)
        if not ftype:
            bot.answer_callback_query(call.id, "⚠️ File missing", show_alert=True)
            return
        folder = get_user_folder(oid)
        path = os.path.join(folder, fn)
        key = f"{oid}_{fn}"

        bot.answer_callback_query(call.id, "🔄 Restarting...")
        if is_bot_running(oid, fn):
            info = bot_scripts.get(key)
            if info:
                kill_process_tree(info)
            bot_scripts.pop(key, None)
            time.sleep(1)

        if ftype == 'py':
            threading.Thread(target=run_script, args=(path, oid, folder, fn, call.message)).start()
        else:
            threading.Thread(target=run_js_script, args=(path, oid, folder, fn, call.message)).start()
    except Exception as e:
        logger.error(f"restart cb: {e}", exc_info=True)


def _cb_delete(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        key = f"{oid}_{fn}"
        if is_bot_running(oid, fn):
            info = bot_scripts.get(key)
            if info:
                kill_process_tree(info)
            bot_scripts.pop(key, None)
            time.sleep(0.4)

        folder = get_user_folder(oid)
        path = os.path.join(folder, fn)
        log = os.path.join(folder, f"{os.path.splitext(fn)[0]}.log")
        for p in (path, log):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        remove_user_file_db(oid, fn)
        bot.answer_callback_query(call.id, "🗑️ Deleted")
        try:
            bot.edit_message_text(f"🗑️ `{fn}` deleted.",
                                  call.message.chat.id, call.message.message_id,
                                  parse_mode='Markdown')
        except Exception:
            pass
    except Exception as e:
        logger.error(f"delete cb: {e}", exc_info=True)


def _cb_logs(call):
    try:
        _, oid_s, fn = call.data.split('_', 2)
        oid = int(oid_s)
        req = call.from_user.id
        if not (req == oid or req in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Not permitted", show_alert=True)
            return
        folder = get_user_folder(oid)
        log = os.path.join(folder, f"{os.path.splitext(fn)[0]}.log")
        if not os.path.exists(log):
            bot.answer_callback_query(call.id, "⚠️ No log file", show_alert=True)
            return
        size = os.path.getsize(log)
        if size == 0:
            content = "(empty)"
        elif size > 100 * 1024:
            with open(log, 'rb') as f:
                f.seek(-100 * 1024, os.SEEK_END)
                content = "(last 100KB)\n...\n" + f.read().decode('utf-8', errors='ignore')
        else:
            with open(log, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        if len(content) > 4000:
            content = "..." + content[-3990:]
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id,
                         f"📜 *Logs* for `{fn}`:\n```\n{content}\n```",
                         parse_mode='Markdown')
    except Exception as e:
        logger.error(f"logs cb: {e}", exc_info=True)


def _cb_speed(call):
    uid = call.from_user.id
    t0 = time.time()
    bot.answer_callback_query(call.id)
    try:
        bot.send_chat_action(call.message.chat.id, 'typing')
    except Exception:
        pass
    ms = round((time.time() - t0) * 1000, 2)
    text = (
        "⚡ *Bot Speed*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ Latency : `{ms} ms`\n"
        f"🚦 Status  : {'🔴 Locked' if bot_locked else '🟢 Unlocked'}\n"
        f"💎 Level   : {get_user_status_text(uid)}"
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_menu_inline(uid), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"speed cb: {e}")


def _cb_stats(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(build_stats_text(call.from_user.id),
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_menu_inline(call.from_user.id),
                              parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "not modified" not in str(e):
            logger.error(f"stats cb: {e}")


def _cb_back(call):
    uid = call.from_user.id
    limit = get_user_file_limit(uid)
    text = (
        f"🏠 *Main Menu*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID     : `{uid}`\n"
        f"💎 Status : {get_user_status_text(uid)}\n"
        f"📁 Files  : `{get_user_file_count(uid)} / {fmt_limit(limit)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Choose an option below:"
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=create_main_menu_inline(uid), parse_mode='Markdown')
    except Exception as e:
        if "not modified" not in str(e):
            logger.error(f"back cb: {e}")
    bot.answer_callback_query(call.id)


def _cb_subs_menu(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("💳 *Subscription Management*",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_subscription_menu(), parse_mode='Markdown')
    except Exception:
        pass


def _cb_lock(call):
    global bot_locked
    bot_locked = True
    logger.warning(f"Locked by {call.from_user.id}")
    bot.answer_callback_query(call.id, "🔒 Locked")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception:
        pass


def _cb_unlock(call):
    global bot_locked
    bot_locked = False
    logger.warning(f"Unlocked by {call.from_user.id}")
    bot.answer_callback_query(call.id, "🔓 Unlocked")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception:
        pass


def _cb_bc_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
                           "📢 Send the message to broadcast (text/photo/video).\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)


def _cb_admin_panel(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("👑 *Admin Panel*",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel(), parse_mode='Markdown')
    except Exception:
        pass


def _cb_add_admin_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "👑 Enter User ID to promote to Admin.\n/cancel to abort.")
    bot.register_next_step_handler(msg, _process_add_admin)


def _cb_remove_admin_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "👑 Enter User ID of Admin to remove.\n/cancel to abort.")
    bot.register_next_step_handler(msg, _process_remove_admin)


def _cb_list_admins(call):
    bot.answer_callback_query(call.id)
    lines = [f"• `{aid}` {'(Owner)' if aid == OWNER_ID else ''}" for aid in sorted(admin_ids)]
    txt = "👑 *Current Admins*\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines)
    try:
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel(), parse_mode='Markdown')
    except Exception:
        pass


def _process_add_admin(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "⚠️ Owner only")
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Cancelled")
        return
    try:
        aid = int(m.text.strip())
        if aid <= 0:
            raise ValueError
        if aid == OWNER_ID:
            bot.reply_to(m, "Already owner")
            return
        if aid in admin_ids:
            bot.reply_to(m, "Already admin")
            return
        add_admin_db(aid)
        bot.reply_to(m, f"✅ `{aid}` promoted to Admin", parse_mode='Markdown')
        try:
            bot.send_message(aid, "🎉 You are now an Admin!")
        except Exception:
            pass
    except ValueError:
        msg = bot.reply_to(m, "⚠️ Invalid ID. Try again or /cancel")
        bot.register_next_step_handler(msg, _process_add_admin)


def _process_remove_admin(m):
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, "⚠️ Owner only")
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Cancelled")
        return
    try:
        aid = int(m.text.strip())
        if aid == OWNER_ID:
            bot.reply_to(m, "Cannot remove owner")
            return
        if remove_admin_db(aid):
            bot.reply_to(m, f"✅ `{aid}` removed from Admin", parse_mode='Markdown')
            try:
                bot.send_message(aid, "ℹ️ You are no longer an Admin.")
            except Exception:
                pass
        else:
            bot.reply_to(m, "❌ Could not remove")
    except ValueError:
        msg = bot.reply_to(m, "⚠️ Invalid ID. Try again or /cancel")
        bot.register_next_step_handler(msg, _process_remove_admin)


def _cb_add_sub_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
                           "💳 Enter `USER_ID DAYS` (e.g. `12345678 30`).\n/cancel to abort.",
                           parse_mode='Markdown')
    bot.register_next_step_handler(msg, _process_add_sub)


def _cb_remove_sub_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to remove sub from.\n/cancel to abort.")
    bot.register_next_step_handler(msg, _process_remove_sub)


def _cb_check_sub_init(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to check.\n/cancel to abort.")
    bot.register_next_step_handler(msg, _process_check_sub)


def _process_add_sub(m):
    if m.from_user.id not in admin_ids:
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Cancelled")
        return
    try:
        parts = m.text.split()
        if len(parts) != 2:
            raise ValueError
        uid = int(parts[0])
        days = int(parts[1])
        if uid <= 0 or days <= 0:
            raise ValueError
        now = datetime.now()
        cur = user_subscriptions.get(uid, {}).get('expiry')
        start = cur if (cur and cur > now) else now
        new_exp = start + timedelta(days=days)
        save_subscription(uid, new_exp)
        bot.reply_to(m, f"✅ Sub for `{uid}` +{days}d\nExpires: `{new_exp:%Y-%m-%d}`",
                     parse_mode='Markdown')
        try:
            bot.send_message(uid, f"💎 Subscription active! Expires: {new_exp:%Y-%m-%d}")
        except Exception:
            pass
    except ValueError:
        msg = bot.reply_to(m, "⚠️ Invalid format. Use `USER_ID DAYS` or /cancel", parse_mode='Markdown')
        bot.register_next_step_handler(msg, _process_add_sub)


def _process_remove_sub(m):
    if m.from_user.id not in admin_ids:
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Cancelled")
        return
    try:
        uid = int(m.text.strip())
        if uid not in user_subscriptions:
            bot.reply_to(m, "⚠️ No active sub")
            return
        remove_subscription_db(uid)
        bot.reply_to(m, f"✅ Sub removed for `{uid}`", parse_mode='Markdown')
        try:
            bot.send_message(uid, "ℹ️ Your subscription has been removed.")
        except Exception:
            pass
    except ValueError:
        msg = bot.reply_to(m, "⚠️ Invalid ID. Try again or /cancel")
        bot.register_next_step_handler(msg, _process_remove_sub)


def _process_check_sub(m):
    if m.from_user.id not in admin_ids:
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Cancelled")
        return
    try:
        uid = int(m.text.strip())
        sub = user_subscriptions.get(uid)
        if not sub:
            bot.reply_to(m, f"ℹ️ No sub for `{uid}`", parse_mode='Markdown')
            return
        exp = sub['expiry']
        if exp > datetime.now():
            days = (exp - datetime.now()).days
            bot.reply_to(m, f"✅ `{uid}` active\nExpires: `{exp:%Y-%m-%d %H:%M}` ({days}d left)",
                         parse_mode='Markdown')
        else:
            bot.reply_to(m, f"⚠️ `{uid}` expired on `{exp:%Y-%m-%d}`", parse_mode='Markdown')
            remove_subscription_db(uid)
    except ValueError:
        msg = bot.reply_to(m, "⚠️ Invalid ID. Try again or /cancel")
        bot.register_next_step_handler(msg, _process_check_sub)


# ============================================================
#  BROADCAST
# ============================================================
def process_broadcast_message(m):
    if m.from_user.id not in admin_ids:
        return
    if (m.text or '').lower() == '/cancel':
        bot.reply_to(m, "Broadcast cancelled")
        return

    has_content = (
        m.text or m.photo or m.video or m.document or m.sticker or m.voice or m.audio
    )
    if not has_content:
        msg = bot.reply_to(m, "⚠️ Empty message. Try again or /cancel")
        bot.register_next_step_handler(msg, process_broadcast_message)
        return

    preview = esc_md(m.text[:1000]) if m.text else "(media)"
    mk = types.InlineKeyboardMarkup()
    mk.row(
        types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_broadcast_{m.message_id}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast"),
    )
    bot.reply_to(m,
                 f"⚠️ *Confirm Broadcast*\n"
                 f"━━━━━━━━━━━━━━━━━━\n"
                 f"To *{len(active_users)}* users:\n\n{preview}",
                 reply_markup=mk, parse_mode='Markdown')


def _cb_confirm_bc(call):
    if call.from_user.id not in admin_ids:
        return
    orig = call.message.reply_to_message
    if not orig:
        bot.edit_message_text("❌ Original message lost",
                              call.message.chat.id, call.message.message_id)
        return
    bot.answer_callback_query(call.id, "🚀 Broadcasting...")
    bot.edit_message_text(f"📢 Broadcasting to {len(active_users)} users...",
                          call.message.chat.id, call.message.message_id)

    text = orig.text
    photo = orig.photo[-1].file_id if orig.photo else None
    video = orig.video.file_id if orig.video else None
    caption = orig.caption if (photo or video) else None

    def _worker():
        sent, failed, blocked = 0, 0, 0
        t0 = time.time()
        users = list(active_users)
        total = len(users)
        for i, uid in enumerate(users):
            try:
                if text:
                    bot.send_message(uid, text, parse_mode='Markdown')
                elif photo:
                    bot.send_photo(uid, photo, caption=caption,
                                   parse_mode='Markdown' if caption else None)
                elif video:
                    bot.send_video(uid, video, caption=caption,
                                   parse_mode='Markdown' if caption else None)
                sent += 1
            except telebot.apihelper.ApiTelegramException as e:
                s = str(e).lower()
                if any(k in s for k in ("blocked", "deactivated", "chat not found",
                                        "kicked", "restricted")):
                    blocked += 1
                elif "flood" in s or "too many" in s:
                    time.sleep(10)
                    failed += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            if (i + 1) % 25 == 0 and i < total - 1:
                time.sleep(1.2)
            elif i % 5 == 0:
                time.sleep(0.15)
        dur = round(time.time() - t0, 2)
        try:
            bot.send_message(call.message.chat.id,
                             f"📢 *Broadcast Complete*\n"
                             f"━━━━━━━━━━━━━━━━━━\n"
                             f"✅ Sent    : `{sent}`\n"
                             f"❌ Failed  : `{failed}`\n"
                             f"🚫 Blocked : `{blocked}`\n"
                             f"👥 Targets : `{total}`\n"
                             f"⏱️ Time    : `{dur}s`",
                             parse_mode='Markdown')
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _cb_cancel_bc(call):
    bot.answer_callback_query(call.id, "Cancelled")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass


# ============================================================
#  CLEANUP
# ============================================================
def cleanup():
    logger.warning("Shutting down — killing running scripts")
    for key in list(bot_scripts.keys()):
        info = bot_scripts.get(key)
        if info:
            kill_process_tree(info)
        bot_scripts.pop(key, None)


atexit.register(cleanup)


def set_bot_commands():
    cmds = [
        types.BotCommand('start', 'Start the bot'),
        types.BotCommand('menu', 'Show main menu'),
        types.BotCommand('uploadfile', 'Upload a script'),
        types.BotCommand('checkfiles', 'Manage your files'),
        types.BotCommand('botspeed', 'Test bot speed'),
        types.BotCommand('statistics', 'View statistics'),
        types.BotCommand('contactowner', 'Contact owner'),
        types.BotCommand('ping', 'Ping the bot'),
    ]
    try:
        bot.set_my_commands(cmds)
        logger.info("✓ Commands registered")
    except Exception as e:
        logger.warning(f"Could not set commands: {e}")


# ============================================================
#  MAIN  ← ✅ 409 CONFLICT FIX IS HERE
# ============================================================
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🤖 Bot Starting")
    logger.info(f"🐍 Python {sys.version.split()[0]}")
    logger.info(f"📁 Upload dir : {UPLOAD_BOTS_DIR}")
    logger.info(f"📊 Data dir   : {IROTECH_DIR}")
    logger.info(f"🔑 Owner      : {OWNER_ID}")
    logger.info(f"🛡️ Admins     : {sorted(admin_ids)}")
    logger.info("=" * 50)

    keep_alive()

    # ─── ✅ CRITICAL FIX FOR 409 "Conflict: can't use getUpdates while webhook is active" ───
    # Telegram allows either webhook OR polling, never both.
    # If a previous deploy (or another service) set a webhook, we MUST remove it first.
    # We retry a few times in case of a transient network error.
    webhook_cleared = False
    for attempt in range(1, 6):
        try:
            # drop_pending_updates=True discards any queued old updates so we don't
            # process duplicate messages after the restart.
            bot.delete_webhook(drop_pending_updates=True)
            logger.info(f"✓ Webhook cleared (attempt {attempt}) — pending updates dropped")
            webhook_cleared = True
            break
        except Exception as e:
            logger.warning(f"Webhook delete attempt {attempt} failed: {e}")
            time.sleep(3)

    if not webhook_cleared:
        logger.error("❌ Could not clear webhook after 5 attempts. "
                     "If this persists, revoke the token via @BotFather and redeploy.")

    # Now safe to register commands and start polling
    set_bot_commands()

    logger.info("🚀 Polling...")
    while True:
        try:
            bot.infinity_polling(logger_level=logging.INFO, timeout=60, long_polling_timeout=30)
        except requests.exceptions.ReadTimeout:
            logger.warning("ReadTimeout — restarting in 5s")
            time.sleep(5)
        except requests.exceptions.ConnectionError as ce:
            logger.error(f"ConnectionError: {ce} — retrying in 15s")
            time.sleep(15)
        except Exception as e:
            logger.critical(f"Polling error: {e}", exc_info=True)
            logger.info("Restarting in 30s")
            time.sleep(30)
        finally:
            time.sleep(1)