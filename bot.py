import asyncio
import os
import re
import time
import zipfile
import shutil
import concurrent.futures
import yt_dlp
import aiosqlite
import aiohttp
import imageio_ffmpeg as ff
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, FSInputFile)
from aiogram.types.reaction_type_emoji import ReactionTypeEmoji
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from pyrogram import Client

# ─── imageio_ffmpeg: автоматически находит ffmpeg без системной установки ───
FFMPEG_PATH = ff.get_ffmpeg_exe()

# ================= НАСТРОЙКИ =================
BOT_TOKEN      = "8780332671:AAGFRCXcjHiO79egzeY7Jfjkt0y2HLTqi6c"
API_ID         = 33120499
API_HASH       = "98835783a52a878e271c0c7acbc24876"
ADMIN_GROUP_ID = -1003152594582
MAIN_ADMIN_ID  = 7720599904
DB_PATH        = "bot_database.db"
DOWNLOADS_DIR  = "downloads"
BOT_USERNAME   = "@VidLoads_Bot"

PRICES_STARS = {7: 50,  30: 150, 60: 250,  365: 1000}
PRICES_RUB   = {7: 80,  30: 150, 60: 300,  365: 1800}
PERIOD_LABELS = {7: "1 неделя", 30: "1 месяц", 60: "2 месяца", 365: "1 год"}

# Таймауты (секунды)
DOWNLOAD_TIMEOUT = 600   # 10 минут на скачивание
ANALYSIS_TIMEOUT = 60    # 1 минута на анализ ссылки
UPLOAD_TIMEOUT   = 600   # 10 минут на отправку

PLATFORM_NAMES = {
    'youtube.com': 'YouTube',   'youtu.be': 'YouTube',
    'tiktok.com':  'TikTok',    'vm.tiktok.com': 'TikTok',
    'instagram.com':'Instagram','instagr.am': 'Instagram',
    'pinterest.':  'Pinterest', 'pin.it': 'Pinterest',
    'twitter.com': 'Twitter/X', 'x.com': 'Twitter/X',
    'vk.com':      'VKontakte', 'vkvideo.ru': 'VKontakte',
    'dailymotion.com':'Dailymotion','vimeo.com': 'Vimeo',
    'reddit.com':  'Reddit',    'redd.it': 'Reddit',
    'facebook.com':'Facebook',  'fb.watch': 'Facebook',
    'twitch.tv':   'Twitch',    'rumble.com': 'Rumble',
    'ok.ru':       'OK.ru',     'coub.com': 'Coub',
    'spotify.com': 'Spotify',   'open.spotify.com': 'Spotify',
    'music.yandex.ru': 'Яндекс Музыка', 'music.yandex.com': 'Яндекс Музыка',
}

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

_session = AiohttpSession(timeout=3600)
bot      = Bot(token=BOT_TOKEN, session=_session)
dp       = Dispatcher()
router   = Router()
pyro_app: Client             = None
pyro_upload_semaphore: asyncio.Semaphore = None

# ThreadPoolExecutor с ограниченными воркерами
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ================= ПРЕМИУМ ЭМОДЗИ =================
# Используйте E("id") в текстах сообщений
def E(emoji_id: str, fallback: str = "•") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

# Удобные константы
E_SETTINGS  = E("5870982283724328568", "⚙")
E_PROFILE   = E("5870994129244131212", "👤")
E_PEOPLE    = E("5870772616305839506", "👥")
E_FILE      = E("5870528606328852614", "📁")
E_STATS     = E("5870921681735781843", "📊")
E_LOCK      = E("6037249452824072506", "🔒")
E_UNLOCK    = E("6037496202990194718", "🔓")
E_BROADCAST = E("6039422865189638057", "📣")
E_CHECK     = E("5870633910337015697", "✅")
E_CROSS     = E("5870657884844462243", "❌")
E_PEN       = E("5870676941614354370", "🖋")
E_TRASH     = E("5870875489362513438", "🗑")
E_LINK      = E("5769289093221454192", "🔗")
E_INFO      = E("6028435952299413210", "ℹ")
E_BOT       = E("6030400221232501136", "🤖")
E_EYE       = E("6037397706505195857", "👁")
E_UPLOAD    = E("5963103826075456248", "⬆")
E_DOWNLOAD  = E("6039802767931871481", "⬇")
E_BELL      = E("6039486778597970865", "🔔")
E_GIFT      = E("6032644646587338669", "🎁")
E_CLOCK     = E("5983150113483134607", "⏰")
E_PARTY     = E("6041731551845159060", "🎉")
E_MEDIA     = E("6035128606563241721", "🖼")
E_WALLET    = E("5769126056262898415", "👛")
E_BOX       = E("5884479287171485878", "📦")
E_CALENDAR  = E("5890937706803894250", "📅")
E_TAG       = E("5886285355279193209", "🏷")
E_TIMELEFT  = E("5775896410780079073", "🕓")
E_BRUSH     = E("6050679691004612757", "🖌")
E_FORMAT    = E("5778479949572738874", "↔")
E_COIN      = E("5904462880941545555", "🪙")
E_SENDMONEY = E("5890848474563352982", "🪙")
E_MONEY     = E("5879814368572478751", "🏧")
E_RELOAD    = E("5345906554510012647", "🔄")
E_BACK      = "◁"  # для кнопок назад


# ================= FSM СТЕЙТЫ =================
class PaymentState(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_promo   = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast  = State()
    waiting_for_limit      = State()
    waiting_for_promo_data = State()

class DownloadState(StatesGroup):
    waiting_for_quality = State()

class MusicState(StatesGroup):
    waiting_for_playlist_confirm = State()


# ================= ПРОГРЕСС =================
progress_data: dict = {}

class SilentLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def get_progress_hook(task_id: int):
    def hook(d):
        if d['status'] == 'downloading':
            clean = lambda s: re.sub(r'\x1b\[[0-9;]*m', '', str(s)).strip()
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            progress_data[task_id] = {
                'percent': clean(d.get('_percent_str', '0%')),
                'speed':   clean(d.get('_speed_str', '—')),
                'eta':     clean(d.get('_eta_str', '—')),
                'phase':   'download',
                'size_mb': round(total / 1024 / 1024, 1) if total else 0,
            }
    return hook


def build_bar(percent_str: str, width: int = 12) -> str:
    try:
        pct = float(percent_str.replace('%', '').strip())
    except Exception:
        pct = 0
    filled = int(pct / 100 * width)
    return '█' * filled + '░' * (width - filled)


async def update_progress_message(msg: Message, task_id: int):
    last_text = ""
    while task_id in progress_data:
        d     = progress_data.get(task_id, {})
        phase = d.get('phase', 'download')
        bar   = build_bar(d.get('percent', '0%'))
        pct   = d.get('percent', '0%')
        spd   = d.get('speed', '—')
        eta   = d.get('eta', '—')
        sz    = d.get('size_mb', 0)
        sz_s  = f" · {sz} МБ" if sz else ""

        if phase == 'download':
            icon, title = E_DOWNLOAD, "Скачиваю..."
        elif phase == 'convert':
            icon, title = E_RELOAD, "Конвертирую..."
        else:
            icon, title = E_UPLOAD, "Отправляю..."

        text = (
            f'<b>{icon} {title}</b>{sz_s}\n\n'
            f'<code>{bar}</code> <b>{pct}</b>\n\n'
            f'{E_RELOAD} Скорость: <code>{spd}</code>\n'
            f'{E_CLOCK} Осталось: <code>{eta}</code>'
        )
        if text != last_text:
            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML)
                last_text = text
            except Exception:
                try:
                    await msg.edit_caption(caption=text, parse_mode=ParseMode.HTML)
                    last_text = text
                except Exception:
                    pass
        await asyncio.sleep(2)


# ================= УТИЛИТЫ =================
def detect_platform(url: str) -> str:
    for domain, name in PLATFORM_NAMES.items():
        if domain in url:
            return name
    return "Веб"

def is_tiktok(url: str) -> bool:
    return any(d in url for d in ('tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'))

def is_spotify(url: str) -> bool:
    return 'spotify.com' in url

def is_yandex_music(url: str) -> bool:
    return 'music.yandex.' in url

def is_music_service(url: str) -> bool:
    return is_spotify(url) or is_yandex_music(url)

