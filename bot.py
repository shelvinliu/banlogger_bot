import os
import re
import pytz
import asyncio
import openpyxl
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.getenv("TOKEN", "7705231017:AAG5L6HyQFcj7I4vlTHynU2wG0hbMOuhzSA")  # 推荐从环境变量读取
WEBHOOK_URL = "https://banlogger-bot.onrender.com"
EXCEL_FILE = "ban_records.xlsx"

# 初始化 Excel 文件
def init_excel():
    if not os.path.exists(EXCEL_FILE):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "BanRecords"
        ws.append(["时间", "群名", "被封用户", "操作管理员", "封禁原因"])
        wb.save(EXCEL_FILE)

def save_to_excel(chat_title, banned_user, admin_user, reason="（未填写）"):
    wb = openpyxl.load_workbook(EXCEL_FILE)
    ws = wb["BanRecords"]
    ws.append([
        datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S"),
        chat_title,
        banned_user,
        admin_user,
        reason
    ])
    wb.save(EXCEL_FILE)

def get_ban_reasons_keyboard(banned_user_id, banned_user_name):
    return [
        [
            InlineKeyboardButton("FUD", callback_data=f"{banned_user_id}|{banned_user_name}|FUD"),
            InlineKeyboardButton("广告内容", callback_data=f"{banned_user_id}|{banned_user_name}|广告内容"),
            InlineKeyboardButton("攻击他人", callback_data=f"{banned_user_id}|{banned_user_name}|攻击他人"),
        ],
        [
            InlineKeyboardButton("诈骗", callback_data=f"{banned_user_id}|{banned_user_name}|诈骗"),
            InlineKeyboardButton("带节奏", callback_data=f"{banned_user_id}|{banned_user_name}|带节奏"),
            InlineKeyboardButton("其他", callback_data=f"{banned_user_id}|{banned_user_name}|其他"),
        ],
    ]

async def delete_later(msg, delay=5):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

def parse_time(time_str):
    time_regex = re.compile(r'(\d+)([dhm])')
    time_dict = {'d': 0, 'h': 0, 'm': 0}
    for match in time_regex.findall(time_str):
        value, unit = match
        value = int(value)
        time_dict[unit] += value
    return timedelta(days=time_dict['d'], hours=time_dict['h'], minutes=time_dict['m'])

