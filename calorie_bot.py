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

BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN", "")
DB_PATH = "calories.db"
HF_MODEL_URL = "https://api-inference.huggingface.co/models/nateraw/food"

FOOD_DATABASE = {
    "pizza": {"cal": 266, "p": 11, "f": 10, "c": 33},
    "hamburger": {"cal": 295, "p": 17, "f": 14, "c": 24},
    "cheeseburger": {"cal": 303, "p": 15, "f": 14, "c": 27},
    "french fries": {"cal": 312, "p": 3.4, "f": 15, "c": 41},
    "nuggets": {"cal": 296, "p": 15, "f": 20, "c": 14},
    "sushi": {"cal": 150, "p": 5, "f": 2, "c": 30},
    "ramen": {"cal": 436, "p": 10, "f": 15, "c": 65},
    "steak": {"cal": 271, "p": 25, "f": 19, "c": 0},
    "chicken": {"cal": 165, "p": 31, "f": 3.6, "c": 0},
    "salmon": {"cal": 208, "p": 20, "f": 13, "c": 0},
    "pasta": {"cal": 131, "p": 5, "f": 1.1, "c": 25},
    "caesar salad": {"cal": 190, "p": 7, "f": 15, "c": 8},
    "omelette": {"cal": 154, "p": 11, "f": 12, "c": 0.7},
    "apple pie": {"cal": 237, "p": 1.9, "f": 11, "c": 34},
    "ice cream": {"cal": 207, "p": 3.5, "f": 11, "c": 24},
    "soup": {"cal": 50, "p": 3, "f": 2, "c": 8},
    "beshbarmak": {"cal": 310, "p": 18, "f": 22, "c": 15},
    "plov": {"cal": 250, "p": 8, "f": 12, "c": 28},
    "shashlik": {"cal": 230, "p": 20, "f": 16, "c": 2},
    "baursak": {"cal": 350, "p": 6, "f": 18, "c": 42},
    "manti": {"cal": 180, "p": 9, "f": 8, "c": 20},
    "shurpa": {"cal": 70, "p": 4, "f": 4, "c": 5},
    "rice": {"cal": 130, "p": 2.7, "f": 0.3, "c": 28},
    "egg": {"cal": 155, "p": 13, "f": 11, "c": 1.1},
    "bread": {"cal": 265, "p": 9, "f": 3, "c": 49},
    "banana": {"cal": 89, "p": 1.1, "f": 0.3, "c": 23},
    "apple": {"cal": 52, "p": 0.3, "f": 0.2, "c": 14},
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Registration(StatesGroup):
    height = State()
    weight = State()
    gender = State()
    age = State()
    goal = State()

class FoodLog(StatesGroup):
    calories = State()
    macros = State()

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                height REAL, weight REAL, gender TEXT,
                age INTEGER, goal INTEGER, calorie_norm REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, date TEXT,
                calories REAL, protein REAL, fat REAL, carbs REAL
            )
        """)
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

async def db_get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * @ FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def db_add_log(user_id, calories, protein, fat, carbs):
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (user_id,date,calories,protein,fat,carbs) VALUES(?,?,?,?,?,?)",
            (user_id, today, calories, protein, fat, carbs),
        )
        await db.commit()

async def db_get_today(user_id):
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COALESCE(SUM(calories),0), COALESCE(SUM(protein),0),
                   COALESCE(SUM(fat),0),     COALESCE(SUM(carbs),0)
            FROM logs WHERE user_id=? AND date=?
        """, (user_id, today)) as c:
            r = await c.fetchone()
            return {"calories": r[0], "protein": r[1], "fat": r[2], "carbs": r[3]}

async def hf_classify_food(image_bytes):
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(HF_MODEL_URL, headers=headers, data=image_bytes) as resp:
                if resp.status == 200:
                    results = await resp.json()
                    return results[0].get("label", "").lower().replace("_", " ")
    except:
        pass
    return None

