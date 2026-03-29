#!/usr/bin/env python3
"""
EmojiSliceBot — production-версия
Все функции: лимиты, подписки (Stars + ручные), админ-панель,
обязательные каналы, реакции, премиум-эмодзи, красивый результат.
"""

import re, json, uuid, requests, shutil, subprocess, os, time, sqlite3, asyncio
from datetime import datetime, date, timedelta
from PIL import Image
from pyrogram import Client, filters, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
)
import imageio_ffmpeg as ff

ffmpeg_path = ff.get_ffmpeg_exe()

# ════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ  ← обязательно замените перед запуском
# ════════════════════════════════════════════════════════════════
API_ID         = "33120499"
API_HASH       = "98835783a52a878e271c0c7acbc24876"
BOT_TOKEN      = "8260544116:AAENp-CgLWgYIBNATO3ehhwSw5k__iq1hOc"
BOT_USERNAME   = "EmojiSliceBot"

SUPER_ADMIN_ID = 7720599904         # ← ВАШ Telegram user_id (число)
ADMIN_GROUP_ID = -1003152594582    # ← ID группы для подтверждения рублёвых платежей

# Реквизиты для ручной оплаты
PAYMENT_CARD   = "79148972924"
PAYMENT_BANK   = "Озон-банк"
PAYMENT_HOLDER = "Даниил Б."

DB_PATH = "bot.db"
PACK_EFFECT_ID = "5046509860389126442"  # эффект на сообщение «пак готов»

# ════════════════════════════════════════════════════════════════
#  ТАРИФЫ
# ════════════════════════════════════════════════════════════════
PLANS = {
    "week":   {"name": "1 неделя",  "days": 7,   "rub": 80,   "stars": 45},
    "month":  {"name": "1 месяц",   "days": 30,  "rub": 150,  "stars": 82},
    "2month": {"name": "2 месяца",  "days": 60,  "rub": 300,  "stars": 165},
    "year":   {"name": "1 год",     "days": 365, "rub": 1800, "stars": 989},
}

app = Client("emoji_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
users_state: dict = {}

# ════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ════════════════════════════════════════════════════════════════
def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT,
                joined_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                plan TEXT, expires_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS daily_cuts (
                user_id INTEGER, cut_date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, cut_date)
            );
            CREATE TABLE IF NOT EXISTS required_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                channel_username TEXT,
                channel_title TEXT
            );
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, plan TEXT, method TEXT,
                status TEXT DEFAULT 'pending',
                admin_msg_id INTEGER, admin_chat_id INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        c.execute("INSERT OR IGNORE INTO settings VALUES ('free_daily_limit','5')")
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (SUPER_ADMIN_ID,))

# ── хелперы БД ──────────────────────────────────────────────────
def get_setting(k):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r[0] if r else None

def set_setting(k, v):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, v))

def register_user(uid, username, first_name):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id,username,first_name) VALUES (?,?,?)",
                  (uid, username or "", first_name or ""))
        c.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?",
                  (username or "", first_name or "", uid))

def is_admin(uid):
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone())

def get_admins():
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM admins").fetchall()]

def add_admin(uid):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))

def remove_admin(uid):
    with _conn() as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (uid,))

def has_active_sub(uid):
    with _conn() as c:
        r = c.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return False
    return datetime.fromisoformat(r[0]) > datetime.now()

def get_sub_info(uid):
    with _conn() as c:
        return c.execute("SELECT plan,expires_at FROM subscriptions WHERE user_id=?", (uid,)).fetchone()

def activate_sub(uid, plan):
    days = PLANS[plan]["days"]
    existing = get_sub_info(uid)
    if existing:
        cur_exp = datetime.fromisoformat(existing[1])
        base = cur_exp if cur_exp > datetime.now() else datetime.now()
    else:
        base = datetime.now()
    new_exp = base + timedelta(days=days)
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO subscriptions (user_id,plan,expires_at) VALUES (?,?,?)",
                  (uid, plan, new_exp.isoformat()))

def get_today_cuts(uid):
    today = date.today().isoformat()
    with _conn() as c:
        r = c.execute("SELECT count FROM daily_cuts WHERE user_id=? AND cut_date=?",
                      (uid, today)).fetchone()
    return r[0] if r else 0

def inc_cuts(uid):
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO daily_cuts (user_id,cut_date,count) VALUES (?,?,1) "
            "ON CONFLICT(user_id,cut_date) DO UPDATE SET count=count+1",
            (uid, today)
        )

def get_required_channels():
    with _conn() as c:
        return c.execute(
            "SELECT id,channel_id,channel_username,channel_title FROM required_channels"
        ).fetchall()

def add_channel(channel_id, username, title):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO required_channels "
            "(channel_id,channel_username,channel_title) VALUES (?,?,?)",
            (channel_id, username, title)
        )

def remove_channel(db_id):
    with _conn() as c:
        c.execute("DELETE FROM required_channels WHERE id=?", (db_id,))

def get_all_users():
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM users").fetchall()]

def get_stats():
    today = date.today().isoformat()
    with _conn() as c:
        tu  = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sub = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE expires_at > datetime('now')"
        ).fetchone()[0]
        ct  = c.execute(
            "SELECT COALESCE(SUM(count),0) FROM daily_cuts WHERE cut_date=?", (today,)
        ).fetchone()[0]
        tt  = c.execute("SELECT COALESCE(SUM(count),0) FROM daily_cuts").fetchone()[0]
    return dict(total_users=tu, active_subs=sub, cuts_today=ct, total_cuts=tt)

def create_pay_req(uid, plan, method):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO payment_requests (user_id,plan,method) VALUES (?,?,?)",
            (uid, plan, method)
        )
        return cur.lastrowid

def get_pay_req(pid):
    with _conn() as c:
        return c.execute(
            "SELECT id,user_id,plan,method,status FROM payment_requests WHERE id=?", (pid,)
        ).fetchone()

