# -*- coding: utf-8 -*-
import logging
import os
import openai
import asyncio
import html
import re
import time
import datetime
import aiosqlite
from collections import deque
from fuzzywuzzy import fuzz
from pycoingecko import CoinGeckoAPI # <--- Для цен

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError, AiogramError
from aiogram.types import BotCommandScopeDefault, BotCommandScopeChat, \
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# --- Конфигурация ---
TELEGRAM_BOT_TOKEN = '8011953984:AAFIxsYrLZ3T97x75WlHHS2WSSr2i-aMqYQ' # Замените на ваш токен
SAMBANOVA_API_KEY = '5821bebd-7c12-4e7c-a2ea-56cf6cd2d328'   # Замените на ваш API ключ
CHANNEL_ID = "@criptaEra1"                   # Замените на ID вашего канала (например, -1001234567890 или @yourchannel)
CHANNEL_LINK = "https://t.me/criptaEra1"       # Замените на ссылку на ваш канал
BOT_NAME = "ИИ-Ассистент Канала 'Крипта-Эра'"
ADMIN_USER_ID = 8638330                    # Замените на ваш Telegram User ID

# --- Настройки ---
DB_NAME = "bot_database.sqlite"
MESSAGE_LIMIT_PER_MONTH = 10
BAN_DURATION_MINUTES = 30
CONTEXT_MAX_MESSAGES = 6
TOXIC_KEYWORDS = ["говно", "говнище", "херня", "дерьмо", "тупой бот"] # Добавьте другие по желанию
RECENT_POSTS_CHECK_COUNT = 50
TOPIC_SIMILARITY_THRESHOLD = 85 # Процент схожести для определения повторной темы
PRICE_PLACEHOLDER_REGEX = r"\{\{PRICE:([A-Za-z0-9\-]+)\}\}" # Регулярка для {{PRICE:SYMBOL}}

# --- Глобальные переменные ---
user_context = {}
limit_enabled = True
bot_username = None
cg = CoinGeckoAPI() # Инициализируем клиент CoinGecko

# Статусы и маркеры
ALLOWED_STATUSES = [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]
COMMON_TOPIC_MARKER = "<<<COMMON_TOPIC_SEARCH_CHANNEL>>>"

# --- Проверка ключей и ID ---
if not TELEGRAM_BOT_TOKEN: raise ValueError("Токен Telegram бота пустой.")
if not SAMBANOVA_API_KEY: raise ValueError("Ключ API Sambanova пустой.")
if ADMIN_USER_ID == 0: raise ValueError("Необходимо указать ADMIN_USER_ID.")

# --- Состояния для FSM ---
class PublishPost(StatesGroup):
    waiting_for_topic = State()
    waiting_for_confirmation = State()
    waiting_for_edit_instructions = State()

# --- Инициализация OpenAI клиента ---
try:
    client = openai.OpenAI(api_key=SAMBANOVA_API_KEY, base_url="https://api.sambanova.ai/v1")
except Exception as e:
    logging.error(f"Ошибка инициализации клиента Sambanova: {e}")
    exit()

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

# --- Инициализация бота и диспетчера ---
bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- База Данных (SQLite) ---
async def db_connect():
    conn = await aiosqlite.connect(DB_NAME)
    await conn.execute('CREATE TABLE IF NOT EXISTS user_limits (user_id INTEGER PRIMARY KEY, month_year TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0)')
    await conn.execute('CREATE TABLE IF NOT EXISTS temporary_bans (user_id INTEGER PRIMARY KEY, expiry_timestamp INTEGER NOT NULL)')
    await conn.execute('CREATE TABLE IF NOT EXISTS published_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, publish_timestamp INTEGER NOT NULL)')
    await conn.commit()
    await conn.close()

async def get_user_limit_data(user_id: int):
     async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT month_year, count FROM user_limits WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def update_user_limit(user_id: int, month_year: str, count: int):
     async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO user_limits (user_id, month_year, count) VALUES (?, ?, ?)", (user_id, month_year, count))
        await db.commit()

async def get_ban_status(user_id: int):
     async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT expiry_timestamp FROM temporary_bans WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                current_time = int(time.time())
                if row[0] > current_time: return row[0]
                else:
                    await db.execute("DELETE FROM temporary_bans WHERE user_id = ?", (user_id,))
                    await db.commit()
                    logging.info(f"Срок бана для пользователя {user_id} истек.")
                    return None
            return None

async def ban_user(user_id: int, duration_minutes: int):
    expiry_timestamp = int(time.time()) + duration_minutes * 60
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO temporary_bans (user_id, expiry_timestamp) VALUES (?, ?)", (user_id, expiry_timestamp))
        await db.commit()
    logging.info(f"Пользователь {user_id} забанен до {datetime.datetime.fromtimestamp(expiry_timestamp)}")

async def add_published_post(topic: str):
    timestamp = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO published_posts (topic, publish_timestamp) VALUES (?, ?)", (topic, timestamp))
        await db.commit()
    logging.info(f"Тема '{topic}' добавлена в историю опубликованных постов.")

async def check_recent_topics(new_topic: str, count: int = RECENT_POSTS_CHECK_COUNT, threshold: int = TOPIC_SIMILARITY_THRESHOLD):
    async with aiosqlite.connect(DB_NAME) as db:
        query = "SELECT topic FROM published_posts ORDER BY publish_timestamp DESC LIMIT ?"
        async with db.execute(query, (count,)) as cursor:
            recent_topics = await cursor.fetchall()
        if not recent_topics: return None
        new_topic_lower = new_topic.lower()
        for (recent_topic,) in recent_topics:
            recent_topic_lower = recent_topic.lower()
            similarity = fuzz.token_sort_ratio(new_topic_lower, recent_topic_lower)
            logging.debug(f"Сравнение: '{new_topic_lower}' vs '{recent_topic_lower}', Схожесть: {similarity}")
            if similarity >= threshold:
                logging.info(f"Найдена похожая недавняя тема: '{recent_topic}' (Схожесть: {similarity} >= {threshold})")
                return recent_topic
        return None

