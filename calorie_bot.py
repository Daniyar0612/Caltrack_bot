"""
Telegram-бот: Калькулятор калорий
Библиотека: aiogram 3.x
База данных: SQLite (aiosqlite)
Распознавание фото: HuggingFace API (nateraw/food)
Данные о калориях: локальный файл foods.py (500+ блюд, без API)

Установка зависимостей:
    pip install aiogram aiosqlite aiohttp

Переменные окружения (Railway → Variables):
    BOT_TOKEN  — токен от @BotFather
    HF_TOKEN   — токен HuggingFace (hf_xxxx)
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from foods import FOODS, search_food, get_food

# ─────────────────────────────────────────────
# Настройки
# ─────────────────────────────────────────────

BOT_TOKEN    = os.getenv("BOT_TOKEN")
HF_TOKEN     = os.getenv("HF_TOKEN", "")
DB_PATH      = "calories.db"
HF_MODEL_URL = "https://api-inference.huggingface.co/models/nateraw/food"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# FSM: состояния
# ─────────────────────────────────────────────

class Registration(StatesGroup):
    height = State()
    weight = State()
    gender = State()
    age    = State()
    goal   = State()


class FoodLog(StatesGroup):
    choose_method = State()  # выбор способа ввода
    calories      = State()  # ручной ввод: калории
    macros        = State()  # ручной ввод: БЖУ
    photo         = State()  # ожидание фото
    weight_input  = State()  # ввод веса порции после фото
    search        = State()  # поиск блюда по тексту
    search_weight = State()  # ввод веса после выбора из поиска


# ─────────────────────────────────────────────
# База данных
# ─────────────────────────────────────────────

async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                height       REAL    NOT NULL,
                weight       REAL    NOT NULL,
                gender       TEXT    NOT NULL,
                age          INTEGER NOT NULL,
                goal         INTEGER NOT NULL,
                calorie_norm REAL    NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                date     TEXT    NOT NULL,
                calories REAL    NOT NULL,
                protein  REAL    NOT NULL,
                fat      REAL    NOT NULL,
                carbs    REAL    NOT NULL
            )
        """)
        await db.commit()


async def db_cleanup_old_logs() -> None:
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE date < ?", (cutoff,))
        await db.commit()


async def db_upsert_user(user_id, height, weight, gender, age, goal, calorie_norm):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, height, weight, gender, age, goal, calorie_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                height=excluded.height, weight=excluded.weight,
                gender=excluded.gender, age=excluded.age,
                goal=excluded.goal, calorie_norm=excluded.calorie_norm
        """, (user_id, height, weight, gender, age, goal, calorie_norm))
        await db.commit()


async def db_get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None


async def db_delete_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM logs  WHERE user_id=?", (user_id,))
        await db.commit()


async def db_add_log(user_id, calories, protein, fat, carbs):
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (user_id,date,calories,protein,fat,carbs) VALUES(?,?,?,?,?,?)",
            (user_id, today, calories, protein, fat, carbs),
        )
        await db.commit()


async def db_get_today(user_id: int) -> dict:
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein),0),
                   COALESCE(SUM(fat),0),     COALESCE(SUM(carbs),0)
            FROM logs WHERE user_id=? AND date=?
        """, (user_id, today)) as c:
            r = await c.fetchone()
            return {"calories": r[0], "protein": r[1], "fat": r[2], "carbs": r[3]}


