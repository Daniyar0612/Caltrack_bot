"""
CalTrack Bot — открывает Mini App
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

BOT_TOKEN  = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")   # URL Railway приложения

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not WEBAPP_URL:
        await message.answer(
            "⚠️ WEBAPP_URL не задан.\n"
            "Добавь в Railway Variables:\n"
            "<code>WEBAPP_URL = https://твой-проект.railway.app</code>"
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🥗 Открыть CalTrack",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]])

    await message.answer(
        "👋 Привет! Я <b>CalTrack</b> — калькулятор калорий.\n\n"
        "Нажми кнопку ниже чтобы открыть приложение 👇",
        reply_markup=kb
    )


async def start_bot():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот запущен")
    await dp.start_polling(bot, skip_updates=True)
