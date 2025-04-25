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
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# Configuration
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH if os.getenv("RENDER_EXTERNAL_URL") else None
EXCEL_FILE = "/tmp/ban_records.xlsx"
TIMEZONE = pytz.timezone('Asia/Shanghai')

# Global application reference
bot_app = None
bot_initialized = False

class BanManager:
    """Ban management core class"""
    @staticmethod
    def init_excel():
        """Initialize Excel file"""
        if not os.path.exists(EXCEL_FILE):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "BanRecords"
            ws.append(["时间", "群名", "被封用户ID", "被封用户名", "操作管理员ID", "操作管理员名", "封禁原因"])
            wb.save(EXCEL_FILE)

    @staticmethod
    def save_to_excel(chat_title: str, banned_user_id: int, banned_user_name: str, 
                     admin_id: int, admin_name: str, reason: str = "未填写"):
        """Save record to Excel"""
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
                admin_id,
                admin_name,
                reason
            ])
            wb.save(EXCEL_FILE)
            print(f"✅ Record saved: {banned_user_name} - {reason}")
        except Exception as e:
            print(f"❌ Failed to save Excel: {e}")
            raise

    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str) -> InlineKeyboardMarkup:
        """Generate ban reason keyboard"""
        buttons = [
            [
                InlineKeyboardButton("FUD", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|FUD"),
                InlineKeyboardButton("广告", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|广告"),
                InlineKeyboardButton("攻击他人", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|攻击他人"),
            ],
            [
                InlineKeyboardButton("诈骗", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|诈骗"),
                InlineKeyboardButton("带节奏", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|带节奏"),
                InlineKeyboardButton("其他", callback_data=f"ban_reason|{banned_user_id}|{banned_user_name}|其他"),
            ]
        ]
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def parse_duration(duration_str: str) -> timedelta:
        """Parse duration string like '1d2h30m'"""
        if not duration_str:
            raise ValueError("Duration cannot be empty")
        
        pattern = re.compile(r'((?P<days>\d+)d)?((?P<hours>\d+)h)?((?P<minutes>\d+)m)?')
        match = pattern.fullmatch(duration_str.replace(" ", ""))
        if not match:
            raise ValueError("Invalid duration format, use like '1d2h30m'")

        parts = {k: int(v) for k, v in match.groupdict().items() if v}
        return timedelta(**parts)

async def delete_message_later(message, delay: int = 5):
    """Delete message after delay"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        print(f"Failed to delete message: {e}")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is admin"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception as e:
        print(f"Failed to check admin status: {e}")
        return False

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /f command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ Only admins can use this command")
        asyncio.create_task(delete_message_later(msg))
        return

    if not update.message.reply_to_message:
        msg = await update.message.reply_text("Please reply to the user's message to kick")
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
            f"🚨 User [{target_user.full_name}](tg://user?id={target_user.id}) has been kicked",
            parse_mode="Markdown"
        )
        
        reply_markup = BanManager.get_ban_reasons_keyboard(
            banned_user_id=target_user.id,
            banned_user_name=target_user.full_name
        )
        
        reason_msg = await update.message.reply_text(
            "Please select ban reason:",
            reply_markup=reply_markup
        )
        
        context.chat_data["last_ban"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id
        }
        
        asyncio.create_task(delete_message_later(kick_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ Kick failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban reason selection"""
    query = update.callback_query
    await query.answer()
    
    try:
        _, user_id_str, user_name, reason = query.data.split("|")
        banned_user_id = int(user_id_str)
    except ValueError:
        error_msg = await query.message.reply_text("⚠️ Invalid callback data")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    last_ban = context.chat_data.get("last_ban", {})
    if query.from_user.id != last_ban.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ Only the admin who performed the kick can select reason")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    if reason == "其他":
        context.user_data["pending_reason"] = {
            "banned_user_id": banned_user_id,
            "banned_user_name": user_name,
            "chat_title": query.message.chat.title,
            "admin_id": query.from_user.id,
            "admin_name": query.from_user.full_name
        }
        msg = await query.message.reply_text("Please enter custom ban reason:")
        asyncio.create_task(delete_message_later(msg))
        return
    
    try:
        BanManager.save_to_excel(
            chat_title=query.message.chat.title,
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            admin_id=query.from_user.id,
            admin_name=query.from_user.full_name,
            reason=reason
        )
        
        confirm_msg = await query.message.reply_text(f"✅ Recorded: {user_name} - {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ Save failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom ban reason"""
    pending_data = context.user_data.get("pending_reason")
    if not pending_data:
        return
    
    reason = update.message.text.strip()
    if not reason:
        error_msg = await update.message.reply_text("❌ Reason cannot be empty")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        BanManager.save_to_excel(
            chat_title=pending_data["chat_title"],
            banned_user_id=pending_data["banned_user_id"],
            banned_user_name=pending_data["banned_user_name"],
            admin_id=pending_data["admin_id"],
            admin_name=pending_data["admin_name"],
            reason=reason
        )
        
        confirm_msg = await update.message.reply_text(f"✅ Custom reason recorded: {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ Save failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    
    context.user_data.pop("pending_reason", None)
    await update.message.delete()

async def mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /j command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ Only admins can use this command")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("Please reply to the user's message to mute")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not context.args:
        msg = await update.message.reply_text("Please specify mute duration, e.g.: /j 1d2h30m")
        asyncio.create_task(delete_message_later(msg))
        return
    
    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    
    try:
        duration = BanManager.parse_duration(" ".join(context.args))
        until_date = datetime.now(TIMEZONE) + duration
        
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        
        mute_msg = await update.message.reply_text(
            f"⏳ User [{target_user.full_name}](tg://user?id={target_user.id}) "
            f"has been muted for {duration}",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_message_later(mute_msg))
        
    except ValueError as e:
        error_msg = await update.message.reply_text(f"❌ Invalid duration format: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ Mute failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unmute command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ Only admins can use this command")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("Please reply to the user's message to unmute")
        asyncio.create_task(delete_message_later(msg))
        return
    
    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        
        unmute_msg = await update.message.reply_text(
            f"✅ User [{target_user.full_name}](tg://user?id={target_user.id}) has been unmuted",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_message_later(unmute_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ Unmute failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /excel command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ Only admins can use this command")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not os.path.exists(EXCEL_FILE):
        error_msg = await update.message.reply_text("❌ Record file doesn't exist")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        with open(EXCEL_FILE, "rb") as file:
            await update.message.reply_document(
                document=file,
                filename="ban_records.xlsx",
                caption="📊 Ban records export"
            )
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ Export failed: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan management"""
    global bot_app, bot_initialized
    
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable not set")
    
    BanManager.init_excel()
    
    # Initialize the Application
    bot_app = Application.builder().token(TOKEN).build()
    
    # Register handlers
    handlers = [
        CommandHandler("f", kick_handler),
        CommandHandler("j", mute_handler),
        CommandHandler("unmute", unmute_handler),
        CommandHandler("excel", excel_handler),
        CallbackQueryHandler(ban_reason_handler, pattern=r"^ban_reason\|"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_handler)
    ]
    
    for handler in handlers:
        bot_app.add_handler(handler)
    
    # Initialize and start
    await bot_app.initialize()
    
    if WEBHOOK_URL:
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        await bot_app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
        print(f"✅ Webhook set to: {WEBHOOK_URL}")
    else:
        await bot_app.start()
        print("✅ Bot started in polling mode")
    
    bot_initialized = True
    
    try:
        me = await bot_app.bot.get_me()
        print(f"🤖 Bot @{me.username} initialized successfully")
    except Exception as e:
        print(f"❌ Failed to verify bot: {e}")
        raise
    
    yield
    
    # Cleanup
    if bot_app:
        if not WEBHOOK_URL:
            await bot_app.stop()
        await bot_app.shutdown()
    bot_initialized = False

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    """Root endpoint"""
    return {
        "status": "running",
        "service": "Telegram Ban Manager",
        "bot_initialized": bot_initialized,
        "webhook_configured": bool(WEBHOOK_URL)
    }

@app.post(WEBHOOK_PATH)
async def process_webhook(request: Request):
    """Handle webhook updates"""
    if not bot_app or not bot_initialized:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, bot_app.bot)
        
        async with bot_app:
            await bot_app.process_update(update)
        
        return {"status": "ok"}
    except Exception as e:
        print(f"Error processing update: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "bot_initialized": bot_initialized,
        "webhook_url": WEBHOOK_URL,
        "timestamp": datetime.now(TIMEZONE).isoformat()
    }

@app.get("/ready")
async def readiness_check():
    """Readiness check endpoint"""
    try:
        me = await bot_app.bot.get_me() if bot_app and bot_initialized else None
        return {
            "ready": bot_initialized,
            "bot_username": me.username if me else None,
            "webhook": bool(WEBHOOK_URL)
        }
    except Exception as e:
        return {
            "ready": False,
            "error": str(e)
        }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
