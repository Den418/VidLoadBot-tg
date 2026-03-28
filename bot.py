import asyncio
import os
import time
import subprocess
import re
import yt_dlp
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, FSInputFile)
from aiogram.types.reaction_type_emoji import ReactionTypeEmoji
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from pyrogram import Client

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8780332671:AAGFRCXcjHiO79egzeY7Jfjkt0y2HLTqi6c"
API_ID = 33120499
API_HASH = "98835783a52a878e271c0c7acbc24876"

ADMIN_GROUP_ID = -1003152594582
MAIN_ADMIN_ID = 7720599904

DB_PATH = "bot_database.db"
DOWNLOADS_DIR = "downloads"
BOT_USERNAME = "@VidLoads_Bot"

PRICES_STARS = {
    7: 50,
    30: 150,
    60: 250,
    365: 1000
}

URL_PATTERN = re.compile(r'https?://\S+')

# Ограничиваем одновременные скачивания, чтобы не положить сервер
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)

if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

_session = AiohttpSession(timeout=3600)
bot = Bot(token=BOT_TOKEN, session=_session)
dp = Dispatcher()
router = Router()

pyro_app: Client = None

# ================= FSM СТЕЙТЫ =================
class PaymentState(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_promo = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_limit = State()
    waiting_for_promo_data = State()

class DownloadState(StatesGroup):
    waiting_for_quality = State()

# ================= ГЛОБАЛЬНЫЙ ПРОГРЕСС =================
progress_data = {}

class SilentLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

def get_progress_hook(task_id_str: str):
    def hook(d):
        if d['status'] == 'downloading':
            percent = d.get('_percent_str', '0%').replace('\x1b[0;94m', '').replace('\x1b[0m', '').strip()
            speed   = d.get('_speed_str', '—').replace('\x1b[0;32m', '').replace('\x1b[0m', '').strip()
            eta     = d.get('_eta_str', '—').replace('\x1b[0;33m', '').replace('\x1b[0m', '').strip()
            progress_data[task_id_str] = {'percent': percent, 'speed': speed, 'eta': eta, 'phase': 'download'}
    return hook

def build_progress_bar(percent_str: str) -> str:
    try:
        pct = float(percent_str.replace('%', '').strip())
    except Exception:
        pct = 0
    filled = int(pct / 10)
    return '▓' * filled + '░' * (10 - filled)

async def update_progress_message(msg: Message, task_id_str: str):
    last_text = ""
    while task_id_str in progress_data:
        data  = progress_data.get(task_id_str, {})
        phase = data.get('phase', 'download')
        bar   = build_progress_bar(data.get('percent', '0%'))
        eta   = data.get('eta', 'Очередь...')

        if phase == 'download':
            text = (
                f'<b><tg-emoji emoji-id="6039802767931871481">⬇️</tg-emoji> Скачивание...</b>\n\n'
                f'{bar} <code>{data.get("percent", "0%")}</code>\n\n'
                f'<tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> Скорость: <code>{data.get("speed", "—")}</code>\n'
                f'<tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> Осталось: <code>{eta}</code>'
            )
        else:
            text = (
                f'<b><tg-emoji emoji-id="5963103826075456248">⬆️</tg-emoji> Отправка...</b>\n\n'
                f'{bar} <code>{data.get("percent", "0%")}</code>\n\n'
                f'<tg-emoji emoji-id="5345906554510012647">🔄</tg-emoji> Скорость: <code>{data.get("speed", "—")}</code>\n'
                f'<tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> Осталось: <code>{eta}</code>'
            )

        if text != last_text:
            try:
                await msg.edit_caption(caption=text, parse_mode=ParseMode.HTML)
                last_text = text
            except Exception:
                pass
        await asyncio.sleep(2)

# ================= ВОДЯНОЙ ЗНАК =================
def add_watermark_sync(input_path: str) -> str:
    output_path = input_path.replace('.mp4', '_wm.mp4')
    watermark_text = f'Скачано с помощью {BOT_USERNAME}'
    cmd = [
        'ffmpeg', '-i', input_path,
        '-vf', (
            f"drawtext=text='{watermark_text}'"
            ":fontcolor=white:fontsize=22:alpha=0.75"
            ":x=10:y=H-th-15"
            ":shadowcolor=black@0.6:shadowx=2:shadowy=2"
            ":box=1:boxcolor=black@0.3:boxborderw=6"
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
                premium_until   INTEGER DEFAULT 0,
                downloads_count INTEGER DEFAULT 0,
                total_downloads INTEGER DEFAULT 0,
                last_reset      INTEGER DEFAULT 0
            )
        ''')
        try:
            await db.execute("ALTER TABLE users ADD COLUMN total_downloads INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, discount_percent INTEGER)''')
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('requisites', 'Карта: 0000')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_limit', '3')")
        await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('watermark_enabled', '0')")
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        await db.commit()

async def get_daily_limit() -> int:
    return int(await get_setting('daily_limit'))

async def is_watermark_enabled() -> bool:
    return (await get_setting('watermark_enabled')) == '1'

async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            res = await cur.fetchone()
            return bool(res and res[0] > int(time.time()))

async def add_premium_days(user_id: int, days: int):
    current_time = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)) as cur:
            res = await cur.fetchone()
            current_prem = res[0] if res and res[0] > current_time else current_time
        new_prem = current_prem + (days * 86400)
        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_prem, user_id))
        await db.commit()