def is_image_url(url: str) -> bool:
    clean = url.split('?')[0].lower()
    return any(clean.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'))

def is_playlist_url(url: str) -> bool:
    """Определяет, является ли ссылка плейлистом."""
    return any(kw in url for kw in (
        'playlist', '/album/', '/set/', 'list=PL', 'list=LL',
        'music.yandex.ru/users/', 'music.yandex.ru/album/'
    ))

def fmt_size(b: int) -> str:
    if b < 1024:        return f"{b} Б"
    if b < 1024**2:     return f"{b/1024:.1f} КБ"
    return f"{b/1024/1024:.1f} МБ"

def fmt_dur(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

async def safe_edit(msg: Message, text: str, parse_mode=ParseMode.HTML):
    try:
        await msg.edit_text(text, parse_mode=parse_mode)
    except Exception:
        try:
            await msg.edit_caption(caption=text, parse_mode=parse_mode)
        except Exception:
            pass


# ================= ВОДЯНОЙ ЗНАК (imageio_ffmpeg) =================
def add_watermark_sync(input_path: str) -> str:
    """Добавляет водяной знак через ffmpeg из imageio_ffmpeg."""
    output_path = input_path.rsplit('.', 1)[0] + '_wm.mp4'
    watermark   = f'Скачано через {BOT_USERNAME}'
    import subprocess
    cmd = [
        FFMPEG_PATH, '-i', input_path,
        '-vf', (
            f"drawtext=text='{watermark}'"
            ":fontcolor=white:fontsize=20:alpha=0.8"
            ":x=10:y=H-th-12"
            ":shadowcolor=black@0.7:shadowx=2:shadowy=2"
            ":box=1:boxcolor=black@0.35:boxborderw=8"
        ),
        '-codec:a', 'copy', '-preset', 'ultrafast', '-y', output_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0 and os.path.exists(output_path):
        os.remove(input_path)
        return output_path
    return input_path


# ================= БАЗА ДАННЫХ =================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT    DEFAULT '',
                first_name      TEXT    DEFAULT '',
                premium_until   INTEGER DEFAULT 0,
                downloads_count INTEGER DEFAULT 0,
                total_downloads INTEGER DEFAULT 0,
                last_reset      INTEGER DEFAULT 0,
                joined_at       INTEGER DEFAULT 0
            )
        ''')
        for col in [
            'total_downloads INTEGER DEFAULT 0',
            'username TEXT DEFAULT ""',
            'first_name TEXT DEFAULT ""',
            'joined_at INTEGER DEFAULT 0',
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass

        await db.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS promo_codes (
                code             TEXT    PRIMARY KEY,
                discount_percent INTEGER NOT NULL,
                max_uses         INTEGER DEFAULT 0,
                uses_count       INTEGER DEFAULT 0,
                expires_at       INTEGER DEFAULT 0,
                created_at       INTEGER DEFAULT 0
            )
        ''')
        for col in [
            'max_uses INTEGER DEFAULT 0',
            'uses_count INTEGER DEFAULT 0',
            'expires_at INTEGER DEFAULT 0',
            'created_at INTEGER DEFAULT 0',
        ]:
            try:
                await db.execute(f"ALTER TABLE promo_codes ADD COLUMN {col}")
            except Exception:
                pass

        await db.execute("INSERT OR IGNORE INTO settings VALUES ('requisites', 'Карта: 0000')")
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('daily_limit', '3')")
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('watermark_enabled', '0')")
        await db.commit()


async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r[0] if r else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
        await db.commit()

async def get_daily_limit() -> int:
    return int(await get_setting('daily_limit') or 3)

async def is_watermark_enabled() -> bool:
    return (await get_setting('watermark_enabled')) == '1'


# ── Промокоды ─────────────────────────────────────────────────────────────────
async def get_all_promos() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code,discount_percent,max_uses,uses_count,expires_at,created_at "
            "FROM promo_codes ORDER BY created_at DESC"
        ) as c:
            rows = await c.fetchall()
    return [{'code':r[0],'discount':r[1],'max_uses':r[2],
              'uses':r[3],'expires_at':r[4],'created_at':r[5]} for r in rows]

async def delete_promo(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        await db.commit()

async def get_promo_discount(code: str) -> int | None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT discount_percent,max_uses,uses_count,expires_at FROM promo_codes WHERE code=?",
            (code,)
        ) as c:
            row = await c.fetchone()
        if not row:
            return None
        discount, max_uses, uses_count, expires_at = row
        if expires_at and now > expires_at:
            return None
        if max_uses and uses_count >= max_uses:
            return None
        await db.execute(
            "UPDATE promo_codes SET uses_count=uses_count+1 WHERE code=?", (code,)
        )
        await db.commit()
    return discount

async def get_promo_error(code: str) -> str:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT max_uses,uses_count,expires_at FROM promo_codes WHERE code=?", (code,)
        ) as c:
            row = await c.fetchone()
    if not row:
        return "Промокод не найден"
    max_uses, uses_count, expires_at = row
    if expires_at and now > expires_at:
        return "Срок действия промокода истёк"
    if max_uses and uses_count >= max_uses:
        return f"Промокод исчерпан (использован {uses_count} раз)"
    return "Промокод недействителен"


# ── Пользователи ──────────────────────────────────────────────────────────────
async def ensure_user(user_id: int, username: str = '', first_name: str = ''):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id,username,first_name,last_reset,joined_at) VALUES (?,?,?,?,?)",
            (user_id, username, first_name, int(time.time()), int(time.time()))
        )
        await db.execute(
            "UPDATE users SET username=?,first_name=? WHERE user_id=?",
            (username, first_name, user_id)
        )
        await db.commit()

async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
            return bool(r and r[0] > int(time.time()))

async def add_premium_days(user_id: int, days: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
        base = r[0] if r and r[0] > now else now
        await db.execute(
            "UPDATE users SET premium_until=? WHERE user_id=?",
            (base + days * 86400, user_id)
        )
        await db.commit()

async def check_limits(user_id: int) -> bool:
    if await is_premium(user_id):
        return True
    limit = await get_daily_limit()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT downloads_count,last_reset FROM users WHERE user_id=?", (user_id,)
        ) as c:
            user = await c.fetchone()
    if not user:
        return limit > 0
    now = int(time.time())
    if now - user[1] > 86400:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET downloads_count=0,last_reset=? WHERE user_id=?",
                (now, user_id)
            )
            await db.commit()
        return limit > 0
    return user[0] < limit

async def get_remaining(user_id: int) -> str:
    if await is_premium(user_id):
        return "∞"
    limit = await get_daily_limit()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT downloads_count,last_reset FROM users WHERE user_id=?", (user_id,)
        ) as c:
            u = await c.fetchone()
    if not u or int(time.time()) - u[1] > 86400:
        return str(limit)
    return str(max(0, limit - u[0]))

async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET downloads_count=downloads_count+1,"
            "total_downloads=total_downloads+1 WHERE user_id=?",
            (user_id,)
        )
        await db.commit()


# ================= КЛАВИАТУРЫ =================
def get_main_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="Купить Premium",
            callback_data="buy_premium",
            icon_custom_emoji_id="6032644646587338669"
        )],
        [
            InlineKeyboardButton(
                text="Профиль",
                callback_data="profile",
                icon_custom_emoji_id="5870994129244131212"
            ),
            InlineKeyboardButton(
                text="Статистика",
                callback_data="my_stats",
                icon_custom_emoji_id="5870921681735781843"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Помощь",
                callback_data="help_menu",
                icon_custom_emoji_id="6028435952299413210"
            ),
            InlineKeyboardButton(
                text="Поделиться",
                callback_data="share_bot",
                icon_custom_emoji_id="5769289093221454192"
            ),
        ],
    ]
    if admin:
        rows.append([InlineKeyboardButton(
            text="Панель администратора",
            callback_data="admin_panel",
            icon_custom_emoji_id="5870982283724328568"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    wm = await is_watermark_enabled()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Статистика",
                callback_data="admin_stats",
                icon_custom_emoji_id="5870921681735781843"
            ),
            InlineKeyboardButton(
                text="Рассылка",
                callback_data="admin_broadcast",
                icon_custom_emoji_id="6039422865189638057"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Лимит скачиваний",
                callback_data="admin_limit",
                icon_custom_emoji_id="5775896410780079073"
            ),
            InlineKeyboardButton(
                text="Промокоды",
                callback_data="admin_promo",
                icon_custom_emoji_id="5886285355279193209"
            ),
        ],
        [InlineKeyboardButton(
            text=f"Водяной знак: {'Вкл' if wm else 'Выкл'}",
            callback_data="admin_toggle_watermark",
            icon_custom_emoji_id="6037496202990194718" if wm else "6037249452824072506"
        )],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_main",
            icon_custom_emoji_id="5893057118545646106"
        )],
    ])


def get_periods_keyboard(discount: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for days, label in PERIOD_LABELS.items():
        rub  = int(PRICES_RUB[days]   * (1 - discount / 100))
        star = int(PRICES_STARS[days] * (1 - discount / 100))
        suffix = f" (-{discount}%)" if discount else ""
        rows.append([InlineKeyboardButton(
            text=f"{label} — {rub}₽ / {star}⭐{suffix}",
            callback_data=f"period_{days}",
            icon_custom_emoji_id="5890937706803894250"
        )])
    rows.append([InlineKeyboardButton(
        text="Назад",
        callback_data="back_to_main",
        icon_custom_emoji_id="5893057118545646106"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_payment_methods_keyboard(days: int, discount: int = 0) -> InlineKeyboardMarkup:
    rub  = int(PRICES_RUB[days]   * (1 - discount / 100))
    star = int(PRICES_STARS[days] * (1 - discount / 100))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Оплатить Stars ({star} звёзд)",
            callback_data=f"pay_stars_{days}",
            icon_custom_emoji_id="5904462880941545555"
        )],
        [InlineKeyboardButton(
            text=f"Перевод на карту ({rub} ₽)",
            callback_data=f"pay_manual_{days}",
            icon_custom_emoji_id="5769126056262898415"
        )],
        [InlineKeyboardButton(
            text="Ввести промокод",
            callback_data="enter_promo",
            icon_custom_emoji_id="5886285355279193209"
        )],
        [InlineKeyboardButton(
            text="Назад",
            callback_data="buy_premium",
            icon_custom_emoji_id="5893057118545646106"
        )],
    ])


# ================= YT-DLP НАСТРОЙКИ =================
COMMON_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