# --- >>> Новая функция для получения цен <<< ---
async def get_crypto_price(symbol: str) -> str:
    """Получает цену криптовалюты с CoinGecko по символу."""
    symbol_lower = symbol.lower()
    coin_id = None # Объявляем заранее
    try:
        # Пытаемся найти ID монеты по символу
        # Загрузка списка монет (оптимально кешировать, но пока так)
        # coins_list = cg.get_coins_list()
        # coin_id = next((coin['id'] for coin in coins_list if coin['symbol'] == symbol_lower), None)

        # Упрощенный вариант: считаем, что символ - это ID
        # TODO: Улучшить поиск ID по символу (т.к. символы не уникальны)
        coin_id = symbol_lower
        if coin_id == "btc": coin_id = "bitcoin" # Частые случаи
        if coin_id == "eth": coin_id = "ethereum"
        if coin_id == "usdt": return "$1.00" # Стейблкоин

        if not coin_id:
            logging.warning(f"Не удалось найти ID для символа '{symbol_lower}' в CoinGecko.")
            return f"{{PRICE_NA:{symbol}}}"

        logging.info(f"Запрос цены для ID: {coin_id}")
        # Получаем простую цену в USD
        price_data = cg.get_price(ids=coin_id, vs_currencies='usd')

        if coin_id in price_data and 'usd' in price_data[coin_id]:
            price = price_data[coin_id]['usd']
            logging.info(f"Цена для {coin_id}: ${price}")
            # Форматируем цену
            if price < 0.01:
                return f"${price:.6f}" # Больше знаков для очень дешевых
            elif price < 1:
                 return f"${price:.4f}"
            else:
                 return f"${price:,.2f}" # Стандартный формат с запятыми
        else:
            logging.warning(f"Не найдена цена для ID '{coin_id}' в ответе CoinGecko: {price_data}")
            return f"{{PRICE_NA:{symbol}}}" # Возвращаем маркер ошибки

    except Exception as e:
        log_coin_id = coin_id if coin_id else "unknown"
        logging.error(f"Ошибка при получении цены для символа '{symbol}' (ID: {log_coin_id}): {e}")
        return f"{{PRICE_ERR:{symbol}}}" # Возвращаем маркер ошибки

# --- >>> Новая функция для обработки плейсхолдеров цен <<< ---
async def process_price_placeholders(text: str) -> str:
    """Находит {{PRICE:SYMBOL}} и заменяет их реальными ценами."""
    processed_text = text
    symbols_found = set() # Чтобы не запрашивать цену одного символа несколько раз

    # Используем lookahead, чтобы не заменять внутри других плейсхолдеров
    for match in re.finditer(PRICE_PLACEHOLDER_REGEX, text):
        symbol = match.group(1).strip()
        placeholder = match.group(0)
        if symbol and symbol not in symbols_found:
            symbols_found.add(symbol)
            # Запрос цены (синхронный, но для простоты)
            price_str = await get_crypto_price(symbol) # Используем await для асинхронной функции
            # Заменяем ВСЕ вхождения этого плейсхолдера
            processed_text = processed_text.replace(placeholder, price_str)

    return processed_text

# --- >>> Новая функция для исправления Markdown <<< ---
def fix_markdown(text: str) -> str:
    """Заменяет **bold** на *bold* и __italic__ на _italic_."""
    # Убедимся, что не ломаем ***bold italic*** или ___bold_italic___
    # Сначала заменяем тройные
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'*_\1_*', text)
    text = re.sub(r'___(.+?)___', r'*_\1_*', text) # Аналог для подчеркивания
    # Затем двойные
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'__(.+?)__', r'_\1_', text) # Аналог для подчеркивания
    return text

# --- Функция проверки подписки (без изменений) ---
async def check_subscription(user_id: int) -> bool:
    logging.debug(f"Проверка подписки для user_id={user_id} на канал={CHANNEL_ID}")
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        logging.info(f"Статус пользователя {user_id} в {CHANNEL_ID}: {member.status}")
        if member.status in ALLOWED_STATUSES:
            logging.debug(f"Пользователь {user_id} ПОДПИСАН.")
            return True
        else:
            logging.debug(f"Пользователь {user_id} НЕ ПОДПИСАН (статус: {member.status}).")
            return False
    except (TelegramBadRequest, TelegramAPIError) as e:
        if "user not found" in str(e).lower() or "member not found" in str(e).lower():
             logging.info(f"Ошибка проверки подписки для user {user_id}: Пользователь не найден в канале {CHANNEL_ID}. Считаем, что не подписан.")
        elif "chat not found" in str(e).lower():
             logging.error(f"Критическая ошибка: Канал {CHANNEL_ID} не найден. Проверьте CHANNEL_ID.")
        else:
             logging.warning(f"Ошибка API/BadRequest при проверке подписки для user {user_id} на {CHANNEL_ID}: {e}. Считаем, что не подписан.")
        return False
    except Exception as e:
        logging.error(f"Неожиданная ошибка проверки подписки для user {user_id} на канал {CHANNEL_ID}: {e}", exc_info=True)
        return False