async def check_limits(user_id: int) -> bool:
    limit = await get_daily_limit()
    if await is_premium(user_id):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT downloads_count, last_reset FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()
    if not user:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id, last_reset) VALUES (?, ?)",
                             (user_id, int(time.time())))
            await db.commit()
        return limit > 0
    current_time = int(time.time())
    if current_time - user[1] > 86400:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET downloads_count = 0, last_reset = ? WHERE user_id = ?",
                             (current_time, user_id))
            await db.commit()
        return limit > 0
    return user[0] < limit

async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET downloads_count = downloads_count + 1, "
            "total_downloads = total_downloads + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()

# ================= КЛАВИАТУРЫ =================
def get_main_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Купить Premium",   callback_data="buy_premium", icon_custom_emoji_id="6032644646587338669")],
        [
            InlineKeyboardButton(text="Профиль",       callback_data="profile", icon_custom_emoji_id="5870994129244131212"),
            InlineKeyboardButton(text="Статистика",    callback_data="my_stats", icon_custom_emoji_id="5870921681735781843"),
        ],
        [InlineKeyboardButton(text="Поделиться ботом", callback_data="share_bot", icon_custom_emoji_id="5769289093221454192")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="Админ Панель", callback_data="admin_panel", icon_custom_emoji_id="5870982283724328568")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    wm       = await is_watermark_enabled()
    wm_label = "Вкл" if wm else "Выкл"
    wm_emoji = "6037496202990194718" if wm else "6037249452824072506"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Статистика", callback_data="admin_stats", icon_custom_emoji_id="5870921681735781843"),
            InlineKeyboardButton(text="Рассылка", callback_data="admin_broadcast", icon_custom_emoji_id="6039422865189638057"),
        ],
        [
            InlineKeyboardButton(text="Лимит скачиваний", callback_data="admin_limit", icon_custom_emoji_id="5775896410780079073"),
            InlineKeyboardButton(text="Промокоды", callback_data="admin_promo", icon_custom_emoji_id="5886285355279193209"),
        ],
        [InlineKeyboardButton(text=f"Водяной знак: {wm_label}", callback_data="admin_toggle_watermark", icon_custom_emoji_id=wm_emoji)],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_main")],
    ])

def get_periods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 Неделя — 50 звезд", callback_data="period_7", icon_custom_emoji_id="5890937706803894250")],
        [InlineKeyboardButton(text="1 Месяц — 150 звезд", callback_data="period_30", icon_custom_emoji_id="5890937706803894250")],
        [InlineKeyboardButton(text="2 Месяца — 250 звезд", callback_data="period_60", icon_custom_emoji_id="5890937706803894250")],
        [InlineKeyboardButton(text="1 Год — 1000 звезд", callback_data="period_365", icon_custom_emoji_id="5890937706803894250")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_main")],
    ])

