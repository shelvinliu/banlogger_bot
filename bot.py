import os
import logging
import asyncio
import json
import base64
import time
import random
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, ChatPermissions
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ChatMemberHandler
from fastapi import FastAPI, Request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup
import aiohttp

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class GoogleSheetsStorage:
    """Google Sheets 存储类"""
    def __init__(self):
        self.credentials = None
        self.client = None
        self.ban_sheet = None
        self.reply_sheet = None
        self.initialized = False
        
    async def initialize(self):
        """初始化 Google Sheets 客户端"""
        if self.initialized:
            return
            
        try:
            # 解码 Base64 编码的凭证
            credentials_json = base64.b64decode(GOOGLE_SHEETS_CREDENTIALS).decode('utf-8')
            credentials_dict = json.loads(credentials_json)
            
            # 创建凭证
            self.credentials = ServiceAccountCredentials.from_json_keyfile_dict(
                credentials_dict,
                ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
            )
            
            # 创建客户端
            self.client = gspread.authorize(self.credentials)
            
            # 尝试打开或创建封禁记录表
            try:
                self.ban_sheet = self.client.open(BAN_RECORDS_SHEET).sheet1
            except gspread.exceptions.SpreadsheetNotFound:
                # 如果表不存在，创建新表
                spreadsheet = self.client.create(BAN_RECORDS_SHEET)
                self.ban_sheet = spreadsheet.sheet1
                # 添加表头
                self.ban_sheet.append_row([
                    "操作时间", "电报群组名称", "用户ID", 
                    "用户名", "名称", "操作管理", 
                    "理由", "操作"
                ])
                logger.info(f"创建新的封禁记录表: {BAN_RECORDS_SHEET}")
            
            # 尝试打开或创建关键词回复表
            try:
                self.reply_sheet = self.client.open(KEYWORD_REPLIES_SHEET).sheet1
                # 检查是否有表头
                headers = self.reply_sheet.row_values(1)
                if not headers or len(headers) < 4:
                    # 如果表头不存在或不完整，添加表头
                    self.reply_sheet.clear()
                    self.reply_sheet.append_row([
                        "关键词", "回复内容", "链接", "链接文本"
                    ])
                    logger.info("添加关键词回复表表头")
            except gspread.exceptions.SpreadsheetNotFound:
                # 如果表不存在，创建新表
                spreadsheet = self.client.create(KEYWORD_REPLIES_SHEET)
                self.reply_sheet = spreadsheet.sheet1
                # 添加表头
                self.reply_sheet.append_row([
                    "关键词", "回复内容", "链接", "链接文本"
                ])
                logger.info(f"创建新的关键词回复表: {KEYWORD_REPLIES_SHEET}")
            
            self.initialized = True
            logger.info("Google Sheets 客户端初始化成功")
            
        except Exception as e:
            logger.error(f"Google Sheets 初始化失败: {e}")
            raise
            
    async def get_keyword_replies(self) -> List[Dict[str, str]]:
        """获取关键词回复列表"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 获取所有记录
            records = self.reply_sheet.get_all_records()
            
            # 过滤出有效的关键词回复
            replies = []
            for record in records:
                if record.get("关键词") and record.get("回复内容"):
                    replies.append({
                        "关键词": record["关键词"],
                        "回复内容": record["回复内容"],
                        "链接": record.get("链接", ""),
                        "链接文本": record.get("链接文本", "")
                    })
                    
            logger.info(f"成功获取 {len(replies)} 条关键词回复")
            return replies
            
        except Exception as e:
            logger.error(f"获取关键词回复失败: {e}")
            return []  # 返回空列表而不是抛出异常
            
    async def add_keyword_reply(self, keyword: str, reply_text: str, link: str = "", link_text: str = "") -> bool:
        """添加关键词回复"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 检查表格是否存在
            if not self.reply_sheet:
                logger.error("Reply sheet not initialized")
                return False
                
            # 获取所有记录
            try:
                records = self.reply_sheet.get_all_records()
                logger.info(f"Retrieved {len(records)} existing records")
            except Exception as e:
                logger.error(f"Failed to get records: {e}")
                records = []
            
            # 检查关键词是否已存在
            for record in records:
                if record.get("关键词") == keyword:
                    logger.warning(f"Keyword already exists: {keyword}")
                    return False
            
            # 准备新行数据
            new_row = [keyword, reply_text, link, link_text]
            logger.info(f"Preparing to add new row: {new_row}")
            
            # 添加新记录
            try:
                self.reply_sheet.append_row(new_row)
                logger.info(f"Successfully added keyword reply: {keyword}")
                return True
            except Exception as e:
                logger.error(f"Failed to append row: {e}")
                return False
            
        except Exception as e:
            logger.error(f"Failed to add keyword reply: {e}")
            return False
            
    async def delete_keyword_reply(self, keyword: str) -> bool:
        """删除关键词回复"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 查找关键词所在行
            records = self.reply_sheet.get_all_records()
            for i, record in enumerate(records, start=2):  # 从第2行开始（跳过标题行）
                if record.get("关键词") == keyword:
                    self.reply_sheet.delete_row(i)
                    logger.info(f"成功删除关键词回复: {keyword}")
                    return True
                    
            return False
                
        except Exception as e:
            logger.error(f"删除关键词回复失败: {e}")
            return False
            
    async def load_from_sheet(self) -> List[Dict[str, str]]:
        """从 Google Sheet 加载封禁记录"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 获取所有记录
            records = self.ban_sheet.get_all_records()
            
            # 过滤出有效的记录
            valid_records = []
            for record in records:
                if record.get("操作时间") and record.get("用户ID"):
                    valid_records.append(record)
                    
            return valid_records
            
        except Exception as e:
            logger.error(f"加载封禁记录失败: {e}")
            return []
            
    async def save_to_sheet(self, record: Dict[str, str]) -> bool:
        """保存封禁记录到 Google Sheet"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 添加新记录
            self.ban_sheet.append_row([
                record.get("操作时间", ""),
                record.get("电报群组名称", ""),
                record.get("用户ID", ""),
                record.get("用户名", ""),
                record.get("名称", ""),
                record.get("操作管理", ""),
                record.get("理由", ""),
                record.get("操作", "")
            ])
            return True
            
        except Exception as e:
            logger.error(f"保存封禁记录失败: {e}")
            return False

# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # Base64编码的JSON凭证
BAN_RECORDS_SHEET = os.getenv("BAN_RECORDS_SHEET", "Ban&Mute Records")    # 封禁记录表名
KEYWORD_REPLIES_SHEET = os.getenv("KEYWORD_REPLIES_SHEET", "KeywordReplies")  # 关键词回复表名
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL', '')}{WEBHOOK_PATH}" if os.getenv("RENDER_EXTERNAL_URL") else None
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))
MAX_RECORDS_DISPLAY = 10
EXCEL_FILE = "ban_records.xlsx"

# 全局变量
ADMIN_USER_IDS = [int(id) for id in os.getenv("ADMIN_USER_IDS", "").split(",") if id]  # 管理员用户ID列表
TARGET_GROUP_ID = 1002444909093  # 目标群组ID
MONITORED_BOT_IDS = [7039829949]  # 要监听的机器人ID列表
bot_app = None
bot_initialized = False
ban_records = []
reply_keywords = {}
sheets_storage = GoogleSheetsStorage()  # 创建 GoogleSheetsStorage 实例

app = FastAPI()

async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查用户是否是管理员"""
    try:
        # 获取用户和聊天信息
        user = update.effective_user
        chat = update.effective_chat
        
        if not user or not chat:
            return False
            
        # 获取用户在群组中的状态
        member = await context.bot.get_chat_member(chat.id, user.id)
        
        # 检查用户是否是管理员或群主
        is_admin = member.status in ['administrator', 'creator']
        logger.info(f"Checking admin status for user {user.id}: {is_admin} (status: {member.status})")
        return is_admin
        
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