def _base_ydl_opts(url: str = "") -> dict:
    opts: dict = {
        'quiet':            True,
        'no_warnings':      True,
        'logger':           SilentLogger(),
        'ignoreerrors':     False,
        'socket_timeout':   20,
        'retries':          3,
        'fragment_retries': 3,
        'http_headers':     {'User-Agent': COMMON_UA},
        'geo_bypass':       True,
        'ffmpeg_location':  FFMPEG_PATH,   # ← наш ffmpeg из imageio_ffmpeg
    }
    ul = url.lower()

    if 'youtube.com' in ul or 'youtu.be' in ul:
        opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}

    elif 'instagram.com' in ul or 'instagr.am' in ul:
        opts['http_headers'].update({
            'Referer':    'https://www.instagram.com/',
            'X-IG-App-ID': '936619743392459',
        })
        opts['extractor_args'] = {'instagram': {'include_dash_manifest': ['0']}}

    elif 'tiktok.com' in ul:
        opts['http_headers'] = {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) '
                'AppleWebKit/605.1.15 Version/17.2 Mobile/15E148 Safari/604.1'
            ),
            'Referer': 'https://www.tiktok.com/',
        }
        opts['extractor_args'] = {
            'tiktok': {'api_hostname': 'api16-normal-c-useast1a.tiktokv.com', 'app_name': 'trill'}
        }

    elif 'pinterest.' in ul or 'pin.it' in ul:
        opts['http_headers'].update({'Referer': 'https://www.pinterest.com/'})

    elif 'twitter.com' in ul or 'x.com' in ul:
        opts['http_headers'].update({'Referer': 'https://x.com/'})

    elif 'vk.com' in ul or 'vkvideo.ru' in ul:
        opts['http_headers'].update({'Referer': 'https://vk.com/'})

    elif 'facebook.com' in ul or 'fb.watch' in ul:
        opts['http_headers'].update({'Referer': 'https://www.facebook.com/'})

    elif 'reddit.com' in ul or 'redd.it' in ul:
        opts['http_headers'].update({'Referer': 'https://www.reddit.com/'})

    elif 'twitch.tv' in ul:
        opts['http_headers'].update({'Referer': 'https://www.twitch.tv/'})

    return opts


