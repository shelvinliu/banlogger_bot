import os
import re
import json
import pytz
import asyncio
import openpyxl
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH if os.getenv("RENDER_EXTERNAL_URL") else None
EXCEL_FILE = "ban_records.xlsx"
TIMEZONE = pytz.timezone('Asia/Shanghai')

# 全局应用引用
bot_app = None
bot_initialized = False

class BanManager:
    """封禁管理核心类"""
    @staticmethod
    def init_excel():
        """初始化 Excel 文件"""
        if not os.path.exists(EXCEL_FILE):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "BanRecords"
            ws.append(["时间", "群名", "被封用户ID", "被封用户名", "操作管理员名", "封禁原因"])
            wb.save(EXCEL_FILE)

    @staticmethod
    def save_to_excel(chat_title: str, banned_user_id: int, banned_user_name: str, admin_name: str, reason: str = "未填写"):
        """保存记录到 Excel"""
        try:
            if not os.path.exists(EXCEL_FILE):
                BanManager.init_excel()
                
            wb = openpyxl.load_workbook(EXCEL_FILE)
            ws = wb["BanRecords"]
            ws.append([
                datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                chat_title,
                banned_user_id,
                banned_user_name,
                admin_name,
                reason
            ])
            wb.save(EXCEL_FILE)
            print(f"✅ 记录已保存: {banned_user_name} - {reason}")
        except Exception as e:
            print(f"❌ 保存到 Excel 失败: {e}")
            raise

    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str) -> InlineKeyboardMarkup:
        """生成封禁理由键盘"""
        buttons = [
            [
                InlineKeyboardButton("FUD", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|FUD"),
                InlineKeyboardButton("广告", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|广告"),
                InlineKeyboardButton("攻击他人", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|攻击他人"),
            ],
            [
                InlineKeyboardButton("诈骗", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|诈骗"),
                InlineKeyboardButton("带节奏", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|带节奏"),
            ]
        ]
        return InlineKeyboardMarkup(buttons)

async def delete_message_later(message, delay: int = 5):
    """延迟删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        print(f"删除消息失败: {e}")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查用户是否是管理员"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception as e:
        print(f"检查管理员身份失败: {e}")
        return False

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /f 命令"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复某个用户的消息以封禁")
        asyncio.create_task(delete_message_later(msg))
        return

    target_user = update.message.reply_to_message.from_user
    chat = update.effective_chat

    try:
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=target_user.id,
            revoke_messages=True
        )
        
        kick_msg = await update.message.reply_text(
            f"🚨 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 已被封禁",
            parse_mode="Markdown"
        )
        
        reply_markup = BanManager.get_ban_reasons_keyboard(
            banned_user_id=target_user.id,
            banned_user_name=target_user.full_name
        )
        
        reason_msg = await update.message.reply_text(
            "请选择封禁理由：",
            reply_markup=reply_markup
        )
        
        context.chat_data["last_ban"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id
        }
        
        asyncio.create_task(delete_message_later(kick_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 封禁失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理封禁理由选择"""
    query = update.callback_query
    await query.answer()
    
    try:
        _, user_id_str, user_name, reason = query.data.split("|")
        banned_user_id = int(user_id_str)
    except ValueError:
        error_msg = await query.message.reply_text("⚠️ 无效的回调数据")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    last_ban = context.chat_data.get("last_ban", {})
    if query.from_user.id != last_ban.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ 只有执行封禁操作的管理员可以选择理由")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        BanManager.save_to_excel(
            chat_title=query.message.chat.title,
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            admin_name=query.from_user.full_name,
            reason=reason
        )
        
        confirm_msg = await query.message.reply_text(f"✅ 记录已保存: {user_name} - {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理自定义封禁理由"""
    pending_data = context.user_data.get("pending_reason")
    if not pending_data:
        return
    
    reason = update.message.text.strip()
    if not reason:
        error_msg = await update.message.reply_text("❌ 理由不能为空")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        BanManager.save_to_excel(
            chat_title=pending_data["chat_title"],
            banned_user_id=pending_data["banned_user_id"],
            banned_user_name=pending_data["banned_user_name"],
            admin_name=pending_data["admin_name"],
            reason=reason
        )
        
        confirm_msg = await update.message.reply_text(f"✅ 自定义理由已保存: {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    
    context.user_data.pop("pending_reason", None)
    await update.message.delete()

async def excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /excel 命令"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not os.path.exists(EXCEL_FILE):
        error_msg = await update.message.reply_text("❌ 记录文件不存在")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        with open(EXCEL_FILE, "rb") as file:
            await update.message.reply_document(
                document=file,
                filename="ban_records.xlsx",
                caption="📊 封禁记录导出"
            )
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 导出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理"""
    global bot_app, bot_initialized
    
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 环境变量未设置")
    
    BanManager.init_excel()
    
    # 初始化 Application
    bot_app = Application.builder().token(TOKEN).build()
    
    # 注册处理程序
    handlers = [
        CommandHandler("f", kick_handler),
        CallbackQueryHandler(ban_reason_handler, pattern="^ban_reason"),
        CommandHandler("excel", excel_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_handler),
    ]
    
    for handler in handlers:
        bot_app.add_handler(handler)
    
    bot_initialized = True
    yield app
    await bot_app.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "OK"}

@app.on_event("startup")
async def startup():
    if bot_initialized:
        await bot_app.start_polling()

@app.on_event("shutdown")
async def shutdown():
    if bot_initialized:
        await bot_app.stop()
