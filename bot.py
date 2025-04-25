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

# Supabase 配置
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 全局变量
bot_app = None
bot_initialized = False

class BanManager:
    """封禁管理类"""
    
    @staticmethod
    async def save_to_db(chat_title: str, banned_user_id: int, banned_user_name: str, 
                       admin_name: str, reason: str = "未填写"):
        """保存记录到数据库"""
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
        
        # 支持中文和字母缩写
        pattern = re.compile(r'((?P<days>\d+)[天d])?((?P<hours>\d+)[小时h])?((?P<minutes>\d+)[分钟m])?')
        match = pattern.fullmatch(duration_str.replace(" ", ""))
        if not match:
            raise ValueError("无效时间格式，请使用如 '1天2小时30分钟' 或 '1d2h30m' 的格式")

        parts = {k: int(v) for k, v in match.groupdict().items() if v}
        return timedelta(**parts)

async def delete_message_later(message, delay: int = 5):
    """延迟删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        print(f"删除消息失败: {e}")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查用户是否是管理员"""
    if not update.effective_chat or not update.effective_user:
        return False
        
    try:
        member = await context.bot.get_chat_member(
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id
        )
        return member.status in ['administrator', 'creator']
    except Exception as e:
        print(f"检查管理员状态失败: {e}")
        return False

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理踢人命令 /踢"""
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
    """处理封禁原因选择"""
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
    """处理自定义封禁原因"""
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
    """处理禁言命令 /禁言"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复要禁言的用户消息")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not context.args:
        msg = await update.message.reply_text("请指定禁言时间，例如: /禁言 1天2小时30分钟")
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
    """处理解除禁言命令 /解禁"""
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
    """处理记录查询命令 /记录"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    try:
        response = supabase.table("ban_records").select("*").execute()
        
        if not response.data:
            msg = await update.message.reply_text("暂无封禁记录")
            asyncio.create_task(delete_message_later(msg))
            return
            
        # 简单显示最近5条记录
        records = response.data[-5:]
        message = "最近5条封禁记录：\n\n"
        for record in records:
            message += (
                f"时间: {record['time']}\n"
                f"群组: {record['group_name']}\n"
                f"用户: {record['banned_user_name']} (ID: {record['banned_user_id']})\n"
                f"管理员: {record['admin_name']}\n"
                f"原因: {record['reason']}\n\n"
            )
        
        await update.message.reply_text(message)
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 查询记录失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理"""
    global bot_app, bot_initialized
    
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 环境变量未设置")
    
    try:
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

async def post_init(application: Application) -> None:
    """初始化后回调"""
    print("✅ 机器人初始化完成")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    """根路由"""
    return {
        "status": "运行中",
        "service": "Telegram封禁管理机器人",
        "bot_initialized": bot_initialized,
        "webhook_configured": bool(WEBHOOK_URL)
    }

@app.post(WEBHOOK_PATH)
async def process_webhook(request: Request):
    """处理Webhook请求"""
    if not bot_app or not bot_initialized:
        raise HTTPException(status_code=503, detail="机器人未初始化")
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, bot_app.bot)
        
        await bot_app.process_update(update)
        return {"status": "ok"}
        
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}")
        raise HTTPException(status_code=400, detail="无效的JSON数据")
    except Exception as e:
        print(f"处理更新失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "正常",
        "bot_ready": bot_initialized,
        "webhook_url": WEBHOOK_URL,
        "timestamp": datetime.now(TIMEZONE).isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
