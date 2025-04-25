import os
import re
import json
import pytz
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from supabase import create_client, Client

# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL')}{WEBHOOK_PATH}" if os.getenv("RENDER_EXTERNAL_URL") else None
TIMEZONE = pytz.timezone('Asia/Shanghai')

# 全局变量
bot_app = None
bot_initialized = False
supabase = None

class BanManager:
    """封禁管理类"""
    
    @staticmethod
    async def save_to_db(chat_title: str, banned_user_id: int, banned_user_name: str, 
                       admin_name: str, reason: str = "未填写"):
        """保存记录到数据库"""
        global supabase
        try:
            data = {
                "time": datetime.now(TIMEZONE).isoformat(),
                "group_name": chat_title,
                "banned_user_id": banned_user_id,
                "banned_user_name": banned_user_name,
                "admin_name": admin_name,
                "reason": reason
            }
            
            response = supabase.table("ban_records").insert(data).execute()
            if response.data:
                print(f"✅ 记录已保存: {banned_user_name} - {reason}")
            else:
                print("❌ 保存到数据库失败")
        except Exception as e:
            print(f"❌ 数据库操作失败: {e}")
            raise

    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str) -> InlineKeyboardMarkup:
        """生成封禁原因键盘"""
        buttons = [
            [
                InlineKeyboardButton("广告", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|广告"),
                InlineKeyboardButton("辱骂", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|辱骂"),
            ],
            [
                InlineKeyboardButton("刷屏", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|刷屏"),
                InlineKeyboardButton("其他", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|其他"),
            ]
        ]
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def parse_duration(duration_str: str) -> timedelta:
        """解析时间字符串如 '1天2小时30分钟'"""
        if not duration_str:
            raise ValueError("时间不能为空")
        
        pattern = re.compile(r'((?P<days>\d+)[天d])?((?P<hours>\d+)[小时h])?((?P<minutes>\d+)[分钟m])?')
        match = pattern.fullmatch(duration_str.replace(" ", ""))
        if not match:
            raise ValueError("无效时间格式，请使用如 '1天2小时30分钟' 或 '1d2h30m' 的格式")

        parts = {k: int(v) for k, v in match.groupdict().items() if v}
        return timedelta(**parts)

async def init_supabase():
    """初始化Supabase客户端"""
    global supabase
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase URL和KEY必须配置")
    
    try:
        # 使用较新的初始化方式
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY, {
            'auto_refresh_token': False,
            'persist_session': False
        })
        print("✅ Supabase客户端初始化成功")
    except Exception as e:
        print(f"❌ Supabase初始化失败: {e}")
        raise

async def delete_message_later(message, delay: int = 5):
    """延迟删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        print(f"删除消息失败: {e}")

# ... [保持其他处理函数不变] ...

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理"""
    global bot_app, bot_initialized
    
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 环境变量未设置")
    
    try:
        # 先初始化Supabase
        await init_supabase()
        
        # 初始化机器人
        bot_app = (
            Application.builder()
            .token(TOKEN)
            .post_init(post_init)
            .build()
        )
        
        # 注册处理器
        handlers = [
            CommandHandler("踢", kick_handler),
            CommandHandler("禁言", mute_handler),
            CommandHandler("解禁", unmute_handler),
            CommandHandler("记录", records_handler),
            CallbackQueryHandler(ban_reason_handler, pattern=r"^ban_reason\|"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_handler)
        ]
        
        for handler in handlers:
            bot_app.add_handler(handler)
        
        # 初始化
        await bot_app.initialize()
        
        # 设置Webhook或轮询
        if WEBHOOK_URL:
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
            await bot_app.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=Update.ALL_TYPES
            )
            print(f"✅ Webhook 已设置为: {WEBHOOK_URL}")
        else:
            await bot_app.start()
            print("✅ 机器人以轮询模式启动")
        
        bot_initialized = True
        
        # 验证机器人
        try:
            me = await bot_app.bot.get_me()
            print(f"🤖 机器人 @{me.username} 初始化成功")
        except Exception as e:
            print(f"❌ 无法验证机器人: {e}")
            raise
        
        yield
        
    finally:
        # 清理
        if bot_app:
            try:
                if not WEBHOOK_URL:
                    await bot_app.stop()
                await bot_app.shutdown()
            except Exception as e:
                print(f"关闭时出错: {e}")
        bot_initialized = False

# ... [保持其余代码不变] ...