# ================= PINTEREST SCRAPER =================
async def pinterest_get_image(url: str) -> str | None:
    """
    Вытаскивает прямую ссылку на оригинальное изображение с Pinterest.
    Сначала пробует API, затем парсинг HTML.
    """
    try:
        # Метод 1 — Pinterest oEmbed
        api = f"https://www.pinterest.com/oembed/?url={url}"
        headers = {'User-Agent': COMMON_UA}
        async with aiohttp.ClientSession() as s:
            async with s.get(api, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    thumb = data.get('thumbnail_url', '')
                    if thumb:
                        # Заменяем 236x на originals для максимального разрешения
                        thumb = re.sub(r'/\d+x/', '/originals/', thumb)
                        return thumb
    except Exception as e:
        print(f"[Pinterest oEmbed] {e}")

    try:
        # Метод 2 — парсинг HTML страницы
        headers = {
            'User-Agent': COMMON_UA,
            'Accept-Language': 'en-US,en;q=0.9',
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                html = await r.text(errors='ignore')

        # Ищем оригинальный URL изображения в мета-тегах
        patterns = [
            r'"url"\s*:\s*"(https://i\.pinimg\.com/originals/[^"]+)"',
            r'"url"\s*:\s*"(https://i\.pinimg\.com/\d+x/[^"]+)"',
            r'property="og:image"\s+content="([^"]+)"',
            r'content="([^"]+)"\s+property="og:image"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                img_url = match.group(1)
                # Пытаемся получить оригинал вместо превью
                img_url = re.sub(r'/\d+x/', '/originals/', img_url)
                return img_url
    except Exception as e:
        print(f"[Pinterest HTML] {e}")

    return None


# ================= SPOTIFY / ЯНДЕКС МУЗЫКА =================
async def get_music_info(url: str) -> dict | None:
    """
    Получает информацию о треке/плейлисте из Spotify или Яндекс Музыки
    через yt-dlp (поддерживает Spotify через spotdl-обёртку и YM через yt-dlp).
    """
    opts = _base_ydl_opts(url)
    opts['extract_flat'] = True   # быстрый анализ без скачивания

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None

        if info.get('_type') == 'playlist':
            entries = [e for e in (info.get('entries') or []) if e]
            return {
                'type':    'playlist',
                'title':   info.get('title', 'Плейлист'),
                'count':   len(entries),
                'entries': entries,
            }
        else:
            return {
                'type':     'track',
                'title':    info.get('title', 'Трек'),
                'artist':   info.get('artist') or info.get('uploader', ''),
                'duration': info.get('duration'),
                'thumb':    info.get('thumbnail'),
            }
    except Exception as e:
        print(f"[get_music_info] {e}")
        return None


def download_music_track_sync(url: str, out_dir: str, task_id: int) -> list[str]:
    """
    Скачивает один трек или весь плейлист как MP3 в папку out_dir.
    Возвращает список скачанных файлов.
    """
    opts = _base_ydl_opts(url)
    opts.update({
        'outtmpl':    os.path.join(out_dir, '%(playlist_index)s_%(title)s.%(ext)s'),
        'format':     'bestaudio/best',
        'ffmpeg_location': FFMPEG_PATH,
        'postprocessors': [{
            'key':              'FFmpegExtractAudio',
            'preferredcodec':   'mp3',
            'preferredquality': '320',
        }, {
            'key':              'FFmpegMetadata',   # теги ID3
            'add_metadata':     True,
        }],
        'writethumbnail':  True,    # обложка альбома
        'embedthumbnail':  True,    # встраиваем обложку в mp3
        'progress_hooks':  [get_progress_hook(task_id)],
        'noplaylist':      False,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    # Собираем скачанные mp3
    files = []
    for f in os.listdir(out_dir):
        if f.endswith('.mp3'):
            files.append(os.path.join(out_dir, f))
    return sorted(files)


async def send_music(
    url: str, user_id: int, status_msg: Message,
    platform: str, is_playlist: bool = False, playlist_title: str = ""
):
    """Скачивает и отправляет музыку (трек или плейлист ZIP)."""
    task_id   = status_msg.message_id
    out_dir   = os.path.join(DOWNLOADS_DIR, f"music_{task_id}")
    os.makedirs(out_dir, exist_ok=True)
    progress_data[task_id] = {'percent': '0%', 'speed': '—', 'eta': '...', 'phase': 'download'}
    updater   = asyncio.create_task(update_progress_message(status_msg, task_id))

    try:
        files = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                _thread_pool,
                download_music_track_sync, url, out_dir, task_id
            ),
            timeout=DOWNLOAD_TIMEOUT
        )

        if not files:
            raise RuntimeError("Треки не скачались")

        progress_data.pop(task_id, None)
        updater.cancel()

        if is_playlist and len(files) > 1:
            # Упаковываем в ZIP
            await safe_edit(
                status_msg,
                f'{E_BOX} <b>Упаковываю {len(files)} треков в ZIP...</b>',
                ParseMode.HTML
            )
            zip_name = re.sub(r'[^\w\s-]', '', playlist_title)[:50] or "playlist"
            zip_path = os.path.join(DOWNLOADS_DIR, f"{zip_name}_{task_id}.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, os.path.basename(f))

            await safe_edit(status_msg, f'{E_UPLOAD} <b>Отправляю ZIP-архив...</b>', ParseMode.HTML)
            size_str = fmt_size(os.path.getsize(zip_path))
            caption  = (
                f'{E_MEDIA} <b>{playlist_title}</b>\n'
                f'{E_BOX} {len(files)} треков · {size_str}\n'
                f'<i>Скачано через {BOT_USERNAME}</i>'
            )

            file_size_mb = os.path.getsize(zip_path) / 1024 / 1024
            if file_size_mb < 50:
                await status_msg.answer_document(
                    FSInputFile(zip_path, filename=f"{zip_name}.zip"),
                    caption=caption, parse_mode=ParseMode.HTML
                )
            else:
                async with pyro_upload_semaphore:
                    await asyncio.wait_for(
                        pyro_app.send_document(
                            chat_id=user_id,
                            document=zip_path,
                            caption=caption,
                            file_name=f"{zip_name}.zip"
                        ),
                        timeout=UPLOAD_TIMEOUT
                    )
            os.remove(zip_path)

        else:
            # Один трек — отправляем напрямую как аудио
            await safe_edit(status_msg, f'{E_UPLOAD} <b>Отправляю трек...</b>', ParseMode.HTML)
            for f in files:
                name = os.path.splitext(os.path.basename(f))[0]
                # Убираем порядковый номер из имени
                name = re.sub(r'^\d+_', '', name)
                size_str = fmt_size(os.path.getsize(f))
                caption  = (
                    f'{E_MEDIA} <b>{name}</b>\n'
                    f'{platform} · {size_str}\n'
                    f'<i>Скачано через {BOT_USERNAME}</i>'
                )
                await status_msg.answer_audio(
                    FSInputFile(f, filename=os.path.basename(f)),
                    caption=caption, parse_mode=ParseMode.HTML
                )

        await status_msg.delete()
        if not await is_premium(user_id):
            await increment_download(user_id)

    except asyncio.TimeoutError:
        progress_data.pop(task_id, None)
        updater.cancel()
        await safe_edit(
            status_msg,
            f'{E_CLOCK} <b>Время ожидания истекло</b>\n'
            f'<i>Плейлист слишком большой. Попробуйте отдельный трек.</i>',
            ParseMode.HTML
        )
    except Exception as e:
        progress_data.pop(task_id, None)
        updater.cancel()
        print(f"[send_music] {e}")
        await safe_edit(
            status_msg,
            f'{E_CROSS} <b>Не удалось скачать музыку</b>\n'
            f'<i>Убедитесь, что ссылка публична и попробуйте снова.</i>',
            ParseMode.HTML
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ================= СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ =================
async def download_photo_direct(photo_url: str) -> str | None:
    """Скачивает изображение напрямую по URL."""
    try:
        headers = {'User-Agent': COMMON_UA, 'Referer': photo_url}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                photo_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60), allow_redirects=True
            ) as r:
                if r.status != 200:
                    return None
                ct  = r.headers.get('Content-Type', '')
                ext = '.jpg'
                if 'png' in ct:  ext = '.png'
                elif 'gif' in ct: ext = '.gif'
                elif 'webp' in ct: ext = '.webp'
                tmp = f"{DOWNLOADS_DIR}/photo_{int(time.time())}{ext}"
                with open(tmp, 'wb') as f:
                    f.write(await r.read())
                # Проверяем, что файл не пустой
                if os.path.getsize(tmp) < 100:
                    os.remove(tmp)
                    return None
                return tmp
    except Exception as e:
        print(f"[download_photo_direct] {e}")
    return None


async def send_photo_smart(file_path: str, msg: Message, caption: str = ''):
    try:
        await msg.answer_photo(FSInputFile(file_path), caption=caption or None,
                               parse_mode=ParseMode.HTML)
    except Exception:
        await msg.answer_document(FSInputFile(file_path), caption=caption or None,
                                  parse_mode=ParseMode.HTML)


# ================= TIKTOK API =================
async def tiktok_get_info(url: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                'https://www.tikwm.com/api/',
                data={'url': url, 'hd': '1'},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                data = await r.json(content_type=None)
        if data.get('code') == 0:
            d = data['data']
            return {
                'title': d.get('title', 'TikTok видео'),
                'thumb': d.get('cover') or d.get('origin_cover'),
                'play':  d.get('hdplay') or d.get('play'),
            }
    except Exception as e:
        print(f"[TikWM] {e}")
    return None


async def tiktok_download_and_send(url: str, user_id: int, status_msg: Message) -> bool:
    info = await tiktok_get_info(url)
    if not info or not info.get('play'):
        return False

    file_path = f"{DOWNLOADS_DIR}/tiktok_{int(time.time())}.mp4"
    try:
        await safe_edit(status_msg, f'{E_DOWNLOAD} <b>Скачиваю TikTok...</b>', ParseMode.HTML)
        async with aiohttp.ClientSession() as s:
            async with s.get(
                info['play'], headers={'User-Agent': COMMON_UA},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as r:
                if r.status != 200:
                    raise Exception(f"HTTP {r.status}")
                with open(file_path, 'wb') as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk)

        if await is_watermark_enabled():
            await safe_edit(status_msg, f'{E_PEN} <b>Добавляю водяной знак...</b>', ParseMode.HTML)
            file_path = await asyncio.to_thread(add_watermark_sync, file_path)

        caption = (
            f'{E_MEDIA} <b>{info["title"][:100]}</b>\n'
            f'TikTok · {fmt_size(os.path.getsize(file_path))}\n'
            f'<i>Скачано через {BOT_USERNAME}</i>'
        )
        await safe_edit(status_msg, f'{E_UPLOAD} <b>Отправляю...</b>', ParseMode.HTML)
        await _send_video_smart(file_path, user_id, status_msg, caption)
        await status_msg.delete()
        return True
    except Exception as e:
        print(f"[TikTok] {e}")
        return False
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ================= ИЗВЛЕЧЕНИЕ МЕДИА-ИНФОРМАЦИИ =================
def extract_media_info_sync(url: str) -> dict | None:
    opts = _base_ydl_opts(url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"[yt-dlp info] {e}")
        return None

    if not info:
        return None

    if info.get('_type') == 'playlist':
        entries = [e for e in (info.get('entries') or []) if e]
        if not entries:
            return None
        info = entries[0]

    vcodec  = info.get('vcodec', '')
    formats = info.get('formats') or []

    # Нет видео и форматов — фото или только аудио
    if (vcodec == 'none' or not vcodec) and not formats:
        thumb = info.get('thumbnail') or info.get('url')
        return {"type": "photo", "url": url, "thumb": thumb, "title": info.get('title', '')}

    # Собираем разрешения
    unique_res: dict[int, dict] = {}
    for f in formats:
        fvc    = f.get('vcodec') or 'none'
        height = f.get('height') or 0
        if fvc == 'none' or height <= 0:
            continue
        tbr = f.get('tbr') or f.get('vbr') or 0
        ex  = unique_res.get(height)
        if not ex or tbr > (ex.get('tbr') or 0):
            unique_res[height] = {'format_id': f['format_id'], 'tbr': tbr}

    if not unique_res:
        # Нет видео форматов — возможно, это изображение
        thumb = info.get('thumbnail') or info.get('url')
        return {"type": "photo", "url": url, "thumb": thumb, "title": info.get('title', '')}

    # Есть ли аудио-форматы
    has_audio = any(
        f.get('acodec') not in (None, 'none') and (f.get('vcodec') in (None, 'none'))
        for f in formats
    )

    return {
        "type":      "video",
        "thumb":     info.get('thumbnail'),
        "title":     info.get('title', 'Видео'),
        "duration":  info.get('duration'),
        "has_audio": has_audio or True,  # всегда показываем аудио-кнопку
        "formats":   {str(h): d['format_id']
                      for h, d in sorted(unique_res.items(), reverse=True)},
    }


# ================= УМНАЯ ОТПРАВКА ВИДЕО =================
async def _send_video_smart(file_path: str, user_id: int, status_msg: Message, caption: str = ''):
    task_id     = status_msg.message_id
    size_mb     = os.path.getsize(file_path) / 1024 / 1024

    if size_mb < 50:
        await status_msg.answer_video(
            FSInputFile(file_path), caption=caption or None, parse_mode=ParseMode.HTML
        )
    else:
        start_time = time.time()
        last_upd   = {'t': 0}

        async def upload_cb(current, total):
            now = time.time()
            if now - last_upd['t'] < 2:
                return
            last_upd['t'] = now
            elapsed = max(now - start_time, 0.01)
            pct     = current * 100 / total
            spd     = (current / elapsed) / 1024 / 1024
            eta     = int((total - current) / max(current / elapsed, 1))
            progress_data[task_id] = {
                'percent': f'{pct:.1f}%',
                'speed':   f'{spd:.1f} МБ/с',
                'eta':     f'{eta}с',
                'phase':   'upload',
                'size_mb': round(total / 1024 / 1024, 1),
            }

        if pyro_upload_semaphore.locked():
            await safe_edit(
                status_msg,
                f'{E_CLOCK} <b>Файл в очереди...</b>\n'
                f'<i>Сервер занят другими загрузками. Файл придёт автоматически.</i>',
                ParseMode.HTML
            )

        async with pyro_upload_semaphore:
            await asyncio.wait_for(
                pyro_app.send_video(
                    chat_id=user_id, video=file_path,
                    caption=caption or None, progress=upload_cb
                ),
                timeout=UPLOAD_TIMEOUT
            )


# ================= ГЛАВНАЯ ФУНКЦИЯ СКАЧИВАНИЯ =================
async def download_and_send_media(
    url: str, user_id: int, status_msg: Message,
    format_id: str = "best", audio_only: bool = False
):
    """
    Скачивает медиа с одной попыткой.
    Зависание исключено: все I/O операции ограничены таймаутами.
    Повторные вызовы делает сам handler при необходимости.
    """
    task_id   = status_msg.message_id
    file_path = None
    platform  = detect_platform(url)

    progress_data[task_id] = {'percent': '0%', 'speed': '—', 'eta': '...', 'phase': 'download'}
    updater = asyncio.create_task(update_progress_message(status_msg, task_id))

    def dl_sync():
        ts   = int(time.time())
        ext  = 'mp3' if audio_only else 'mp4'
        base = f"{DOWNLOADS_DIR}/dl_{ts}"
        opts = _base_ydl_opts(url)
        opts.update({
            'outtmpl':             f"{base}.%(ext)s",
            'merge_output_format': 'mp4',
            'progress_hooks':      [get_progress_hook(task_id)],
        })

        if audio_only:
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key':              'FFmpegExtractAudio',
                'preferredcodec':   'mp3',
                'preferredquality': '320',
            }]
        elif format_id == "best":
            opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        else:
            opts['format'] = f"{format_id}+bestaudio/bestvideo+bestaudio/best"

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Ищем скачанный файл
        for candidate_ext in ('.mp3', '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4a', '.ogg'):
            p = base + candidate_ext
            if os.path.exists(p):
                return p
        # Fallback — поиск по timestamp
        for f in sorted(os.listdir(DOWNLOADS_DIR)):
            if str(ts) in f:
                return os.path.join(DOWNLOADS_DIR, f)
        return base + f'.{ext}'

    try:
        # Запускаем скачивание в thread pool с жёстким таймаутом
        future = _thread_pool.submit(dl_sync)
        try:
            file_path = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, future.result),
                timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            future.cancel()
            raise

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("Файл не найден после скачивания")

        size_str = fmt_size(os.path.getsize(file_path))

        # Водяной знак (только для видео)
        if not audio_only and await is_watermark_enabled():
            await safe_edit(status_msg, f'{E_PEN} <b>Добавляю водяной знак...</b>', ParseMode.HTML)
            progress_data[task_id] = {'percent': '50%', 'speed': '—', 'eta': '...', 'phase': 'convert'}
            file_path = await asyncio.to_thread(add_watermark_sync, file_path)

        # Переходим к отправке
        progress_data[task_id] = {'percent': '0%', 'speed': '—', 'eta': '...', 'phase': 'upload'}
        await safe_edit(
            status_msg,
            f'{E_UPLOAD} <b>Отправляю файл...</b>\n<i>{size_str}</i>',
            ParseMode.HTML
        )

        caption = (
            f'{E_DOWNLOAD} <b>{platform}</b> · {size_str}\n'
            f'<i>Скачано через {BOT_USERNAME}</i>'
        )

        if audio_only:
            await status_msg.answer_audio(
                FSInputFile(file_path), caption=caption, parse_mode=ParseMode.HTML
            )
        else:
            await _send_video_smart(file_path, user_id, status_msg, caption)

        progress_data.pop(task_id, None)
        updater.cancel()
        await status_msg.delete()

        if not await is_premium(user_id):
            await increment_download(user_id)
        remaining = await get_remaining(user_id)
        if not await is_premium(user_id):
            limit = await get_daily_limit()
            try:
                await bot.send_message(
                    user_id,
                    f'{E_CHECK} Готово! Осталось скачиваний сегодня: <b>{remaining}/{limit}</b>',
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    except asyncio.TimeoutError:
        progress_data.pop(task_id, None)
        updater.cancel()
        await safe_edit(
            status_msg,
            f'{E_CLOCK} <b>Время ожидания истекло</b>\n\n'
            f'Файл слишком большой или сервер медленно отвечает.\n'
            f'<i>Попробуйте качество пониже.</i>',
            ParseMode.HTML
        )

    except Exception as e:
        progress_data.pop(task_id, None)
        updater.cancel()
        err = str(e).lower()
        print(f"[download] {e}")

        if 'private' in err:
            msg_text = f'{E_LOCK} <b>Видео приватное</b>\n<i>Доступ закрыт.</i>'
        elif 'not available' in err or 'unavailable' in err:
            msg_text = f'{E_CROSS} <b>Видео недоступно</b>\n<i>Возможно, гео-блокировка или удалено.</i>'
        elif '403' in err:
            msg_text = f'{E_LOCK} <b>Доступ запрещён (403)</b>\n<i>Сайт требует авторизацию.</i>'
        elif 'unsupported url' in err:
            msg_text = f'{E_CROSS} <b>Этот сайт не поддерживается</b>'
        else:
            msg_text = (
                f'{E_CROSS} <b>Ошибка при скачивании</b>\n\n'
                f'<i>Попробуйте другое качество или повторите позже.</i>'
            )

        await safe_edit(
            status_msg,
            msg_text,
            ParseMode.HTML
        )

    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


# ================= БАЗОВЫЕ ХЭНДЛЕРЫ =================
@router.message(F.text == "/start")
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.react([ReactionTypeEmoji(emoji="❤‍🔥")])
    uid   = message.from_user.id
    await ensure_user(uid, message.from_user.username or '', message.from_user.first_name or '')
    admin = (uid == MAIN_ADMIN_ID)
    limit = await get_daily_limit()
    prem  = await is_premium(uid)

    status_line = (
        f'{E_GIFT} У вас активирован <b>Premium!</b>'
        if prem else
        f'{E_DOWNLOAD} Бесплатно: <b>{limit}</b> скачивания в сутки'
    )

    text = (
        f'{E_BOT} Привет, <b>{message.from_user.first_name}</b>!\n\n'
        f'Я — <b>VidLoads Bot</b> — скачиваю видео, аудио и фото\n'
        f'с популярных платформ прямо в Telegram.\n\n'
        f'<b>{E_INFO} Как использовать:</b>\n'
        f'Просто отправь мне ссылку — и я всё сделаю!\n\n'
        f'<b>{E_LINK} Платформы:</b>\n'
        f'YouTube · TikTok · Instagram · Pinterest\n'
        f'Twitter/X · VK · Reddit · Spotify · Яндекс Музыка и другие\n\n'
        f'{status_line}\n'
        f'{E_GIFT} Premium: безлимит + максимальное качество\n\n'
        f'<i>Просто пришли ссылку — и начнём!</i>'
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(admin))


@router.message(F.text == "/help")
@router.callback_query(F.data == "help_menu")
async def help_handler(event):
    msg = event.message if isinstance(event, CallbackQuery) else event
    text = (
        f'{E_INFO} <b>Справка по боту</b>\n\n'
        f'<b>{E_DOWNLOAD} Как скачать видео:</b>\n'
        f'1. Скопируй ссылку\n'
        f'2. Отправь её в чат\n'
        f'3. Выбери качество или формат\n'
        f'4. Жди — файл придёт автоматически!\n\n'
        f'<b>🎵 Аудио MP3:</b>\n'
        f'Кнопка <i>"Аудио MP3"</i> в меню качества\n\n'
        f'<b>🎶 Spotify / Яндекс Музыка:</b>\n'
        f'Поддерживаются треки и плейлисты.\n'
        f'Плейлист придёт как ZIP-архив с MP3.\n\n'
        f'<b>{E_LINK} Сайты:</b>\n'
        f'▪ YouTube, Shorts\n'
        f'▪ TikTok (HD без вотермарки)\n'
        f'▪ Instagram, Reels\n'
        f'▪ Pinterest (видео и фото)\n'
        f'▪ Twitter/X · VK · Reddit · Facebook\n'
        f'▪ Spotify · Яндекс Музыка\n'
        f'▪ Twitch · Rumble · Vimeo и др.\n\n'
        f'<b>{E_GIFT} Premium даёт:</b>\n'
        f'▪ Безлимитные скачивания\n'
        f'▪ Максимальное качество (4K)\n\n'
        f'<b>Команды:</b>\n'
        f'/start — главное меню\n'
        f'/help — эта справка\n'
        f'/mystats — статистика'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Купить Premium",
            callback_data="buy_premium",
            icon_custom_emoji_id="6032644646587338669"
        )],
        [InlineKeyboardButton(
            text="Главное меню",
            callback_data="back_to_main",
            icon_custom_emoji_id="5893057118545646106"
        )],
    ])
    if isinstance(event, CallbackQuery):
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await event.react([ReactionTypeEmoji(emoji="👌")])
        await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@router.message(F.text == "/mystats")
