"""
Telegram-бот: Калькулятор калорий
Библиотека: aiogram 3.x
База данных: SQLite (aiosqlite)

Установка зависимостей:
    pip install aiogram aiosqlite

Запуск:
    Замените BOT_TOKEN на токен вашего бота (получить у @BotFather)
    python calorie_bot.py
"""

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ─────────────────────────────────────────────
# Настройки
# ─────────────────────────────────────────────

import os
BOT_TOKEN = os.getenv("BOT_TOKEN")  # токен берётся из переменной окружения
DB_PATH = "calories.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# FSM: состояния
# ─────────────────────────────────────────────

class Registration(StatesGroup):
    """Шаги регистрации нового пользователя."""
    height = State()
    weight = State()
    gender = State()
    age    = State()
    goal   = State()


class FoodLog(StatesGroup):
    """Шаги ввода еды (калории → БЖУ)."""
    calories = State()
    macros   = State()


# ─────────────────────────────────────────────
# База данных
# ─────────────────────────────────────────────

async def db_init() -> None:
    """Создаёт таблицы, если их ещё нет."""
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
    """Удаляет записи логов старше 7 дней."""
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE date < ?", (cutoff,))
        await db.commit()


async def db_upsert_user(
    user_id: int,
    height: float,
    weight: float,
    gender: str,
    age: int,
    goal: int,
    calorie_norm: float,
) -> None:
    """Сохраняет или обновляет данные пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, height, weight, gender, age, goal, calorie_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                height       = excluded.height,
                weight       = excluded.weight,
                gender       = excluded.gender,
                age          = excluded.age,
                goal         = excluded.goal,
                calorie_norm = excluded.calorie_norm
        """, (user_id, height, weight, gender, age, goal, calorie_norm))
        await db.commit()


async def db_get_user(user_id: int) -> dict | None:
    """Возвращает данные пользователя или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def db_delete_user(user_id: int) -> None:
    """Удаляет пользователя и все его логи."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM logs WHERE user_id = ?", (user_id,))
        await db.commit()


async def db_add_log(
    user_id: int,
    calories: float,
    protein: float,
    fat: float,
    carbs: float,
) -> None:
    """Добавляет запись о еде за сегодня."""
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO logs (user_id, date, calories, protein, fat, carbs)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, today, calories, protein, fat, carbs))
        await db.commit()


async def db_get_today(user_id: int) -> dict:
    """Возвращает суммарные калории и БЖУ за сегодня."""
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT
                COALESCE(SUM(calories), 0) AS calories,
                COALESCE(SUM(protein),  0) AS protein,
                COALESCE(SUM(fat),      0) AS fat,
                COALESCE(SUM(carbs),    0) AS carbs
            FROM logs
            WHERE user_id = ? AND date = ?
        """, (user_id, today)) as cursor:
            row = await cursor.fetchone()
            return {
                "calories": row[0],
                "protein":  row[1],
                "fat":      row[2],
                "carbs":    row[3],
            }


async def db_get_week(user_id: int) -> list[dict]:
    """Возвращает статистику по дням за последние 7 дней."""
    cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                date,
                SUM(calories) AS calories,
                SUM(protein)  AS protein,
                SUM(fat)      AS fat,
                SUM(carbs)    AS carbs
            FROM logs
            WHERE user_id = ? AND date >= ?
            GROUP BY date
            ORDER BY date
        """, (user_id, cutoff)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def calc_calorie_norm(
    weight: float,
    height: float,
    age: int,
    gender: str,
    goal: int,
) -> float:
    """
    Формула Mifflin-St Jeor + коэффициент активности 1.2 + цель.
    goal: 1 — похудение (×0.8), 2 — поддержание (×1.0), 3 — набор (×1.2)
    """
    if gender == "м":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161

    tdee = bmr * 1.2  # минимальная активность

    goal_multipliers = {1: 0.8, 2: 1.0, 3: 1.2}
    return round(tdee * goal_multipliers.get(goal, 1.0), 1)


def goal_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура выбора цели."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="1 — Похудение"),
                KeyboardButton(text="2 — Поддержание"),
                KeyboardButton(text="3 — Набор массы"),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gender_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура выбора пола."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="М"),
                KeyboardButton(text="Ж"),
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


GOAL_LABELS = {1: "Похудение", 2: "Поддержание", 3: "Набор массы"}

remove_kb = ReplyKeyboardRemove()


# ─────────────────────────────────────────────
# Роутер и хендлеры
# ─────────────────────────────────────────────

router = Router()


# ── /start ────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Начало регистрации."""
    await state.clear()
    await message.answer(
        "👋 Привет! Я калькулятор калорий.\n\n"
        "Давай заполним данные. Введи свой <b>рост в см</b>:",
        reply_markup=remove_kb,
    )
    await state.set_state(Registration.height)