def get_payment_methods_keyboard(days: int, discount: int = 0) -> InlineKeyboardMarkup:
    price = int(PRICES_STARS[days] * (1 - discount / 100))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить Stars ({price} звезд)", callback_data=f"pay_stars_{days}", icon_custom_emoji_id="5904462880941545555")],
        [InlineKeyboardButton(text="Оплатить по реквизитам", callback_data=f"pay_manual_{days}", icon_custom_emoji_id="5769126056262898415")],
        [InlineKeyboardButton(text="Ввести промокод", callback_data="enter_promo", icon_custom_emoji_id="5886285355279193209")],
        [InlineKeyboardButton(text="Назад", callback_data="buy_premium")],
    ])

# ================= БАЗОВЫЕ ХЭНДЛЕРЫ =================
@router.message(F.text == "/start")
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.react([ReactionTypeEmoji(emoji="❤‍🔥")])
    admin_status = message.from_user.id == MAIN_ADMIN_ID
    limit = await get_daily_limit()

    text = (
        f'<b><tg-emoji emoji-id="6030400221232501136">🤖</tg-emoji> Привет, {message.from_user.first_name}!</b>\n\n'
        f'Я — <b>VidLoads Bot</b> — скачиваю видео и медиа с популярных платформ прямо в Telegram.\n\n'
        f'<b><tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> Как использовать:</b>\n'
        f'Просто отправь мне ссылку — и я всё сделаю!\n\n'
        f'<b><tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Бесплатно:</b> {limit} скачивания в сутки\n'
        f'<b><tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> Premium:</b> Безлимит + максимальное качество'
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, last_reset) VALUES (?, ?)", (message.from_user.id, int(time.time())))
        await db.commit()
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(admin_status))

@router.message(F.text == "/help")
async def help_handler(message: Message):
    text = f'<b><tg-emoji emoji-id="6028435952299413210">ℹ</tg-emoji> Просто отправь мне ссылку, и я всё скачаю!</b>'
    await message.answer(text, parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="Главное меню", callback_data="back_to_main")]
                         ]))

@router.message(F.text == "/mystats")
async def mystats_command(message: Message):
    await show_my_stats(message.from_user.id, message, edit=False)

@router.callback_query(F.data == "my_stats")
async def my_stats_callback(callback: CallbackQuery):
    await show_my_stats(callback.from_user.id, callback.message, edit=True)

async def show_my_stats(user_id: int, msg, edit: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until, downloads_count, total_downloads FROM users WHERE user_id = ?", (user_id,)) as cur:
            user = await cur.fetchone()

    if not user:
        text = '<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Данных пока нет. Начни скачивать!</b>'
    else:
        prem_until, today_count, total = user
        is_prem   = prem_until > int(time.time())
        limit     = await get_daily_limit()
        status    = f'<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> До {time.strftime("%d.%m.%Y", time.localtime(prem_until))}' if is_prem else '<tg-emoji emoji-id="5870657884844462243">❌</tg-emoji> Нет'
        today_str = "∞" if is_prem else f"{today_count}/{limit}"

        text = (
            f'<b><tg-emoji emoji-id="5870921681735781843">📊</tg-emoji> Твоя статистика</b>\n\n'
            f'<tg-emoji emoji-id="5870994129244131212">👤</tg-emoji> ID: <code>{user_id}</code>\n'
            f'<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> Premium: {status}\n\n'
            f'<b><tg-emoji emoji-id="6039802767931871481">⬇️</tg-emoji> Скачивания:</b>\n'
            f'▪ Сегодня: {today_str}\n'
            f'▪ Всего: <b>{total or 0}</b>'
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="back_to_main")]])
    if edit:
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(F.data == "share_bot")
async def share_bot_handler(callback: CallbackQuery):
    share_url = f"https://t.me/share/url?url=https://t.me/VidLoads_Bot&text=Качаю видео с сайтов через этого бота!"
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="5769289093221454192">🔗</tg-emoji> Поделись ботом с друзьями!</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Поделиться", url=share_url, icon_custom_emoji_id="5963103826075456248")],
            [InlineKeyboardButton(text="Назад", callback_data="back_to_main")],
        ])
    )

