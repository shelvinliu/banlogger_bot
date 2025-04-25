import os
import re
import pytz
import asyncio
import openpyxl
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# 配置常量
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # 必须从环境变量获取
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH  # Render自动提供
EXCEL_FILE = "ban_records.xlsx"
TIMEZONE = pytz.timezone('Asia/Shanghai')

# 初始化 FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app

    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 环境变量未设置")
    
    BanManager.init_excel()
    
    bot_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

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

    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
        print(f"Webhook 已设置: {WEBHOOK_URL}")
    else:
        print("警告: WEBHOOK_URL 未设置，将无法接收更新")

    yield  # 表示 lifespan 中的“运行期”开始

app = FastAPI(title="Telegram Ban Manager Bot", lifespan=lifespan)
bot_app: Optional[Application] = None
    # （可选）你也可以在这里处理一些清理逻辑
class BanManager:
    """封禁管理核心类"""
    @staticmethod
    def init_excel():
        """初始化Excel记录文件"""
        if not os.path.exists(EXCEL_FILE):
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "BanRecords"
            ws.append(["时间", "群名", "被封用户ID", "被封用户名", "操作管理员ID", "操作管理员名", "封禁原因"])
            wb.save(EXCEL_FILE)

    @staticmethod
    def save_to_excel(chat_title: str, banned_user_id: int, banned_user_name: str, 
                     admin_id: int, admin_name: str, reason: str = "未填写"):
        """保存记录到Excel"""
        try:
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
        except Exception as e:
            print(f"保存Excel失败: {e}")
            raise

    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str) -> InlineKeyboardMarkup:
        """生成封禁原因选择键盘"""
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
        """解析时间字符串如 '1d2h30m'"""
        if not duration_str:
            raise ValueError("时间不能为空")
        
        pattern = re.compile(r'((?P<days>\d+)d)?((?P<hours>\d+)h)?((?P<minutes>\d+)m)?')
        match = pattern.fullmatch(duration_str.replace(" ", ""))
        if not match:
            raise ValueError("无效时间格式，请使用如 '1d2h30m' 的格式")

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
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception as e:
        print(f"检查管理员状态失败: {e}")
        return False

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理踢人命令 /f"""
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
        # 踢出用户
        await context.bot.ban_chat_member(
            chat_id=chat.id,
            user_id=target_user.id,
            revoke_messages=True
        )
        
        # 发送踢出通知
        kick_msg = await update.message.reply_text(
            f"🚨 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 已被踢出",
            parse_mode="Markdown"
        )
        
        # 添加封禁原因选择
        reply_markup = BanManager.get_ban_reasons_keyboard(
            banned_user_id=target_user.id,
            banned_user_name=target_user.full_name
        )
        
        reason_msg = await update.message.reply_text(
            "请选择封禁原因：",
            reply_markup=reply_markup
        )
        
        # 记录操作上下文
        context.chat_data["last_ban"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id
        }
        
        # 设置自动删除
        asyncio.create_task(delete_message_later(kick_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 踢出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理封禁原因选择"""
    query = update.callback_query
    await query.answer()
    
    # 验证回调数据格式
    try:
        _, user_id_str, user_name, reason = query.data.split("|")
        banned_user_id = int(user_id_str)
    except ValueError:
        error_msg = await query.message.reply_text("⚠️ 无效的回调数据")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 验证操作权限
    last_ban = context.chat_data.get("last_ban", {})
    if query.from_user.id != last_ban.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ 只有执行踢出的管理员能选择原因")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 处理"其他"原因
    if reason == "其他":
        context.user_data["pending_reason"] = {
            "banned_user_id": banned_user_id,
            "banned_user_name": user_name,
            "chat_title": query.message.chat.title,
            "admin_id": query.from_user.id,
            "admin_name": query.from_user.full_name
        }
        msg = await query.message.reply_text("请输入自定义封禁原因:")
        asyncio.create_task(delete_message_later(msg))
        return
    
    # 保存记录
    try:
        BanManager.save_to_excel(
            chat_title=query.message.chat.title,
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            admin_id=query.from_user.id,
            admin_name=query.from_user.full_name,
            reason=reason
        )
        
        confirm_msg = await query.message.reply_text(
            f"✅ 已记录: {user_name} - {reason}"
        )
        asyncio.create_task(delete_message_later(confirm_msg))
        asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理自定义封禁原因"""
    pending_data = context.user_data.get("pending_reason")
    if not pending_data:
        return
    
    reason = update.message.text.strip()
    if not reason:
        error_msg = await update.message.reply_text("❌ 原因不能为空")
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
        
        confirm_msg = await update.message.reply_text(
            f"✅ 已记录自定义原因: {reason}"
        )
        asyncio.create_task(delete_message_later(confirm_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
    
    context.user_data.pop("pending_reason", None)
    await update.message.delete()

async def mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理禁言命令 /j"""
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
    """处理解除禁言命令 /unmute"""
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

async def excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理导出Excel命令 /excel"""
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
                filename="封禁记录.xlsx",
                caption="📊 封禁记录导出"
            )
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 导出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))

@app.on_event("startup")
async def initialize_bot():
    """初始化机器人"""
    global bot_app
    
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 环境变量未设置")
    
    BanManager.init_excel()
    
    # 创建机器人实例
    bot_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    
    # 注册处理器
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
    
    # 设置Webhook
    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
        print(f"Webhook 已设置: {WEBHOOK_URL}")
    else:
        print("警告: WEBHOOK_URL 未设置，将无法接收更新")

async def post_init(app: Application):
    """机器人初始化后执行"""
    print(f"机器人 @{app.bot.username} 已启动")

@app.post(WEBHOOK_PATH)
async def process_webhook(request: Request):
    """处理Webhook请求"""
    if not bot_app:
        raise HTTPException(status_code=503, detail="机器人未初始化")
    
    try:
        update_data = await request.json()
        update = Update.from_dict(update_data)
        await bot_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "bot_ready": bool(bot_app)}
# ---------- 主程序入口 ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