async def delete_message_later(message, delay: int = 120):  # Set delay to 2 minutes
    """在指定时间后删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"删除消息失败: {e}")

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/start命令"""
    user = update.effective_user
    chat = update.effective_chat
    
    # 获取当前时间
    current_time = datetime.now(TIMEZONE)
    hour = current_time.hour
    
    # 根据时间选择问候语
    if 5 <= hour < 12:
        greeting = "🌅 早安"
    elif 12 <= hour < 18:
        greeting = "☀️ 午安"
    else:
        greeting = "🌙 晚安"
    
    # 构建欢迎消息
    welcome_message = (
        f"{greeting}，{user.full_name if user else '朋友'}！\n\n"
        "🤖 我是封禁管理机器人，可以帮助你管理群组。\n\n"
        "📋 主要功能：\n"
        "├─ 👮 封禁管理\n"
        "│  ├─ /b - 封禁用户（回复消息使用）\n"
        "│  ├─ /m - 禁言用户（回复消息并指定时间）\n"
        "│  └─ /um - 解除禁言\n\n"
        "├─ 📊 记录管理\n"
        "│  ├─ /records - 查看封禁记录\n"
        "│  ├─ /search <关键词> - 搜索封禁记录\n"
        "│  └─ /export - 导出封禁记录\n\n"
        "├─ 📝 关键词回复\n"
        "│  └─ /reply - 管理关键词自动回复\n\n"
        "├─ 🌟 问候功能\n"
        "│  ├─ /morning - 早安问候\n"
        "│  ├─ /noon - 午安问候\n"
        "│  ├─ /night - 晚安问候\n"
        "│  └─ /comfort - 安慰消息\n\n"
        "└─ 🔄 消息转发\n"
        "   └─ 自动转发指定机器人的消息到目标群组\n\n"
        "⚠️ 注意：\n"
        "• 请确保机器人有管理员权限\n"
        "• 部分功能仅管理员可用\n"
        "• 使用前请仔细阅读命令说明\n"
        "• 关键词回复支持自定义链接和文本\n"
        "• 问候功能支持多种风格和随机彩蛋\n"
        "• 消息转发功能需要配置目标群组ID和监听机器人ID"
    )
    
    # 发送欢迎消息
    await update.message.reply_text(welcome_message)
    logger.info(f"新用户启动: {user.full_name if user else 'Unknown'} (ID: {user.id if user else 'Unknown'})")

async def ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理封禁命令"""
    if not await check_admin(update, context):
        return
        
    try:
        # 获取消息
        message = update.message
        if not message:
            return
            
        # 获取回复的消息
        reply_to_message = message.reply_to_message
        if not reply_to_message:
            await message.reply_text("请回复要封禁的用户消息")
            return
            
        # 获取用户信息
        user = reply_to_message.from_user
        if not user:
            await message.reply_text("无法获取用户信息")
            return
            
        # 获取群组信息
        chat = message.chat
        if not chat:
            await message.reply_text("无法获取群组信息")
            return
            
        # 获取封禁理由
        reason = " ".join(context.args) if context.args else "无理由"
        
        # 创建封禁记录
        banned_user_name = user.first_name  # Display name
        banned_username = f"@{user.username}" if user.username else "无"  # Use existing username with @
        context.chat_data["last_ban"] = {
            "operator_id": message.from_user.id,
            "chat_title": chat.title,
            "user_id": user.id,
            "banned_user_name": banned_user_name,
            "banned_username": banned_username
        }
        
        # 创建理由选择按钮
        keyboard = [
            [
                InlineKeyboardButton("广告", callback_data=f"ban_reason|{user.id}|{user.username}|广告"),
                InlineKeyboardButton("FUD", callback_data=f"ban_reason|{user.id}|{user.username}|FUD")
            ],
            [
                InlineKeyboardButton("带节奏", callback_data=f"ban_reason|{user.id}|{user.username}|带节奏"),
                InlineKeyboardButton("攻击他人", callback_data=f"ban_reason|{user.id}|{user.username}|攻击他人")
            ],
            [
                InlineKeyboardButton("诈骗", callback_data=f"ban_reason|{user.id}|{user.username}|诈骗")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 发送选择理由的消息
        await message.reply_text(
            f"请选择封禁用户 {user.first_name} 的理由：",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"处理封禁命令时出错: {e}")
        await message.reply_text("处理封禁命令时出错")

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理封禁原因选择"""
    query = update.callback_query
    await query.answer()
    
    try:
        action, user_id_str, username, reason = query.data.split("|")
        banned_user_id = int(user_id_str)
        last_ban = context.chat_data.get("last_ban", {})  # Ensure last_ban is defined
        banned_user_name = last_ban.get("banned_user_name", "")  # Get display name from context
        banned_username = f"@{username}" if username else "无"  # Use username from callback data
    except ValueError:
        error_msg = await query.message.reply_text("⚠️ 无效的回调数据")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 验证操作权限
    if query.from_user.id != last_ban.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ 只有执行封禁的管理员能选择原因")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 保存封禁记录
    try:
        success = await sheets_storage.save_to_sheet(
            {
                "操作时间": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                "电报群组名称": query.message.chat.title,
                "用户ID": banned_user_id,
                "用户名": banned_username,
                "名称": banned_user_name,
                "操作管理": query.from_user.full_name,
                "理由": reason,
                "操作": "封禁"
            }
        )
        
        if success:
            # 封禁用户并删除消息
            await context.bot.ban_chat_member(
                chat_id=query.message.chat.id,
                user_id=banned_user_id,
                revoke_messages=True  # 删除用户的所有消息
            )
            
            confirm_msg = await query.message.reply_text(f"✅ 已封禁用户 {banned_user_name} 并删除其消息 - 理由: {reason}")
            asyncio.create_task(delete_message_later(confirm_msg))
            asyncio.create_task(delete_message_later(query.message))
        else:
            error_msg = await query.message.reply_text("❌ 保存记录失败")
            asyncio.create_task(delete_message_later(error_msg))
            asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        asyncio.create_task(delete_message_later(query.message))
        logger.error(f"保存封禁原因失败: {e}")