async def mystats_command(message: Message):
    await show_my_stats(message.from_user.id, message, edit=False)

@router.callback_query(F.data == "my_stats")
async def my_stats_callback(callback: CallbackQuery):
    await show_my_stats(callback.from_user.id, callback.message, edit=True)


async def show_my_stats(user_id: int, msg, edit: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT premium_until,downloads_count,total_downloads,joined_at "
            "FROM users WHERE user_id=?", (user_id,)
        ) as c:
            user = await c.fetchone()

    if not user:
        text = f'{E_STATS} <b>Статистика</b>\n\nДанных пока нет. Начни скачивать!'
    else:
        pu, today, total, joined = user
        is_prem = pu > int(time.time())
        limit   = await get_daily_limit()
        status  = (f'{E_CHECK} до {datetime.fromtimestamp(pu).strftime("%d.%m.%Y")}') \
                  if is_prem else f'{E_CROSS} не активирован'
        today_s = "∞ безлимит" if is_prem else f"{today} из {limit}"
        joined_s = datetime.fromtimestamp(joined).strftime("%d.%m.%Y") if joined else "—"
        text = (
            f'{E_STATS} <b>Твоя статистика</b>\n\n'
            f'{E_PROFILE} ID: <code>{user_id}</code>\n'
            f'{E_CALENDAR} В боте с: {joined_s}\n\n'
            f'{E_GIFT} Premium: {status}\n\n'
            f'<b>{E_DOWNLOAD} Скачивания:</b>\n'
            f'▪ Сегодня: {today_s}\n'
            f'▪ Всего: <b>{total or 0}</b>'
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Назад", callback_data="back_to_main",
                             icon_custom_emoji_id="5893057118545646106")
    ]])
    if edit:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@router.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT premium_until,downloads_count,total_downloads FROM users WHERE user_id=?",
            (uid,)
        ) as c:
            user = await c.fetchone()
    if not user:
        return await callback.answer("Профиль не найден", show_alert=True)

    pu, today, total = user
    is_prem  = pu > int(time.time())
    prem_str = (f'{E_CHECK} до {datetime.fromtimestamp(pu).strftime("%d.%m.%Y")}') \
               if is_prem else f'{E_CROSS} не активирован'
    limit    = await get_daily_limit()
    dl_str   = "∞ безлимит" if is_prem else f"{today}/{limit}"
    username = f"@{callback.from_user.username}" if callback.from_user.username else "—"

    text = (
        f'{E_PROFILE} <b>Ваш профиль</b>\n\n'
        f'Имя: <b>{callback.from_user.first_name}</b>\n'
        f'Username: {username}\n'
        f'ID: <code>{uid}</code>\n\n'
        f'{E_GIFT} Premium: {prem_str}\n'
        f'{E_DOWNLOAD} Сегодня: {dl_str}\n'
        f'{E_BOX} Всего скачиваний: {total or 0}'
    )
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Купить Premium", callback_data="buy_premium",
                                  icon_custom_emoji_id="6032644646587338669")],
            [InlineKeyboardButton(text="Назад", callback_data="back_to_main",
                                  icon_custom_emoji_id="5893057118545646106")],
        ]
    ))


@router.callback_query(F.data == "share_bot")
async def share_bot_handler(callback: CallbackQuery):
    text_share = f"Качаю видео и музыку с YouTube, TikTok, Spotify прямо в Telegram — {BOT_USERNAME}"
    share_url  = f"https://t.me/share/url?url=https://t.me/VidLoads_Bot&text={text_share}"
    await callback.message.edit_text(
        f'{E_LINK} <b>Поделись ботом с друзьями!</b>\n\n'
        f'Нажми кнопку ниже — и они тоже смогут скачивать видео и музыку!',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Поделиться", url=share_url,
                                  icon_custom_emoji_id="5769289093221454192")],
            [InlineKeyboardButton(text="Назад", callback_data="back_to_main",
                                  icon_custom_emoji_id="5893057118545646106")],
        ])
    )