def calc_calorie_norm(weight, height, age, gender, goal):
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "м" else -161)
    return round(bmr * 1.2 * {1: 0.8, 2: 1.0, 3: 1.2}.get(goal, 1.0), 1)

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚀 <b>Привет! Я CalTrackBot.</b>\nДавай настроим твой профиль. Введи рост (см):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.height)

@router.message(Registration.height)
async def reg_height(message: Message, state: FSMContext):
    await state.update_data(height=float(message.text))
    await message.answer("⚖️ Введи свой текущий вес (кг):")
    await state.set_state(Registration.weight)

@router.message(Registration.weight)
async def reg_weight(message: Message, state: FSMContext):
    await state.update_data(weight=float(message.text))
    await message.answer("👤 Выбери пол:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="М"), KeyboardButton(text="Ж")]], resize_keyboard=True))
    await state.set_state(Registration.gender)

@router.message(Registration.gender)
async def reg_gender(message: Message, state: FSMContext):
    await state.update_data(gender=message.text.lower())
    await message.answer("🎂 Сколько тебе лет?", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Registration.age)

@router.message(Registration.age)
async def reg_age(message: Message, state: FSMContext):
    await state.update_data(age=int(message.text))
    await message.answer("🎯 Выбери цель:\n1. Похудение 📉\n2. Поддержание ✨\n3. Набор массы 💪")
    await state.set_state(Registration.goal)

@router.message(Registration.goal)
async def reg_goal(message: Message, state: FSMContext):
    goal = int(message.text[0])
    data = await state.get_data()
    norm = calc_calorie_norm(data["weight"], data["height"], data["age"], data["gender"], goal)
    await db_upsert_user(message.from_user.id, data["height"], data["weight"], data["gender"], data["age"], goal, norm)
    await state.clear()
    await message.answer(f"✅ Профиль настроен!\n🔥 Твоя дневная норма: <b>{norm} ккал</b>\n\nПрисылай фото еды, чтобы записать калории! 📸")

@router.message(F.photo)
async def food_photo(message: Message, state: FSMContext):
    user = await db_get_user(message.from_user.id)
    if not user: return
    
    msg = await message.answer("🤖 <i>Думаю... распознаю блюдо...</i>")
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    label = await hf_classify_food(buf.read())
    
    await msg.delete()
    if label in FOOD_DATABASE:
        nutrients = FOOD_DATABASE[label]
        await db_add_log(message.from_user.id, nutrients["cal"], nutrients["p"], nutrients["f"], nutrients["c"])
        res = await db_get_today(message.from_user.id)
        await message.answer(
            f"🍴 Кажется, это <b>{label}</b>!\n"
            f"➕ Добавлено: {nutrients['cal']} ккал\n\n"
            f"📊 Итог за сегодня: {res['calories']} / {user['calorie_norm']} ккал"
        )
    else:
        await message.answer(f"🧐 Вижу <b>{label}</b>, но не знаю калорийность.\nВведи число калорий вручную (ккал): ✍️")
        await state.set_state(FoodLog.calories)

@router.message(FoodLog.calories)
async def manual_cal(message: Message, state: FSMContext):
    await state.update_data(cal=float(message.text))
    await message.answer("🍗 Введи Б Ж У через пробел (например: 10 5 20):")
    await state.set_state(FoodLog.macros)

@router.message(FoodLog.macros)
async def manual_macros(message: Message, state: FSMContext):
    p, f, c = map(float, message.text.split())
    data = await state.get_data()
    await db_add_log(message.from_user.id, data["cal"], p, f, c)
    await state.clear()
    await message.answer("💾 Сохранил! Ты молодец. 🌟")

@router.message(Command("today"))
async def cmd_today(message: Message):
    res = await db_get_today(message.from_user.id)
    await message.answer(
        f"📅 <b>Итоги дня:</b>\n"
        f"🔥 Энергия: {res['calories']} ккал\n"
        f"🥩 Белки: {res['protein']}г\n"
        f"🥑 Жиры: {res['fat']}г\n"
        f"🍞 Углеводы: {res['carbs']}г"
    )

async def main():
    await db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())