# --- Системные промпты (обновлены) ---
SYSTEM_PROMPT_QA = f"""You are an expert AI assistant specializing exclusively in cryptocurrency and blockchain technology for the channel '{CHANNEL_ID}'.
Answer the user's questions concisely and accurately, focusing only on crypto-related aspects.
Politely decline questions unrelated to crypto.
**If you need the current price of a cryptocurrency, use the placeholder `{{PRICE:SYMBOL}}` (e.g., `{{PRICE:BTC}}`, `{{PRICE:ETH}}`). Do not invent prices.**
If the user's question is about a very basic, fundamental, or commonly discussed topic in crypto, append the exact marker '{COMMON_TOPIC_MARKER}' at the VERY END of your response.
Provide only the direct answer, without any extra commentary or meta-tags like <think>. Consider the provided conversation history for context.
Use Markdown formatting with *bold* and _italic_ only. **Do NOT use** `**bold**` or `__italic__`.
"""

SYSTEM_PROMPT_POST = f"""You are an expert AI writer for the crypto channel '{CHANNEL_ID}'.
**Writing Style:** Write in a clear, concise, slightly informal tone. Use relevant emojis sparingly (e.g., 🚀, 📈, 💡, 💰). Focus on practical info and key takeaways. Use short paragraphs or bullet points. Use Markdown formatting with *bold* and _italic_ only. **Do NOT use** `**bold**` or `__italic__`. Avoid technical jargon or explain it briefly.
**Pricing:** If you need the current price of a cryptocurrency, use the placeholder `{{PRICE:SYMBOL}}` (e.g., `{{PRICE:BTC}}`, `{{PRICE:ETH}}`). Do not invent prices.
Generate ONLY the post content based on the user's topic, adhering strictly to the style and pricing instructions. No greetings, intros, or conclusions.
"""

# --- >>> ИЗМЕНЕНО: Убран f-префикс, {переменные} с одинарными скобками, {{PRICE}} с двойными <<< ---
SYSTEM_PROMPT_EDIT = """You are an expert AI editor revising a Telegram post for the crypto channel '{channel_id}'.
You will receive the original topic, the previously generated post text, and the admin's edit instructions.
Revise the *previous post text* according to the admin's instructions.
Maintain the original topic and the channel's writing style (clear, concise, slightly informal, emojis, *bold*/*italic* Markdown only, **no `**bold**` or `__italic__`).
**Pricing:** If you need the current price of a cryptocurrency, use the placeholder `{{PRICE:SYMBOL}}`. Do not invent prices.
Output ONLY the revised post content. No explanations or apologies.

**Original Topic:**
{topic}

**Previous Post Text:**
{previous_content}

**Admin's Edit Instructions:**
{edit_instructions}

**Revised Post Text:**
"""