async def db_get_week(user_id: int) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT date, SUM(calories) AS calories, SUM(protein) AS protein,
                   SUM(fat) AS fat, SUM(carbs) AS carbs
            FROM logs WHERE user_id=? AND date>=?
            GROUP BY date ORDER BY date
        """, (user_id, cutoff)) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# HuggingFace: распознавание блюда по фото
# ─────────────────────────────────────────────

async def hf_classify_food(image_bytes: bytes) -> str | None:
    """
    Отправляет фото в HuggingFace.
    Возвращает название блюда на английском или None.
    """
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HF_MODEL_URL,
                headers=headers,
                data=image_bytes,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"HF error {resp.status}: {await resp.text()}")
                    return None
                results = await resp.json()
                if isinstance(results, list) and results:
                    return results[0].get("label", "").replace("_", " ")
    except Exception as e:
        logger.error(f"HF exception: {e}")
    return None


# Маппинг английских названий HuggingFace → русские названия в нашей базе
HF_TO_RU: dict[str, str] = {
    "apple pie":            "пирог с яблоком",
    "baby back ribs":       "шашлык из свинины",
    "baklava":              "пахлава",
    "beef carpaccio":       "говядина жареная",
    "beef tartare":         "говядина сырая",
    "beet salad":           "свёкла варёная",
    "bibimbap":             "рис варёный",
    "bread pudding":        "булочка",
    "breakfast burrito":    "буррито",
    "bruschetta":           "брускетта",
    "caesar salad":         "салат цезарь",
    "cannoli":              "пирожное эклер",
    "caprese salad":        "салат капрезе",
    "carrot cake":          "торт морковный",
    "cheesecake":           "чизкейк",
    "chicken curry":        "карри куриное",
    "chicken quesadilla":   "кесадилья",
    "chicken wings":        "куриное крыло",
    "chocolate cake":       "торт шоколадный",
    "churros":              "чуррос",
    "clam chowder":         "суп с фрикадельками",
    "club sandwich":        "клаб-сэндвич",
    "creme brulee":         "крем-брюле",
    "cup cakes":            "капкейки",
    "donuts":               "пончики",
    "dumplings":            "дамплинги",
    "edamame":              "горошек зелёный",
    "eggs benedict":        "яйца бенедикт",
    "falafel":              "фалафель",
    "filet mignon":         "бифштекс",
    "fish and chips":       "минтай жареный",
    "french fries":         "картофель фри",
    "french onion soup":    "суп грибной",
    "french toast":         "французский тост",
    "fried calamari":       "кальмар варёный",
    "fried rice":           "рис жареный",
    "frozen yogurt":        "йогурт натуральный",
    "garlic bread":         "хлеб белый",
    "gnocchi":              "пельмени варёные",
    "greek salad":          "салат греческий",
    "grilled cheese sandwich": "сэндвич с ветчиной",
    "grilled salmon":       "лосось запечённый",
    "guacamole":            "гуакамоле",
    "gyoza":                "гёдза",
    "hamburger":            "гамбургер",
    "hot dog":              "хот-дог",
    "hummus":               "хумус",
    "ice cream":            "мороженое пломбир",
    "lasagna":              "лазанья",
    "macaroni and cheese":  "паста болоньезе",
    "macarons":             "макаруны",
    "miso soup":            "мисо-суп",
    "mussels":              "мидии варёные",
    "nachos":               "начос",
    "omelette":             "омлет",
    "onion rings":          "луковые кольца",
    "oysters":              "мидии варёные",
    "pad thai":             "пад тай",
    "paella":               "паэлья",
    "pancakes":             "панкейки",
    "panna cotta":          "панна котта",
    "peking duck":          "утка жареная",
    "pho":                  "фо",
    "pizza":                "пицца маргарита",
    "pork chop":            "свинина жареная",
    "ramen":                "рамен",
    "ravioli":              "пельмени варёные",
    "red velvet cake":      "торт красный бархат",
    "risotto":              "ризотто",
    "samosa":               "самса",
    "sashimi":              "сашими",
    "scallops":             "креветки варёные",
    "shrimp and grits":     "креветки варёные",
    "spaghetti bolognese":  "паста болоньезе",
    "spaghetti carbonara":  "паста карбонара",
    "spring rolls":         "ролл",
    "steak":                "бифштекс",
    "strawberry shortcake": "торт медовик",
    "sushi":                "суши",
    "tacos":                "тако",
    "takoyaki":             "гёдза",
    "tiramisu":             "тирамису",
    "tuna tartare":         "тунец свежий",
    "waffles":              "вафли",
}


def hf_label_to_ru(label: str) -> str | None:
    """Переводит метку HuggingFace в русское название из нашей базы."""
    label = label.lower().strip()
    return HF_TO_RU.get(label)


# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def calc_calories_for_weight(
    nutrition: tuple[float, float, float, float], grams: float
) -> tuple[float, float, float, float]:
    """Пересчитывает КБЖУ с 100г на нужный вес порции."""
    cal, p, f, c = nutrition
    k = grams / 100
    return round(cal * k, 1), round(p * k, 1), round(f * k, 1), round(c * k, 1)


def calc_calorie_norm(weight, height, age, gender, goal) -> float:
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "м" else -161)
    return round(bmr * 1.2 * {1: 0.8, 2: 1.0, 3: 1.2}.get(goal, 1.0), 1)


def goal_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="1 — Похудение"),
            KeyboardButton(text="2 — Поддержание"),
            KeyboardButton(text="3 — Набор массы"),
        ]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="М"), KeyboardButton(text="Ж")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def food_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔍 Найти блюдо", callback_data="food:search"),
            InlineKeyboardButton(text="📸 Фото",        callback_data="food:photo"),
        ],
        [
            InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="food:manual"),
        ],
    ])


def search_results_keyboard(results: list) -> InlineKeyboardMarkup:
    """Кнопки с результатами поиска."""
    buttons = []
    for name, (cal, p, f, c) in results:
        label = f"{name.capitalize()} ({cal} ккал/100г)"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"pick:{name}"
        )])
    buttons.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="food:manual")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_photo_keyboard(food_name: str) -> InlineKeyboardMarkup:
    """Кнопки после распознавания фото."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, это оно",    callback_data=f"pick:{food_name}")],
        [InlineKeyboardButton(text="🔍 Найти другое",   callback_data="food:search")],
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="food:manual")],
    ])