@router.callback_query(F.data == "back_to_main")
async def back_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    admin_status = callback.from_user.id == MAIN_ADMIN_ID
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="6030400221232501136">🤖</tg-emoji> Главное меню</b>\n\nОтправь мне ссылку!',
        parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard(admin_status)
    )

@router.callback_query(F.data == "profile")
async def profile_handler(callback: CallbackQuery):
    await show_my_stats(callback.from_user.id, callback.message, edit=True)

# ================= ОПЛАТА =================
@router.callback_query(F.data == "buy_premium")
async def buy_premium_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="5890937706803894250">📅</tg-emoji> Выберите период Premium подписки:</b>',
        parse_mode=ParseMode.HTML, reply_markup=get_periods_keyboard()
    )

@router.callback_query(F.data.startswith("period_"))
async def period_selected(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[1])
    await state.update_data(period=days)
    data = await state.get_data()
    discount = data.get('discount', 0)

    text = f'<b><tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Оплата ({days} дней)</b>'
    if discount > 0:
        text += f'\n\n<tg-emoji emoji-id="5870633910337015697">✅</tg-emoji> Применена скидка: <b>{discount}%</b>'

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_payment_methods_keyboard(days, discount))

@router.callback_query(F.data == "enter_promo")
async def enter_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text('<b><tg-emoji emoji-id="5886285355279193209">🏷</tg-emoji> Отправьте ваш промокод:</b>',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="buy_premium")]]))
    await state.set_state(PaymentState.waiting_for_promo)

@router.message(PaymentState.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT discount_percent FROM promo_codes WHERE code = ?", (message.text.strip(),)) as cur:
            promo = await cur.fetchone()

    if promo:
        await state.update_data(discount=promo[0])
        data = await state.get_data()
        await message.answer(f'<b>✅ Промокод на скидку {promo[0]}% активирован!</b>', parse_mode=ParseMode.HTML,
                             reply_markup=get_payment_methods_keyboard(data.get('period', 30), promo[0]))
    else:
        await message.answer('<b>❌ Промокод не найден.</b>', parse_mode=ParseMode.HTML)
    await state.set_state(None)

@router.callback_query(F.data.startswith("pay_manual_"))
async def pay_manual_handler(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[2])
    await state.update_data(payment_days=days)
    reqs = await get_setting('requisites')

    await callback.message.edit_text(
        f'<b><tg-emoji emoji-id="5769126056262898415">👛</tg-emoji> Оплата по реквизитам</b>\n\n'
        f'Переведите сумму сюда:\n<code>{reqs}</code>\n\nЗатем отправьте <b>скриншот чека</b> сюда.',
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="buy_premium")]])
    )
    await state.set_state(PaymentState.waiting_for_receipt)

