"""
Telegram-бот: Калькулятор калорий
Только ручной ввод — просто и надёжно.

Зависимости:
    pip install aiogram aiosqlite

Переменные Railway:
    BOT_TOKEN — токен от @BotFather
"""

import asyncio
import logging
import os
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
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH   = "calories.db"
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# FSM
# ══════════════════════════════════════════════

class Reg(StatesGroup):
    """Регистрация пользователя по шагам."""
    height = State()
    weight = State()
    gender = State()
    age    = State()
    goal   = State()


class Log(StatesGroup):
    """Ввод приёма пищи."""
    waiting = State()   # ждём: "калории Б Ж У" одной строкой


# ══════════════════════════════════════════════
# БД
# ══════════════════════════════════════════════

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                height       REAL,
                weight       REAL,
                gender       TEXT,
                age          INTEGER,
                goal         INTEGER,
                calorie_norm REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER,
                date     TEXT,
                calories REAL,
                protein  REAL,
                fat      REAL,
                carbs    REAL
            )
        """)
        await db.commit()


async def db_cleanup():
    """Удаляет логи старше 7 дней."""
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE date < ?", (cutoff,))
        await db.commit()


async def db_save_user(uid, height, weight, gender, age, goal, norm):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                height=excluded.height, weight=excluded.weight,
                gender=excluded.gender, age=excluded.age,
                goal=excluded.goal, calorie_norm=excluded.calorie_norm
        """, (uid, height, weight, gender, age, goal, norm))
        await db.commit()


async def db_get_user(uid) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ) as c:
            r = await c.fetchone()
            return dict(r) if r else None


async def db_del_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM logs  WHERE user_id=?", (uid,))
        await db.commit()


async def db_add_log(uid, cal, p, f, c):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(user_id,date,calories,protein,fat,carbs)"
            " VALUES(?,?,?,?,?,?)",
            (uid, date.today().strftime("%Y-%m-%d"), cal, p, f, c)
        )
        await db.commit()


async def db_today(uid) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein),0),
                   COALESCE(SUM(fat),0),     COALESCE(SUM(carbs),0)
            FROM logs WHERE user_id=? AND date=?
        """, (uid, date.today().strftime("%Y-%m-%d"))) as c:
            r = await c.fetchone()
            return {"cal": r[0], "p": r[1], "f": r[2], "c": r[3]}


async def db_week(uid) -> list:
    cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT date,
                   SUM(calories) cal, SUM(protein) p,
                   SUM(fat) f,        SUM(carbs) c
            FROM logs WHERE user_id=? AND date>=?
            GROUP BY date ORDER BY date
        """, (uid, cutoff)) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ══════════════════════════════════════════════
# Расчёт нормы калорий
# ══════════════════════════════════════════════

def calc_norm(weight, height, age, gender, goal) -> float:
    """Формула Mifflin-St Jeor + активность 1.2 + цель."""
    bmr = 10*weight + 6.25*height - 5*age + (5 if gender == "м" else -161)
    mult = {1: 0.8, 2: 1.0, 3: 1.2}[goal]
    return round(bmr * 1.2 * mult, 1)


# ══════════════════════════════════════════════
# Клавиатуры
# ══════════════════════════════════════════════

def kb_gender():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="М"), KeyboardButton(text="Ж")]],
        resize_keyboard=True, one_time_keyboard=True
    )

def kb_goal():
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="1 — Похудение"),
            KeyboardButton(text="2 — Поддержание"),
            KeyboardButton(text="3 — Набор массы"),
        ]],
        resize_keyboard=True, one_time_keyboard=True
    )

GOALS = {1: "Похудение", 2: "Поддержание", 3: "Набор массы"}
no_kb = ReplyKeyboardRemove()


# ══════════════════════════════════════════════
# Роутер
# ══════════════════════════════════════════════

router = Router()


# ── /start ────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 Привет! Я считаю калории.\n\n"
        "Давай заполним твои данные.\n"
        "Введи <b>рост в см</b>:",
        reply_markup=no_kb
    )
    await state.set_state(Reg.height)