def update_pay_req(pid, status, admin_msg_id=None, admin_chat_id=None):
    with _conn() as c:
        if admin_msg_id:
            c.execute(
                "UPDATE payment_requests SET status=?,admin_msg_id=?,admin_chat_id=? WHERE id=?",
                (status, admin_msg_id, admin_chat_id, pid)
            )
        else:
            c.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, pid))


# ════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════════════════════════
def tg_api(method, **kwargs):
    """Вызов Bot API через requests."""
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=kwargs
    ).json()

async def set_reaction(client, message: Message, emoji: str = "👌"):
    try:
        await client.send_reaction(message.chat.id, message.id, emoji=emoji)
    except Exception:
        pass

async def check_channels(client, user_id: int):
    """Возвращает список каналов, на которые пользователь НЕ подписан."""
    channels = get_required_channels()
    not_subbed = []
    for (db_id, ch_id, ch_username, ch_title) in channels:
        try:
            member = await client.get_chat_member(ch_id, user_id)
            status = member.status.value if hasattr(member.status, "value") else str(member.status)
            if "left" in status or "kicked" in status or "banned" in status:
                not_subbed.append((ch_id, ch_username, ch_title))
        except Exception:
            not_subbed.append((ch_id, ch_username, ch_title))
    return not_subbed

def channels_keyboard(not_subbed: list) -> InlineKeyboardMarkup:
    buttons = []
    for (ch_id, ch_username, ch_title) in not_subbed:
        link = f"https://t.me/{ch_username.lstrip('@')}" if ch_username else f"https://t.me/c/{str(ch_id).lstrip('-100')}"
        buttons.append([InlineKeyboardButton(f"📢 {ch_title}", url=link)])
    buttons.append([InlineKeyboardButton(
        "✅ Проверить подписку",
        callback_data="check_channels",
        icon_custom_emoji_id="5870633910337015697"
    )])
    return InlineKeyboardMarkup(buttons)


# ════════════════════════════════════════════════════════════════
#  НАРЕЗКА
# ════════════════════════════════════════════════════════════════
def slice_image(image_path, user_id, cols, rows):
    img = Image.open(image_path).convert("RGBA")
    tile_size = 100
    img = img.resize((cols * tile_size, rows * tile_size), Image.Resampling.LANCZOS)
    user_dir = os.path.abspath(f"temp/{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    pieces = []
    for y in range(rows):
        for x in range(cols):
            piece = img.crop((x*tile_size, y*tile_size, (x+1)*tile_size, (y+1)*tile_size))
            path = os.path.join(user_dir, f"piece_{x}_{y}.webp")
            piece.save(path, "WEBP", lossless=True)
            pieces.append(path)
    return pieces, "static"


def slice_video(video_path, user_id, cols, rows):
    user_dir = os.path.abspath(f"temp/{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    prep = os.path.join(user_dir, "prep.webm")
    tw, th = cols * 100, rows * 100

    cmd = [
        ffmpeg_path, "-y", "-i", video_path,
        "-t", "3", "-an",
        "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}",
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "1M", "-crf", "30",
        prep
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        cmd[10] = "libvpx"
        result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg: {result.stderr[-500:]}")

    pieces = []
    codec = "libvpx" if cmd[10] == "libvpx" else "libvpx-vp9"
    for y in range(rows):
        for x in range(cols):
            path = os.path.join(user_dir, f"piece_{x}_{y}.webm")
            subprocess.run([
                ffmpeg_path, "-y", "-i", prep,
                "-vf", f"crop=100:100:{x*100}:{y*100}",
                "-c:v", codec, "-pix_fmt", "yuva420p", "-b:v", "500k",
                path
            ], capture_output=True)
            pieces.append(path)

    if os.path.exists(prep):
        os.remove(prep)
    return pieces, "video"


def create_emoji_pack(user_id, raw_link, title, base_emoji, pieces_paths, format_type):
    clean = re.sub(r'[^a-zA-Z0-9]', '', raw_link)
    suffix = str(uuid.uuid4())[:8]
    pack_name = f"e_{clean}_{suffix}_by_{BOT_USERNAME}"

    files, stickers = {}, []
    for i, path in enumerate(pieces_paths):
        key = f"img_{i}"
        files[key] = open(path, "rb")
        stickers.append({"sticker": f"attach://{key}", "format": format_type, "emoji_list": [base_emoji]})

    init_s = stickers[:50]
    init_f = {s["sticker"].split("//")[1]: files[s["sticker"].split("//")[1]] for s in init_s}

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/createNewStickerSet",
        data={"user_id": user_id, "name": pack_name, "title": title,
              "sticker_type": "custom_emoji", "stickers": json.dumps(init_s)},
        files=init_f
    ).json()

    if not resp.get("ok"):
        for f in files.values(): f.close()
        return resp, pack_name

    if len(stickers) > 50:
        for st in stickers[50:]:
            key = st["sticker"].split("//")[1]
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/addStickerToSet",
                data={"user_id": user_id, "name": pack_name, "sticker": json.dumps(st)},
                files={key: files[key]}
            )
            time.sleep(0.1)

    for f in files.values(): f.close()
    return resp, pack_name


def get_pack_emoji_preview(pack_name: str, count: int) -> str:
    """Получает tg-emoji теги для первой строки стикеров пака."""
    resp = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getStickerSet",
        params={"name": pack_name}
    ).json()
    html = ""
    if resp.get("ok"):
        for sticker in resp["result"]["stickers"][:count]:
            ceid = sticker.get("custom_emoji_id")
            base = sticker.get("emoji", "🧩")
            if ceid:
                html += f'<tg-emoji emoji-id="{ceid}">{base}</tg-emoji>'
    return html


# ════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════════════
def kb_main_channel():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📢 Канал разработчика",
            url="https://t.me/YaktonTech",
            icon_custom_emoji_id="6039422865189638057"
        )
    ]])

def kb_grid():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("3×3",   callback_data="grid_3_3"),   InlineKeyboardButton("4×4",   callback_data="grid_4_4")],
        [InlineKeyboardButton("5×5",   callback_data="grid_5_5"),   InlineKeyboardButton("7×7",   callback_data="grid_7_7")],
        [InlineKeyboardButton("10×10", callback_data="grid_10_10"), InlineKeyboardButton("12×12", callback_data="grid_12_12")],
    ])

