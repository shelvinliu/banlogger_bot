import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH if os.getenv("RENDER_EXTERNAL_URL") else None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """处理 FastAPI 生命周期事件"""
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 未设置")
    
    # 初始化 Telegram Bot
    global bot_app
    bot_app = ApplicationBuilder().token(TOKEN).build()
    
    # 注册处理器
    bot_app.add_handler(CommandHandler("start", start_handler))
    # 添加其他处理器...
    
    # 设置 Webhook
    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(WEBHOOK_URL)
        print(f"Webhook 设置为: {WEBHOOK_URL}")
    
    yield
    
    # 关闭逻辑
    if bot_app:
        await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

# 你的路由和其他代码...