GOAL_LABELS = {1: "Похудение", 2: "Поддержание", 3: "Набор массы"}
remove_kb   = ReplyKeyboardRemove()


# ─────────────────────────────────────────────
# Роутер и хендлеры
# ─────────────────────────────────────────────

router = Router()


# ══════════════════════════════════════════════
# /start — регистрация
# ══════════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Я калькулятор калорий.\n\n"
        "Давай заполним данные. Введи свой <b>рост в см</b>:",
        reply_markup=remove_kb,
    )
    await state.set_state(Registration.height)


@router.message(Registration.height)
async def reg_height(message: Message, state: FSMContext) -> None:
    try:
        h = float(message.text.replace(",", "."))
        if not (100 <= h <= 250): raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи рост числом от 100 до 250. Пример: <b>175</b>")
        return
    await state.update_data(height=h)
    await message.answer("Введи свой <b>вес в кг</b>:")
    await state.set_state(Registration.weight)


@router.message(Registration.weight)
async def reg_weight(message: Message, state: FSMContext) -> None:
    try:
        w = float(message.text.replace(",", "."))
        if not (30 <= w <= 300): raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи вес числом от 30 до 300. Пример: <b>70</b>")
        return
    await state.update_data(weight=w)
    await message.answer("Выбери <b>пол</b>:", reply_markup=gender_keyboard())
    await state.set_state(Registration.gender)


@router.message(Registration.gender)
async def reg_gender(message: Message, state: FSMContext) -> None:
    g = message.text.strip().lower()
    if g not in ("м", "ж"):
        await message.answer("⚠️ Выбери пол с помощью кнопок.", reply_markup=gender_keyboard())
        return
    await state.update_data(gender=g)
    await message.answer("Введи свой <b>возраст</b> (лет):", reply_markup=remove_kb)
    await state.set_state(Registration.age)


@router.message(Registration.age)
async def reg_age(message: Message, state: FSMContext) -> None:
    try:
        a = int(message.text.strip())
        if not (10 <= a <= 120): raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи возраст числом от 10 до 120. Пример: <b>25</b>")
        return
    await state.update_data(age=a)
    await message.answer("Выбери <b>цель</b>:", reply_markup=goal_keyboard())
    await state.set_state(Registration.goal)