@router.callback_query(F.data == "back_to_main")
async def back_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    admin = (callback.from_user.id == MAIN_ADMIN_ID)
    await callback.message.edit_text(
        f'{E_BOT} <b>Главное меню</b>\n\n'
        f'Отправь ссылку на видео, музыку или изображение — скачаю для тебя!\n\n'
        f'<i>YouTube · TikTok · Spotify · Pinterest и 1000+ других сайтов</i>',
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(admin)
    )


# ================= ОПЛАТА =================
@router.callback_query(F.data == "buy_premium")
async def buy_premium_handler(callback: CallbackQuery, state: FSMContext):
    data     = await state.get_data()
    discount = data.get('discount', 0)
    text = (
        f'{E_GIFT} <b>Premium подписка</b>\n\n'
        f'Безлимитные скачивания и максимальное качество (4K).\n\n'
        f'<b>{E_CALENDAR} Выберите период:</b>'
    )
    if discount:
        text += f'\n\n{E_CHECK} Применена скидка: <b>{discount}%</b>'
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=get_periods_keyboard(discount))


@router.callback_query(F.data.startswith("period_"))
async def period_selected(callback: CallbackQuery, state: FSMContext):
    days     = int(callback.data.split("_")[1])
    await state.update_data(period=days)
    data     = await state.get_data()
    discount = data.get('discount', 0)
    label    = PERIOD_LABELS[days]
    rub      = int(PRICES_RUB[days]   * (1 - discount / 100))
    star     = int(PRICES_STARS[days] * (1 - discount / 100))
    text = (
        f'{E_WALLET} <b>Оплата — {label}</b>\n\n'
        f'Стоимость: <b>{rub} ₽</b>  или  <b>{star} ⭐</b>\n\n'
        f'Выберите способ оплаты:'
    )
    if discount:
        text += f'\n{E_CHECK} Скидка: <b>{discount}%</b>'
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=get_payment_methods_keyboard(days, discount))


@router.callback_query(F.data == "enter_promo")
async def enter_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f'{E_TAG} <b>Введите промокод</b>\n\nОтправьте промокод следующим сообщением:',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Назад", callback_data="buy_premium",
                                 icon_custom_emoji_id="5893057118545646106")
        ]])
    )
    await state.set_state(PaymentState.waiting_for_promo)


@router.message(PaymentState.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    code     = message.text.strip().upper()
    discount = await get_promo_discount(code)
    if discount is not None:
        await state.update_data(discount=discount)
        data  = await state.get_data()
        days  = data.get('period', 30)
        label = PERIOD_LABELS.get(days, f"{days} дней")
        rub   = int(PRICES_RUB[days]   * (1 - discount / 100))
        star  = int(PRICES_STARS[days] * (1 - discount / 100))
        await message.answer(
            f'{E_CHECK} <b>Промокод активирован!</b>\n\n'
            f'Скидка: <b>{discount}%</b>\n'
            f'Цена за {label}: <b>{rub} ₽</b> / <b>{star} ⭐</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=get_payment_methods_keyboard(days, discount)
        )
    else:
        reason = await get_promo_error(code)
        await message.answer(
            f'{E_CROSS} <b>Промокод недействителен</b>\n\n<i>{reason}</i>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Другой промокод", callback_data="enter_promo",
                                      icon_custom_emoji_id="5886285355279193209")],
                [InlineKeyboardButton(text="К подписке", callback_data="buy_premium",
                                      icon_custom_emoji_id="5893057118545646106")],
            ])
        )
    await state.set_state(None)


@router.callback_query(F.data.startswith("pay_manual_"))
async def pay_manual_handler(callback: CallbackQuery, state: FSMContext):
    days     = int(callback.data.split("_")[2])
    data     = await state.get_data()
    discount = data.get('discount', 0)
    price    = int(PRICES_RUB[days] * (1 - discount / 100))
    label    = PERIOD_LABELS[days]
    reqs     = await get_setting('requisites')
    await state.update_data(payment_days=days, payment_price=price)
    disc_line = f'\n{E_CHECK} Скидка {discount}% применена\n' if discount else '\n'
    await callback.message.edit_text(
        f'{E_WALLET} <b>Оплата переводом — {label}</b>\n\n'
        f'Сумма: <b>{price} ₽</b>{disc_line}\n'
        f'Реквизиты:\n<code>{reqs}</code>\n\n'
        f'После оплаты отправьте <b>скриншот чека</b> в этот чат.',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="buy_premium",
                                 icon_custom_emoji_id="5870657884844462243")
        ]])
    )
    await state.set_state(PaymentState.waiting_for_receipt)


@router.message(PaymentState.waiting_for_receipt, F.photo)
async def receipt_received(message: Message, state: FSMContext):
    uid   = message.from_user.id
    data  = await state.get_data()
    days  = data.get('payment_days', 30)
    price = data.get('payment_price', PRICES_RUB.get(days, 0))
    label = PERIOD_LABELS.get(days, f"{days} дней")
    kb    = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить", callback_data=f"approve_{uid}_{days}",
                             icon_custom_emoji_id="5870633910337015697"),
        InlineKeyboardButton(text="Отклонить",   callback_data=f"reject_{uid}",
                             icon_custom_emoji_id="5870657884844462243"),
    ]])
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    await bot.send_photo(
        chat_id=ADMIN_GROUP_ID, photo=message.photo[-1].file_id,
        caption=(
            f'{E_SENDMONEY} <b>Новая заявка на оплату</b>\n\n'
            f'{E_PROFILE} {message.from_user.first_name} ({username})\n'
            f'ID: <code>{uid}</code>\n'
            f'{E_CALENDAR} Период: <b>{label}</b>\n'
            f'{E_COIN} Сумма: <b>{price} ₽</b>'
        ),
        parse_mode=ParseMode.HTML, reply_markup=kb
    )
    await message.answer(
        f'{E_CLOCK} <b>Чек отправлен на проверку!</b>\n\n'
        f'Администратор проверит в ближайшее время.\n'
        f'Вам придёт уведомление.',
        parse_mode=ParseMode.HTML
    )
    await state.clear()


@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def process_payment(callback: CallbackQuery):
    parts  = callback.data.split("_")
    action = parts[0]
    uid    = int(parts[1])
    if action == "approve":
        days  = int(parts[2])
        label = PERIOD_LABELS.get(days, f"{days} дней")
        await add_premium_days(uid, days)
        await bot.send_message(
            uid,
            f'{E_PARTY} <b>Premium активирован!</b>\n\n'
            f'Подписка: <b>{label}</b>\n'
            f'Безлимитные скачивания доступны прямо сейчас!',
            parse_mode=ParseMode.HTML
        )
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n{E_CHECK} <b>Одобрено</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await bot.send_message(
            uid,
            f'{E_CROSS} <b>Оплата отклонена</b>\n\nПроверьте реквизиты и попробуйте снова.',
            parse_mode=ParseMode.HTML
        )
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n{E_CROSS} <b>Отклонено</b>",
            parse_mode=ParseMode.HTML
        )


@router.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars_handler(callback: CallbackQuery, state: FSMContext):
    days  = int(callback.data.split("_")[2])
    data  = await state.get_data()
    price = int(PRICES_STARS[days] * (1 - data.get('discount', 0) / 100))
    label = PERIOD_LABELS[days]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Premium — {label}",
        description="Безлимитные скачивания + максимальное качество (4K)",
        payload=f"premium_{days}", currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=price)]
    )

@router.pre_checkout_query()
async def pre_checkout_handler(pq: PreCheckoutQuery):
    await pq.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    days  = int(message.successful_payment.invoice_payload.split("_")[1])
    label = PERIOD_LABELS.get(days, f"{days} дней")
    await add_premium_days(message.from_user.id, days)
    await message.answer(
        f'{E_PARTY} <b>Premium активирован!</b>\n\nПодписка: <b>{label}</b>\n'
        f'Безлимитные скачивания доступны прямо сейчас!',
        parse_mode=ParseMode.HTML
    )


# ================= АДМИНКА =================
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    await callback.message.edit_text(
        f'{E_SETTINGS} <b>Панель администратора</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=await get_admin_panel_keyboard()
    )


@router.callback_query(F.data == "admin_toggle_watermark")
async def admin_toggle_watermark(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    current = await is_watermark_enabled()
    await set_setting('watermark_enabled', '0' if current else '1')
    state_text = 'включён' if not current else 'выключен'
    await callback.answer(f"Водяной знак {state_text}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=await get_admin_panel_keyboard())


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_u = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE premium_until>?", (int(time.time()),)
        ) as c:
            prem_u = (await c.fetchone())[0]
        async with db.execute("SELECT SUM(total_downloads) FROM users") as c:
            total_dl = (await c.fetchone())[0] or 0
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE last_reset>?", (int(time.time()) - 86400,)
        ) as c:
            active = (await c.fetchone())[0]
    wm    = f'{E_CHECK} Включён' if await is_watermark_enabled() else f'{E_CROSS} Выключен'
    limit = await get_daily_limit()
    promos = await get_all_promos()
    await callback.message.edit_text(
        f'{E_STATS} <b>Статистика бота</b>\n\n'
        f'{E_PEOPLE} Всего пользователей: <b>{total_u}</b>\n'
        f'{E_CHECK} Активных сегодня: <b>{active}</b>\n'
        f'{E_GIFT} Premium: <b>{prem_u}</b>\n'
        f'{E_DOWNLOAD} Всего скачиваний: <b>{total_dl}</b>\n\n'
        f'{E_TIMELEFT} Лимит в сутки: <b>{limit}</b>\n'
        f'{E_PEN} Водяной знак: {wm}\n'
        f'{E_TAG} Промокодов: <b>{len(promos)}</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Назад", callback_data="admin_panel",
                                 icon_custom_emoji_id="5893057118545646106")
        ]])
    )


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    await callback.message.edit_text(
        f'{E_BROADCAST} <b>Рассылка</b>\n\n'
        f'Отправьте сообщение для рассылки всем пользователям.\n'
        f'<i>Поддерживаются текст, фото, видео, документы.</i>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="admin_panel",
                                 icon_custom_emoji_id="5870657884844462243")
        ]])
    )
    await state.set_state(AdminStates.waiting_for_broadcast)


