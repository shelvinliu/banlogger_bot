import re
import pytz
import openpyxl
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from datetime import datetime, timedelta

TOKEN = "7705231017:AAG5L6HyQFcj7I4vlTHynU2wG0hbMOuhzSA"
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

# 禁言时间解析
def parse_time(time_str):
    time_regex = re.compile(r'(\d+)([dhm])')  # 匹配 '1d', '2h', '3m' 这样的格式
    time_dict = {'d': 0, 'h': 0, 'm': 0}  # 默认禁言时间为 0 天 0 小时 0 分钟

    for match in time_regex.findall(time_str):
        value, unit = match
        value = int(value)
        if unit == 'd':
            time_dict['d'] += value
        elif unit == 'h':
            time_dict['h'] += value
        elif unit == 'm':
            time_dict['m'] += value

    # 返回禁言的总时长
    return timedelta(days=time_dict['d'], hours=time_dict['h'], minutes=time_dict['m'])

# /kick 命令处理
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

        # 记录当前操作管理员
        context.chat_data["current_ban"] = {
            "banned_user_id": target_user.id,
            "operator_id": update.message.from_user.id
        }

        asyncio.create_task(delete_later(kick_msg))
        asyncio.create_task(delete_later(reason_msg))

    except Exception as e:
        await update.message.reply_text(f"❌ 踢人失败：{e}")

# 处理封禁原因选择
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

# 自定义封禁原因处理
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

# /Mute 命令处理
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某人的消息并输入 /j 来禁言")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if len(context.args) < 1:
        await update.message.reply_text("请指定禁言时间。例如: /j 1d 1h 表示禁言 1天 1小时")
        return

    mute_duration = ' '.join(context.args)  # 获取用户输入的时间
    try:
        mute_time = parse_time(mute_duration)  # 解析禁言时间
    except Exception as e:
        await update.message.reply_text(f"❌ 时间格式错误: {e}")
        return

    if mute_time.total_seconds() <= 0:
        await update.message.reply_text("❌ 禁言时间必须大于 0。")
        return

    try:
        # 禁言目标用户
        permissions = ChatPermissions(can_send_messages=False)  # 禁止发送消息
        await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=permissions, until_date=update.message.date + mute_time)

        mute_msg = await update.message.reply_text(
            f"✅ 已成功禁言用户 {target_user.mention_html()} {mute_duration}",
            parse_mode="HTML"
        )

        # 延迟 5 秒删除禁言消息
        asyncio.create_task(delete_later(mute_msg))

    except Exception as e:
        await update.message.reply_text(f"❌ 禁言失败：{e}")

# /Unmute 命令处理
async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("请回复某人的消息并输入 /Unmute 来解除禁言")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    try:
        # 解除禁言，恢复权限
        permissions = ChatPermissions(can_send_messages=True)  # 恢复发送消息的权限
        await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=permissions)

        unmute_msg = await update.message.reply_text(
            f"✅ 已成功解除用户 {target_user.mention_html()} 的禁言",
            parse_mode="HTML"
        )

        # 延迟 5 秒删除解除禁言消息
        asyncio.create_task(delete_later(unmute_msg))

    except Exception as e:
        await update.message.reply_text(f"❌ 解除禁言失败：{e}")
# /excel 下载封禁记录
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

# 启动 Bot
init_excel()
app = ApplicationBuilder().token(TOKEN).build()

# 添加命令处理器
app.add_handler(CommandHandler("f", kick_command))
app.add_handler(CallbackQueryHandler(handle_ban_reason))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_reason_handler))
app.add_handler(CommandHandler("excel", download_excel))
app.add_handler(CommandHandler("j", mute_command))  # 新增 /Mute 命令
app.add_handler(CommandHandler("Unmute", unmute_command))  # 新增 /Unmute 命令

print("🤖 Bot 已启动...")
app.run_polling()
