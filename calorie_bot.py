"""
Telegram-бот: Калькулятор калорий
Зависимости: pip install aiogram aiosqlite aiohttp
Переменные Railway: BOT_TOKEN, HF_TOKEN
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

from foods import get_food, hf_to_ru, search_food

# ─────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
HF_TOKEN     = os.getenv("HF_TOKEN", "")
DB_PATH      = "calories.db"
HF_MODEL_URL = "https://api-inference.huggingface.co/models/nateraw/food"
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# FSM
# ══════════════════════════════════════════════

class Reg(StatesGroup):
    height = State()
    weight = State()
    gender = State()
    age    = State()
    goal   = State()


class Food(StatesGroup):
    method   = State()   # выбор способа
    cal      = State()   # ручной: калории
    macros   = State()   # ручной: БЖУ
    photo    = State()   # ждём фото
    search   = State()   # ввод названия
    grams    = State()   # ввод граммов (после выбора блюда)


# ══════════════════════════════════════════════
# БД
# ══════════════════════════════════════════════

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, height REAL, weight REAL,
            gender TEXT, age INTEGER, goal INTEGER, calorie_norm REAL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            date TEXT, calories REAL, protein REAL, fat REAL, carbs REAL)""")
        await db.commit()


async def db_cleanup():
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE date<?", (cutoff,))
        await db.commit()


async def db_save_user(uid, height, weight, gender, age, goal, norm):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO users VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
            height=excluded.height, weight=excluded.weight,
            gender=excluded.gender, age=excluded.age,
            goal=excluded.goal, calorie_norm=excluded.calorie_norm""",
            (uid, height, weight, gender, age, goal, norm))
        await db.commit()


async def db_get_user(uid) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
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
            "INSERT INTO logs(user_id,date,calories,protein,fat,carbs) VALUES(?,?,?,?,?,?)",
            (uid, date.today().strftime("%Y-%m-%d"), cal, p, f, c))
        await db.commit()


async def db_today(uid) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein),0),
                   COALESCE(SUM(fat),0), COALESCE(SUM(carbs),0)
            FROM logs WHERE user_id=? AND date=?""",
            (uid, date.today().strftime("%Y-%m-%d"))) as c:
            r = await c.fetchone()
            return {"cal": r[0], "p": r[1], "f": r[2], "c": r[3]}