# ── Регистрация: рост ─────────────────────────

@router.message(Registration.height)
async def reg_height(message: Message, state: FSMContext) -> None:
    try:
        height = float(message.text.replace(",", "."))
        if not (100 <= height <= 250):
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи рост числом от 100 до 250. Пример: <b>175</b>")
        return

    await state.update_data(height=height)
    await message.answer("Введи свой <b>вес в кг</b>:")
    await state.set_state(Registration.weight)


# ── Регистрация: вес ──────────────────────────

@router.message(Registration.weight)
async def reg_weight(message: Message, state: FSMContext) -> None:
    try:
        weight = float(message.text.replace(",", "."))
        if not (30 <= weight <= 300):
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи вес числом от 30 до 300. Пример: <b>70</b>")
        return

    await state.update_data(weight=weight)
    await message.answer("Выбери <b>пол</b>:", reply_markup=gender_keyboard())
    await state.set_state(Registration.gender)


# ── Регистрация: пол ──────────────────────────

@router.message(Registration.gender)
async def reg_gender(message: Message, state: FSMContext) -> None:
    gender = message.text.strip().lower()
    if gender not in ("м", "ж", "м", "ж"):  # учёт заглавных
        gender = gender[0] if gender else ""
    if gender not in ("м", "ж"):
        await message.answer(
            "⚠️ Выбери пол с помощью кнопок ниже.",
            reply_markup=gender_keyboard(),
        )
        return

    await state.update_data(gender=gender)
    await message.answer("Введи свой <b>возраст</b> (лет):", reply_markup=remove_kb)
    await state.set_state(Registration.age)


# ── Регистрация: возраст ──────────────────────

@router.message(Registration.age)
async def reg_age(message: Message, state: FSMContext) -> None:
    try:
        age = int(message.text.strip())
        if not (10 <= age <= 120):
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введи возраст числом от 10 до 120. Пример: <b>25</b>")
        return

    await state.update_data(age=age)
    await message.answer(
        "Выбери <b>цель</b>:",
        reply_markup=goal_keyboard(),
    )
    await state.set_state(Registration.goal)


# ── Регистрация: цель ─────────────────────────

@router.message(Registration.goal)
async def reg_goal(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    # Принимаем и "1", и "1 — Похудение"
    goal_map = {"1": 1, "2": 2, "3": 3}
    goal = goal_map.get(text[0]) if text else None

    if goal is None:
        await message.answer(
            "⚠️ Выбери цель с помощью кнопок.",
            reply_markup=goal_keyboard(),
        )
        return

    data = await state.get_data()
    calorie_norm = calc_calorie_norm(
        weight=data["weight"],
        height=data["height"],
        age=data["age"],
        gender=data["gender"],
        goal=goal,
    )

    await db_upsert_user(
        user_id=message.from_user.id,
        height=data["height"],
        weight=data["weight"],
        gender=data["gender"],
        age=data["age"],
        goal=goal,
        calorie_norm=calorie_norm,
    )

    await state.clear()
    await message.answer(
        f"✅ Данные сохранены!\n\n"
        f"🎯 Цель: <b>{GOAL_LABELS[goal]}</b>\n"
        f"🔥 Дневная норма: <b>{calorie_norm} ккал</b>\n\n"
        f"Теперь отправляй приёмы пищи:\n"
        f"<b>Строка 1:</b> калории (число)\n"
        f"<b>Строка 2:</b> Б Ж У через пробел\n\n"
        f"Пример:\n<code>450\n30 10 50</code>",
        reply_markup=remove_kb,
    )


# ── Ввод еды: калории ─────────────────────────

@router.message(FoodLog.calories)
async def food_calories(message: Message, state: FSMContext) -> None:
    try:
        calories = float(message.text.strip().replace(",", "."))
        if calories < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "⚠️ Введи калории числом (≥ 0). Пример: <code>450</code>"
        )
        return

    await state.update_data(calories=calories)
    await message.answer(
        "Теперь введи <b>Б Ж У</b> через пробел (граммы):\n"
        "Пример: <code>30 10 50</code>"
    )
    await state.set_state(FoodLog.macros)


# ── Ввод еды: БЖУ ────────────────────────────

