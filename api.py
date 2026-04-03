"""
CalTrack — FastAPI Backend
Обслуживает Mini App и API эндпоинты
"""

import os
from datetime import date, datetime, timedelta

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = "calories.db"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════
# База данных
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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                date       TEXT,
                calories   REAL,
                protein    REAL,
                fat        REAL,
                carbs      REAL,
                food_name  TEXT DEFAULT ''
            )
        """)
        await db.commit()


async def db_cleanup():
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE date < ?", (cutoff,))
        await db.commit()


# ══════════════════════════════════════════════
# Pydantic модели
# ══════════════════════════════════════════════

class UserCreate(BaseModel):
    user_id: int
    height:  float
    weight:  float
    gender:  str
    age:     int
    goal:    int


class LogCreate(BaseModel):
    user_id:   int
    calories:  float
    protein:   float = 0
    fat:       float = 0
    carbs:     float = 0
    food_name: str   = ""


# ══════════════════════════════════════════════
# Расчёт нормы
# ══════════════════════════════════════════════

def calc_norm(weight, height, age, gender, goal) -> float:
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "м" else -161)
    return round(bmr * 1.2 * {1: 0.8, 2: 1.0, 3: 1.2}[goal], 1)


# ══════════════════════════════════════════════
# API эндпоинты
# ══════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    await db_init()


@app.get("/api/user/{user_id}")
async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ) as c:
            row = await c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User not found")
            return dict(row)


@app.post("/api/user")
async def save_user(data: UserCreate):
    norm = calc_norm(data.weight, data.height, data.age, data.gender, data.goal)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                height=excluded.height, weight=excluded.weight,
                gender=excluded.gender, age=excluded.age,
                goal=excluded.goal, calorie_norm=excluded.calorie_norm
        """, (data.user_id, data.height, data.weight,
              data.gender, data.age, data.goal, norm))
        await db.commit()
    return {"ok": True, "calorie_norm": norm}


@app.delete("/api/user/{user_id}")
async def delete_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM logs  WHERE user_id=?", (user_id,))
        await db.commit()
    return {"ok": True}


@app.post("/api/log")
async def add_log(data: LogCreate):
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO logs (user_id,date,calories,protein,fat,carbs,food_name)
            VALUES (?,?,?,?,?,?,?)
        """, (data.user_id, today, data.calories,
              data.protein, data.fat, data.carbs, data.food_name))
        await db.commit()
    return {"ok": True}


@app.get("/api/today/{user_id}")
async def get_today(user_id: int):
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Агрегат
        async with db.execute("""
            SELECT COALESCE(SUM(calories),0) cal,
                   COALESCE(SUM(protein),0)  p,
                   COALESCE(SUM(fat),0)      f,
                   COALESCE(SUM(carbs),0)    c
            FROM logs WHERE user_id=? AND date=?
        """, (user_id, today)) as cur:
            agg = await cur.fetchone()
        # Список записей
        async with db.execute("""
            SELECT id, calories, protein, fat, carbs, food_name
            FROM logs WHERE user_id=? AND date=?
            ORDER BY id DESC
        """, (user_id, today)) as cur:
            logs = [dict(r) for r in await cur.fetchall()]

    return {
        "calories": agg[0], "protein": agg[1],
        "fat":      agg[2], "carbs":   agg[3],
        "logs":     logs,
    }


@app.get("/api/week/{user_id}")
async def get_week(user_id: int):
    cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT date,
                   SUM(calories) calories, SUM(protein) protein,
                   SUM(fat) fat,           SUM(carbs)   carbs
            FROM logs WHERE user_id=? AND date>=?
            GROUP BY date ORDER BY date
        """, (user_id, cutoff)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return rows


@app.delete("/api/log/{log_id}")
async def delete_log(log_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM logs WHERE id=?", (log_id,))
        await db.commit()
    return {"ok": True}


# Статика Mini App (должна быть последней!)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