@router.message(Registration.goal)
async def reg_goal(message: Message, state: FSMContext) -> None:
    goal_map = {"1": 1, "2": 2, "3": 3}
    goal = goal_map.get(message.text.strip()[0]) if message.text.strip() else None
    if not goal:
        await message.answer("⚠️ Выбери цель с помощью кнопок.", reply_markup=goal_keyboard())
        return
    data = await state.get_data()
    norm = calc_calorie_norm(data["weight"], data["height"], data["age"], data["gender"], goal)
    await db_upsert_user(message.from_user.id, data["height"], data["weight"],
                         data["gender"], data["age"], goal, norm)
    await state.clear()
    await message.answer(
        f"✅ Данные сохранены!\n\n"
        f"🎯 Цель: <b>{GOAL_LABELS[goal]}</b>\n"
        f"🔥 Дневная норма: <b>{norm} ккал</b>\n\n"
        f"Чтобы добавить еду — напиши мне или отправь фото блюда.",
        reply_markup=remove_kb,
    )


# ══════════════════════════════════════════════
# Начало ввода еды
# ══════════════════════════════════════════════

async def ask_food_method(message: Message, state: FSMContext) -> None:
    await state.set_state(FoodLog.choose_method)
    await message.answer(
        "Как добавить приём пищи?",
        reply_markup=food_method_keyboard(),
    )