@router.message(AdminStates.waiting_for_broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as c:
            users = await c.fetchall()
    count, failed = 0, 0
    for (uid,) in users:
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await message.answer(
        f'{E_CHECK} <b>Рассылка завершена!</b>\n\n'
        f'Доставлено: <b>{count}</b>\n'
        f'Не доставлено: <b>{failed}</b> (заблокировали бота)',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Назад", callback_data="admin_panel",
                                 icon_custom_emoji_id="5893057118545646106")
        ]])
    )
    await state.clear()


@router.callback_query(F.data == "admin_limit")
async def admin_limit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    current = await get_daily_limit()
    await callback.message.edit_text(
        f'{E_TIMELEFT} <b>Лимит скачиваний</b>\n\n'
        f'Текущий лимит: <b>{current}</b> в сутки\n\nОтправьте новое число:',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="admin_panel",
                                 icon_custom_emoji_id="5870657884844462243")
        ]])
    )
    await state.set_state(AdminStates.waiting_for_limit)


@router.message(AdminStates.waiting_for_limit)
async def set_limit(message: Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    if message.text and message.text.isdigit():
        await set_setting('daily_limit', message.text)
        await message.answer(
            f'{E_CHECK} <b>Лимит изменён: {message.text} в сутки</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Назад", callback_data="admin_panel",
                                     icon_custom_emoji_id="5893057118545646106")
            ]])
        )
    else:
        await message.answer(f'{E_CROSS} Введите целое число.')
    await state.clear()


# ── Промокоды (Админ) ─────────────────────────────────────────────────────────
async def build_promo_list_text() -> str:
    promos = await get_all_promos()
    now    = int(time.time())
    if not promos:
        return f'{E_TAG} <b>Промокоды</b>\n\nПромокодов пока нет.'
    lines = [f'{E_TAG} <b>Промокоды</b> ({len(promos)} шт.)\n']
    for p in promos:
        uses_str = f"{p['uses']}/{p['max_uses']}" if p['max_uses'] else f"{p['uses']}/∞"
        exp_str  = ""
        if p['expires_at']:
            exp = datetime.fromtimestamp(p['expires_at']).strftime('%d.%m.%Y')
            expired = now > p['expires_at']
            exp_str = f" · до {exp}" + (" ⚠ истёк" if expired else "")
        lines.append(f"<code>{p['code']}</code> — {p['discount']}% · {uses_str} активаций{exp_str}")
    return "\n".join(lines)


async def get_admin_promo_keyboard() -> InlineKeyboardMarkup:
    promos = await get_all_promos()
    rows   = []
    for p in promos:
        rows.append([
            InlineKeyboardButton(text=f"{p['code']} — {p['discount']}%",
                                 callback_data=f"promo_info_{p['code']}",
                                 icon_custom_emoji_id="5886285355279193209"),
            InlineKeyboardButton(text="Удалить", callback_data=f"promo_del_{p['code']}",
                                 icon_custom_emoji_id="5870875489362513438"),
        ])
    rows.append([InlineKeyboardButton(text="Создать промокод", callback_data="promo_create",
                                      icon_custom_emoji_id="5870633910337015697")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin_panel",
                                      icon_custom_emoji_id="5893057118545646106")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_promo")
async def admin_promo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    text = await build_promo_list_text()
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=await get_admin_promo_keyboard())


@router.callback_query(F.data.startswith("promo_del_"))
async def promo_delete(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    code = callback.data.removeprefix("promo_del_")
    await delete_promo(code)
    await callback.answer(f"Промокод {code} удалён")
    text = await build_promo_list_text()
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=await get_admin_promo_keyboard())


@router.callback_query(F.data == "promo_create")
async def promo_create(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID:
        return
    await callback.message.edit_text(
        f'{E_TAG} <b>Создание промокода</b>\n\n'
        f'Формат:\n<code>КОД СКИДКА [макс_активаций] [ДД.ММ.ГГГГ]</code>\n\n'
        f'Примеры:\n'
        f'<code>SUMMER 30</code> — 30%, без ограничений\n'
        f'<code>SALE 50 100</code> — 50%, макс. 100 активаций\n'
        f'<code>VIP 40 50 31.12.2025</code> — 40%, 50 активаций, до 31.12.2025\n'
        f'<code>NY 25 0 31.12.2025</code> — 25%, ∞ активаций, до 31.12.2025',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Отмена", callback_data="admin_promo",
                                 icon_custom_emoji_id="5870657884844462243")
        ]])
    )
    await state.set_state(AdminStates.waiting_for_promo_data)


@router.message(AdminStates.waiting_for_promo_data)
async def create_promo(message: Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID:
        return
    try:
        parts      = message.text.strip().split()
        code       = parts[0].upper()
        discount   = int(parts[1])
        if not (1 <= discount <= 100):
            raise ValueError("Скидка от 1 до 100")
        max_uses   = int(parts[2]) if len(parts) >= 3 else 0
        expires_at = 0
        if len(parts) >= 4:
            expires_at = int(datetime.strptime(parts[3], '%d.%m.%Y').timestamp())

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO promo_codes "
                "(code,discount_percent,max_uses,uses_count,expires_at,created_at) "
                "VALUES (?,?,?,0,?,?)",
                (code, discount, max_uses, expires_at, int(time.time()))
            )
            await db.commit()

        parts_info = [f'{E_CHECK} Промокод <code>{code}</code> создан!\n\nСкидка: <b>{discount}%</b>']
        parts_info.append(f'Лимит: <b>{"∞" if not max_uses else max_uses}</b> активаций')
        if expires_at:
            parts_info.append(f'До: <b>{datetime.fromtimestamp(expires_at).strftime("%d.%m.%Y")}</b>')
        else:
            parts_info.append('Срок: <b>∞ бессрочно</b>')

        lines = "\n".join(
            f"▪ {PERIOD_LABELS[d]}: {int(PRICES_RUB[d]*(1-discount/100))} ₽ / "
            f"{int(PRICES_STARS[d]*(1-discount/100))} ⭐"
            for d in PERIOD_LABELS
        )
        parts_info.append(f'\n<b>Цены со скидкой:</b>\n{lines}')
        await message.answer(
            "\n".join(parts_info),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="К промокодам", callback_data="admin_promo",
                                     icon_custom_emoji_id="5886285355279193209")
            ]])
        )
    except Exception as e:
        await message.answer(
            f'{E_CROSS} <b>Ошибка:</b> {e}\n\n'
            f'Формат: <code>КОД СКИДКА [макс_активаций] [ДД.ММ.ГГГГ]</code>',
            parse_mode=ParseMode.HTML
        )
    await state.clear()