@router.message(PaymentState.waiting_for_receipt, F.photo)
async def receipt_received(message: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get('payment_days', 30)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Одобрить", callback_data=f"approve_{message.from_user.id}_{days}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"reject_{message.from_user.id}"),
    ]])
    await bot.send_photo(ADMIN_GROUP_ID, photo=message.photo[-1].file_id,
                         caption=f'<b>Новая оплата!</b>\nЮзер: <code>{message.from_user.id}</code>\nПериод: {days} дней',
                         parse_mode=ParseMode.HTML, reply_markup=kb)
    await message.answer('<b><tg-emoji emoji-id="5983150113483134607">⏰</tg-emoji> Чек отправлен на проверку!</b>', parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def process_payment(callback: CallbackQuery):
    action, uid = callback.data.split("_")[0:2]
    if action == "approve":
        await add_premium_days(int(uid), int(callback.data.split("_")[2]))
        await bot.send_message(uid, '<b>🎉 Premium активирован!</b>', parse_mode=ParseMode.HTML)
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ <b>Одобрено</b>", parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(uid, '<b>❌ Оплата отклонена.</b>', parse_mode=ParseMode.HTML)
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ <b>Отклонено</b>", parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("pay_stars_"))
async def pay_stars_handler(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[2])
    data = await state.get_data()
    price = int(PRICES_STARS[days] * (1 - data.get('discount', 0) / 100))

    await bot.send_invoice(
        chat_id=callback.from_user.id, title=f"Premium на {days} дней",
        description="Безлимитные скачивания", payload=f"premium_{days}",
        currency="XTR", prices=[LabeledPrice(label="Premium", amount=price)]
    )

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    days = int(message.successful_payment.invoice_payload.split("_")[1])
    await add_premium_days(message.from_user.id, days)
    await message.answer('<b>🎉 Premium активирован!</b>', parse_mode=ParseMode.HTML)

# ================= АДМИНКА =================
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID: return
    await callback.message.edit_text('<b>⚙ Панель администратора</b>', parse_mode=ParseMode.HTML, reply_markup=await get_admin_panel_keyboard())

@router.callback_query(F.data == "admin_toggle_watermark")
async def admin_toggle_watermark(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID: return
    new_val = '0' if await is_watermark_enabled() else '1'
    await set_setting('watermark_enabled', new_val)
    await callback.answer(f"Водяной знак {'включён' if new_val == '1' else 'выключен'}", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=await get_admin_panel_keyboard())

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != MAIN_ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur: total_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (int(time.time()),)) as cur: premium_users = (await cur.fetchone())[0]
        async with db.execute("SELECT SUM(total_downloads) FROM users") as cur: total_dl = (await cur.fetchone())[0] or 0

    wm_status = '✅ Включён' if await is_watermark_enabled() else '❌ Выключен'
    await callback.message.edit_text(
        f'<b>📊 Статистика бота</b>\n\n👥 Всего: <b>{total_users}</b>\n🎁 Premium: <b>{premium_users}</b>\n'
        f'⬇️ Загрузок: <b>{total_dl}</b>\n🖋 Водяной знак: {wm_status}',
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin_panel")]])
    )

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MAIN_ADMIN_ID: return
    await callback.message.edit_text('<b>📣 Отправьте сообщение для рассылки:</b>', parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.waiting_for_broadcast)

@router.message(AdminStates.waiting_for_broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            users = await cur.fetchall()
    count = 0
    for (uid,) in users:
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f'<b>✅ Рассылка завершена. Получили: {count} чел.</b>', parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "admin_limit")
async def admin_limit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text('<b>🕓 Отправьте количество скачиваний в день:</b>', parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.waiting_for_limit)

@router.message(AdminStates.waiting_for_limit)
async def set_limit(message: Message, state: FSMContext):
    if message.text.isdigit():
        await set_setting('daily_limit', message.text)
        await message.answer(f'<b>✅ Лимит изменён на {message.text}</b>', parse_mode=ParseMode.HTML)
    else:
        await message.answer('<b>❌ Введите число.</b>', parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "admin_promo")
async def admin_promo(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text('<b>🏷 Отправьте промокод и скидку через пробел (напр. SUMMER 50)</b>', parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.waiting_for_promo_data)

@router.message(AdminStates.waiting_for_promo_data)
async def create_promo(message: Message, state: FSMContext):
    try:
        code, discount = message.text.split()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO promo_codes (code, discount_percent) VALUES (?, ?)", (code, int(discount)))
            await db.commit()
        await message.answer(f'<b>✅ Промокод <code>{code}</code> на скидку {discount}% создан!</b>', parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer('<b>❌ Ошибка формата. Пример: SUMMER 50</b>', parse_mode=ParseMode.HTML)
    await state.clear()

# ================= СКАЧИВАНИЕ =================

def _base_ydl_opts() -> dict:
    return {
        'quiet': True,
        'no_warnings': True,
        'logger': SilentLogger(),
        'socket_timeout': 30, # ФИКС: отваливаемся по таймауту, чтобы не вешать потоки
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'skip': ['hls', 'dash'],
            }
        },
        'ignoreerrors': False,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Linux; Android 12; Pixel 6) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/112.0.0.0 Mobile Safari/537.36'
            )
        },
    }

