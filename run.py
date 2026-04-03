"""
CalTrack — точка входа
Запускает FastAPI сервер и Telegram бота одновременно
"""

import asyncio
import os

import uvicorn

from api import app, db_init
from bot import start_bot


async def main():
    await db_init()

    port = int(os.getenv("PORT", 8000))

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    await asyncio.gather(
        server.serve(),
        start_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