# ── Регистрация ───────────────────────────────

@router.message(Reg.height)
async def reg_height(msg: Message, state: FSMContext):
    try:
        h = float(msg.text.replace(",", "."))
        assert 100 <= h <= 250
    except Exception:
        await msg.answer("⚠️ Введи число от 100 до 250.\nПример: <b>175</b>")
        return
    await state.update_data(height=h)
    await msg.answer("Введи <b>вес в кг</b>:")
    await state.set_state(Reg.weight)


@router.message(Reg.weight)
async def reg_weight(msg: Message, state: FSMContext):
    try:
        w = float(msg.text.replace(",", "."))
        assert 30 <= w <= 300
    except Exception:
        await msg.answer("⚠️ Введи число от 30 до 300.\nПример: <b>70</b>")
        return
    await state.update_data(weight=w)
    await msg.answer("Выбери <b>пол</b>:", reply_markup=kb_gender())
    await state.set_state(Reg.gender)


@router.message(Reg.gender)
async def reg_gender(msg: Message, state: FSMContext):
    g = msg.text.strip().lower()
    if g not in ("м", "ж"):
        await msg.answer("⚠️ Нажми кнопку <b>М</b> или <b>Ж</b>.",
                         reply_markup=kb_gender())
        return
    await state.update_data(gender=g)
    await msg.answer("Введи <b>возраст</b>:", reply_markup=no_kb)
    await state.set_state(Reg.age)


@router.message(Reg.age)
async def reg_age(msg: Message, state: FSMContext):
    try:
        a = int(msg.text.strip())
        assert 10 <= a <= 120
    except Exception:
        await msg.answer("⚠️ Введи число от 10 до 120.\nПример: <b>25</b>")
        return
    await state.update_data(age=a)
    await msg.answer("Выбери <b>цель</b>:", reply_markup=kb_goal())
    await state.set_state(Reg.goal)


@router.message(Reg.goal)
async def reg_goal(msg: Message, state: FSMContext):
    goal_map = {"1": 1, "2": 2, "3": 3}
    goal = goal_map.get(msg.text.strip()[0] if msg.text.strip() else "")
    if not goal:
        await msg.answer("⚠️ Нажми одну из кнопок.", reply_markup=kb_goal())
        return

    d    = await state.get_data()
    norm = calc_norm(d["weight"], d["height"], d["age"], d["gender"], goal)
    await db_save_user(msg.from_user.id, d["height"], d["weight"],
                       d["gender"], d["age"], goal, norm)
    await state.clear()

    await msg.answer(
        f"✅ Готово!\n\n"
        f"🎯 Цель: <b>{GOALS[goal]}</b>\n"
        f"🔥 Норма: <b>{norm} ккал/день</b>\n\n"
        f"Чтобы записать еду — отправь одним сообщением:\n"
        f"<code>калории белки жиры углеводы</code>\n\n"
        f"Пример: <code>450 30 10 50</code>",
        reply_markup=no_kb
    )


# ── Ввод еды ──────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message, state: FSMContext):
    """
    Пользователь пишет: 450 30 10 50
    Бот сохраняет и показывает итог дня.
    """
    # Если идёт регистрация — не перехватываем
    if await state.get_state() is not None:
        return

    user = await db_get_user(msg.from_user.id)
    if not user:
        await msg.answer("Сначала зарегистрируйся: /start")
        return

    # Парсим 4 числа: калории белки жиры углеводы
    try:
        parts = msg.text.strip().split()
        assert len(parts) == 4
        cal, p, f, c = (float(x.replace(",", ".")) for x in parts)
        assert all(v >= 0 for v in (cal, p, f, c))
    except Exception:
        await msg.answer(
            "⚠️ Отправь <b>4 числа через пробел</b>:\n"
            "<code>калории белки жиры углеводы</code>\n\n"
            "Пример: <code>450 30 10 50</code>"
        )
        return

    # Сохраняем
    await db_add_log(msg.from_user.id, cal, p, f, c)

    # Считаем итог дня
    today = await db_today(msg.from_user.id)
    norm  = user["calorie_norm"]
    eaten = today["cal"]
    left  = round(norm - eaten, 1)

    await msg.answer(
        f"✅ Записано!\n\n"
        f"<b>Сегодня:</b> {round(eaten, 1)} ккал\n"
        f"<b>Осталось:</b> {left} ккал\n\n"
        f"Б: {round(today['p'], 1)}\n"
        f"Ж: {round(today['f'], 1)}\n"
        f"У: {round(today['c'], 1)}"
    )