# ================= ОБРАБОТКА ССЫЛОК =================
@router.message(F.text.regexp(r'https?://'))
async def process_link(message: Message, state: FSMContext):
    await message.react([ReactionTypeEmoji(emoji="👌")])
    uid = message.from_user.id
    await ensure_user(uid, message.from_user.username or '', message.from_user.first_name or '')

    if not await check_limits(uid):
        limit = await get_daily_limit()
        return await message.answer(
            f'{E_TIMELEFT} <b>Лимит скачиваний исчерпан!</b>\n\n'
            f'Сегодня скачано: <b>{limit}/{limit}</b>\n'
            f'Лимит обновится через несколько часов,\n'
            f'или оформите <b>Premium</b> для безлимитного доступа.',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Купить Premium", callback_data="buy_premium",
                                     icon_custom_emoji_id="6032644646587338669")
            ]])
        )

    url      = message.text.strip()
    platform = detect_platform(url)

    msg = await message.answer(
        f'{E_EYE} <b>Анализирую ссылку...</b>\n<i>{platform}</i>',
        parse_mode=ParseMode.HTML
    )

    # ── Музыкальные сервисы (Spotify / Яндекс Музыка) ──────────────────────
    if is_music_service(url):
        await safe_edit(msg, f'{E_EYE} <b>Получаю информацию...</b>', ParseMode.HTML)
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(lambda: _get_music_info_sync(url)),
                timeout=30
            )
        except Exception:
            info = None

        if not info:
            await safe_edit(
                msg,
                f'{E_CROSS} <b>Не удалось получить информацию</b>\n\n'
                f'<i>Убедитесь, что ссылка публична.</i>',
                ParseMode.HTML
            )
            return

        is_pl = (info['type'] == 'playlist')
        if is_pl:
            await state.update_data(music_url=url, music_platform=platform,
                                    music_title=info['title'], music_is_playlist=True)
            await state.set_state(MusicState.waiting_for_playlist_confirm)
            await msg.edit_text(
                f'{E_BOX} <b>{info["title"]}</b>\n\n'
                f'{E_MEDIA} {info["count"]} треков · {platform}\n\n'
                f'Скачать весь плейлист как ZIP-архив с MP3?',
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Скачать ZIP", callback_data="music_dl_playlist",
                                         icon_custom_emoji_id="5884479287171485878")],
                    [InlineKeyboardButton(text="Отмена", callback_data="back_to_main",
                                         icon_custom_emoji_id="5870657884844462243")],
                ])
            )
        else:
            dur_s = fmt_dur(info.get('duration'))
            await msg.edit_text(
                f'{E_MEDIA} <b>{info["title"]}</b>\n'
                f'{info.get("artist", "")}{"  ·  " + dur_s if dur_s else ""}\n\n'
                f'Скачать трек как MP3?',
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Скачать MP3", callback_data="music_dl_track",
                                         icon_custom_emoji_id="6039802767931871481")],
                    [InlineKeyboardButton(text="Отмена", callback_data="back_to_main",
                                         icon_custom_emoji_id="5870657884844462243")],
                ])
            )
            await state.update_data(music_url=url, music_platform=platform,
                                    music_title=info['title'], music_is_playlist=False)
            await state.set_state(MusicState.waiting_for_playlist_confirm)
        return

    # ── Прямые ссылки на изображения ───────────────────────────────────────
    if is_image_url(url):
        await safe_edit(msg, f'{E_DOWNLOAD} <b>Скачиваю изображение...</b>', ParseMode.HTML)
        file_path = await download_photo_direct(url)
        if file_path and os.path.exists(file_path):
            caption = f'{E_MEDIA} {platform} · {fmt_size(os.path.getsize(file_path))}\n<i>Скачано через {BOT_USERNAME}</i>'
            await send_photo_smart(file_path, msg, caption)
            os.remove(file_path)
            await msg.delete()
            if not await is_premium(uid):
                await increment_download(uid)
        else:
            await safe_edit(msg, f'{E_CROSS} <b>Не удалось скачать изображение.</b>', ParseMode.HTML)
        return

    # ── Pinterest (специальный scraper) ─────────────────────────────────────
    if 'pinterest.' in url or 'pin.it' in url:
        await safe_edit(msg, f'{E_EYE} <b>Получаю данные Pinterest...</b>', ParseMode.HTML)
        img_url = await pinterest_get_image(url)
        if img_url:
            await safe_edit(msg, f'{E_DOWNLOAD} <b>Скачиваю изображение...</b>', ParseMode.HTML)
            file_path = await download_photo_direct(img_url)
            if file_path and os.path.exists(file_path):
                caption = f'{E_MEDIA} Pinterest · {fmt_size(os.path.getsize(file_path))}\n<i>Скачано через {BOT_USERNAME}</i>'
                await send_photo_smart(file_path, msg, caption)
                os.remove(file_path)
                await msg.delete()
                if not await is_premium(uid):
                    await increment_download(uid)
                return
        # Если scraper не нашёл картинку — попробуем через yt-dlp (может быть видео)

    # ── TikTok ──────────────────────────────────────────────────────────────
    if is_tiktok(url):
        ok = await tiktok_download_and_send(url, uid, msg)
        if ok:
            if not await is_premium(uid):
                await increment_download(uid)
            return
        await safe_edit(msg, f'{E_RELOAD} <b>Переключаюсь на резервный метод...</b>', ParseMode.HTML)

    # ── Универсальный yt-dlp ─────────────────────────────────────────────────
    try:
        media_info = await asyncio.wait_for(
            asyncio.to_thread(extract_media_info_sync, url),
            timeout=ANALYSIS_TIMEOUT
        )
    except asyncio.TimeoutError:
        media_info = None
    except Exception as e:
        print(f"[process_link] {e}")
        media_info = None

    if not media_info:
        await msg.edit_text(
            f'{E_CROSS} <b>Не удалось обработать ссылку</b>\n\n'
            f'<b>Возможные причины:</b>\n'
            f'▪ Видео приватное или удалено\n'
            f'▪ Сайт не поддерживается\n'
            f'▪ Требуется авторизация\n\n'
            f'<i>Проверьте ссылку и попробуйте снова.</i>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Главное меню", callback_data="back_to_main",
                                     icon_custom_emoji_id="5893057118545646106")
            ]])
        )
        return

    await state.update_data(url=url)

    # Фото
    if media_info["type"] == "photo":
        await safe_edit(msg, f'{E_DOWNLOAD} <b>Скачиваю изображение...</b>', ParseMode.HTML)
        photo_url = media_info.get("thumb")
        if photo_url:
            file_path = await download_photo_direct(photo_url)
            if file_path and os.path.exists(file_path):
                caption = f'{E_MEDIA} {platform}\n<i>Скачано через {BOT_USERNAME}</i>'
                await send_photo_smart(file_path, msg, caption)
                os.remove(file_path)
                await msg.delete()
                if not await is_premium(uid):
                    await increment_download(uid)
                return
        await safe_edit(msg, f'{E_CROSS} <b>Не удалось скачать изображение.</b>', ParseMode.HTML)
        return

    # Видео — выбор качества
    await state.update_data(formats=media_info["formats"])
    await state.set_state(DownloadState.waiting_for_quality)

    prem           = await is_premium(uid)
    heights_sorted = sorted(media_info["formats"].keys(), key=lambda x: int(x), reverse=True)
    premium_h      = heights_sorted[:2] if len(heights_sorted) > 2 else []

    rows = []
    for h in heights_sorted:
        locked = (h in premium_h and not prem)
        if locked:
            rows.append([InlineKeyboardButton(
                text=f"{h}p  (Premium)",
                callback_data=f"dl_quality_{h}",
                icon_custom_emoji_id="6037249452824072506"
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"{h}p",
                callback_data=f"dl_quality_{h}",
                icon_custom_emoji_id="6039802767931871481"
            )])

    rows.append([InlineKeyboardButton(
        text="Аудио MP3",
        callback_data="dl_audio",
        icon_custom_emoji_id="5870528606328852614"
    )])
    rows.append([InlineKeyboardButton(
        text="Отмена",
        callback_data="back_to_main",
        icon_custom_emoji_id="5870657884844462243"
    )])

    title   = media_info.get('title', 'Видео')
    dur     = media_info.get('duration')
    dur_str = f"\n{E_CLOCK} {fmt_dur(dur)}" if dur else ""
    caption = (
        f'{E_MEDIA} <b>{title[:100]}</b>\n'
        f'{platform}{dur_str}\n\n'
        f'Выберите качество:'
    )
    try:
        if media_info.get("thumb"):
            await message.answer_photo(
                photo=media_info["thumb"], caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
            await msg.delete()
        else:
            await msg.edit_text(caption, parse_mode=ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await msg.edit_text(caption, parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ── Подтверждение скачивания музыки ──────────────────────────────────────────
@router.callback_query(
    MusicState.waiting_for_playlist_confirm,
    F.data.in_({"music_dl_track", "music_dl_playlist"})
)
async def music_download_confirm(callback: CallbackQuery, state: FSMContext):
    data      = await state.get_data()
    url       = data.get('music_url', '')
    platform  = data.get('music_platform', 'Музыка')
    title     = data.get('music_title', 'Трек')
    is_pl     = data.get('music_is_playlist', False)

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await send_music(url, callback.from_user.id, callback.message, platform, is_pl, title)


# ── Качество видео ────────────────────────────────────────────────────────────
@router.callback_query(DownloadState.waiting_for_quality, F.data.startswith("dl_quality_"))
async def download_quality(callback: CallbackQuery, state: FSMContext):
    selected     = callback.data.split("_")[2]
    data         = await state.get_data()
    formats_dict = data.get("formats", {})
    prem         = await is_premium(callback.from_user.id)

    heights_sorted = sorted(formats_dict.keys(), key=lambda x: int(x), reverse=True)
    premium_h      = heights_sorted[:2] if len(heights_sorted) > 2 else []

    if selected in premium_h and not prem:
        return await callback.answer(
            "Это качество доступно только с Premium!\n\nОформите подписку для доступа к максимальному качеству.",
            show_alert=True
        )

    await callback.message.edit_reply_markup(reply_markup=None)
    await download_and_send_media(
        data["url"], callback.from_user.id,
        callback.message, formats_dict[selected]
    )
    await state.clear()


@router.callback_query(DownloadState.waiting_for_quality, F.data == "dl_audio")
async def download_audio(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=None)
    await download_and_send_media(
        data["url"], callback.from_user.id,
        callback.message, audio_only=True
    )
    await state.clear()


# ── Вспомогательная синхронная функция для музыки ────────────────────────────
def _get_music_info_sync(url: str) -> dict | None:
    opts = _base_ydl_opts(url)
    opts['extract_flat'] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        if info.get('_type') == 'playlist':
            entries = [e for e in (info.get('entries') or []) if e]
            return {'type': 'playlist', 'title': info.get('title', 'Плейлист'), 'count': len(entries)}
        return {
            'type':     'track',
            'title':    info.get('title', 'Трек'),
            'artist':   info.get('artist') or info.get('uploader', ''),
            'duration': info.get('duration'),
            'thumb':    info.get('thumbnail'),
        }
    except Exception as e:
        print(f"[_get_music_info_sync] {e}")
        return None


# ================= ЗАПУСК =================
async def main():
    global pyro_app, pyro_upload_semaphore
    await init_db()
    dp.include_router(router)
    pyro_upload_semaphore = asyncio.Semaphore(3)
    pyro_app = Client("pyro_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    await pyro_app.start()
    print(f"✅ Бот {BOT_USERNAME} запущен!")
    print(f"✅ ffmpeg: {FFMPEG_PATH}")
    try:
        await dp.start_polling(bot)
    finally:
        await pyro_app.stop()
        _thread_pool.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