# Текст вне FSM → предлагаем меню
@router.message(F.text & ~F.text.startswith("/"))
async def food_entry_start(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return
    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    await ask_food_method(message, state)


# Фото вне FSM → сразу начинаем распознавание
@router.message(F.photo)
async def photo_entry_start(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return
    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    await state.set_state(FoodLog.photo)
    await food_photo(message, state)


# ══════════════════════════════════════════════
# Callbacks: выбор способа
# ══════════════════════════════════════════════

@router.callback_query(F.data == "food:manual")
async def cb_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        "Введи <b>калории</b> числом:\nПример: <code>450</code>"
    )
    await state.set_state(FoodLog.calories)


@router.callback_query(F.data == "food:photo")
async def cb_photo_btn(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup()
    await callback.message.answer("📸 Отправь фото своей еды:")
    await state.set_state(FoodLog.photo)


@router.callback_query(F.data == "food:search")
async def cb_search_btn(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup()
    await callback.message.answer(
        "🔍 Напиши название блюда или продукта:\n"
        "Пример: <code>гречка</code>, <code>курица</code>, <code>плов</code>"
    )
    await state.set_state(FoodLog.search)


# ══════════════════════════════════════════════
# Поиск блюда по тексту
# ══════════════════════════════════════════════

@router.message(FoodLog.search)
async def food_search(message: Message, state: FSMContext) -> None:
    query   = message.text.strip()
    results = search_food(query)

    if not results:
        await message.answer(
            f"😔 Блюдо <b>{query}</b> не найдено в базе.\n"
            "Попробуй другое название или введи вручную.",
            reply_markup=food_method_keyboard(),
        )
        await state.set_state(FoodLog.choose_method)
        return

    await message.answer(
        f"Нашёл {len(results)} вариант(а). Выбери:",
        reply_markup=search_results_keyboard(results),
    )


# ══════════════════════════════════════════════
# Callback: выбор блюда из результатов поиска
# ══════════════════════════════════════════════

@router.callback_query(F.data.startswith("pick:"))
async def cb_pick_food(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup()

    food_name = callback.data.split(":", 1)[1]
    nutrition = get_food(food_name)

    if not nutrition:
        await callback.message.answer("⚠️ Блюдо не найдено. Попробуй снова.")
        await ask_food_method(callback.message, state)
        return

    cal100, p100, f100, c100 = nutrition
    await state.update_data(food_name=food_name, nutrition=list(nutrition))

    await callback.message.answer(
        f"🍽 <b>{food_name.capitalize()}</b>\n"
        f"На 100г: {cal100} ккал  Б{p100} Ж{f100} У{c100}\n\n"
        f"Сколько грамм ты съел?\n"
        f"Пример: <code>200</code>"
    )
    await state.set_state(FoodLog.search_weight)


# ══════════════════════════════════════════════
# Ввод веса порции (после поиска или фото)
# ══════════════════════════════════════════════

async def process_weight_input(message: Message, state: FSMContext) -> None:
    """Общая обработка ввода веса порции."""
    try:
        grams = float(message.text.strip().replace(",", "."))
        if not (1 <= grams <= 5000): raise ValueError
    except ValueError:
        await message.answer(
            "⚠️ Введи вес числом в граммах (от 1 до 5000).\n"
            "Пример: <code>200</code>"
        )
        return

    data = await state.get_data()
    nutrition = tuple(data["nutrition"])
    cal, p, f, c = calc_calories_for_weight(nutrition, grams)
    await state.clear()
    await save_and_show(message, cal, p, f, c)


@router.message(FoodLog.search_weight)
async def food_search_weight(message: Message, state: FSMContext) -> None:
    await process_weight_input(message, state)


@router.message(FoodLog.weight_input)
async def food_photo_weight(message: Message, state: FSMContext) -> None:
    await process_weight_input(message, state)


# ══════════════════════════════════════════════
# Ручной ввод: калории → БЖУ
# ══════════════════════════════════════════════

@router.message(FoodLog.calories)
async def food_calories(message: Message, state: FSMContext) -> None:
    try:
        cal = float(message.text.strip().replace(",", "."))
        if cal < 0: raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи калории числом (≥ 0). Пример: <code>450</code>")
        return
    await state.update_data(calories=cal)
    await message.answer(
        "Теперь введи <b>Б Ж У</b> через пробел (граммы):\n"
        "Пример: <code>30 10 50</code>"
    )
    await state.set_state(FoodLog.macros)


@router.message(FoodLog.macros)
async def food_macros(message: Message, state: FSMContext) -> None:
    try:
        parts = message.text.strip().split()
        if len(parts) != 3: raise ValueError
        p, f, c = (float(x.replace(",", ".")) for x in parts)
        if any(v < 0 for v in (p, f, c)): raise ValueError
    except ValueError:
        await message.answer(
            "⚠️ Три числа через пробел (Б Ж У). Пример: <code>30 10 50</code>"
        )
        return
    data = await state.get_data()
    await state.clear()
    await save_and_show(message, data["calories"], p, f, c)


# ══════════════════════════════════════════════
# Фото → HuggingFace → результат
# ══════════════════════════════════════════════

@router.message(FoodLog.photo, F.photo)
async def food_photo(message: Message, state: FSMContext) -> None:
    await state.clear()

    if not HF_TOKEN:
        await message.answer(
            "⚠️ <b>HF_TOKEN</b> не найден.\n"
            "Добавь его в Railway → Variables."
        )
        await ask_food_method(message, state)
        return

    wait_msg = await message.answer("🔍 Распознаю блюдо...")

    # Скачиваем и отправляем в HuggingFace
    photo = message.photo[-1]
    file  = await message.bot.get_file(photo.file_id)
    buf   = await message.bot.download_file(file.file_path)
    label = await hf_classify_food(buf.read())
    await wait_msg.delete()

    if not label:
        await message.answer(
            "😔 Не удалось распознать блюдо. Попробуй сфотографировать получше.",
            reply_markup=food_method_keyboard(),
        )
        await state.set_state(FoodLog.choose_method)
        return

    # Переводим в русское название из нашей базы
    ru_name   = hf_label_to_ru(label)
    nutrition = get_food(ru_name) if ru_name else None

    if not nutrition:
        # Пробуем поиск по английскому слову
        results = search_food(label.split()[0])
        if results:
            ru_name   = results[0][0]
            nutrition = results[0][1]

    if not nutrition:
        await message.answer(
            f"🤔 Вижу что-то похожее на <b>{label}</b>, но не нашёл в базе.\n"
            "Поищи вручную:",
            reply_markup=food_method_keyboard(),
        )
        await state.set_state(FoodLog.choose_method)
        return

    cal100, p100, f100, c100 = nutrition

    await message.answer(
        f"🍽 Похоже это <b>{ru_name.capitalize()}</b>\n"
        f"На 100г: {cal100} ккал  Б{p100} Ж{f100} У{c100}\n\n"
        f"Это верно?",
        reply_markup=confirm_photo_keyboard(ru_name),
    )


@router.message(FoodLog.photo)
async def food_photo_wrong(message: Message) -> None:
    await message.answer("📸 Пожалуйста, отправь <b>фото</b> блюда.")


# ══════════════════════════════════════════════
# После выбора блюда (фото или поиск) → спрашиваем вес
# ══════════════════════════════════════════════

@router.callback_query(F.data.startswith("pick:"))
async def cb_pick_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup()

    food_name = callback.data.split(":", 1)[1]
    nutrition = get_food(food_name)

    if not nutrition:
        await callback.message.answer("⚠️ Блюдо не найдено. Попробуй снова.")
        await ask_food_method(callback.message, state)
        return

    cal100, p100, f100, c100 = nutrition
    await state.update_data(food_name=food_name, nutrition=list(nutrition))

    await callback.message.answer(
        f"⚖️ Сколько грамм <b>{food_name.capitalize()}</b> ты съел?\n"
        f"(На 100г: {cal100} ккал)\n\n"
        f"Пример: <code>200</code>"
    )
    await state.set_state(FoodLog.weight_input)


# ══════════════════════════════════════════════
# Сохранение и вывод итога дня
# ══════════════════════════════════════════════

async def save_and_show(message: Message, calories: float,
                        protein: float, fat: float, carbs: float) -> None:
    uid   = message.from_user.id
    await db_add_log(uid, calories, protein, fat, carbs)
    user  = await db_get_user(uid)
    today = await db_get_today(uid)
    norm  = user["calorie_norm"] if user else 0
    eaten = today["calories"]
    await message.answer(
        f"✅ Записано!\n\n"
        f"<b>Сегодня:</b> {round(eaten, 1)} ккал\n"
        f"<b>Осталось:</b> {round(norm - eaten, 1)} ккал\n"
        f"Б: {round(today['protein'], 1)}\n"
        f"Ж: {round(today['fat'], 1)}\n"
        f"У: {round(today['carbs'], 1)}"
    )


# ══════════════════════════════════════════════
# /today, /week, /reset
# ══════════════════════════════════════════════

@router.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    today = await db_get_today(message.from_user.id)
    norm  = user["calorie_norm"]
    eaten = today["calories"]
    await message.answer(
        f"📊 <b>Сегодня, {date.today().strftime('%d.%m.%Y')}</b>\n\n"
        f"Норма:    {norm} ккал\n"
        f"Съедено:  {round(eaten, 1)} ккал\n"
        f"Осталось: {round(norm - eaten, 1)} ккал\n\n"
        f"Б: {round(today['protein'], 1)}\n"
        f"Ж: {round(today['fat'], 1)}\n"
        f"У: {round(today['carbs'], 1)}"
    )


@router.message(Command("week"))
async def cmd_week(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся: /start")
        return
    rows = await db_get_week(message.from_user.id)
    if not rows:
        await message.answer("За последние 7 дней нет записей.")
        return
    norm  = user["calorie_norm"]
    lines = ["📅 <b>Статистика за 7 дней</b>\n"]
    total_cal = total_p = total_f = total_c = 0.0
    for r in rows:
        d   = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m")
        cal = round(r["calories"], 1)
        total_cal += cal
        total_p   += r["protein"]
        total_f   += r["fat"]
        total_c   += r["carbs"]
        diff = round(norm - cal, 1)
        sign = "+" if diff >= 0 else ""
        lines.append(
            f"<b>{d}</b>: {cal} ккал ({sign}{diff})  "
            f"Б{round(r['protein'],1)} "
            f"Ж{round(r['fat'],1)} "
            f"У{round(r['carbs'],1)}"
        )
    days = len(rows)
    lines.append(
        f"\n<b>Итого:</b> {round(total_cal, 1)} ккал за {days} дн.\n"
        f"<b>Среднее:</b> {round(total_cal / days, 1)} ккал/день\n"
        f"Б: {round(total_p,1)}  Ж: {round(total_f,1)}  У: {round(total_c,1)}"
    )
    await message.answer("\n".join(lines))


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db_delete_user(message.from_user.id)
    await message.answer("🗑 Все данные удалены. Чтобы начать заново — /start")


# ─────────────────────────────────────────────
# Фоновая очистка логов
# ─────────────────────────────────────────────

async def cleanup_loop() -> None:
    while True:
        try:
            await db_cleanup_old_logs()
        except Exception as e:
            logger.error(f"Ошибка очистки: {e}")
        await asyncio.sleep(86_400)


# ─────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────

async def main() -> None:
    await db_init()
    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(cleanup_loop())
    logger.info("Бот запущен.")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