async def mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理禁言命令"""
    if not await check_admin(update, context):
        return
        
    try:
        # 获取消息
        message = update.message
        if not message:
            return
            
        # 获取回复的消息
        reply_to_message = message.reply_to_message
        if not reply_to_message:
            await message.reply_text("请回复要禁言的用户消息")
            return
            
        # 获取用户信息
        user = reply_to_message.from_user
        if not user:
            await message.reply_text("无法获取用户信息")
            return
            
        # 获取群组信息
        chat = message.chat
        if not chat:
            await message.reply_text("无法获取群组信息")
            return
            
        # 获取禁言时间
        if len(context.args) < 1:
            await message.reply_text("请指定禁言时间，例如: /m 1d2h30m")
            return
        
        # 解析禁言时间
        duration_str = " ".join(context.args)
        try:
            # 解析时间格式
            days = 0
            hours = 0
            minutes = 0
            
            if "d" in duration_str:
                days = int(duration_str.split("d")[0])
                duration_str = duration_str.split("d")[1]
            if "h" in duration_str:
                hours = int(duration_str.split("h")[0])
                duration_str = duration_str.split("h")[1]
            if "m" in duration_str:
                minutes = int(duration_str.split("m")[0])
                
            duration = timedelta(days=days, hours=hours, minutes=minutes)
            until_date = datetime.now(TIMEZONE) + duration
            
        except ValueError:
            await message.reply_text("时间格式错误，请使用例如: 1d2h30m 的格式")
            return
            
        # 保存操作上下文
        banned_user_name = user.first_name  # Display name
        banned_username = f"@{user.username}" if user.username else "无"  # Use existing username with @
        context.chat_data["last_mute"] = {
            "operator_id": message.from_user.id,
            "chat_title": chat.title,
            "user_id": user.id,
            "banned_user_name": banned_user_name,
            "banned_username": banned_username,
            "duration": duration_str
        }
        
        # 创建理由选择按钮
        keyboard = [
            [
                InlineKeyboardButton("广告", callback_data=f"mute_reason|{user.id}|{user.username}|广告"),
                InlineKeyboardButton("FUD", callback_data=f"mute_reason|{user.id}|{user.username}|FUD")
            ],
            [
                InlineKeyboardButton("带节奏", callback_data=f"mute_reason|{user.id}|{user.username}|带节奏"),
                InlineKeyboardButton("攻击他人", callback_data=f"mute_reason|{user.id}|{user.username}|攻击他人")
            ],
            [
                InlineKeyboardButton("诈骗", callback_data=f"mute_reason|{user.id}|{user.username}|诈骗")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 发送选择理由的消息
        await message.reply_text(
            f"请选择禁言用户 {user.first_name} 的理由：",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"处理禁言命令时出错: {e}")
        await message.reply_text("处理禁言命令时出错")

async def mute_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理禁言原因选择"""
    query = update.callback_query
    await query.answer()
    
    try:
        action, user_id_str, username, reason = query.data.split("|")
        muted_user_id = int(user_id_str)
        last_mute = context.chat_data.get("last_mute", {})  # Ensure last_mute is defined
        banned_user_name = last_mute.get("banned_user_name", "")  # Get display name from context
        banned_username = f"@{username}" if username else "无"  # Use username from callback data
    except ValueError:
        return  # 无效的回调数据，直接返回
    
    # 验证操作权限
    if query.from_user.id != last_mute.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ 只有执行禁言的管理员能选择原因")
        asyncio.create_task(delete_message_later(error_msg))
        return  # 只有执行操作的管理员能选择原因，其他人点击不做任何处理
    
    # 保存记录
    try:
        success = await sheets_storage.save_to_sheet(
            {
                "操作时间": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                "电报群组名称": last_mute.get("chat_title", query.message.chat.title),
                "用户ID": muted_user_id,
                "用户名": banned_username,
                "名称": banned_user_name,
                "操作管理": query.from_user.full_name,
                "理由": reason,
                "操作": f"禁言 {last_mute.get('duration', '')}"  # Move duration to operation field
            }
        )
        
        if success:
            # 禁言用户
            await context.bot.restrict_chat_member(
                chat_id=query.message.chat.id,
                user_id=muted_user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                    can_change_info=False,
                ),
                until_date=datetime.now(TIMEZONE) + timedelta(minutes=1)  # Example duration
            )
            
            confirm_msg = await query.message.reply_text(f"✅ 已禁言用户 {banned_user_name} - 理由: {reason}")
            asyncio.create_task(delete_message_later(confirm_msg))
            asyncio.create_task(delete_message_later(query.message))
        else:
            error_msg = await query.message.reply_text("❌ 保存记录失败")
            asyncio.create_task(delete_message_later(error_msg))
            asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 操作失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        asyncio.create_task(delete_message_later(query.message))
        logger.error(f"禁言用户失败: {e}")

