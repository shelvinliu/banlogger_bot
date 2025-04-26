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
from supabase import create_client

# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL')}{WEBHOOK_PATH}" if os.getenv("RENDER_EXTERNAL_URL") else None
TIMEZONE = pytz.timezone('Asia/Shanghai')

# 全局变量
bot_app = None
bot_initialized = False
supabase_client = None

class BanManager:
    """封禁管理类"""
    
    @staticmethod
    async def save_to_db(chat_title: str, banned_user_id: int, banned_user_name: str, 
                       admin_name: str, reason: str = "未填写"):
        """保存记录到数据库"""
        global supabase_client
        try:
            data = {
                "time": datetime.now(TIMEZONE).isoformat(),
                "group_name": chat_title,
                "banned_user_id": banned_user_id,
                "banned_user_name": banned_user_name,
                "admin_name": admin_name,
                "reason": reason
            }
            
            response = supabase_client.table("ban_records").insert(data).execute()
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
    global supabase_client
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase URL和KEY必须配置")
    
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase客户端初始化成功")
        
        # 测试连接
        test = supabase_client.table("ban_records").select("*").limit(1).execute()
        print("✅ Supabase连接测试成功")
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

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is admin"""
    if not update.effective_chat or not update.effective_user:
        return False
        
    try:
        member = await context.bot.get_chat_member(
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id
        )
        return member.status in ['administrator', 'creator']
    except Exception as e:
        print(f"Failed to check admin status: {e}")
        return False

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /f command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复要踢出的用户消息")
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
            f"🚨 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 已被踢出",
            parse_mode="Markdown"
        )
        
        reply_markup = BanManager.get_ban_reasons_keyboard(
            banned_user_id=target_user.id,
            banned_user_name=target_user.full_name
        )
        
        reason_msg = await update.message.reply_text(
            "请选择封禁原因：",
            reply_markup=reply_markup
        )
        
        context.chat_data["last_ban"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id
        }
        
        asyncio.create_task(delete_message_later(kick_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 踢出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban reason selection"""
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
        error_msg = await query.message.reply_text("⚠️ 只有执行踢出的管理员能选择原因")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    if reason == "其他":
        context.user_data["pending_reason"] = {
            "banned_user_id": banned_user_id,
            "banned_user_name": user_name,
            "chat_title": query.message.chat.title,
            "admin_name": query.from_user.full_name
        }
        msg = await query.message.reply_text("请输入自定义封禁原因:")
        asyncio.create_task(delete_message_later(msg))
        return
    
    try:
        await BanManager.save_to_db(
            chat_title=query.message.chat.title,
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            admin_name=query.from_user.full_name,
            reason=reason
        )
        
        confirm_msg = await query.message.reply_text(f"✅ 已记录: {user_name} - {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom ban reason"""
    if "pending_reason" not in context.user_data:
        return
    
    pending_data = context.user_data["pending_reason"]
    reason = update.message.text.strip()
    
    if not reason:
        error_msg = await update.message.reply_text("❌ 原因不能为空")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    try:
        await BanManager.save_to_db(
            chat_title=pending_data["chat_title"],
            banned_user_id=pending_data["banned_user_id"],
            banned_user_name=pending_data["banned_user_name"],
            admin_name=pending_data["admin_name"],
            reason=reason
        )
        
        confirm_msg = await update.message.reply_text(f"✅ 已记录自定义原因: {reason}")
        asyncio.create_task(delete_message_later(confirm_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    
    context.user_data.pop("pending_reason", None)
    await update.message.delete()

async def mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /j command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复要禁言的用户消息")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not context.args:
        msg = await update.message.reply_text("请指定禁言时间，例如: /j 1d2h30m")
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
            f"⏳ 用户 [{target_user.full_name}](tg://user?id={target_user.id}) "
            f"已被禁言 {duration}",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_message_later(mute_msg))
        
    except ValueError as e:
        error_msg = await update.message.reply_text(f"❌ 时间格式错误: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 禁言失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unmute command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复要解除禁言的用户消息")
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
            f"✅ 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 已解除禁言",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_message_later(unmute_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 解除禁言失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def records_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /records command"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    try:
        # 从Supabase获取记录
        response = supabase_client.table("ban_records").select("*").execute()
        records = response.data
        
        if not records:
            msg = await update.message.reply_text("暂无封禁记录")
            asyncio.create_task(delete_message_later(msg))
            return
        
        # 格式化记录
        message = "📊 封禁记录:\n\n"
        for record in records[:10]:  # 限制显示数量
            message += (
                f"🕒 {record.get('time', '未知时间')}\n"
                f"👤 用户: {record.get('banned_user_name', '未知用户')} (ID: {record.get('banned_user_id', '未知')}\n"
                f"👮 管理员: {record.get('admin_name', '未知')}\n"
                f"📝 原因: {record.get('reason', '未填写')}\n"
                f"💬 群组: {record.get('group_name', '未知群组')}\n"
                "━━━━━━━━━━━━━━\n