# --- Клавиатура для подтверждения поста (добавлена кнопка копирования) ---
def get_confirmation_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="publish_post_confirm")],
        [InlineKeyboardButton(text="✏️ Редактировать (AI)", callback_data="publish_post_edit")],
        [InlineKeyboardButton(text="📝 Копировать текст", callback_data="publish_post_copy")], # Добавляем кнопку копирования
        [InlineKeyboardButton(text="❌ Отмена", callback_data="publish_post_cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- Регистрация Хендлеров ---

# 1. Команды, работающие всегда
@dp.message(Command(commands=['start', 'help']))
async def send_welcome(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    user_id = message.from_user.id if message.from_user else 0
    if current_state is not None:
        logging.info(f"Пользователь {user_id} вызвал /start во время состояния {current_state}. Отменяем состояние.")
        await state.clear()
        await message.reply("Предыдущее действие отменено.")

    if user_id and user_id in user_context:
        del user_context[user_id]
        logging.info(f"Контекст для пользователя {user_id} очищен.")

    await message.reply(f"Привет! Я {BOT_NAME}.\n"
                        f"Для работы со мной необходимо быть подписчиком канала: {CHANNEL_LINK}\n"
                        f"Задавай свой вопрос после подписки!")

@dp.message(Command('cancel'), F.state != None)
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    logging.info(f"Пользователь {message.from_user.id} отменил состояние {current_state}")
    await state.clear()
    await message.reply("Действие отменено.", reply_markup=types.ReplyKeyboardRemove())

@dp.message(Command('cancel'), F.state == None)
async def cancel_no_state_handler(message: types.Message):
    logging.debug(f"Пользователь {message.from_user.id} вызвал /cancel, но нет активного состояния.")
    await message.reply("Нет активных действий для отмены.")

# 2. Админские команды
@dp.message(Command('limiton'))
async def cmd_limit_on(message: types.Message):
    global limit_enabled
    if not message.from_user or message.from_user.id != ADMIN_USER_ID: return
    limit_enabled = True
    logging.info(f"Админ {ADMIN_USER_ID} включил лимиты сообщений.")
    await message.reply("Лимиты сообщений для пользователей ВКЛЮЧЕНЫ.")

@dp.message(Command('limitoff'))
async def cmd_limit_off(message: types.Message):
    global limit_enabled
    if not message.from_user or message.from_user.id != ADMIN_USER_ID: return
    limit_enabled = False
    logging.info(f"Админ {ADMIN_USER_ID} выключил лимиты сообщений.")
    await message.reply("Лимиты сообщений для пользователей ВЫКЛЮЧЕНЫ.")

@dp.message(Command(commands=['publish']))
async def cmd_publish_start(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID: return

    current_state = await state.get_state()
    if current_state is not None:
        await message.reply(f"Вы уже находитесь в процессе ({current_state}). Введите /cancel для отмены сначала.")
        return

    logging.info(f"Админ {ADMIN_USER_ID} инициировал публикацию поста.")
    await state.set_state(PublishPost.waiting_for_topic)
    await message.reply("Введите тему для нового поста в канале:")

# 3. Хендлеры FSM
@dp.message(PublishPost.waiting_for_topic, F.text)
async def process_publish_topic(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID:
        logging.warning(f"Сообщение в состоянии waiting_for_topic от не-админа {message.from_user.id}, сброс состояния.")
        await state.clear()
        return

    topic = message.text.strip()
    if not topic:
        await message.reply("Тема не может быть пустой. Введите тему или /cancel.")
        return

    logging.info(f"Получена тема поста от админа ({ADMIN_USER_ID}): '{topic}'")

    similar_topic = await check_recent_topics(topic)
    if similar_topic:
        await message.reply(f"⚠️ *Похожая тема:*\nНедавно был пост: \"{html.escape(similar_topic)}\".\nПродолжить генерацию \"{html.escape(topic)}\"?",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                               [InlineKeyboardButton(text="Да, продолжить", callback_data=f"proceed_publish:{topic}")], # Тему передаем в callback_data
                               [InlineKeyboardButton(text="Нет, отменить", callback_data="publish_post_cancel")]
                           ]), parse_mode=ParseMode.HTML) # Используем HTML для escape
        # Сохраняем тему в состоянии на случай, если пользователь нажмет "Да"
        await state.update_data(pending_topic=topic)
        # Не меняем состояние, ждем callback
        return

    # Если похожей темы нет, сразу генерируем
    await generate_and_confirm_post(message, state, topic)

# Новая функция-обертка для генерации и подтверждения, чтобы избежать дублирования кода
async def generate_and_confirm_post(message_or_callback: types.Message | CallbackQuery, state: FSMContext, topic: str):
    """Генерирует пост, обрабатывает цены и отправляет на подтверждение."""
    if isinstance(message_or_callback, types.Message):
        chat_id = message_or_callback.chat.id
        reply_func = message_or_callback.reply
        message_to_delete = None
    else: # CallbackQuery
        chat_id = message_or_callback.message.chat.id
        reply_func = message_or_callback.message.answer # Отвечаем новым сообщением
        message_to_delete = message_or_callback.message # Удаляем предыдущее сообщение с кнопками/предупреждением
        await message_or_callback.answer() # Закрываем часики

    processing_msg = await bot.send_message(chat_id, f"Принято! Генерирую пост на тему \"{html.escape(topic)}\". Это может занять некоторое время...")
    if message_to_delete:
        try:
            await message_to_delete.delete()
        except TelegramBadRequest:
            logging.warning(f"Не удалось удалить сообщение {message_to_delete.message_id}")

    await bot.send_chat_action(chat_id, "typing")

    try:
        logging.info(f"Вызов AI для генерации поста по теме: '{topic}'")
        response = client.chat.completions.create(
            model="DeepSeek-R1", # Замените на вашу модель, если нужно
            messages=[{"role": "system", "content": SYSTEM_PROMPT_POST},{"role": "user", "content": topic}],
            temperature=0.7, top_p=0.9
        )
        post_content_raw = response.choices[0].message.content
        logging.info(f"Сырой ответ AI для поста: {post_content_raw}")

        # --- >>> Обработка Markdown и цен <<< ---
        post_content_fixed_md = fix_markdown(post_content_raw)
        # Асинхронная обработка плейсхолдеров
        post_content_processed = await process_price_placeholders(post_content_fixed_md)
        # Убираем <think> теги
        post_content_final = re.sub(r"^\s*<think>.*?</think>\s*", "", post_content_processed, flags=re.DOTALL | re.IGNORECASE).strip()
        logging.info(f"Финальный контент поста (цены/MD обработаны): {post_content_final}")
        # --- >>> Конец обработки <<< ---

        if not post_content_final:
            logging.error(f"Контент поста для темы '{topic}' оказался пустым.")
            await processing_msg.edit_text("AI вернул пустой ответ. Не удалось сгенерировать пост. /cancel")
            await state.clear()
            return

        await state.update_data(topic=topic, generated_content=post_content_final)
        await state.set_state(PublishPost.waiting_for_confirmation)
        await processing_msg.delete()

        # Используем Markdown для превью, т.к. пост будет в Markdown
        # Экранируем спецсимволы Markdown *перед* отправкой превью, чтобы они отображались как текст
        preview_text = post_content_final # Не экранируем, т.к. используем parse_mode=MARKDOWN

        try:
            await bot.send_message(
                chat_id,
                f"*Предпросмотр поста на тему \"{topic.replace('*','\\*').replace('_','\\_').replace('`','\\`')}\":*\n\n" # Экранируем тему
                f"{preview_text}\n\n"
                f"--------------------\n"
                f"Выберите действие:",
                reply_markup=get_confirmation_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e):
                 logging.error(f"Ошибка парсинга Markdown в сгенерированном посте для превью: {e}\nТекст: {preview_text}")
                 await bot.send_message(
                    chat_id,
                    f"<b>Предпросмотр поста на тему \"{html.escape(topic)}\":</b>\n\n"
                    f"{html.escape(post_content_final)}\n\n" # Отправляем как HTML escape
                    f"--------------------\n"
                    f"<b>Ошибка:</b> Не удалось отобразить форматирование. Пост содержит неверный Markdown.\n"
                    f"Выберите действие:",
                    reply_markup=get_confirmation_keyboard(),
                    parse_mode=ParseMode.HTML
                 )
            else:
                 raise e # Перебрасываем другие ошибки

    except openai.APIError as e:
        logging.error(f"Ошибка API Sambanova при генерации поста: {e}")
        await processing_msg.edit_text("Ошибка при обращении к AI для генерации поста. /cancel")
        await state.clear()
    except Exception as e:
        logging.error(f"Неожиданная ошибка в generate_and_confirm_post: {e}", exc_info=True)
        await processing_msg.edit_text("Непредвиденная ошибка при генерации поста. /cancel")
        await state.clear()


@dp.callback_query(PublishPost.waiting_for_confirmation, F.data.startswith("publish_post_"))
async def handle_confirmation_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[-1] # confirm, edit, copy, cancel
    message = callback.message # Сообщение с кнопками

    await callback.answer() # Сразу отвечаем на колбек

    if action == "cancel":
        logging.info(f"Админ {callback.from_user.id} отменил публикацию.")
        await state.clear()
        await message.edit_text("Публикация отменена.")
        return

    data = await state.get_data()
    topic = data.get("topic")
    post_content = data.get("generated_content")

    if not topic or not post_content:
        logging.error("Не найдены данные в FSM при подтверждении/редактировании/копировании.")
        await message.edit_text("Ошибка: не найдены данные. Начните заново с /publish.")
        await state.clear()
        return

    if action == "edit":
        logging.info(f"Админ {callback.from_user.id} запросил редактирование поста '{topic}'.")
        await state.set_state(PublishPost.waiting_for_edit_instructions)
        await message.edit_text("Введите ваши инструкции по редактированию поста (например: 'сделай тон более формальным', 'убери последний абзац', 'добавь про риски {{PRICE:SOL}}'). Или /cancel для отмены.")
        return

    # --- >>> Обработка кнопки Копировать <<< ---
    if action == "copy":
        logging.info(f"Админ {callback.from_user.id} скопировал текст поста '{topic}'.")
        await state.clear()
        await message.edit_reply_markup(reply_markup=None) # Убираем кнопки
        # Отправляем чистый текст для копирования в виде кода, чтобы Markdown не сломался
        try:
             await message.answer(f"Текст поста для копирования:\n\n`{post_content}`", parse_mode=ParseMode.MARKDOWN)
             await message.answer("Публикация отменена. Вы можете скопировать текст выше и опубликовать вручную.")
        except TelegramBadRequest as e:
             logging.warning(f"Ошибка при отправке текста для копирования (Markdown): {e}. Отправка как простой текст.")
             await message.answer(f"Текст поста для копирования:\n\n{post_content}") # Отправляем как есть
             await message.answer("Публикация отменена. Вы можете скопировать текст выше и опубликовать вручную.")
        return
    # --- >>> Конец обработки <<< ---

    if action == "confirm":
        logging.info(f"Админ {callback.from_user.id} подтвердил публикацию поста '{topic}'.")
        # Публикация в канал
        try:
            logging.info(f"Попытка публикации поста '{topic}' в канал {CHANNEL_ID}...")
            # --- >>> Исправляем Markdown и Обрабатываем цены ПЕРЕД публикацией <<< ---
            # Обработка уже была сделана перед показом превью, повторять не нужно,
            # используем post_content из состояния state.
            final_content_to_publish = post_content
            # --- >>> Конец обработки <<< ---

            # Отправляем с ParseMode.MARKDOWN
            await bot.send_message(chat_id=CHANNEL_ID, text=final_content_to_publish, parse_mode=ParseMode.MARKDOWN)
            logging.info(f"Пост на тему '{topic}' успешно опубликован в {CHANNEL_ID}")
            await add_published_post(topic) # Добавляем в БД опубликованных
            await message.edit_text(f"✅ Пост \"{html.escape(topic)}\" опубликован в {CHANNEL_LINK}!")

        except TelegramBadRequest as e:
             if "can't parse entities" in str(e):
                 logging.error(f"Ошибка парсинга Markdown при ПУБЛИКАЦИИ поста в канал {CHANNEL_ID}: {e}\nТекст: {final_content_to_publish}")
                 await message.answer(f"❌ Не удалось опубликовать пост. Ошибка форматирования Markdown.\nПопробуйте отредактировать пост или /cancel.")
                 await message.edit_reply_markup(reply_markup=None) # Убираем кнопки у превью
             else:
                 logging.error(f"Ошибка API (BadRequest) при публикации поста в канал {CHANNEL_ID}: {e}", exc_info=True)
                 await message.answer(f"❌ Не удалось опубликовать пост. Ошибка API: {e}")
                 await message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError as e:
             logging.error(f"Ошибка API при публикации поста в канал {CHANNEL_ID}: {e}", exc_info=True)
             global bot_username
             bot_mention = f"@{bot_username}" if bot_username else "бот"
             await message.answer(f"❌ Не удалось опубликовать пост. Ошибка API: {e}\nУбедитесь, что {bot_mention} является администратором в {CHANNEL_ID} с правом публикации сообщений.")
             await message.edit_reply_markup(reply_markup=None) # Убираем кнопки у превью
        except Exception as e:
             logging.error(f"Неожиданная ошибка при публикации поста в канал {CHANNEL_ID}: {e}", exc_info=True)
             await message.answer("❌ Произошла неожиданная ошибка при публикации поста.")
             await message.edit_reply_markup(reply_markup=None)

        await state.clear()

# --- >>> Обработчик инструкций по редактированию (обновлен) <<< ---
@dp.message(PublishPost.waiting_for_edit_instructions, F.text)
async def handle_edit_instructions(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_USER_ID: return

    edit_instructions = message.text.strip()
    if not edit_instructions:
        await message.reply("Инструкции не могут быть пустыми. Опишите, что нужно изменить, или /cancel.")
        return

    logging.info(f"Получены инструкции по редактированию от админа: '{edit_instructions}'")
    processing_msg = await message.reply("Принято! Вношу правки в пост...")
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        data = await state.get_data()
        topic = data.get("topic")
        previous_content = data.get("generated_content")

        if not topic or not previous_content:
            logging.error("Не найдены данные FSM при редактировании.")
            await processing_msg.edit_text("Ошибка: данные потеряны. Начните заново с /publish.")
            await state.clear()
            return

        # --- >>> ИЗМЕНЕНО: Добавлен channel_id в format <<< ---
        edit_prompt = SYSTEM_PROMPT_EDIT.format(
            channel_id=CHANNEL_ID,
            topic=topic,
            previous_content=previous_content,
            edit_instructions=edit_instructions
        )

        logging.info(f"Вызов AI для редактирования поста.")
        response = client.chat.completions.create(
            model="DeepSeek-R1", # Замените на вашу модель
            messages=[{"role": "user", "content": edit_prompt}],
            temperature=0.5, top_p=0.9
        )
        edited_content_raw = response.choices[0].message.content
        logging.info(f"Сырой ответ AI после редактирования: {edited_content_raw}")

        # --- >>> Обработка Markdown и цен ПОСЛЕ редактирования <<< ---
        edited_content_fixed_md = fix_markdown(edited_content_raw)
        edited_content_processed = await process_price_placeholders(edited_content_fixed_md)
        edited_content_final = re.sub(r"^\s*<think>.*?</think>\s*", "", edited_content_processed, flags=re.DOTALL | re.IGNORECASE).strip()
        logging.info(f"Отредактированный контент поста (цены/MD): {edited_content_final}")
        # --- >>> Конец обработки <<< ---

        if not edited_content_final:
            logging.error("Отредактированный контент поста пуст.")
            await processing_msg.edit_text("AI вернул пустой ответ после редактирования. Показываю предыдущую версию.")
            await state.set_state(PublishPost.waiting_for_confirmation)
            # Показываем старый контент
            try:
                await bot.send_message(message.chat.id, f"*Предпросмотр поста на тему \"{topic.replace('*','\\*').replace('_','\\_').replace('`','\\`')}\":*\n\n{previous_content}\n\n-----\nВыберите действие:", reply_markup=get_confirmation_keyboard(), parse_mode=ParseMode.MARKDOWN)
            except TelegramBadRequest as e:
                logging.error(f"Ошибка парсинга Markdown при показе СТАРОГО контента после неудачного редактирования: {e}")
                await bot.send_message(message.chat.id, f"<b>Предпросмотр поста на тему \"{html.escape(topic)}\":</b>\n\n{html.escape(previous_content)}\n\n-----\n<b>Ошибка:</b> Не удалось отформатировать. Выберите действие:", reply_markup=get_confirmation_keyboard(), parse_mode=ParseMode.HTML)
            return

        await state.update_data(generated_content=edited_content_final) # Обновляем контент в состоянии
        await state.set_state(PublishPost.waiting_for_confirmation)
        logging.info("Возвращаемся к подтверждению с отредактированным постом.")
        await processing_msg.delete()

        # Показываем новый контент
        try:
            await bot.send_message(message.chat.id, f"*Предпросмотр поста (после ред.):*\n\n{edited_content_final}\n\n-----\nВыберите действие:", reply_markup=get_confirmation_keyboard(), parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest as e:
             if "can't parse entities" in str(e):
                 logging.error(f"Ошибка парсинга Markdown в ОТРЕДАКТИРОВАННОМ посте для превью: {e}\nТекст: {edited_content_final}")
                 await bot.send_message(
                    message.chat.id,
                    f"<b>Предпросмотр поста (после ред.):</b>\n\n"
                    f"{html.escape(edited_content_final)}\n\n" # Отправляем как HTML escape
                    f"--------------------\n"
                    f"<b>Ошибка:</b> Не удалось отобразить форматирование. Пост содержит неверный Markdown.\n"
                    f"Выберите действие:",
                    reply_markup=get_confirmation_keyboard(),
                    parse_mode=ParseMode.HTML
                 )
             else:
                 raise e # Перебрасываем другие ошибки

    except openai.APIError as e:
        logging.error(f"Ошибка API Sambanova при редактировании поста: {e}")
        await processing_msg.edit_text("Ошибка при обращении к AI. Попробуйте еще раз или /cancel.")
    except Exception as e:
        logging.error(f"Неожиданная ошибка в handle_edit_instructions: {e}", exc_info=True)
        await processing_msg.edit_text("Ошибка при редактировании. Попробуйте /cancel.")
        await state.clear()


@dp.callback_query(F.data.startswith("proceed_publish:"))
async def handle_proceed_publish(callback: CallbackQuery, state: FSMContext):
    # Извлекаем тему из callback_data
    try:
        topic = callback.data.split(":", 1)[1]
    except IndexError:
        logging.error(f"Не удалось извлечь тему из callback_data: {callback.data}")
        await callback.answer("Ошибка обработки запроса.", show_alert=True)
        await state.clear()
        return

    logging.info(f"Админ {callback.from_user.id} решил продолжить публикацию несмотря на предупреждение о повторе темы '{topic}'.")
    # Используем generate_and_confirm_post, передавая callback и извлеченную тему
    await generate_and_confirm_post(callback, state, topic)


# 4. Основной хендлер для текста
@dp.message(F.text)
async def handle_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        logging.warning(f"handle_text вызван с активным состоянием {current_state} для user {message.from_user.id}. Игнорируем.")
        # Можно отправить сообщение пользователю, что нужно сначала завершить/отменить текущее действие
        # await message.reply("Пожалуйста, сначала завершите или отмените (/cancel) текущее действие.")
        return

    if not message.text or not message.from_user: return

    user_id = message.from_user.id
    user_text = message.text.strip()
    username = message.from_user.username or f"id{user_id}"
    logging.info(f"Получено сообщение от {user_id} ('{username}'): '{user_text}'")

    # ПРОВЕРКА БАНА
    ban_expiry = await get_ban_status(user_id)
    if ban_expiry:
        remaining_seconds = ban_expiry - int(time.time())
        remaining_minutes = (remaining_seconds + 59) // 60
        logging.info(f"Пользователь {user_id} забанен. Осталось минут: {remaining_minutes}")
        await message.reply(f"Вы временно заблокированы. Осталось: ~{remaining_minutes} мин.")
        return

    # ПРОВЕРКА ЦЕНЗУРЫ
    lower_text = user_text.lower()
    if any(keyword in lower_text for keyword in TOXIC_KEYWORDS):
        logging.warning(f"Пользователь {user_id} отправил токсичное сообщение: '{user_text}'")
        await ban_user(user_id, BAN_DURATION_MINUTES)
        await message.reply(f"Общение в таком тоне недопустимо. Бан на {BAN_DURATION_MINUTES} минут.")
        return

    # ПРОВЕРКА ЛИМИТА
    message_count_before_ai = 0
    current_month_year = ""
    can_proceed = True
    if limit_enabled and user_id != ADMIN_USER_ID:
        current_month_year = datetime.datetime.now().strftime("%Y-%m")
        limit_data = await get_user_limit_data(user_id)
        message_count = 0; stored_month_year = None
        if limit_data: stored_month_year, message_count = limit_data

        if stored_month_year != current_month_year:
            logging.info(f"Новый месяц ({current_month_year}) для {user_id}. Сброс лимита.")
            # Не обновляем сразу, обновим после успешного ответа AI
            message_count_before_ai = 0
        else:
            if message_count >= MESSAGE_LIMIT_PER_MONTH:
                logging.info(f"{user_id} достиг лимита ({message_count}/{MESSAGE_LIMIT_PER_MONTH}) в {current_month_year}.")
                # Пока не отправляем сообщение, сделаем это после проверки подписки
                can_proceed = False
            else:
                message_count_before_ai = message_count

    # ПРОВЕРКА ПОДПИСКИ
    is_subscribed = await check_subscription(user_id)
    if not is_subscribed:
        logging.info(f"Пользователь {user_id} не подписан.")
        await message.reply(f"Для использования бота необходимо быть подписчиком канала: {CHANNEL_LINK}")
        return

    # Проверка лимита (теперь после проверки подписки)
    if not can_proceed:
        # Пользователь подписан, но лимит исчерпан
        limit_data = await get_user_limit_data(user_id) # Получаем актуальное значение
        message_count = limit_data[1] if limit_data else MESSAGE_LIMIT_PER_MONTH
        sub_msg = (f"Вы использовали свой лимит сообщений на этот месяц ({message_count}/{MESSAGE_LIMIT_PER_MONTH}).\n"
                   f"Новые сообщения будут доступны в следующем месяце.\n"
                   f"Вы можете обсудить интересующие вас темы в комментариях канала: {CHANNEL_LINK}")
                   # f"Для снятия ограничений рассмотрите вариант подписки [Инфо - ЗАГЛУШКА]." # Строка про подписку
        await message.reply(sub_msg)
        return

    # Все проверки пройдены
    logging.info(f"Пользователь {user_id} подписан и лимит не превышен. Обработка запроса...")
    await bot.send_chat_action(message.chat.id, "typing")

    # КОНТЕКСТ
    if user_id not in user_context:
        user_context[user_id] = deque(maxlen=CONTEXT_MAX_MESSAGES)
    user_context[user_id].append({"role": "user", "content": user_text})
    messages_for_api = [{"role": "system", "content": SYSTEM_PROMPT_QA}] + list(user_context[user_id])

    try:
        # ВЫЗОВ AI
        response = client.chat.completions.create(
            model="DeepSeek-R1", # Замените на вашу модель
            messages=messages_for_api,
            temperature=0.6, top_p=0.9
        )
        ai_response_raw = response.choices[0].message.content
        logging.info(f"Ответ AI (QA) для {user_id}: {ai_response_raw}")

        # --- >>> Обработка Markdown и цен в QA ответах <<< ---
        ai_response_fixed_md = fix_markdown(ai_response_raw)
        ai_response_processed = await process_price_placeholders(ai_response_fixed_md)
        ai_response = re.sub(r"^\s*<think>.*?</think>\s*", "", ai_response_processed, flags=re.DOTALL | re.IGNORECASE).strip()
        # --- >>> Конец обработки <<< ---

        # Обновление контекста
        if ai_response:
            user_context[user_id].append({"role": "assistant", "content": ai_response})
        else:
            # Если ответ пустой, убираем последний запрос пользователя из контекста
            user_context[user_id].pop()
            logging.warning("AI вернул пустой ответ, запрос пользователя удален из контекста.")

        # Обработка ответа
        if ai_response.endswith(COMMON_TOPIC_MARKER):
             logging.info(f"AI пометил тему '{user_text}' как общую.")
             actual_answer = ai_response[:-len(COMMON_TOPIC_MARKER)].strip()
             reply_text = (f"{actual_answer}\n\n"
                           f"💡 _Похоже, это часто обсуждаемая тема. Ответ также можно поискать в канале "
                           f"или его комментариях: {CHANNEL_LINK} (используйте поиск)._")

             # Отправляем основной ответ
             try:
                 await message.answer(reply_text, parse_mode=ParseMode.MARKDOWN)
                 # УВЕЛИЧЕНИЕ СЧЕТЧИКА после успешной отправки
                 if limit_enabled and user_id != ADMIN_USER_ID:
                     new_count = message_count_before_ai + 1
                     await update_user_limit(user_id, current_month_year, new_count)
                     logging.info(f"Счетчик {user_id} -> {new_count}/{MESSAGE_LIMIT_PER_MONTH} в {current_month_year}.")

             except (TelegramBadRequest, AiogramError) as e:
                 if "can't parse entities" in str(e):
                     logging.error(f"Ошибка парсинга Markdown от AI (common topic): {e}. Ответ:\n{reply_text}")
                     await message.reply("Извините, AI вернул ответ в некорректном формате. Попробуйте еще раз.")
                     # Убираем некорректный ответ из контекста
                     if user_context[user_id] and user_context[user_id][-1]["role"] == "assistant": user_context[user_id].pop()
                 else: raise e # Переброс других ошибок Telegram

        else:
            if not ai_response:
                 logging.error("Ответ AI пуст после обработки.")
                 await message.reply("Не удалось получить ответ от AI.")
            else:
                 # Отправляем с Markdown
                 try:
                     await message.answer(ai_response, parse_mode=ParseMode.MARKDOWN)
                     # УВЕЛИЧЕНИЕ СЧЕТЧИКА после успешной отправки
                     if limit_enabled and user_id != ADMIN_USER_ID:
                         new_count = message_count_before_ai + 1
                         await update_user_limit(user_id, current_month_year, new_count)
                         logging.info(f"Счетчик {user_id} -> {new_count}/{MESSAGE_LIMIT_PER_MONTH} в {current_month_year}.")

                 except (TelegramBadRequest, AiogramError) as e:
                     if "can't parse entities" in str(e):
                         logging.error(f"Ошибка парсинга Markdown от AI: {e}. Ответ:\n{ai_response}")
                         await message.reply("Извините, AI вернул ответ в некорректном формате. Попробуйте еще раз.")
                         # Убираем некорректный ответ
                         if user_context[user_id] and user_context[user_id][-1]["role"] == "assistant": user_context[user_id].pop()
                     else:
                         raise e # Переброс других ошибок Telegram


    except openai.APIError as e:
        logging.error(f"Ошибка API Sambanova: {e}")
        await message.reply("В данный момент наблюдаются проблемы с доступом к AI. Попробуйте позже.")
        # Убираем последний запрос пользователя из контекста при ошибке API
        if user_id in user_context and user_context[user_id] and user_context[user_id][-1]["role"] == "user": user_context[user_id].pop()
    except (TelegramBadRequest, AiogramError) as e:
        # Обработка ошибок Telegram/Aiogram, не связанных с парсингом Markdown (они обрабатываются выше)
        logging.error(f"Неожиданная ошибка Telegram/Aiogram в handle_text: {e}", exc_info=True)
        await message.reply("Произошла ошибка при отправке ответа. Попробуйте позже.")
        if user_id in user_context and user_context[user_id] and user_context[user_id][-1]["role"] == "assistant": user_context[user_id].pop() # Убираем ответ, если он был добавлен
        elif user_id in user_context and user_context[user_id] and user_context[user_id][-1]["role"] == "user": user_context[user_id].pop() # Убираем запрос если не было ответа
    except Exception as e:
        logging.error(f"Неожиданная ошибка в handle_text: {e}", exc_info=True)
        await message.reply("Произошла непредвиденная ошибка. Попробуйте еще раз.")
        # Убираем последний запрос пользователя из контекста
        if user_id in user_context and user_context[user_id] and user_context[user_id][-1]["role"] == "user": user_context[user_id].pop()


# --- Запуск бота ---
async def main():
    global bot_username
    await db_connect()
    logging.info("База данных подключена и таблицы проверены.")

    # Получаем информацию о боте
    try:
        me = await bot.get_me()
        bot_username = me.username
        logging.info(f"Бот запущен как @{bot_username}")
    except Exception as e:
        logging.error(f"Не удалось получить информацию о себе: {e}")

    # Устанавливаем команды
    default_commands = [
        types.BotCommand(command="/start", description="Начать/Сбросить диалог"),
        types.BotCommand(command="/help", description="Показать это сообщение"),
        types.BotCommand(command="/cancel", description="Отменить ввод/действие")
    ]
    admin_commands = default_commands + [
        types.BotCommand(command="/publish", description="! Опубликовать пост в канал"),
        types.BotCommand(command="/limiton", description="! Включить лимит сообщений"),
        types.BotCommand(command="/limitoff", description="! Выключить лимит сообщений")
    ]
    try:
        await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
        if ADMIN_USER_ID != 0:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID))
            logging.info("Команды установлены для всех пользователей и для админа.")
        else:
            logging.warning("ADMIN_USER_ID не указан, админские команды не установлены персонально.")
            logging.info("Команды установлены для всех пользователей.")
    except Exception as e:
        logging.error(f"Не удалось установить команды бота: {e}")

    # Проверка доступа к каналу
    logging.info(f"Проверка доступа к каналу {CHANNEL_ID}...")
    try:
        chat_info = await bot.get_chat(CHANNEL_ID)
        logging.info(f"Доступ к каналу '{chat_info.title}' ({CHANNEL_ID}) есть.")
    except Exception as e:
        logging.error(f"Нет доступа к каналу {CHANNEL_ID}: {e}")
        logging.warning("Проверка подписки и публикация постов могут не работать!")

    # Сброс вебхука и старт поллинга
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info(f"Запуск бота {BOT_NAME}...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("Сессия бота закрыта.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен вручную.")
    except ValueError as e:
        logging.error(f"Критическая ошибка конфигурации: {e}")
    except Exception as e:
        logging.error(f"Критическая ошибка при запуске или работе бота: {e}", exc_info=True)