# ── /today ────────────────────────────────────

@router.message(Command("today"))
async def cmd_today(msg: Message, state: FSMContext):
    await state.clear()
    user = await db_get_user(msg.from_user.id)
    if not user:
        await msg.answer("Сначала зарегистрируйся: /start")
        return

    today = await db_today(msg.from_user.id)
    norm  = user["calorie_norm"]
    eaten = today["cal"]

    await msg.answer(
        f"📊 <b>{date.today().strftime('%d.%m.%Y')}</b>\n\n"
        f"Норма:    {norm} ккал\n"
        f"Съедено:  {round(eaten, 1)} ккал\n"
        f"Осталось: {round(norm - eaten, 1)} ккал\n\n"
        f"Б: {round(today['p'], 1)}\n"
        f"Ж: {round(today['f'], 1)}\n"
        f"У: {round(today['c'], 1)}"
    )


# ── /week ─────────────────────────────────────

@router.message(Command("week"))
async def cmd_week(msg: Message, state: FSMContext):
    await state.clear()
    user = await db_get_user(msg.from_user.id)
    if not user:
        await msg.answer("Сначала зарегистрируйся: /start")
        return

    rows = await db_week(msg.from_user.id)
    if not rows:
        await msg.answer("За последние 7 дней нет записей.")
        return

    norm = user["calorie_norm"]
    lines = ["📅 <b>Статистика за 7 дней</b>\n"]
    tc = tp = tf = tcc = 0.0

    for r in rows:
        d   = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m")
        cal = round(r["cal"], 1)
        tc += cal; tp += r["p"]; tf += r["f"]; tcc += r["c"]
        diff = round(norm - cal, 1)
        sign = "+" if diff >= 0 else ""
        lines.append(
            f"<b>{d}</b>: {cal} ккал ({sign}{diff})"
            f"  Б{round(r['p'],1)} Ж{round(r['f'],1)} У{round(r['c'],1)}"
        )

    n = len(rows)
    lines.append(
        f"\n<b>Итого:</b> {round(tc,1)} ккал · {n} дн.\n"
        f"<b>Среднее:</b> {round(tc/n,1)} ккал/день\n"
        f"Б{round(tp,1)}  Ж{round(tf,1)}  У{round(tcc,1)}"
    )
    await msg.answer("\n".join(lines))


# ── /reset ────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(msg: Message, state: FSMContext):
    await state.clear()
    await db_del_user(msg.from_user.id)
    await msg.answer("🗑 Данные удалены.\nНачать заново: /start")


# ── /help ─────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "📖 <b>Как пользоваться</b>\n\n"
        "Чтобы записать приём пищи — отправь <b>4 числа через пробел</b>:\n"
        "<code>калории белки жиры углеводы</code>\n\n"
        "Пример: <code>450 30 10 50</code>\n\n"
        "<b>Команды:</b>\n"
        "/today — статистика за сегодня\n"
        "/week  — статистика за 7 дней\n"
        "/reset — удалить все данные\n"
        "/start — изменить профиль"
    )


# ══════════════════════════════════════════════
# Фоновая очистка и запуск
# ══════════════════════════════════════════════

async def cleanup_loop():
    while True:
        try:
            await db_cleanup()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(86400)


async def main():
    await db_init()
    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(cleanup_loop())
    logger.info("Бот запущен!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