async def db_week(uid) -> list:
    cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT date, SUM(calories) cal, SUM(protein) p,
                   SUM(fat) f, SUM(carbs) c
            FROM logs WHERE user_id=? AND date>=?
            GROUP BY date ORDER BY date""", (uid, cutoff)) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ══════════════════════════════════════════════
# HuggingFace
# ══════════════════════════════════════════════

async def hf_recognize(image_bytes: bytes) -> str | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(HF_MODEL_URL,
                              headers={"Authorization": f"Bearer {HF_TOKEN}"},
                              data=image_bytes,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                if isinstance(data, list) and data:
                    return data[0].get("label", "").replace("_", " ")
    except Exception as e:
        logger.error(f"HF error: {e}")
    return None


# ══════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════

def calc_norm(weight, height, age, gender, goal) -> float:
    bmr = 10*weight + 6.25*height - 5*age + (5 if gender == "м" else -161)
    return round(bmr * 1.2 * {1: 0.8, 2: 1.0, 3: 1.2}[goal], 1)


def scale(nutrition, grams) -> tuple:
    k = grams / 100
    return tuple(round(v * k, 1) for v in nutrition)


# ── Клавиатуры ────────────────────────────────

def kb_gender():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="М"), KeyboardButton(text="Ж")]],
        resize_keyboard=True, one_time_keyboard=True)


def kb_goal():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="1 — Похудение"),
                   KeyboardButton(text="2 — Поддержание"),
                   KeyboardButton(text="3 — Набор массы")]],
        resize_keyboard=True, one_time_keyboard=True)


def kb_method():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти блюдо",    callback_data="m:search"),
         InlineKeyboardButton(text="📸 Фото",           callback_data="m:photo")],
        [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="m:manual")],
    ])


def kb_results(results):
    rows = []
    for name, (cal, *_) in results:
        rows.append([InlineKeyboardButton(
            text=f"{name.capitalize()} · {cal} ккал/100г",
            callback_data=f"pick:{name}")])
    rows.append([InlineKeyboardButton(
        text="✍️ Ввести вручную", callback_data="m:manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_photo_confirm(ru_name):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, это оно",      callback_data=f"pick:{ru_name}")],
        [InlineKeyboardButton(text="🔍 Найти другое",     callback_data="m:search")],
        [InlineKeyboardButton(text="✍️ Ввести вручную",   callback_data="m:manual")],
    ])


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
    await msg.answer("👋 Привет! Я калькулятор калорий.\n\n"
                     "Введи свой <b>рост в см</b>:", reply_markup=no_kb)
    await state.set_state(Reg.height)


@router.message(Reg.height)
async def reg_h(msg: Message, state: FSMContext):
    try:
        h = float(msg.text.replace(",", "."))
        assert 100 <= h <= 250
    except Exception:
        await msg.answer("⚠️ Введи рост от 100 до 250. Пример: <b>175</b>")
        return
    await state.update_data(height=h)
    await msg.answer("Теперь введи <b>вес в кг</b>:")
    await state.set_state(Reg.weight)


@router.message(Reg.weight)
async def reg_w(msg: Message, state: FSMContext):
    try:
        w = float(msg.text.replace(",", "."))
        assert 30 <= w <= 300
    except Exception:
        await msg.answer("⚠️ Введи вес от 30 до 300. Пример: <b>70</b>")
        return
    await state.update_data(weight=w)
    await msg.answer("Выбери <b>пол</b>:", reply_markup=kb_gender())
    await state.set_state(Reg.gender)


@router.message(Reg.gender)
async def reg_g(msg: Message, state: FSMContext):
    g = msg.text.strip().lower()
    if g not in ("м", "ж"):
        await msg.answer("⚠️ Нажми кнопку М или Ж.", reply_markup=kb_gender())
        return
    await state.update_data(gender=g)
    await msg.answer("Введи <b>возраст</b> (лет):", reply_markup=no_kb)
    await state.set_state(Reg.age)


@router.message(Reg.age)
async def reg_a(msg: Message, state: FSMContext):
    try:
        a = int(msg.text.strip())
        assert 10 <= a <= 120
    except Exception:
        await msg.answer("⚠️ Введи возраст от 10 до 120. Пример: <b>25</b>")
        return
    await state.update_data(age=a)
    await msg.answer("Выбери <b>цель</b>:", reply_markup=kb_goal())
    await state.set_state(Reg.goal)


@router.message(Reg.goal)
async def reg_goal(msg: Message, state: FSMContext):
    g = {"1": 1, "2": 2, "3": 3}.get(msg.text.strip()[0] if msg.text.strip() else "")
    if not g:
        await msg.answer("⚠️ Нажми одну из кнопок.", reply_markup=kb_goal())
        return
    d = await state.get_data()
    norm = calc_norm(d["weight"], d["height"], d["age"], d["gender"], g)
    await db_save_user(msg.from_user.id, d["height"], d["weight"],
                       d["gender"], d["age"], g, norm)
    await state.clear()
    await msg.answer(
        f"✅ Готово!\n\n"
        f"🎯 Цель: <b>{GOALS[g]}</b>\n"
        f"🔥 Норма: <b>{norm} ккал/день</b>\n\n"
        f"Напиши мне или отправь фото — добавим еду.", reply_markup=no_kb)


# ── Старт ввода еды ───────────────────────────

async def ask_method(msg: Message, state: FSMContext):
    await state.set_state(Food.method)
    await msg.answer("Как добавить еду?", reply_markup=kb_method())


# Любой текст вне FSM → меню
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(msg: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if not await db_get_user(msg.from_user.id):
        await msg.answer("Сначала зарегистрируйся: /start")
        return
    await ask_method(msg, state)


# Фото вне FSM → сразу распознаём
@router.message(F.photo)
async def on_photo_outer(msg: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    if not await db_get_user(msg.from_user.id):
        await msg.answer("Сначала зарегистрируйся: /start")
        return
    await state.set_state(Food.photo)
    await handle_photo(msg, state)


# ── Callbacks: выбор метода ───────────────────

@router.callback_query(F.data == "m:manual")
async def cb_manual(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup()
    await cb.message.answer("Введи <b>калории</b>:\nПример: <code>450</code>")
    await state.set_state(Food.cal)


@router.callback_query(F.data == "m:photo")
async def cb_photo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup()
    await cb.message.answer("📸 Отправь фото блюда:")
    await state.set_state(Food.photo)


@router.callback_query(F.data == "m:search")
async def cb_search(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup()
    await cb.message.answer(
        "🔍 Напиши название блюда:\n"
        "Пример: <code>гречка</code>, <code>плов</code>, <code>курица</code>")
    await state.set_state(Food.search)


# ── Поиск блюда ───────────────────────────────

@router.message(Food.search)
async def on_search(msg: Message, state: FSMContext):
    results = search_food(msg.text.strip())
    if not results:
        await msg.answer(
            f"😔 <b>{msg.text}</b> не найдено.\n"
            "Попробуй другое название:", reply_markup=kb_method())
        await state.set_state(Food.method)
        return
    await msg.answer("Выбери блюдо:", reply_markup=kb_results(results))
    # Остаёмся в search чтобы дождаться callback


# ── Выбор блюда из списка ─────────────────────

@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup()

    food_name = cb.data[5:]  # убираем "pick:"
    nutrition = get_food(food_name)

    if not nutrition:
        await cb.message.answer("⚠️ Не нашёл блюдо. Попробуй снова.")
        await ask_method(cb.message, state)
        return

    cal100, p100, f100, c100 = nutrition
    await state.update_data(nutrition=list(nutrition))
    await cb.message.answer(
        f"<b>{food_name.capitalize()}</b> — {cal100} ккал на 100г\n\n"
        f"⚖️ Сколько грамм ты съел?\nПример: <code>200</code>")
    await state.set_state(Food.grams)


# ── Ввод граммов ──────────────────────────────

@router.message(Food.grams)
async def on_grams(msg: Message, state: FSMContext):
    try:
        grams = float(msg.text.strip().replace(",", "."))
        assert 1 <= grams <= 5000
    except Exception:
        await msg.answer("⚠️ Введи вес в граммах. Пример: <code>200</code>")
        return
    data = await state.get_data()
    cal, p, f, c = scale(data["nutrition"], grams)
    await state.clear()
    await record_and_show(msg, cal, p, f, c)


# ── Ручной ввод: калории → БЖУ ───────────────

@router.message(Food.cal)
async def on_cal(msg: Message, state: FSMContext):
    try:
        cal = float(msg.text.strip().replace(",", "."))
        assert cal >= 0
    except Exception:
        await msg.answer("⚠️ Введи число. Пример: <code>450</code>")
        return
    await state.update_data(cal=cal)
    await msg.answer("Теперь <b>Б Ж У</b> через пробел:\nПример: <code>30 10 50</code>")
    await state.set_state(Food.macros)


@router.message(Food.macros)
async def on_macros(msg: Message, state: FSMContext):
    try:
        p, f, c = (float(x.replace(",", ".")) for x in msg.text.strip().split())
        assert all(v >= 0 for v in (p, f, c))
    except Exception:
        await msg.answer("⚠️ Три числа через пробел. Пример: <code>30 10 50</code>")
        return
    data = await state.get_data()
    await state.clear()
    await record_and_show(msg, data["cal"], p, f, c)


# ── Обработка фото ────────────────────────────

@router.message(Food.photo, F.photo)
async def handle_photo(msg: Message, state: FSMContext):
    await state.clear()

    if not HF_TOKEN:
        await msg.answer("⚠️ HF_TOKEN не задан в Railway Variables.")
        await ask_method(msg, state)
        return

    wait = await msg.answer("🔍 Распознаю...")

    photo = msg.photo[-1]
    file  = await msg.bot.get_file(photo.file_id)
    buf   = await msg.bot.download_file(file.file_path)
    label = await hf_recognize(buf.read())
    await wait.delete()

    if not label:
        await msg.answer("😔 Не удалось распознать. Попробуй ещё раз.",
                         reply_markup=kb_method())
        await state.set_state(Food.method)
        return

    ru = hf_to_ru(label)
    nutrition = get_food(ru) if ru else None

    if not ru or not nutrition:
        await msg.answer(
            f"🤔 Вижу <b>{label}</b>, но нет в базе.\n"
            "Найди вручную:", reply_markup=kb_method())
        await state.set_state(Food.method)
        return

    cal100, p100, f100, c100 = nutrition
    await state.update_data(nutrition=list(nutrition))

    await msg.answer(
        f"🍽 Похоже это <b>{ru.capitalize()}</b>\n"
        f"На 100г: {cal100} ккал · Б{p100} Ж{f100} У{c100}\n\n"
        f"Это верно?",
        reply_markup=kb_photo_confirm(ru))


@router.message(Food.photo)
async def photo_wrong(msg: Message):
    await msg.answer("📸 Отправь именно <b>фото</b>.")


# ── Запись и показ итога ──────────────────────

async def record_and_show(msg: Message, cal, p, f, c):
    uid   = msg.from_user.id
    await db_add_log(uid, cal, p, f, c)
    user  = await db_get_user(uid)
    today = await db_today(uid)
    norm  = user["calorie_norm"] if user else 0

    await msg.answer(
        f"✅ Записано!\n\n"
        f"<b>Сегодня:</b> {round(today['cal'],1)} ккал\n"
        f"<b>Осталось:</b> {round(norm - today['cal'],1)} ккал\n"
        f"Б: {round(today['p'],1)}\n"
        f"Ж: {round(today['f'],1)}\n"
        f"У: {round(today['c'],1)}")


# ── /today ────────────────────────────────────

@router.message(Command("today"))
async def cmd_today(msg: Message, state: FSMContext):
    await state.clear()
    user = await db_get_user(msg.from_user.id)
    if not user:
        await msg.answer("Сначала зарегистрируйся: /start")
        return
    t    = await db_today(msg.from_user.id)
    norm = user["calorie_norm"]
    await msg.answer(
        f"📊 <b>{date.today().strftime('%d.%m.%Y')}</b>\n\n"
        f"Норма:    {norm} ккал\n"
        f"Съедено:  {round(t['cal'],1)} ккал\n"
        f"Осталось: {round(norm-t['cal'],1)} ккал\n\n"
        f"Б: {round(t['p'],1)}\n"
        f"Ж: {round(t['f'],1)}\n"
        f"У: {round(t['c'],1)}")


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
        await msg.answer("За 7 дней нет записей.")
        return
    norm  = user["calorie_norm"]
    lines = ["📅 <b>Статистика за 7 дней</b>\n"]
    tc = tp = tf = tcc = 0.0
    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d").strftime("%d.%m")
        cal = round(r["cal"], 1)
        tc += cal; tp += r["p"]; tf += r["f"]; tcc += r["c"]
        diff = round(norm - cal, 1)
        lines.append(f"<b>{d}</b>: {cal} ккал ({'+' if diff>=0 else ''}{diff})"
                     f"  Б{round(r['p'],1)} Ж{round(r['f'],1)} У{round(r['c'],1)}")
    n = len(rows)
    lines.append(f"\n<b>Итого:</b> {round(tc,1)} ккал · {n} дн.\n"
                 f"<b>Среднее:</b> {round(tc/n,1)} ккал/день\n"
                 f"Б{round(tp,1)} Ж{round(tf,1)} У{round(tcc,1)}")
    await msg.answer("\n".join(lines))


# ── /reset ────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(msg: Message, state: FSMContext):
    await state.clear()
    await db_del_user(msg.from_user.id)
    await msg.answer("🗑 Данные удалены. Начать заново: /start")


# ══════════════════════════════════════════════
# Фоновая очистка и запуск
# ══════════════════════════════════════════════

async def cleanup_loop():
    while True:
        try:
            await db_cleanup()
        except Exception as e:
            logger.error(e)
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