async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理解除禁言命令"""
    if not await check_admin(update, context):
        return
        
    try:
        # 获取消息
        message = update.message
        if not message:
            return
            
        # 获取群组信息
        chat = message.chat
        if not chat:
            await message.reply_text("无法获取群组信息")
            return
            
        # 检查是否提供了用户名
        if not context.args:
            await message.reply_text("请使用 @username 指定要解除禁言的用户")
            return
            
        # 获取用户名并移除 @ 符号
        username = context.args[0].lstrip('@')
        if not username:
            await message.reply_text("请提供有效的用户名")
            return
            
        try:
            # 尝试通过用户名获取用户
            chat_member = await context.bot.get_chat_member(chat.id, username)
            user = chat_member.user
        except Exception as e:
            logger.error(f"通过用户名获取用户失败: {e}")
            # 尝试通过用户ID获取
            try:
                # 如果用户名是纯数字，尝试作为用户ID处理
                if username.isdigit():
                    chat_member = await context.bot.get_chat_member(chat.id, int(username))
                    user = chat_member.user
                else:
                    raise Exception("用户名无效")
            except Exception as e:
                logger.error(f"通过用户ID获取用户失败: {e}")
                await message.reply_text(f"无法找到用户 @{username}，请确保用户名正确且用户在群组中")
                return
            
        # 创建解除禁言记录
        record = {
            "操作时间": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
            "电报群组名称": chat.title,
            "用户ID": str(user.id),
            "用户名": f"@{user.username}" if user.username else "无",
            "名称": user.first_name,
            "操作管理": message.from_user.first_name,
            "理由": "解除禁言",
            "操作": "解除禁言"
        }
        
        # 保存到 Google Sheet
        success = await sheets_storage.save_to_sheet(record)
        if not success:
            await message.reply_text("保存解除禁言记录失败")
            return
            
        # 添加到内存中的记录列表
        ban_records.append(record)
        
        # 解除禁言
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
                can_pin_messages=True,
                can_change_info=True,
            )
        )
        
        # 发送确认消息
        await message.reply_text(
            f"✅ 已解除禁言用户 {user.first_name} (ID: {user.id})\n"
            f"⏰ 时间: {record['操作时间']}"
        )
        
    except Exception as e:
        logger.error(f"处理解除禁言命令时出错: {e}")
        await message.reply_text("处理解除禁言命令时出错")

async def keyword_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词回复命令"""
    if not await check_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not context.args:
        # 创建主菜单按钮
        keyboard = [
            [
                InlineKeyboardButton("➕ 添加回复", callback_data="reply:add"),
                InlineKeyboardButton("✏️ 修改回复", callback_data="reply:edit")
            ],
            [
                InlineKeyboardButton("🗑️ 删除回复", callback_data="reply:delete"),
                InlineKeyboardButton("📋 查看列表", callback_data="reply:list")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "📝 关键词回复管理\n\n"
            "请选择要执行的操作：",
            reply_markup=reply_markup
        )
        return

async def reply_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词回复的回调"""
    query = update.callback_query
    await query.answer()
    
    if not await check_admin(update, context):
        await query.message.edit_text("❌ 只有管理员可以使用此命令")
        return
        
    try:
        action_type, *action_data = query.data.split(":")
        action = action_data[0] if action_data else ""
        
        if action_type != "reply":
            await query.message.edit_text("❌ 无效的操作")
            return
            
        if action == "add":
            # 开始添加流程
            context.user_data["reply_flow"] = {
                "step": 1,
                "action": "add"
            }
            # 发送新消息而不是编辑原消息
            await query.message.reply_text(
                "📝 添加关键词回复\n\n"
                "第1步：请回复此消息，输入关键词\n"
                "输入 /cancel 取消操作"
            )
            
        elif action == "edit":
            # 获取所有关键词
            replies = await sheets_storage.get_keyword_replies()
            if not replies:
                await query.message.edit_text("暂无关键词回复可修改")
                return
                
            # 创建关键词选择按钮
            keyboard = []
            for reply in replies:
                keyboard.append([InlineKeyboardButton(
                    f"🔑 {reply['关键词']}",
                    callback_data=f"reply:edit_keyword:{reply['关键词']}"
                )])
                
            keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="reply:menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "📝 修改关键词回复\n\n"
                "请选择要修改的关键词：",
                reply_markup=reply_markup
            )
            
        elif action == "delete":
            # 获取所有关键词
            replies = await sheets_storage.get_keyword_replies()
            if not replies:
                await query.message.edit_text("暂无关键词回复可删除")
                return
                
            # 创建关键词选择按钮
            keyboard = []
            for reply in replies:
                keyboard.append([InlineKeyboardButton(
                    f"🗑️ {reply['关键词']}",
                    callback_data=f"reply:delete_keyword:{reply['关键词']}"
                )])
                
            keyboard.append([InlineKeyboardButton("🔙 返回", callback_data="reply:menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "🗑️ 删除关键词回复\n\n"
                "请选择要删除的关键词：",
                reply_markup=reply_markup
            )
            
        elif action == "list":
            replies = await sheets_storage.get_keyword_replies()
            
            if not replies:
                await query.message.edit_text("暂无关键词回复配置")
                return
                
            message = "📋 关键词回复列表:\n\n"
            for reply in replies:
                message += (
                    f"🔑 关键词: {reply['关键词']}\n"
                    f"💬 回复: {reply['回复内容']}\n"
                )
                if reply.get("链接"):
                    message += f"🔗 链接: {reply['链接']} ({reply.get('链接文本', '点击这里')})\n"
                message += "━━━━━━━━━━━━━━\n"
                
            keyboard = [[InlineKeyboardButton("🔙 返回", callback_data="reply:menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(message, reply_markup=reply_markup)
            
        elif action == "menu":
            # 返回主菜单
            keyboard = [
                [
                    InlineKeyboardButton("➕ 添加回复", callback_data="reply:add"),
                    InlineKeyboardButton("✏️ 修改回复", callback_data="reply:edit")
                ],
                [
                    InlineKeyboardButton("🗑️ 删除回复", callback_data="reply:delete"),
                    InlineKeyboardButton("📋 查看列表", callback_data="reply:list")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "📝 关键词回复管理\n\n"
                "请选择要执行的操作：",
                reply_markup=reply_markup
            )
            
        elif action == "edit_keyword":
            keyword = action_data[1] if len(action_data) > 1 else ""
            replies = await sheets_storage.get_keyword_replies()
            existing_reply = next((r for r in replies if r["关键词"] == keyword), None)
            
            if not existing_reply:
                await query.message.edit_text(f"❌ 未找到关键词: {keyword}")
                return
                
            # 开始修改流程
            context.user_data["reply_flow"] = {
                "step": 2,
                "action": "edit",
                "keyword": keyword,
                "existing_reply": existing_reply
            }
            
            await query.message.edit_text(
                f"📝 修改关键词回复: {keyword}\n\n"
                f"当前回复内容: {existing_reply['回复内容']}\n"
                f"当前链接: {existing_reply.get('链接', '无')}\n"
                f"当前链接文本: {existing_reply.get('链接文本', '无')}\n\n"
                "请输入新的回复内容\n"
                "输入 /cancel 取消操作"
            )
            
        elif action == "delete_keyword":
            keyword = action_data[1] if len(action_data) > 1 else ""
            
            # 创建确认按钮
            keyboard = [
                [
                    InlineKeyboardButton("✅ 确认删除", callback_data=f"reply:confirm_delete:{keyword}"),
                    InlineKeyboardButton("❌ 取消", callback_data="reply:delete")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                f"⚠️ 确认删除关键词回复: {keyword}\n\n"
                "此操作不可恢复！",
                reply_markup=reply_markup
            )
            
        elif action == "confirm_delete":
            keyword = action_data[1] if len(action_data) > 1 else ""
            success = await sheets_storage.delete_keyword_reply(keyword)
            
            if success:
                await query.message.edit_text(f"✅ 已删除关键词回复: {keyword}")
            else:
                await query.message.edit_text(f"❌ 删除失败: {keyword}")
                
            # 返回主菜单
            await asyncio.sleep(2)
            keyboard = [
                [
                    InlineKeyboardButton("➕ 添加回复", callback_data="reply:add"),
                    InlineKeyboardButton("✏️ 修改回复", callback_data="reply:edit")
                ],
                [
                    InlineKeyboardButton("🗑️ 删除回复", callback_data="reply:delete"),
                    InlineKeyboardButton("📋 查看列表", callback_data="reply:list")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "📝 关键词回复管理\n\n"
                "请选择要执行的操作：",
                reply_markup=reply_markup
            )
            
        else:
            await query.message.edit_text("❌ 无效的操作")
            
    except Exception as e:
        logger.error(f"处理回调时出错: {e}")
        await query.message.edit_text("❌ 操作失败，请重试")

async def handle_reply_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词回复的流程"""
    logger.info("handle_reply_flow called")
    
    if not update.message or not update.message.text:
        logger.warning("No message or text in update")
        return
        
    if "reply_flow" not in context.user_data:
        logger.warning("No reply_flow in user_data")
        return
        
    # 检查是否是回复机器人的消息
    if not update.message.reply_to_message:
        logger.warning("No reply_to_message found")
        return
        
    if not update.message.reply_to_message.from_user.is_bot:
        logger.warning(f"Message is not a reply to bot's message. Reply message: {update.message.reply_to_message.text}")
        return
        
    flow = context.user_data["reply_flow"]
    text = update.message.text
    
    logger.info(f"Processing reply flow: step={flow.get('step')}, action={flow.get('action')}, text={text}")
    logger.info(f"Reply to message: {update.message.reply_to_message.text}")
    
    try:
        if flow["step"] == 1:
            # 第一步：获取关键词
            flow["keyword"] = text
            flow["step"] = 2
            context.user_data["reply_flow"] = flow  # 确保状态被保存
            logger.info(f"Step 1 completed, keyword set to: {text}")
            sent_message = await update.message.reply_text(
                f"📝 关键词: {text}\n\n"
                "第2步：请回复此消息，输入回复内容\n"
                "输入 /cancel 取消操作"
            )
            asyncio.create_task(delete_message_later(sent_message, delay=300))
            
        elif flow["step"] == 2:
            # 第二步：获取回复内容
            flow["reply_text"] = text
            flow["step"] = 3
            context.user_data["reply_flow"] = flow  # 确保状态被保存
            logger.info(f"Step 2 completed, reply text set to: {text}")
            sent_message = await update.message.reply_text(
                f"📝 关键词: {flow['keyword']}\n"
                f"💬 回复内容: {text}\n\n"
                "第3步：请回复此消息，输入链接和链接文本（可选）\n"
                "格式：链接 [链接文本]文本\n"
                "例如：https://example.com [链接文本]点击这里\n"
                "直接回复 /skip 跳过此步\n"
                "输入 /cancel 取消操作"
            )
            asyncio.create_task(delete_message_later(sent_message, delay=300))
            
        elif flow["step"] == 3:
            # 第三步：获取链接信息
            if text.lower() == "/skip":
                link = ""
                link_text = ""
                logger.info("Skipping link step")
            else:
                # 解析链接和链接文本
                if "[链接文本]" in text:
                    parts = text.split("[链接文本]")
                    link = parts[0].strip()
                    link_text = parts[1].strip() if len(parts) > 1 else "点击这里"
                else:
                    link = text.strip()
                    link_text = "点击这里"
            
            logger.info(f"Step 3 completed, link={link}, link_text={link_text}")
            
            # 保存回复
            action_text = "修改" if flow["action"] == "edit" else "添加"
            
            if flow["action"] == "edit":
                # 修改时先删除旧的
                await sheets_storage.delete_keyword_reply(flow["keyword"])
            
            success = await sheets_storage.add_keyword_reply(
                keyword=flow["keyword"],
                reply_text=flow["reply_text"],
                link=link,
                link_text=link_text
            )
            
            if success:
                sent_message = await update.message.reply_text(
                    f"✅ 已{action_text}关键词回复:\n\n"
                    f"🔑 关键词: {flow['keyword']}\n"
                    f"💬 回复: {flow['reply_text']}\n"
                    f"🔗 链接: {link if link else '无'}\n"
                    f"📝 链接文本: {link_text if link else '无'}"
                )
            else:
                sent_message = await update.message.reply_text(f"❌ {action_text}关键词回复失败")
            
            # 设置定时删除消息
            asyncio.create_task(delete_message_later(sent_message, delay=300))
            
            # 清理流程数据
            del context.user_data["reply_flow"]
            logger.info("Reply flow completed and cleaned up")
            
    except Exception as e:
        logger.error(f"Error in handle_reply_flow: {e}")
        sent_message = await update.message.reply_text("❌ 操作失败，请重试")
        asyncio.create_task(delete_message_later(sent_message, delay=300))
        # 清理流程数据
        if "reply_flow" in context.user_data:
            del context.user_data["reply_flow"]

async def auto_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """自动回复关键词消息"""
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.lower().strip()
    replies = await sheets_storage.get_keyword_replies()
    
    for reply in replies:
        if reply["关键词"].lower() in text:
            # 构建回复内容
            reply_text = reply["回复内容"]
            
            # 如果有链接，添加按钮
            if reply.get("链接"):
                keyboard = [[InlineKeyboardButton(
                    reply.get("链接文本", "点击这里"), 
                    url=reply["链接"]
                )]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # 添加表情和格式化
                formatted_reply = (
                    f"✨ {reply_text}\n\n"
                    f"💡 点击下方按钮了解更多："
                )
                
                sent_message = await update.message.reply_text(
                    formatted_reply,
                    reply_markup=reply_markup
                )
            else:
                # 没有链接时也添加一些美化
                formatted_reply = (
                    f"✨ {reply_text}\n\n"
                    f"💫 需要帮助可以随时问我哦~"
                )
                sent_message = await update.message.reply_text(formatted_reply)
            
            # 设置定时删除消息
            asyncio.create_task(delete_message_later(sent_message, delay=300))  # 5分钟后删除
            break

async def records_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/records命令"""
    if not await check_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    global ban_records
    
    try:
        if not ban_records:
            msg = await update.message.reply_text("暂无封禁记录")
            asyncio.create_task(delete_message_later(msg, delay=10))
            return
        
        # 获取最近的记录
        recent_records = sorted(ban_records, key=lambda x: x.get("操作时间", ""), reverse=True)[:MAX_RECORDS_DISPLAY]
        
        message = "📊 最近封禁记录:\n\n"
        for record in recent_records:
            record_time = datetime.fromisoformat(record["操作时间"]).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
            message += (
                f"🕒 {record_time}\n"
                f"👤 用户: {record.get('名称', '未知')} "
                f"(ID: {record.get('用户ID', '未知')}) "
                f"[{record.get('用户名', '无')}]\n"
                f"👮 管理员: {record.get('操作管理', '未知')}\n"
                f"📝 原因: {record.get('理由', '未填写')}\n"
                f"💬 群组: {record.get('电报群组名称', '未知')}\n"
                f"🔧 操作: {record.get('操作', '未知')}\n"
                "━━━━━━━━━━━━━━\n"
            )
        
        msg = await update.message.reply_text(message)
        asyncio.create_task(delete_message_later(msg, delay=30))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 获取记录失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"获取封禁记录失败: {e}")

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/search命令"""
    if not await check_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not context.args:
        msg = await update.message.reply_text("请输入搜索关键词，例如: /search 广告")
        asyncio.create_task(delete_message_later(msg))
        return

    keyword = " ".join(context.args)
    global ban_records

    try:
        # 在内存中搜索记录
        matched_records = [
            record for record in ban_records
            if keyword.lower() in record.get("理由", "").lower() or
               keyword.lower() in record.get("名称", "").lower() or
               keyword.lower() in record.get("用户名", "").lower() or
               keyword.lower() in record.get("电报群组名称", "").lower()
        ]

        if not matched_records:
            msg = await update.message.reply_text("未找到匹配的封禁记录")
            asyncio.create_task(delete_message_later(msg, delay=10))
            return

        message = f"🔍 搜索结果 (关键词: {keyword}):\n\n"
        for record in matched_records[:MAX_RECORDS_DISPLAY]:
            record_time = datetime.fromisoformat(record["操作时间"]).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
            message += (
                f"🕒 {record_time}\n"
                f"👤 用户: {record.get('名称', '未知')} "
                f"(ID: {record.get('用户ID', '未知')}) "
                f"[{record.get('用户名', '无')}]\n"
                f"👮 管理员: {record.get('操作管理', '未知')}\n"
                f"📝 原因: {record.get('理由', '未填写')}\n"
                f"💬 群组: {record.get('电报群组名称', '未知')}\n"
                f"🔧 操作: {record.get('操作', '未知')}\n"
                "━━━━━━━━━━━━━━\n"
            )

        msg = await update.message.reply_text(message)
        asyncio.create_task(delete_message_later(msg, delay=60))

    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 搜索失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"搜索封禁记录失败: {e}")

async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/export命令，发送Excel文件"""
    if not await check_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    global ban_records
    
    try:
        if not ban_records:
            msg = await update.message.reply_text("暂无封禁记录可导出")
            asyncio.create_task(delete_message_later(msg))
            return
        
        # 确保Excel文件是最新的
        df = pd.DataFrame(ban_records)
        
        # 确保所有字段都存在
        required_columns = [
            "操作时间", "电报群组名称", "用户ID", 
            "用户名", "名称", "操作管理", 
            "理由", "操作"
        ]
        
        # 添加缺失的列
        for col in required_columns:
            if col not in df.columns:
                df[col] = ""
        
        # 重新排序列
        df = df[required_columns]
        
        # 保存到Excel
        df.to_excel(EXCEL_FILE, index=False, engine="openpyxl")
        
        # 发送文件
        with open(EXCEL_FILE, "rb") as file:
            await update.message.reply_document(
                document=file,
                caption="📊 封禁记录导出",
                filename="ban_records.xlsx"
            )
        
        logger.info("封禁记录已导出")
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 导出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"导出封禁记录失败: {e}")

async def morning_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理早安问候"""
    user = update.effective_user
    greetings = [
        # 王者风范系列 (30条)
        f"👑 {user.first_name}早安！全宇宙最可爱的生物已上线~",
        f"🌟 {user.first_name}早上好！今日份的 superstar 正在启动中...",
        f"🎯 {user.first_name}早安！精准命中我心巴的选手来了~",
        f"💎 {user.first_name}早上好！人类高质量样本开始营业啦！",
        f"✨ {user.first_name}早安！移动的荷尔蒙发射器已激活~",
        f"🦄 {user.first_name}早上好！稀有度SSR的你终于睡醒啦~",
        f"🍯 {user.first_name}早安！甜度超标警告！今日糖分已充满~",
        f"🎁 {user.first_name}早上好！上帝最得意的作品在吗？",
        f"🌍 {user.first_name}早安！地球今日因你开机而转动~",
        f"⚡ {user.first_name}早上好！行走的芳心纵火犯请签收~",
        f"🧨 {user.first_name}早安！人形开心果开始今日爆破~",
        f"🎠 {user.first_name}早上好！迪士尼在逃公主/王子上线~",
        f"🍓 {user.first_name}早安！甜心狙击手准备就绪！",
        f"🛸 {user.first_name}早上好！外星系偷跑的萌物被我们发现啦~",
        f"🎪 {user.first_name}早上好！马戏团最抢手的明星演员来咯~",
        f"🎆 {user.first_name}早上好！烟花秀主火炬手已就位~",
        f"🧿 {user.first_name}早上好！锦鲤本鲤开始散发好运~",
        f"🎨 {user.first_name}早上好！梵高看了都点赞的艺术品醒啦~",
        f"🍩 {user.first_name}早上好！甜甜圈中间的糖霜来咯~",
        f"🎯 {user.first_name}早上好！丘比特之箭准备发射~",
        f"🎻 {user.first_name}早上好！我的耳朵说想听你说话~",
        f"🎭 {user.first_name}早安！奥斯卡欠你小金人哦~",
        # 基础问候 (20条)
        f"🌞 {user.first_name}早安呀！今天也是元气满满的一天呢~",
        f"☕ {user.first_name}早上好！要记得吃早餐哦！",
        f"🐦 早起的{user.first_name}有虫吃！今天也要加油鸭~",
        f"🌻 {user.first_name}早安！你今天的笑容价值100分！",
        f"🍞 面包会有的，{user.first_name}的好运也会有的，早上好！",
        f"✨ {user.first_name}今天也要闪闪发光哦！早安~",
        f"🐱 喵~{user.first_name}早上好！本机器人已为你充满电啦！",
        f"🌄 {user.first_name}这么早就起床啦？真是自律的小可爱呢！",
        f"🍵 晨光微熹，{user.first_name}早安！今天也要对自己好一点~",
        f"🚀 {user.first_name}早上好！准备开始今天的冒险了吗？",
    ]
    
    # 随机选择一条问候语
    reply = random.choice(greetings)
    
    # 10%概率附加特别彩蛋
    if random.random() < 0.1:
        reply += "\n\n🎁 彩蛋：你是今天第{}个说早安的天使~".format(random.randint(1,100))
    sent_message = await update.message.reply_text(reply)
    logger.info(f"🌅 向 {user.full_name} 发送了早安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def noon_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理午安问候"""
    user = update.effective_user
    NOON_GREETINGS = [
        # 温馨系列
        f"☀️ {user.first_name}午安！阳光正好，微风不燥~",
        f"🌤️ {user.first_name}中午好！记得好好休息哦~",
        f"🍱 {user.first_name}午安！午餐时间到啦~",
        f"🌿 {user.first_name}中午好！让眼睛休息一下吧~",
        
        # 幽默系列
        f"😴 {user.first_name}午安！要不要来个小憩？",
        f"🍜 {user.first_name}中午好！干饭人干饭魂~",
        f"🌞 {user.first_name}午安！太阳公公说该休息啦~",
        f"💤 {user.first_name}中午好！充电时间到~",
        
        # 励志系列
        f"💪 {user.first_name}午安！下午继续加油哦~",
        f"🌟 {user.first_name}中午好！休息是为了更好的前进~",
        f"🌻 {user.first_name}午安！像向日葵一样充满活力~",
        
        # 特别彩蛋
        f"🎁 {user.first_name}午安！你是今天第{random.randint(1,100)}个说午安的小可爱~",
        f"✨ {user.first_name}中午好！今天的午餐主题是：{random.choice(['美食','休息','充电','放松'])}~"
    ]
    
    # 随机选择一条问候语
    reply = random.choice(NOON_GREETINGS)
    
    # 10%概率附加彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "☀️", "💫", "🌤️"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个说午安的小可爱~"
    
    sent_message = await update.message.reply_text(reply)
    logger.info(f"☀️ 向 {user.full_name} 发送了午安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def goodnight_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理晚安问候"""
    user = update.effective_user
    GOODNIGHT_GREETINGS = [
        # 温馨系列
        f"🌙 {user.first_name}晚安！愿你有个甜美的梦~",
        f"✨ {user.first_name}晚安！星星会守护你的梦~",
        f"🌛 {user.first_name}晚安！月亮会照亮你的梦~",
        f"🛏️ {user.first_name}晚安！被子已经暖好啦~",
        
        # 幽默系列
        f"😴 {user.first_name}晚安！再不睡就要变成熊猫啦~",
        f"🌙 {user.first_name}晚安！梦里记得给我留个位置~",
        f"🛌 {user.first_name}晚安！床说它想你了~",
        f"💤 {user.first_name}晚安！明天见，小懒虫~",
        
        # 励志系列
        f"🌠 {user.first_name}晚安！今天的你很棒，明天继续加油~",
        f"🌟 {user.first_name}晚安！休息是为了更好的明天~",
        f"🌙 {user.first_name}晚安！养精蓄锐，明天再战~",
        
        # 特别彩蛋
        f"🌙 {user.first_name}晚安！今晚的梦境主题是：{random.choice(['冒险','美食','旅行','童话'])}~",
        f"✨ {user.first_name}晚安！你是今天第{random.randint(1,100)}个说晚安的小可爱~"
    ]
    
    # 随机选择一条问候语
    reply = random.choice(GOODNIGHT_GREETINGS)
    
    # 10%概率附加彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "🌙", "💫", "🌠"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个说晚安的小可爱~"
    
    sent_message = await update.message.reply_text(reply)
    logger.info(f"🌙 向 {user.full_name} 发送了晚安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def comfort_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理安慰命令"""
    user = update.effective_user
    COMFORT_MESSAGES = [
        # 温暖系列
        f"🤗 {user.first_name}，抱抱你~ 一切都会好起来的",
        f"💖 {user.first_name}，你并不孤单，我在这里陪着你",
        f"✨ {user.first_name}，风雨过后总会有彩虹",
        f"🌱 {user.first_name}，每个低谷都是新的开始",
        
        # 鼓励系列
        f"💪 {user.first_name}，你比想象中更坚强",
        f"🌟 {user.first_name}，困难只是暂时的，你一定能克服",
        f"🌻 {user.first_name}，像向日葵一样，永远面向阳光",
        f"🌈 {user.first_name}，生活就像彩虹，需要经历风雨才能看到美丽",
        
        # 治愈系列
        f"🫂 {user.first_name}，给你一个温暖的拥抱",
        f"🌙 {user.first_name}，让烦恼随月光消散",
        f"🌊 {user.first_name}，让心情像海浪一样平静",
        f"🌿 {user.first_name}，深呼吸，放松心情",
        
        # 特别彩蛋
        f"🎁 {user.first_name}，送你一份勇气大礼包：{random.choice(['坚持','希望','勇气','信心'])}",
        f"✨ {user.first_name}，你是第{random.randint(1,100)}个需要安慰的小可爱，但你是最特别的"
    ]
    
    # 随机选择一条安慰语
    reply = random.choice(COMFORT_MESSAGES)
    
    # 10%概率附加彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "💫", "🎁", "💝"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个需要安慰的小可爱~"
    
    sent_message = await update.message.reply_text(reply)
    logger.info(f"🤗 向 {user.full_name} 发送了安慰消息")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.strip().lower()  # 转换为小写进行比较
    logger.info(f"Processing message: {text}")
    
    # 早安关键词（转换为小写进行比较）
    morning_keywords = [kw.lower() for kw in ["早安", "早上好", "good morning", "morning", "gm", "早"]]
    # 午安关键词
    noon_keywords = [kw.lower() for kw in ["午安", "中午好", "good noon", "noon"]]
    # 晚安关键词
    night_keywords = [kw.lower() for kw in ["晚安", "晚上好", "good night", "night", "gn"]]
    
    # 精确匹配关键词（不区分大小写）
    if text in morning_keywords:
        logger.info(f"Morning greeting triggered for exact match: {text}")
        await morning_greeting_handler(update, context)
    elif text in noon_keywords:
        logger.info(f"Noon greeting triggered for exact match: {text}")
        await noon_greeting_handler(update, context)
    elif text in night_keywords:
        logger.info(f"Night greeting triggered for exact match: {text}")
        await goodnight_greeting_handler(update, context)

async def ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理封禁命令"""
    if not await check_admin(update, context):
        return
        
    try:
        # 获取消息
        message = update.message
        if not message:
            return
            
        # 获取回复的消息
        reply_to_message = message.reply_to_message
        if not reply_to_message:
            await message.reply_text("请回复要封禁的用户消息")
            return
            
        # 获取用户信息
        user = reply_to_message.from_user
        if not user:
            await message.reply_text("无法获取用户信息")
            return
            
        # 获取群组信息
        chat = message.chat
        if not chat:
            await message.reply_text("无法获取群组信息")
            return
            
        # 获取封禁理由
        reason = " ".join(context.args) if context.args else "无理由"
        
        # 创建封禁记录
        banned_user_name = user.first_name  # Display name
        banned_username = f"@{user.username}" if user.username else "无"  # Use existing username with @
        context.chat_data["last_ban"] = {
            "operator_id": message.from_user.id,
            "chat_title": chat.title,
            "user_id": user.id,
            "banned_user_name": banned_user_name,
            "banned_username": banned_username
        }
        
        # 创建理由选择按钮
        keyboard = [
            [
                InlineKeyboardButton("广告", callback_data=f"ban_reason|{user.id}|{user.username}|广告"),
                InlineKeyboardButton("FUD", callback_data=f"ban_reason|{user.id}|{user.username}|FUD")
            ],
            [
                InlineKeyboardButton("带节奏", callback_data=f"ban_reason|{user.id}|{user.username}|带节奏"),
                InlineKeyboardButton("攻击他人", callback_data=f"ban_reason|{user.id}|{user.username}|攻击他人")
            ],
            [
                InlineKeyboardButton("诈骗", callback_data=f"ban_reason|{user.id}|{user.username}|诈骗")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 发送选择理由的消息
        await message.reply_text(
            f"请选择封禁用户 {user.first_name} 的理由：",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"处理封禁命令时出错: {e}")
        await message.reply_text("处理封禁命令时出错")

async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理解除封禁命令"""
    if not await check_admin(update, context):
        return
        
    try:
        # 获取消息
        message = update.message
        if not message:
            return
            
        # 获取群组信息
        chat = message.chat
        if not chat:
            await message.reply_text("无法获取群组信息")
            return
            
        # 检查是否提供了用户名
        if not context.args:
            await message.reply_text("请使用 @username 指定要解除封禁的用户")
            return
            
        # 获取用户名并移除 @ 符号
        username = context.args[0].lstrip('@')
        if not username:
            await message.reply_text("请提供有效的用户名")
            return
            
        try:
            # 获取用户信息
            chat_member = await context.bot.get_chat_member(chat.id, username)
            user = chat_member.user
        except Exception as e:
            await message.reply_text(f"无法找到用户 @{username}")
            return
            
        # 创建解除封禁记录
        record = {
            "操作时间": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
            "电报群组名称": chat.title,
            "用户ID": str(user.id),
            "用户名": f"@{user.username}" if user.username else "无",
            "名称": user.first_name,
            "操作管理": message.from_user.first_name,
            "理由": "解除封禁",
            "操作": "解除封禁"
        }
        
        # 保存到 Google Sheet
        success = await sheets_storage.save_to_sheet(record)
        if not success:
            await message.reply_text("保存解除封禁记录失败")
            return
            
        # 添加到内存中的记录列表
        ban_records.append(record)
        
        # 解除封禁
        await context.bot.unban_chat_member(
            chat_id=chat.id,
            user_id=user.id
        )
        
        # 发送确认消息
        await message.reply_text(
            f"✅ 已解除封禁用户 {user.first_name} (ID: {user.id})\n"
            f"⏰ 时间: {record['操作时间']}"
        )
        
    except Exception as e:
        logger.error(f"处理解除封禁命令时出错: {e}")
        await message.reply_text("处理解除封禁命令时出错")

async def chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理群组成员变更事件"""
    try:
        # 获取变更信息
        chat_member = update.chat_member
        if not chat_member:
            return
            
        # 获取变更前后的状态
        old_status = chat_member.old_chat_member.status
        new_status = chat_member.new_chat_member.status
        
        # 检查是否是踢出或封禁操作
        if (old_status == "member" and 
            (new_status == "kicked" or new_status == "banned")):
            
            # 获取用户信息
            user = chat_member.new_chat_member.user
            if not user:
                return
                
            # 获取群组信息
            chat = update.effective_chat
            if not chat:
                return
                
            # 获取操作者信息
            from_user = update.effective_user
            if not from_user:
                return
                
            # 创建记录
            record = {
                "操作时间": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                "电报群组名称": chat.title,
                "用户ID": str(user.id),
                "用户名": user.username or "无",
                "名称": user.first_name,
                "操作管理": from_user.first_name,
                "理由": "通过 Telegram 界面操作",
                "操作": "封禁"  # 将踢出改为封禁
            }
            
            # 保存到 Google Sheet
            success = await sheets_storage.save_to_sheet(record)
            if not success:
                logger.error("保存封禁记录失败")
                return
                
            # 添加到内存中的记录列表
            ban_records.append(record)
            
            logger.info(
                f"记录到封禁操作: {user.first_name} (ID: {user.id}) "
                f"在群组 {chat.title} 被 {from_user.first_name} 封禁"
            )
            
    except Exception as e:
        logger.error(f"处理群组成员变更事件时出错: {e}")

async def forward_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息转发"""
    if not update.message or not update.message.from_user:
        return
        
    # 检查消息是否来自要监听的机器人
    if update.message.from_user.id in MONITORED_BOT_IDS:
        try:
            # 获取消息内容
            message = update.message
            
            # 转发到目标群组
            if TARGET_GROUP_ID:
                try:
                    # 直接转发消息
                    await message.forward(chat_id=TARGET_GROUP_ID)
                    logger.info(f"已转发来自机器人 {message.from_user.first_name} 的消息到群组 {TARGET_GROUP_ID}")
                except Exception as e:
                    logger.error(f"转发消息到群组 {TARGET_GROUP_ID} 失败: {e}")
                    
        except Exception as e:
            logger.error(f"处理转发消息时出错: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global bot_app, bot_initialized, ban_records
    
    try:
        # 初始化 Telegram Bot
        bot_app = (
            ApplicationBuilder()
            .token(TOKEN)
            .build()
        )
        
        # 添加命令处理器
        bot_app.add_handler(CommandHandler("start", start_handler))
        bot_app.add_handler(CommandHandler("k", ban_handler))
        bot_app.add_handler(CommandHandler("m", mute_handler))
        bot_app.add_handler(CommandHandler("um", unmute_handler))
        bot_app.add_handler(CommandHandler("records", records_handler))
        bot_app.add_handler(CommandHandler("search", search_handler))
        bot_app.add_handler(CommandHandler("export", export_handler))
        bot_app.add_handler(CommandHandler("reply", keyword_reply_handler))
        bot_app.add_handler(CommandHandler("morning", morning_greeting_handler))
        bot_app.add_handler(CommandHandler("noon", noon_greeting_handler))
        bot_app.add_handler(CommandHandler("night", goodnight_greeting_handler))
        bot_app.add_handler(CommandHandler("comfort", comfort_handler))
        bot_app.add_handler(CommandHandler("ub", unban_handler))
        
        # 添加回调处理器
        bot_app.add_handler(CallbackQueryHandler(ban_reason_handler, pattern="^ban_reason"))
        bot_app.add_handler(CallbackQueryHandler(mute_reason_handler, pattern="^mute_reason"))
        bot_app.add_handler(CallbackQueryHandler(reply_callback_handler, pattern="^reply:"))
        
        # 添加消息处理器 - 调整顺序和过滤器
        # 1. 首先处理回复消息
        bot_app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_flow))
        # 2. 然后处理问候功能
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        # 3. 最后处理关键词回复
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, auto_reply_handler))
        # 4. 处理其他消息
        bot_app.add_handler(MessageHandler(filters.ALL, forward_message_handler))
        
        # 添加群组成员变更处理器
        bot_app.add_handler(ChatMemberHandler(chat_member_handler))
        
        # 从 Google Sheet 加载数据
        ban_records = await sheets_storage.load_from_sheet()
        logger.info(f"Loaded {len(ban_records)} records from Google Sheet")
        
        # 启动 bot
        await bot_app.initialize()
        await bot_app.start()
        bot_initialized = True
        
        yield
        
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise
        
    finally:
        # 清理资源
        if bot_app:
            await bot_app.stop()
            await bot_app.shutdown()

# 创建 FastAPI 应用
app = FastAPI(lifespan=lifespan)

# 添加 webhook 路由
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """处理 Telegram webhook 请求"""
    if not bot_app:
        raise HTTPException(status_code=500, detail="Bot not initialized")
        
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 添加根路径处理
@app.get("/")
async def root():
    """根路径处理"""
    return {"status": "ok", "message": "Telegram Bot is running"}

# 添加健康检查路由
@app.get("/health")
@app.head("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "ok",
        "bot_status": "running" if bot_initialized else "not initialized",
        "timestamp": datetime.now(TIMEZONE).isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