async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某人的消息并输入 /f 来踢人")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    try:
        await context.bot.ban_chat_member(chat_id, target_user.id)

        kick_msg = await update.message.reply_text(
            f"✅ 已踢出用户：{target_user.mention_html()}",
            parse_mode="HTML"
        )

        keyboard = get_ban_reasons_keyboard(target_user.id, target_user.username or target_user.full_name)
        reply_markup = InlineKeyboardMarkup(keyboard)

        reason_msg = await update.message.reply_text(
            f"🚨 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 被踢出群组。\n请选择封禁原因：",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        context.chat_data["current_ban"] = {
            "banned_user_id": target_user.id,
            "operator_id": update.message.from_user.id
        }

        asyncio.create_task(delete_later(kick_msg))
        asyncio.create_task(delete_later(reason_msg))

    except Exception as e:
        await update.message.reply_text(f"❌ 踢人失败：{e}")

async def handle_ban_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat.id

    try:
        banned_user_id, banned_user_name, selected_reason = query.data.split("|")
    except ValueError:
        msg = await query.message.reply_text("⚠️ 解析封禁原因失败")
        asyncio.create_task(delete_later(msg))
        return

    current_ban = context.chat_data.get("current_ban", {})
    if user_id != current_ban.get("operator_id"):
        msg = await query.message.reply_text("⚠️ 只能由最初执行 /kick 的管理员选择封禁原因。")
        asyncio.create_task(delete_later(msg))
        return

    member = await context.bot.get_chat_member(chat_id, user_id)
    if member.status not in ["administrator", "creator"]:
        msg = await query.message.reply_text("⚠️ 只有管理员可以选择封禁原因。")
        asyncio.create_task(delete_later(msg))
        return

    if selected_reason == "其他":
        context.user_data["pending_reason_user"] = {
            "banned_user_name": banned_user_name,
            "chat_title": query.message.chat.title,
            "admin_name": query.from_user.username or query.from_user.full_name
        }
        msg = await query.message.reply_text(
            f"✏️ 请输入 {banned_user_name} 的自定义封禁原因："
        )
        asyncio.create_task(delete_later(msg))
        return

    save_to_excel(
        query.message.chat.title,
        banned_user_name,
        query.from_user.username or query.from_user.full_name,
        selected_reason
    )

    msg = await query.message.reply_text(f"✅ 封禁原因已记录：{banned_user_name} -> {selected_reason}")
    asyncio.create_task(delete_later(msg))
    asyncio.create_task(delete_later(query.message))

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data.get("pending_reason_user")
    if not data:
        return

    reason_text = update.message.text.strip()
    if not reason_text:
        msg = await update.message.reply_text("❌ 封禁原因不能为空，请重新输入。")
        asyncio.create_task(delete_later(msg))
        return

    save_to_excel(
        data["chat_title"],
        data["banned_user_name"],
        data["admin_name"],
        reason_text
    )

    msg = await update.message.reply_text(f"✅ 自定义封禁原因已记录：{data['banned_user_name']} -> {reason_text}")
    asyncio.create_task(delete_later(msg))

    context.user_data.pop("pending_reason_user", None)
    await update.message.delete()

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某人的消息并输入 /j 来禁言")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if len(context.args) < 1:
        await update.message.reply_text("请指定禁言时间。例如: /j 1d 1h 表示禁言 1天 1小时")
        return

    mute_duration = ' '.join(context.args)
    try:
        mute_time = parse_time(mute_duration)
    except Exception as e:
        await update.message.reply_text(f"❌ 时间格式错误: {e}")
        return

    if mute_time.total_seconds() <= 0:
        await update.message.reply_text("❌ 禁言时间必须大于 0。")
        return

    try:
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(
            chat_id, target_user.id,
            permissions=permissions,
            until_date=update.message.date + mute_time
        )
        mute_msg = await update.message.reply_text(
            f"✅ 已成功禁言用户 {target_user.mention_html()} {mute_duration}",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_later(mute_msg))
    except Exception as e:
        await update.message.reply_text(f"❌ 禁言失败：{e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某人的消息并输入 /Unmute 来解除禁言")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    try:
        permissions = ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=permissions)

        unmute_msg = await update.message.reply_text(
            f"✅ 已成功解除用户 {target_user.mention_html()} 的禁言",
            parse_mode="HTML"
        )
        asyncio.create_task(delete_later(unmute_msg))
    except Exception as e:
        await update.message.reply_text(f"❌ 解除禁言失败：{e}")

async def download_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(EXCEL_FILE):
        await update.message.reply_text("❌ Excel 文件不存在。")
        return

    with open(EXCEL_FILE, "rb") as file:
        await update.message.reply_document(
            document=file,
            filename=EXCEL_FILE,
            caption="📄 这是封禁记录的Excel文件。"
        )

# FastAPI 实例
app = FastAPI()
bot_app = None  # Telegram 应用实例

@app.on_event("startup")
async def startup():
    global bot_app
    init_excel()
    bot_app = ApplicationBuilder().token(TOKEN).build()

    bot_app.add_handler(CommandHandler("f", kick_command))
    bot_app.add_handler(CallbackQueryHandler(handle_ban_reason))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_handler))
    bot_app.add_handler(CommandHandler("excel", download_excel))
    bot_app.add_handler(CommandHandler("j", mute_command))
    bot_app.add_handler(CommandHandler("Unmute", unmute_command))

    await bot_app.bot.set_webhook(WEBHOOK_URL)
    print("Webhook 已设置")

@app.post(WEBHOOK_PATH)
async def process_update(request: Request):
    update_data = await request.json()
    update = Update.de_json(update_data, bot_app.bot)
    await bot_app.process_update(update)
    return {"status": "ok"}