def extract_media_info_sync(url: str):
    opts = _base_ydl_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            return None

    if not info: return None

    if info.get('_type') == 'playlist':
        entries = info.get('entries') or []
        if not entries: return None
        info = entries[0]
        if not info: return None

    if info.get('vcodec') == 'none' and not info.get('formats'):
        return {"type": "photo", "url": url, "thumb": info.get('thumbnail')}

    formats = info.get('formats') or []
    unique_res = {}
    for f in formats:
        height = f.get('height')
        vcodec = f.get('vcodec', 'none')
        if vcodec and vcodec != 'none' and height and height > 0:
            if height not in unique_res:
                unique_res[height] = f['format_id']
            else:
                existing = next((x for x in formats if x['format_id'] == unique_res[height]), None)
                if existing and (f.get('tbr') or 0) > (existing.get('tbr') or 0):
                    unique_res[height] = f['format_id']

    if not unique_res:
        return {"type": "photo", "url": url, "thumb": info.get('thumbnail')}

    return {
        "type": "video",
        "thumb": info.get('thumbnail'),
        "title": info.get('title', 'Видео'),
        "formats": {str(h): unique_res[h] for h in sorted(unique_res.keys(), reverse=True)}
    }

# ФИКС: Читаем текст ИЛИ caption, чтобы ссылки не ломались при пересылке медиа
@router.message(F.text | F.caption)
async def process_link(message: Message, state: FSMContext):
    text = message.text or message.caption
    if not text: return
    
    match = URL_PATTERN.search(text)
    if not match: return
    
    url = match.group(0)
    await message.react([ReactionTypeEmoji(emoji="👌")])
    uid = message.from_user.id

    if not await check_limits(uid):
        return await message.answer(
            f'<b><tg-emoji emoji-id="5775896410780079073">🕓</tg-emoji> Лимит скачиваний исчерпан!</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Купить Premium", callback_data="buy_premium", icon_custom_emoji_id="6032644646587338669")]])
        )

    msg = await message.answer('<b><tg-emoji emoji-id="6037397706505195857">👁</tg-emoji> Анализирую ссылку...</b>', parse_mode=ParseMode.HTML)
    media_info = await asyncio.to_thread(extract_media_info_sync, url)

    if not media_info:
        return await msg.edit_text('<b>❌ Не удалось обработать ссылку.</b>', parse_mode=ParseMode.HTML)

    if media_info["type"] == "photo":
        await msg.edit_text('<b>⬇️ Скачиваю медиа...</b>', parse_mode=ParseMode.HTML)
        # ФИКС: Откидываем тяжелую задачу в фон, чтобы не стопить хэндлер
        asyncio.create_task(download_and_send_media(url, uid, msg))
        return

    await state.update_data(url=url, formats=media_info["formats"])
    await state.set_state(DownloadState.waiting_for_quality)

    prem = await is_premium(uid)
    heights = list(media_info["formats"].keys())
    premium_heights = heights[:2] if len(heights) > 2 else []

    rows = []
    for h in heights:
        if h in premium_heights and not prem:
            rows.append([InlineKeyboardButton(text=f"{h}p (Premium)", callback_data=f"dl_quality_{h}", icon_custom_emoji_id="6037249452824072506")])
        else:
            rows.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"dl_quality_{h}", icon_custom_emoji_id="6039802767931871481")])

    text_caption = f'<b>🖼 {media_info["title"]}</b>\n\nВыберите качество:'
    try:
        if media_info["thumb"]:
            await message.answer_photo(photo=media_info["thumb"], caption=text_caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            await msg.delete()
        else:
            await msg.edit_text(text_caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await msg.edit_text(text_caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def download_and_send_media(url: str, user_id: int, status_msg: Message, format_id: str = "best"):
    # ФИКС: Уникальный ID таски, чтобы юзеры не пересекались
    task_id = f"{status_msg.chat.id}_{status_msg.message_id}"
    file_path = None
    progress_data[task_id] = {'percent': '0%', 'speed': '—', 'eta': 'Очередь...', 'phase': 'download'}
    updater_task = asyncio.create_task(update_progress_message(status_msg, task_id))

    def dl_sync():
        filename = f"{DOWNLOADS_DIR}/vid_{int(time.time())}.mp4"
        opts = _base_ydl_opts()
        opts.update({
            'outtmpl': filename,
            'format': f"{format_id}+bestaudio/best" if format_id != "best" else "bestvideo+bestaudio/best",
            'merge_output_format': 'mp4',
            'progress_hooks': [get_progress_hook(task_id)],
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        if not os.path.exists(filename):
            base = filename.replace('.mp4', '')
            for ext in ('.mp4', '.mkv', '.webm', '.mov'):
                if os.path.exists(base + ext):
                    return base + ext
        return filename

    try:
        # Ждем своей очереди по семафору
        async with DOWNLOAD_SEMAPHORE:
            file_path = await asyncio.to_thread(dl_sync)

            if not os.path.exists(file_path):
                raise FileNotFoundError()

            if await is_watermark_enabled():
                try: await status_msg.edit_caption(caption='<b>🖋 Добавляю водяной знак...</b>', parse_mode=ParseMode.HTML)
                except: pass
                file_path = await asyncio.to_thread(add_watermark_sync, file_path)

            progress_data[task_id] = {'percent': '0%', 'speed': '—', 'eta': '...', 'phase': 'upload'}
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

            if file_size_mb < 50:
                try: await status_msg.edit_caption(caption='<b>⬆️ Отправляю файл...</b>', parse_mode=ParseMode.HTML)
                except: pass
                await status_msg.answer_video(FSInputFile(file_path))
            else:
                start_time = time.time()
                last_update = {'t': 0}

                async def upload_progress(current, total):
                    now = time.time()
                    if now - last_update['t'] < 2: return
                    last_update['t'] = now
                    elapsed = max(now - start_time, 0.01)
                    pct = current * 100 / total
                    speed_mb = (current / elapsed) / (1024 * 1024)
                    eta_s = int((total - current) / max(current / elapsed, 1))
                    progress_data[task_id] = {'percent': f'{pct:.1f}%', 'speed': f'{speed_mb:.1f} MB/s', 'eta': f'{eta_s}s', 'phase': 'upload'}

                await asyncio.wait_for(
                    pyro_app.send_video(chat_id=user_id, video=file_path, progress=upload_progress),
                    timeout=600
                )

        progress_data.pop(task_id, None)
        updater_task.cancel()
        await status_msg.delete()

        if not await is_premium(user_id):
            await increment_download(user_id)

    except asyncio.TimeoutError:
        progress_data.pop(task_id, None)
        updater_task.cancel()
        try: await status_msg.edit_text('<b>❌ Превышено время скачивания.</b>', parse_mode=ParseMode.HTML)
        except: pass

    except Exception as e:
        progress_data.pop(task_id, None)
        updater_task.cancel()
        print(f"[ERROR] {e}")
        try: await status_msg.edit_text('<b>❌ Ошибка при скачивании.</b>', parse_mode=ParseMode.HTML)
        except: pass

    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

@router.callback_query(DownloadState.waiting_for_quality, F.data.startswith("dl_quality_"))
async def download_quality(callback: CallbackQuery, state: FSMContext):
    selected = callback.data.split("_")[2]
    data = await state.get_data()
    formats_dict = data.get("formats", {})

    if selected in (list(formats_dict.keys())[:2] if len(formats_dict) > 2 else []) and not await is_premium(callback.from_user.id):
        return await callback.answer("Это качество доступно только с Premium!", show_alert=True)

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear() # Очищаем стейт ПЕРЕД загрузкой, чтобы юзер не был заблокирован
    # Запускаем загрузку как отдельную независимую таску
    asyncio.create_task(download_and_send_media(data["url"], callback.from_user.id, callback.message, formats_dict[selected]))

# ================= ЗАПУСК =================
async def main():
    global pyro_app
    await init_db()
    dp.include_router(router)
    pyro_app = Client("pyro_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    await pyro_app.start()
    print(f"Бот {BOT_USERNAME} запущен!")
    try: await dp.start_polling(bot)
    finally: await pyro_app.stop()

if __name__ == "__main__":
    asyncio.run(main())