def kb_subscribe_plans(method: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора тарифа (method: stars / rub)."""
    rows = []
    for key, p in PLANS.items():
        price = f"{p['stars']} ⭐" if method == "stars" else f"{p['rub']}₽"
        rows.append([InlineKeyboardButton(
            f"{p['name']} — {price}",
            callback_data=f"sub_buy_{key}_{method}"
        )])
    rows.append([InlineKeyboardButton(
        "◁ Назад", callback_data="sub_menu"
    )])
    return InlineKeyboardMarkup(rows)

def kb_sub_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "⭐ Оплатить звёздами",
            callback_data="sub_method_stars",
            icon_custom_emoji_id="5904462880941545555"
        )],
        [InlineKeyboardButton(
            "💳 Оплатить рублями",
            callback_data="sub_method_rub",
            icon_custom_emoji_id="5890848474563352982"
        )],
        [InlineKeyboardButton(
            "◁ Назад", callback_data="close_menu"
        )],
    ])

def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика",  callback_data="admin_stats",    icon_custom_emoji_id="5870921681735781843"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin_users",   icon_custom_emoji_id="5870772616305839506"),
        ],
        [
            InlineKeyboardButton("👤 Админы",       callback_data="admin_admins",  icon_custom_emoji_id="5870994129244131212"),
            InlineKeyboardButton("⚙️ Лимит",        callback_data="admin_limit",   icon_custom_emoji_id="5870982283724328568"),
        ],
        [
            InlineKeyboardButton("📣 Рассылка",     callback_data="admin_broadcast",icon_custom_emoji_id="6039422865189638057"),
            InlineKeyboardButton("📢 Каналы",       callback_data="admin_channels", icon_custom_emoji_id="5370599459661045441"),
        ],
    ])

def kb_back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◁ Назад в панель", callback_data="admin_menu")]])


# ════════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════════
@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, message: Message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    limit = get_setting("free_daily_limit") or "5"
    await message.reply_text(
        '<b><tg-emoji emoji-id="5870064214977439621">👋</tg-emoji> Добро пожаловать!</b>\n\n'
        '<i>Бот нарезает картинки и видео на сетки премиум‑эмодзи.</i>\n\n'
        '<b>Как это работает:</b>\n'
        '<blockquote>'
        '1️⃣ Отправьте картинку, видео или GIF.\n'
        '   ⚠️ Видео и GIF обрезаются до <b>3 секунд</b>.\n'
        '2️⃣ Выберите размер сетки.\n'
        '3️⃣ Укажите ссылку, название и эмодзи.\n'
        '4️⃣ Получите готовый пак!'
        '</blockquote>\n\n'
        f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Бесплатный лимит: <b>{limit} нарезок/день</b>.\n'
        '<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> Безлимит — оформите подписку /subscribe',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_main_channel()
    )


# ════════════════════════════════════════════════════════════════
#  /subscribe — меню подписки
# ════════════════════════════════════════════════════════════════
@app.on_message(filters.command("subscribe") & filters.private)
async def cmd_subscribe(client, message: Message):
    await show_sub_menu(message)

async def show_sub_menu(target):
    """target — Message или CallbackQuery."""
    uid = target.from_user.id if hasattr(target, "from_user") else target.message.from_user.id
    info = get_sub_info(uid)
    if info and datetime.fromisoformat(info[1]) > datetime.now():
        plan_name = PLANS.get(info[0], {}).get("name", info[0])
        exp = datetime.fromisoformat(info[1]).strftime("%d.%m.%Y")
        text = (
            f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
            f'<b>Подписка активна!</b>\n\n'
            f'Тариф: <b>{plan_name}</b>\n'
            f'Действует до: <b>{exp}</b>'
        )
    else:
        text = (
            '<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> '
            '<b>Оформить подписку</b>\n\n'
            'Выберите способ оплаты:'
        )
    if isinstance(target, Message):
        await target.reply_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb_sub_menu())
    else:
        await target.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb_sub_menu())


# ════════════════════════════════════════════════════════════════
#  /admin — панель администратора
# ════════════════════════════════════════════════════════════════
@app.on_message(filters.command("admin") & filters.private)
async def cmd_admin(client, message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply_text(
        '<b><tg-emoji emoji-id="5870982283724328568">⚙️</tg-emoji> Панель администратора</b>',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_admin_main()
    )


# ════════════════════════════════════════════════════════════════
#  УНИВЕРСАЛЬНЫЙ ХЭНДЛЕР ПРИВАТНЫХ СООБЩЕНИЙ
# ════════════════════════════════════════════════════════════════
@app.on_message(filters.private & ~filters.command(["start", "admin", "subscribe"]))
async def universal_private(client, message: Message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name)
    state_data = users_state.get(uid, {})
    state = state_data.get("state")

    # ── Состояния администратора ──────────────────────────
    if state == "ADMIN_BROADCAST":
        await do_broadcast(client, message, uid)
        return

    if state == "ADMIN_ADD_ADMIN" and message.text:
        await handle_add_admin_text(client, message, uid)
        return

    if state == "ADMIN_ADD_CHANNEL" and message.text:
        await handle_add_channel_text(client, message, uid)
        return

    if state == "ADMIN_SET_LIMIT" and message.text:
        await handle_set_limit_text(client, message, uid)
        return

    # ── Квитанция об оплате рублями ───────────────────────
    if state == "PAYMENT_RECEIPT" and message.photo:
        await handle_receipt_photo(client, message, uid, state_data)
        return

    # ── Обычный флоу: текст или медиа ────────────────────
    if message.text:
        await handle_text_step(client, message, uid)
    elif message.photo or message.video or message.animation or (
        message.document and message.document.mime_type and
        message.document.mime_type.startswith("video/")
    ):
        await handle_media(client, message, uid)


# ════════════════════════════════════════════════════════════════
#  ОБРАБОТКА МЕДИА
# ════════════════════════════════════════════════════════════════
async def handle_media(client, message: Message, uid: int):
    # 1. Проверка обязательных каналов
    not_subbed = await check_channels(client, uid)
    if not_subbed:
        return await message.reply_text(
            '<tg-emoji emoji-id="6039422865189638057">📣</tg-emoji> '
            '<b>Подпишитесь на каналы для использования бота:</b>',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=channels_keyboard(not_subbed)
        )

    # 2. Проверка дневного лимита
    if not has_active_sub(uid):
        free_limit = int(get_setting("free_daily_limit") or "5")
        if get_today_cuts(uid) >= free_limit:
            return await message.reply_text(
                f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                f'<b>Дневной лимит исчерпан</b> ({free_limit} нарезок).\n\n'
                f'Оформите подписку для безлимитных нарезок 👇',
                parse_mode=enums.ParseMode.HTML,
                reply_markup=kb_sub_menu()
            )

    # 3. Определяем тип и размер
    file_size = 0
    is_video = False
    will_be_trimmed = False

    if message.photo:
        media_type, ext = "photo", "jpg"
        file_size = message.photo.file_size or 0
    elif message.video:
        media_type, ext, is_video = "video", "mp4", True
        file_size = message.video.file_size or 0
        if message.video.duration and message.video.duration > 3:
            will_be_trimmed = True
    elif message.animation:
        media_type, ext, is_video = "video", "mp4", True
        file_size = message.animation.file_size or 0
        if message.animation.duration and message.animation.duration > 3:
            will_be_trimmed = True
    elif message.document:
        media_type, ext, is_video = "video", "mp4", True
        file_size = message.document.file_size or 0
    else:
        return

    if file_size > 20 * 1024 * 1024:
        return await message.reply_text(
            '<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
            'Файл слишком большой! Лимит — <b>20 МБ</b>.',
            parse_mode=enums.ParseMode.HTML
        )

    # 4. Реакция 👌
    await set_reaction(client, message)

    # 5. Предупреждение об обрезке
    trim_note = ""
    if will_be_trimmed:
        trim_note = (
            '\n\n<tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> '
            '<i>Видео/GIF длиннее 3 сек — будет обрезано автоматически.</i>'
        )

    msg = await message.reply_text(
        f'<tg-emoji emoji-id="5870676941614354370">🖋</tg-emoji> '
        f'<i>Скачиваю медиа...</i>{trim_note}',
        parse_mode=enums.ParseMode.HTML
    )

    # 6. Скачивание
    user_dir = os.path.abspath(f"temp/{uid}")
    os.makedirs(user_dir, exist_ok=True)
    target_path = os.path.join(user_dir, f"main.{ext}")

    try:
        downloaded = await message.download()
        if not downloaded:
            return await msg.edit_text("❌ Ошибка скачивания.")
        shutil.move(downloaded, target_path)
    except Exception as e:
        return await msg.edit_text(
            f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
            f'<b>Ошибка:</b> <code>{e}</code>',
            parse_mode=enums.ParseMode.HTML
        )

    users_state[uid] = {
        "state": "WAITING_GRID",
        "media_path": target_path,
        "media_type": media_type,
        "msg_id": msg.id
    }

    await msg.edit_text(
        '<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        '<b>Медиа получено!</b>\n\n'
        '<tg-emoji emoji-id="5878702545262414162">🔪</tg-emoji> '
        'Выберите размер сетки:',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_grid()
    )


# ════════════════════════════════════════════════════════════════
#  CALLBACK: СЕТКА
# ════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^grid_(\d+)_(\d+)$"))
async def cb_grid(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if uid not in users_state or users_state[uid].get("state") != "WAITING_GRID":
        return await cb.answer("⚠️ Сначала отправьте файл.", show_alert=True)

    cols = int(cb.matches[0].group(1))
    rows = int(cb.matches[0].group(2))
    users_state[uid].update({"grid": (cols, rows), "state": "WAITING_LINK"})

    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        f'Сетка: <b>{cols}×{rows}</b>\n\n'
        f'<tg-emoji emoji-id="5769289093221454192">🔗</tg-emoji> '
        f'<b>Шаг 1 из 3</b> — Введите короткое имя для ссылки на пак.\n'
        f'<i>Только латинские буквы и цифры (a–z, 0–9)</i>',
        parse_mode=enums.ParseMode.HTML
    )
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  ТЕКСТОВЫЕ ШАГИ: ССЫЛКА → НАЗВАНИЕ → ЭМОДЗИ
# ════════════════════════════════════════════════════════════════
async def handle_text_step(client, message: Message, uid: int):
    state_data = users_state.get(uid)
    if not state_data:
        return

    state = state_data.get("state")

    # ── Шаг 1: имя ссылки ────────────────────────────────
    if state == "WAITING_LINK":
        text = message.text.strip()

        # Блокируем кириллицу
        if re.search(r'[а-яёА-ЯЁ]', text):
            return await message.reply_text(
                '<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                '<b>Только латинские буквы!</b>\n'
                'Кириллица не допускается. Попробуйте ещё раз:',
                parse_mode=enums.ParseMode.HTML
            )

        # Только a-z, 0-9, _
        if not re.match(r'^[a-zA-Z0-9_]+$', text):
            return await message.reply_text(
                '<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                'Допустимы только <b>латинские буквы</b>, цифры и _',
                parse_mode=enums.ParseMode.HTML
            )

        users_state[uid]["link"] = text.lower()
        users_state[uid]["state"] = "WAITING_TITLE"
        await message.reply_text(
            '<tg-emoji emoji-id="5870801517140775623">🔗</tg-emoji> '
            '<b>Шаг 2 из 3</b> — Введите <b>красивое название</b> пака\n'
            '<i>(будет отображаться у пользователей)</i>',
            parse_mode=enums.ParseMode.HTML
        )

    # ── Шаг 2: название ──────────────────────────────────
    elif state == "WAITING_TITLE":
        users_state[uid]["title"] = message.text.strip() + f" @{BOT_USERNAME}"
        users_state[uid]["state"] = "WAITING_EMOJI"
        await message.reply_text(
            '<tg-emoji emoji-id="5870764288364252592">🙂</tg-emoji> '
            '<b>Шаг 3 из 3</b> — Отправьте <b>один эмодзи</b>,\n'
            'к которому будут привязаны части пака (например: 🧩 🖼 🪄):',
            parse_mode=enums.ParseMode.HTML
        )

    # ── Шаг 3: базовый эмодзи + создание пака ────────────
    elif state == "WAITING_EMOJI":
        base_emoji = message.text.strip()
        data = users_state.get(uid, {})

        if not data.get("media_path"):
            return await message.reply_text("❌ Данные утеряны. Отправьте файл заново.")

        # Грубая проверка: 1 эмодзи (не более 8 байт в UTF-8 для одного эмодзи с вариантом)
        if len(base_emoji.encode("utf-8")) > 16:
            return await message.reply_text(
                '<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                'Пожалуйста, отправьте <b>только один эмодзи</b>.',
                parse_mode=enums.ParseMode.HTML
            )

        proc_msg = await message.reply_text(
            '<tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> '
            '<i>Создаю пак, подождите...\n(Большие сетки и видео занимают больше времени)</i>',
            parse_mode=enums.ParseMode.HTML
        )

        cols, rows = data["grid"]

        try:
            if data["media_type"] == "photo":
                pieces, fmt = slice_image(data["media_path"], uid, cols, rows)
            else:
                pieces, fmt = slice_video(data["media_path"], uid, cols, rows)

            response, pack_name = create_emoji_pack(
                uid, data["link"], data["title"], base_emoji, pieces, fmt
            )

            if response.get("ok"):
                pack_url = f"https://t.me/addemoji/{pack_name}"
                # Получаем превью эмодзи из пака (первая строка = cols штук)
                emoji_preview = get_pack_emoji_preview(pack_name, cols)

                final_text = (
                    '<tg-emoji emoji-id="6041731551845159060">🎉</tg-emoji> '
                    '<b>Ваш пак готов!</b>\n\n'
                    f'{emoji_preview}\n\n'
                    f'<a href="{pack_url}">➕ Добавить набор: {data["title"]}</a>'
                )

                # Увеличиваем счётчик только если успешно
                inc_cuts(uid)

                # Удаляем сообщение «Создаю пак...»
                try:
                    await proc_msg.delete()
                except Exception:
                    pass

                # Отправляем финальное сообщение через raw Bot API (для message_effect_id)
                tg_api(
                    "sendMessage",
                    chat_id=uid,
                    text=final_text,
                    parse_mode="HTML",
                    message_effect_id=PACK_EFFECT_ID,
                    link_preview_options={"is_disabled": True}
                )
            else:
                await proc_msg.edit_text(
                    f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                    f'<b>Ошибка API:</b> {response.get("description")}',
                    parse_mode=enums.ParseMode.HTML
                )

        except Exception as e:
            await proc_msg.edit_text(
                f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                f'<b>Ошибка:</b> <code>{e}</code>',
                parse_mode=enums.ParseMode.HTML
            )
        finally:
            shutil.rmtree(os.path.abspath(f"temp/{uid}"), ignore_errors=True)
            users_state.pop(uid, None)


# ════════════════════════════════════════════════════════════════
#  CALLBACK: ПОДПИСКА
# ════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^sub_"))
async def cb_sub(client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data

    if data == "sub_menu":
        await cb.message.edit_text(
            '<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> '
            '<b>Оформить подписку</b>\n\nВыберите способ оплаты:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_sub_menu()
        )

    elif data == "sub_method_stars":
        await cb.message.edit_text(
            '<tg-emoji emoji-id="5904462880941545555">🪙</tg-emoji> '
            '<b>Оплата звёздами</b>\n\nВыберите тариф:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_subscribe_plans("stars")
        )

    elif data == "sub_method_rub":
        await cb.message.edit_text(
            '<tg-emoji emoji-id="5890848474563352982">💳</tg-emoji> '
            '<b>Оплата рублями</b>\n\nВыберите тариф:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_subscribe_plans("rub")
        )

    elif data.startswith("sub_buy_"):
        # sub_buy_{plan}_{method}
        parts = data.split("_")  # ['sub', 'buy', plan, method]
        plan = parts[2]
        method = parts[3]
        plan_info = PLANS.get(plan)
        if not plan_info:
            return await cb.answer("Неизвестный тариф", show_alert=True)

        if method == "stars":
            # Отправляем Stars-инвойс через Bot API
            result = tg_api(
                "sendInvoice",
                chat_id=uid,
                title=f"Подписка {plan_info['name']}",
                description=f"Безлимитные нарезки на {plan_info['name']}",
                payload=f"stars_{plan}_{uid}",
                currency="XTR",
                prices=[{"label": plan_info["name"], "amount": plan_info["stars"]}]
            )
            if not result.get("ok"):
                await cb.answer(f"Ошибка: {result.get('description', '?')}", show_alert=True)
            else:
                await cb.answer("Инвойс отправлен! 👇")

        elif method == "rub":
            # Создаём заявку и показываем реквизиты
            pay_id = create_pay_req(uid, plan, "rub")
            users_state[uid] = {"state": "PAYMENT_RECEIPT", "payment_id": pay_id, "plan": plan}

            await cb.message.edit_text(
                f'<tg-emoji emoji-id="5890848474563352982">🪙</tg-emoji> '
                f'<b>Реквизиты для оплаты</b>\n\n'
                f'Банк: <b>{PAYMENT_BANK}</b>\n'
                f'Номер телефона: <code>{PAYMENT_CARD}</code>\n'
                f'Получатель: <b>{PAYMENT_HOLDER}</b>\n'
                f'Сумма: <b>{plan_info["rub"]}₽</b>\n'
                f'Тариф: <b>{plan_info["name"]}</b>\n\n'
                f'После оплаты отправьте <b>скриншот чека</b> в этот чат.\n'
                f'<i>Заявка #{pay_id}</i>',
                parse_mode=enums.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")
                ]])
            )

    await cb.answer()


@app.on_callback_query(filters.regex(r"^cancel_payment$"))
async def cb_cancel_payment(client, cb: CallbackQuery):
    uid = cb.from_user.id
    users_state.pop(uid, None)
    await cb.message.edit_text(
        "❌ Оплата отменена.",
        reply_markup=kb_sub_menu()
    )
    await cb.answer()


@app.on_callback_query(filters.regex(r"^close_menu$"))
async def cb_close_menu(client, cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()

# ════════════════════════════════════════════════════════════════
#  PRE-CHECKOUT (Stars) - Исправлено через raw_update
# ════════════════════════════════════════════════════════════════
@app.on_raw_update()
async def raw_precheckout(client, update, users, chats):
    # Ловим сырое событие предварительного чекаута от Telegram
    if type(update).__name__ == "UpdateBotPrecheckoutQuery":
        # Подтверждаем платеж через твою функцию tg_api
        tg_api("answerPreCheckoutQuery", pre_checkout_query_id=str(update.query_id), ok=True)

# ════════════════════════════════════════════════════════════════
#  SUCCESSFUL PAYMENT (Stars)
# ════════════════════════════════════════════════════════════════
@app.on_message(filters.successful_payment & filters.private)
async def payment_success(client, message: Message):
    uid = message.from_user.id
    payload = message.successful_payment.invoice_payload  # stars_{plan}_{uid}
    try:
        _, plan, _ = payload.split("_", 2)
        if plan not in PLANS:
            raise ValueError("unknown plan")
        activate_sub(uid, plan)
        plan_info = PLANS[plan]
        exp = get_sub_info(uid)
        exp_date = datetime.fromisoformat(exp[1]).strftime("%d.%m.%Y") if exp else "?"
        await message.reply_text(
            f'<tg-emoji emoji-id="6041731551845159060">🎉</tg-emoji> '
            f'<b>Подписка активирована!</b>\n\n'
            f'Тариф: <b>{plan_info["name"]}</b>\n'
            f'Действует до: <b>{exp_date}</b>\n\n'
            f'Теперь у вас безлимитные нарезки!',
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await message.reply_text(f"❌ Ошибка активации: {e}")


# ════════════════════════════════════════════════════════════════
#  КВИТАНЦИЯ (ручная оплата рублями)
# ════════════════════════════════════════════════════════════════
async def handle_receipt_photo(client, message: Message, uid: int, state_data: dict):
    pay_id = state_data.get("payment_id")
    plan = state_data.get("plan")
    req = get_pay_req(pay_id)

    if not req or req[4] != "pending":
        users_state.pop(uid, None)
        return await message.reply_text("❌ Заявка не найдена или уже обработана.")

    plan_info = PLANS.get(plan, {})
    uname = f"@{message.from_user.username}" if message.from_user.username else str(uid)
    fname = message.from_user.first_name or ""

    caption = (
        f'💰 <b>Новая заявка #{pay_id}</b>\n\n'
        f'👤 {fname} ({uname})\n'
        f'ID: <code>{uid}</code>\n'
        f'📦 Тариф: <b>{plan_info.get("name", plan)}</b>\n'
        f'💵 Сумма: <b>{plan_info.get("rub", "?")}₽</b>'
    )
    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Одобрить",
            callback_data=f"pay_approve_{pay_id}",
            icon_custom_emoji_id="5870633910337015697"
        ),
        InlineKeyboardButton(
            "❌ Отклонить",
            callback_data=f"pay_reject_{pay_id}",
            icon_custom_emoji_id="5870657884844462243"
        )
    ]])

    try:
        sent = await message.copy(
            ADMIN_GROUP_ID,
            caption=caption,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=approve_kb
        )
        update_pay_req(pay_id, "waiting", admin_msg_id=sent.id, admin_chat_id=ADMIN_GROUP_ID)
    except Exception as e:
        update_pay_req(pay_id, "waiting")
        # Если группа недоступна — уведомляем всех админов
        for admin_id in get_admins():
            try:
                await message.copy(
                    admin_id,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=approve_kb
                )
            except Exception:
                pass

    users_state.pop(uid, None)
    await message.reply_text(
        f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        f'<b>Чек получен!</b>\n\n'
        f'Заявка #{pay_id} отправлена на проверку.\n'
        f'Ожидайте подтверждения — обычно в течение нескольких часов.',
        parse_mode=enums.ParseMode.HTML
    )


# ════════════════════════════════════════════════════════════════
#  CALLBACK: ОДОБРИТЬ / ОТКЛОНИТЬ ПЛАТЁЖ
# ════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^pay_(approve|reject)_(\d+)$"))
async def cb_payment_decision(client, cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа.", show_alert=True)

    action = cb.matches[0].group(1)
    pay_id = int(cb.matches[0].group(2))
    req = get_pay_req(pay_id)

    if not req:
        return await cb.answer("Заявка не найдена.", show_alert=True)

    _, user_id, plan, method, status = req

    if status != "waiting" and status != "pending":
        return await cb.answer(f"Заявка уже обработана: {status}", show_alert=True)

    if action == "approve":
        activate_sub(user_id, plan)
        update_pay_req(pay_id, "approved")
        plan_info = PLANS.get(plan, {})
        exp = get_sub_info(user_id)
        exp_date = datetime.fromisoformat(exp[1]).strftime("%d.%m.%Y") if exp else "?"

        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Одобрено (@{cb.from_user.username or cb.from_user.id})", callback_data="noop")
        ]]))

        try:
            await client.send_message(
                user_id,
                f'<tg-emoji emoji-id="6041731551845159060">🎉</tg-emoji> '
                f'<b>Подписка активирована!</b>\n\n'
                f'Тариф: <b>{plan_info.get("name", plan)}</b>\n'
                f'Действует до: <b>{exp_date}</b>\n\n'
                f'Теперь у вас безлимитные нарезки!',
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass
        await cb.answer("✅ Подписка выдана!")

    else:  # reject
        update_pay_req(pay_id, "rejected")
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"❌ Отклонено (@{cb.from_user.username or cb.from_user.id})", callback_data="noop")
        ]]))
        try:
            await client.send_message(
                user_id,
                f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
                f'<b>Платёж отклонён.</b>\n\n'
                f'Заявка #{pay_id} не прошла проверку.\n'
                f'Если это ошибка — обратитесь в поддержку.',
                parse_mode=enums.ParseMode.HTML
            )
        except Exception:
            pass
        await cb.answer("❌ Отклонено.")


# ════════════════════════════════════════════════════════════════
#  CALLBACK: ПРОВЕРИТЬ ПОДПИСКУ НА КАНАЛЫ
# ════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^check_channels$"))
async def cb_check_channels(client, cb: CallbackQuery):
    uid = cb.from_user.id
    not_subbed = await check_channels(client, uid)
    if not_subbed:
        await cb.message.edit_reply_markup(reply_markup=channels_keyboard(not_subbed))
        await cb.answer("Вы ещё не подписались на все каналы.", show_alert=True)
    else:
        await cb.message.edit_text(
            '<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
            'Отлично! Теперь отправьте медиа для нарезки.',
            parse_mode=enums.ParseMode.HTML
        )
        await cb.answer("Готово!")


# ════════════════════════════════════════════════════════════════
#  ADMIN PANEL CALLBACKS
# ════════════════════════════════════════════════════════════════
@app.on_callback_query(filters.regex(r"^admin_"))
async def cb_admin(client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_admin(uid):
        return await cb.answer("Нет доступа.", show_alert=True)

    data = cb.data

    # ── Главное меню ───────────────────────────────────────
    if data == "admin_menu":
        await cb.message.edit_text(
            '<b><tg-emoji emoji-id="5870982283724328568">⚙️</tg-emoji> Панель администратора</b>',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_admin_main()
        )

    # ── Статистика ─────────────────────────────────────────
    elif data == "admin_stats":
        s = get_stats()
        limit = get_setting("free_daily_limit") or "5"
        text = (
            '<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Статистика</b>\n\n'
            f'👥 Всего пользователей: <b>{s["total_users"]}</b>\n'
            f'<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> '
            f'Активных подписок: <b>{s["active_subs"]}</b>\n'
            f'🔪 Нарезок сегодня: <b>{s["cuts_today"]}</b>\n'
            f'📈 Всего нарезок: <b>{s["total_cuts"]}</b>\n'
            f'<tg-emoji emoji-id="5870982283724328568">⚙️</tg-emoji> '
            f'Бесплатный лимит/день: <b>{limit}</b>'
        )
        await cb.message.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=kb_back_admin())

    # ── Выгрузка пользователей ─────────────────────────────
    elif data == "admin_users":
        users = get_all_users()
        txt = "\n".join(str(u) for u in users)
        file_path = f"temp/users_{uid}.txt"
        os.makedirs("temp", exist_ok=True)
        with open(file_path, "w") as f:
            f.write(f"# Всего: {len(users)}\n{txt}")
        await cb.message.reply_document(
            document=file_path,
            caption=f'<tg-emoji emoji-id="6039802767931871481">⬇️</tg-emoji> '
                    f'ID пользователей ({len(users)} шт.)',
            parse_mode=enums.ParseMode.HTML
        )
        os.remove(file_path)
        await cb.answer()
        return

    # ── Список и управление админами ───────────────────────
    elif data == "admin_admins":
        admins = get_admins()
        rows = []
        for a in admins:
            btn_text = f"🗑 {a}" + (" [Вы]" if a == uid else "") + (" [Главный]" if a == SUPER_ADMIN_ID else "")
            if a != SUPER_ADMIN_ID:
                rows.append([InlineKeyboardButton(btn_text, callback_data=f"admin_del_admin_{a}")])
            else:
                rows.append([InlineKeyboardButton(btn_text, callback_data="noop")])
        rows.append([InlineKeyboardButton(
            "➕ Добавить админа",
            callback_data="admin_add_admin_start",
            icon_custom_emoji_id="5870633910337015697"
        )])
        rows.append([InlineKeyboardButton("◁ Назад", callback_data="admin_menu")])
        await cb.message.edit_text(
            '<b><tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> Администраторы</b>\n'
            '<i>(нажмите на ID для удаления)</i>',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )

    elif data == "admin_add_admin_start":
        users_state[uid] = {"state": "ADMIN_ADD_ADMIN", "prev_msg_id": cb.message.id}
        await cb.message.edit_text(
            '<tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> '
            'Отправьте <b>Telegram ID</b> нового администратора:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◁ Отмена", callback_data="admin_admins")
            ]])
        )

    elif data.startswith("admin_del_admin_"):
        target_id = int(data.split("_")[-1])
        if target_id == SUPER_ADMIN_ID:
            return await cb.answer("Нельзя удалить главного администратора.", show_alert=True)
        remove_admin(target_id)
        await cb.answer(f"Администратор {target_id} удалён.", show_alert=True)
        # Обновляем список
        admins = get_admins()
        rows = []
        for a in admins:
            btn_text = f"🗑 {a}" + (" [Вы]" if a == uid else "") + (" [Главный]" if a == SUPER_ADMIN_ID else "")
            if a != SUPER_ADMIN_ID:
                rows.append([InlineKeyboardButton(btn_text, callback_data=f"admin_del_admin_{a}")])
            else:
                rows.append([InlineKeyboardButton(btn_text, callback_data="noop")])
        rows.append([InlineKeyboardButton("➕ Добавить", callback_data="admin_add_admin_start")])
        rows.append([InlineKeyboardButton("◁ Назад", callback_data="admin_menu")])
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(rows))

    # ── Лимит ─────────────────────────────────────────────
    elif data == "admin_limit":
        current = get_setting("free_daily_limit") or "5"
        users_state[uid] = {"state": "ADMIN_SET_LIMIT", "prev_msg_id": cb.message.id}
        await cb.message.edit_text(
            f'<tg-emoji emoji-id="5870982283724328568">⚙️</tg-emoji> '
            f'<b>Лимит бесплатных нарезок</b>\n\n'
            f'Текущий: <b>{current} в день</b>\n\n'
            f'Введите новое число:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◁ Отмена", callback_data="admin_menu")
            ]])
        )

    # ── Рассылка ────────────────────────────────────────────
    elif data == "admin_broadcast":
        users_state[uid] = {"state": "ADMIN_BROADCAST"}
        await cb.message.edit_text(
            '<tg-emoji emoji-id="6039422865189638057">📣</tg-emoji> '
            '<b>Рассылка</b>\n\n'
            'Отправьте любое сообщение (текст, фото, видео и т.д.).\n'
            'Оно будет разослано всем пользователям с полным сохранением форматирования.',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◁ Отмена", callback_data="admin_broadcast_cancel")
            ]])
        )

    elif data == "admin_broadcast_cancel":
        users_state.pop(uid, None)
        await cb.message.edit_text(
            '<b><tg-emoji emoji-id="5870982283724328568">⚙️</tg-emoji> Панель администратора</b>',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_admin_main()
        )

    # ── Обязательные каналы ─────────────────────────────────
    elif data == "admin_channels":
        await show_channels_list(cb.message)

    elif data == "admin_add_channel_start":
        users_state[uid] = {"state": "ADMIN_ADD_CHANNEL", "prev_msg_id": cb.message.id}
        await cb.message.edit_text(
            '<tg-emoji emoji-id="6039422865189638057">📢</tg-emoji> '
            '<b>Добавить обязательный канал</b>\n\n'
            'Отправьте <b>@username</b> публичного канала\n'
            'или <b>числовой ID</b> приватного канала:',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◁ Отмена", callback_data="admin_channels")
            ]])
        )

    elif data.startswith("admin_del_channel_"):
        ch_db_id = int(data.split("_")[-1])
        remove_channel(ch_db_id)
        await cb.answer("Канал удалён.", show_alert=True)
        await show_channels_list(cb.message)

    await cb.answer()


async def show_channels_list(target_message):
    channels = get_required_channels()
    rows = []
    for (db_id, ch_id, ch_username, ch_title) in channels:
        rows.append([InlineKeyboardButton(
            f"🗑 {ch_title} ({ch_username or ch_id})",
            callback_data=f"admin_del_channel_{db_id}"
        )])
    rows.append([InlineKeyboardButton(
        "➕ Добавить канал",
        callback_data="admin_add_channel_start",
        icon_custom_emoji_id="5870633910337015697"
    )])
    rows.append([InlineKeyboardButton("◁ Назад", callback_data="admin_menu")])
    text = (
        '<b><tg-emoji emoji-id="6039422865189638057">📢</tg-emoji> '
        'Обязательные каналы</b>\n'
        '<i>(нажмите на канал для удаления)</i>'
        if channels else
        '<b><tg-emoji emoji-id="6039422865189638057">📢</tg-emoji> '
        'Обязательные каналы</b>\n\n<i>Список пуст.</i>'
    )
    try:
        await target_message.edit_text(
            text, parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(client, cb: CallbackQuery):
    await cb.answer()


# ════════════════════════════════════════════════════════════════
#  ADMIN TEXT INPUTS
# ════════════════════════════════════════════════════════════════
async def handle_add_admin_text(client, message: Message, uid: int):
    text = message.text.strip()
    if not text.isdigit():
        return await message.reply_text("❌ Введите числовой Telegram ID.")
    target_id = int(text)
    add_admin(target_id)
    users_state.pop(uid, None)
    await message.reply_text(
        f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        f'Администратор <code>{target_id}</code> добавлен.',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_admin_main()
    )


async def handle_add_channel_text(client, message: Message, uid: int):
    text = message.text.strip()
    try:
        chat = await client.get_chat(text)
        cid = str(chat.id)
        username = f"@{chat.username}" if chat.username else cid
        title = chat.title or text
        add_channel(cid, username, title)
        users_state.pop(uid, None)
        await message.reply_text(
            f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
            f'Канал <b>{title}</b> добавлен.',
            parse_mode=enums.ParseMode.HTML,
            reply_markup=kb_admin_main()
        )
    except Exception as e:
        await message.reply_text(
            f'<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> '
            f'Не удалось найти канал: <code>{e}</code>\n\n'
            f'Убедитесь, что бот добавлен в канал, и попробуйте снова.',
            parse_mode=enums.ParseMode.HTML
        )


async def handle_set_limit_text(client, message: Message, uid: int):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 0:
        return await message.reply_text("❌ Введите неотрицательное целое число.")
    set_setting("free_daily_limit", text)
    users_state.pop(uid, None)
    await message.reply_text(
        f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        f'Лимит установлен: <b>{text} нарезок/день</b>.',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_admin_main()
    )


# ════════════════════════════════════════════════════════════════
#  РАССЫЛКА
# ════════════════════════════════════════════════════════════════
async def do_broadcast(client, source_message: Message, admin_uid: int):
    users_state.pop(admin_uid, None)

    status_msg = await source_message.reply_text(
        '<tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> '
        '<i>Рассылка началась...</i>',
        parse_mode=enums.ParseMode.HTML
    )

    all_users = get_all_users()
    ok, fail = 0, 0

    for user_id in all_users:
        if user_id == admin_uid:
            continue
        try:
            await source_message.copy(user_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 msg/s — безопасный темп

    await status_msg.edit_text(
        f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> '
        f'<b>Рассылка завершена</b>\n\n'
        f'Доставлено: <b>{ok}</b>\n'
        f'Ошибок: <b>{fail}</b>',
        parse_mode=enums.ParseMode.HTML,
        reply_markup=kb_back_admin()
    )


# ════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    os.makedirs("temp", exist_ok=True)
    init_db()
    print("✅ База данных инициализирована.")
    print(f"✅ Супер-администратор: {SUPER_ADMIN_ID}")
    print("🚀 Бот запущен!")
    app.run()