@router.message(FoodLog.macros)
async def food_macros(message: Message, state: FSMContext) -> None:
    try:
        parts = message.text.strip().split()
        if len(parts) != 3:
            raise ValueError
        protein, fat, carbs = (float(p.replace(",", ".")) for p in parts)
        if any(v < 0 for v in (protein, fat, carbs)):
            raise ValueError
    except ValueError:
        await message.answer(
            "⚠️ Введи три числа через пробел (Б Ж У). Пример: <code>30 10 50</code>"
        )
        return

    data = await state.get_data()
    await state.clear()

    user_id = message.from_user.id

    # Сохраняем лог
    await db_add_log(
        user_id=user_id,
        calories=data["calories"],
        protein=protein,
        fat=fat,
        carbs=carbs,
    )

    # Получаем итог за день
    user = await db_get_user(user_id)
    today = await db_get_today(user_id)

    norm = user["calorie_norm"] if user else 0
    eaten = today["calories"]
    left = round(norm - eaten, 1)

    await message.answer(
        f"<b>Сегодня:</b> {round(eaten, 1)} ккал\n"
        f"<b>Осталось:</b> {left} ккал\n"
        f"Б: {round(today['protein'], 1)}\n"
        f"Ж: {round(today['fat'], 1)}\n"
        f"У: {round(today['carbs'], 1)}"
    )


# ── Обычное сообщение → начало ввода еды ─────

@router.message(F.text & ~F.text.startswith("/"))
async def food_entry_start(message: Message, state: FSMContext) -> None:
    """
    Если пользователь не в FSM и пишет текст —
    пытаемся трактовать как начало ввода калорий.
    """
    current = await state.get_state()
    if current is not None:
        # Уже в каком-то состоянии — не перехватываем
        return

    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Сначала зарегистрируйся: /start"
        )
        return

    # Пробуем распарсить калории сразу
    try:
        calories = float(message.text.strip().replace(",", "."))
        if calories < 0:
            raise ValueError
        await state.update_data(calories=calories)
        await message.answer(
            "Теперь введи <b>Б Ж У</b> через пробел:\n"
            "Пример: <code>30 10 50</code>"
        )
        await state.set_state(FoodLog.macros)
    except ValueError:
        # Не число — объясняем формат
        await message.answer(
            "Чтобы записать приём пищи, отправь двумя строками:\n"
            "<b>Строка 1:</b> калории\n"
            "<b>Строка 2:</b> Б Ж У через пробел\n\n"
            "Пример:\n<code>450\n30 10 50</code>"
        )


# ── /today ────────────────────────────────────

@router.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await db_get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся: /start")
        return

    today = await db_get_today(message.from_user.id)
    norm = user["calorie_norm"]
    eaten = today["calories"]
    left = round(norm - eaten, 1)

    await message.answer(
        f"📊 <b>Сегодня, {date.today().strftime('%d.%m.%Y')}</b>\n\n"
        f"Норма:    {norm} ккал\n"
        f"Съедено:  {round(eaten, 1)} ккал\n"
        f"Осталось: {left} ккал\n\n"
        f"Б: {round(today['protein'], 1)}\n"
        f"Ж: {round(today['fat'], 1)}\n"
        f"У: {round(today['carbs'], 1)}"
    )


# ── /week ─────────────────────────────────────

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

    norm = user["calorie_norm"]
    lines = ["📅 <b>Статистика за 7 дней</b>\n"]
    total_cal = total_p = total_f = total_c = 0.0

    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m")
        cal = round(r["calories"], 1)
        total_cal += cal
        total_p += r["protein"]
        total_f += r["fat"]
        total_c += r["carbs"]
        diff = round(norm - cal, 1)
        sign = "+" if diff >= 0 else ""
        lines.append(
            f"<b>{d}</b>: {cal} ккал  "
            f"(баланс: {sign}{diff})  "
            f"Б{round(r['protein'],1)} Ж{round(r['fat'],1)} У{round(r['carbs'],1)}"
        )

    days = len(rows)
    lines.append(
        f"\n<b>Итого:</b> {round(total_cal, 1)} ккал за {days} дн.\n"
        f"<b>Среднее:</b> {round(total_cal / days, 1)} ккал/день\n"
        f"Б: {round(total_p, 1)}  Ж: {round(total_f, 1)}  У: {round(total_c, 1)}"
    )

    await message.answer("\n".join(lines))


# ── /reset ────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db_delete_user(message.from_user.id)
    await message.answer(
        "🗑 Все данные удалены. Чтобы начать заново — /start"
    )


# ─────────────────────────────────────────────
# Фоновая задача: очистка старых логов
# ─────────────────────────────────────────────

async def cleanup_loop() -> None:
    """Каждые 24 часа удаляет логи старше 7 дней."""
    while True:
        try:
            await db_cleanup_old_logs()
            logger.info("Старые логи очищены.")
        except Exception as e:
            logger.error(f"Ошибка при очистке логов: {e}")
        await asyncio.sleep(86_400)  # 24 часа


# ─────────────────────────────────────────────
# Точка входа
# ─────────────────────────────────────────────

async def main() -> None:
    # Инициализация БД
    await db_init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Запускаем фоновую очистку параллельно
    asyncio.create_task(cleanup_loop())

    logger.info("Бот запущен.")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
