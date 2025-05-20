import os
import logging
import asyncio
import json
import base64
import time as time_module  # 重命名 time 模块
import random
import re
from datetime import datetime, timedelta, time, timezone
from typing import Dict, List, Any, Optional
from contextlib import asynccontextmanager
import csv
import io

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
        self.reminder_sheet = None  # 添加提醒记录表
        self.initialized = False
        self.last_cleanup_date = None  # 添加最后清理日期记录
        
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

            # 尝试打开或创建提醒记录表
            try:
                self.reminder_sheet = self.client.open("DailyReminders").sheet1
                # 检查是否有表头
                headers = self.reminder_sheet.row_values(1)
                if not headers or len(headers) < 2:
                    # 如果表头不存在或不完整，添加表头
                    self.reminder_sheet.clear()
                    self.reminder_sheet.append_row([
                        "用户ID", "日期"
                    ])
                    logger.info("添加提醒记录表表头")
            except gspread.exceptions.SpreadsheetNotFound:
                # 如果表不存在，创建新表
                spreadsheet = self.client.create("DailyReminders")
                self.reminder_sheet = spreadsheet.sheet1
                # 添加表头
                self.reminder_sheet.append_row([
                    "用户ID", "日期"
                ])
                logger.info(f"创建新的提醒记录表: DailyReminders (ID: {spreadsheet.id})")
                logger.info(f"表格链接: https://docs.google.com/spreadsheets/d/{spreadsheet.id}")
            
            self.initialized = True
            logger.info("Google Sheets 客户端初始化成功")
            
        except Exception as e:
            logger.error(f"Google Sheets 初始化失败: {e}")
            raise

    async def cleanup_old_reminders(self):
        """清理旧的提醒记录"""
        if not self.initialized:
            await self.initialize()
            
        try:
            current_date = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
            
            # 确保表格存在
            if not self.reminder_sheet:
                spreadsheet = self.client.create("DailyReminders")
                self.reminder_sheet = spreadsheet.sheet1
                self.reminder_sheet.append_row(["用户ID", "日期"])
                logger.info("已创建新的提醒记录表")
                return
            
            try:
                # 获取所有记录
                records = self.reminder_sheet.get_all_records()
                
                # 检查是否有今天的记录
                has_today_records = any(record.get("日期") == current_date for record in records)
                
                # 如果没有今天的记录，说明是新的一天，需要清理
                if not has_today_records:
                    try:
                        # 清空表格
                        self.reminder_sheet.clear()
                        
                        # 重新添加表头
                        self.reminder_sheet.append_row(["用户ID", "日期"])
                        logger.info("已清理提醒记录，开始新的一天")
                    except Exception as e:
                        logger.error(f"清理表格失败: {e}")
                        # 如果清理失败，尝试重新初始化表格
                        await self._recreate_reminder_sheet()
            except Exception as e:
                logger.error(f"获取记录失败: {e}")
                await self._recreate_reminder_sheet()
            
        except Exception as e:
            logger.error(f"清理提醒记录失败: {e}")
            await self._recreate_reminder_sheet()

    async def _recreate_reminder_sheet(self):
        """重新创建提醒记录表"""
        try:
            spreadsheet = self.client.create("DailyReminders")
            self.reminder_sheet = spreadsheet.sheet1
            self.reminder_sheet.append_row(["用户ID", "日期"])
            logger.info("已重新创建提醒记录表")
        except Exception as e:
            logger.error(f"重新创建表格失败: {e}")

    async def check_daily_reminder(self, user_id: int, date: str) -> bool:
        """检查用户是否已经收到过今日提醒"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 确保表格存在
            if not self.reminder_sheet:
                await self._recreate_reminder_sheet()
                return False
            
            # 先尝试清理旧记录
            await self.cleanup_old_reminders()
            
            try:
                # 获取所有记录
                records = self.reminder_sheet.get_all_records()
                
                # 检查是否存在匹配的记录
                for record in records:
                    if str(record.get("用户ID")) == str(user_id) and record.get("日期") == date:
                        return True
                return False
            except Exception as e:
                logger.error(f"获取记录失败: {e}")
                await self._recreate_reminder_sheet()
                return False
            
        except Exception as e:
            logger.error(f"检查提醒记录失败: {e}")
            return False

    async def save_daily_reminder(self, user_id: int, date: str) -> bool:
        """保存提醒记录"""
        if not self.initialized:
            await self.initialize()
            
        try:
            # 确保表格存在
            if not self.reminder_sheet:
                await self._recreate_reminder_sheet()
            
            try:
                # 检查是否已经存在相同的记录
                records = self.reminder_sheet.get_all_records()
                for record in records:
                    if str(record.get("用户ID")) == str(user_id) and record.get("日期") == date:
                        return True  # 如果已存在，直接返回成功
                
                # 添加新记录
                self.reminder_sheet.append_row([str(user_id), date])
                return True
            except Exception as e:
                logger.error(f"保存记录失败: {e}")
                await self._recreate_reminder_sheet()
                # 重新尝试保存
                try:
                    self.reminder_sheet.append_row([str(user_id), date])
                    logger.info("已重新创建提醒记录表并保存记录")
                    return True
                except Exception as e:
                    logger.error(f"重新保存记录失败: {e}")
                    return False
            
        except Exception as e:
            logger.error(f"保存提醒记录失败: {e}")
            return False

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
TIMEZONE = pytz.timezone('Asia/Shanghai')  # 设置为北京时间
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
# 在全局变量部分添加
USER_DAILY_REMINDERS = {}  # 用于记录用户每日提醒状态
# 在文件开头的全局变量部分添加
# 全局变量
mystonks_reminder_enabled = True  # MyStonks 提醒开关

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
            
        # 检查是否已经在处理这个用户
        if "last_ban" in context.chat_data:
            last_ban = context.chat_data["last_ban"]
            if last_ban.get("user_id") == user.id and last_ban.get("operator_id") != message.from_user.id:
                # 如果其他管理员正在处理这个用户，直接返回
                return
                
        # 创建封禁记录
        banned_user_name = user.first_name  # Display name
        banned_username = f"@{user.username}" if user.username else "无"  # Use existing username with @
        context.chat_data["last_ban"] = {
            "operator_id": message.from_user.id,
            "chat_title": chat.title,
            "user_id": user.id,
            "banned_user_name": banned_user_name,
            "banned_username": banned_username,
            "message_id": message.message_id  # 添加消息ID
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
        sent_message = await message.reply_text(
            f"请选择封禁用户 {user.first_name} 的理由：",
            reply_markup=reply_markup
        )
        
        # 30秒后删除消息
        asyncio.create_task(delete_message_later(sent_message, delay=30))
        
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
        last_ban = context.chat_data.get("last_ban", {})
        
        # 检查是否是同一个操作
        if not last_ban or last_ban.get("user_id") != banned_user_id:
            return  # 如果不是同一个操作，直接返回
            
        # 验证操作权限
        if query.from_user.id != last_ban.get("operator_id"):
            return  # 如果不是执行操作的管理员，直接返回，不显示任何消息
            
        banned_user_name = last_ban.get("banned_user_name", "")
        banned_username = f"@{username}" if username else "无"
        
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
                # 获取被回复的消息
                replied_message = None
                try:
                    # 获取原始命令消息
                    command_message = await context.bot.get_message(
                        chat_id=query.message.chat.id,
                        message_id=last_ban.get("message_id")
                    )
                    if command_message and command_message.reply_to_message:
                        replied_message = command_message.reply_to_message
                except Exception as e:
                    logger.error(f"获取被回复消息失败: {e}")
                
                # 封禁用户并删除消息
                await context.bot.ban_chat_member(
                    chat_id=query.message.chat.id,
                    user_id=banned_user_id,
                    revoke_messages=True  # 删除用户的所有消息
                )
                
                # 如果找到了被回复的消息，尝试删除它
                if replied_message:
                    try:
                        await replied_message.delete()
                    except Exception as e:
                        logger.error(f"删除被回复消息失败: {e}")
            
                # 立即删除选择理由的消息
                await query.message.delete()
                
                # 发送确认消息并立即删除
                confirm_msg = await query.message.reply_text(f"✅ 已封禁用户 {banned_user_name} 并删除其消息 - 理由: {reason}")
                await asyncio.sleep(2)  # 等待2秒让用户看到确认消息
                await confirm_msg.delete()
                
                # 清理操作数据
                if "last_ban" in context.chat_data:
                    del context.chat_data["last_ban"]
            else:
                error_msg = await query.message.reply_text("❌ 保存记录失败")
                asyncio.create_task(delete_message_later(error_msg, delay=10))  # 错误消息10秒后删除
                asyncio.create_task(delete_message_later(query.message, delay=10))
            
        except Exception as e:
            error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
            asyncio.create_task(delete_message_later(error_msg, delay=10))  # 错误消息10秒后删除
            asyncio.create_task(delete_message_later(query.message, delay=10))
            logger.error(f"保存封禁原因失败: {e}")
            
    except ValueError:
        return  # 无效的回调数据，直接返回

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
        
    # 检查是否在回复流程中
    if "reply_flow" not in context.user_data:
        return  # 如果不在回复流程中，直接返回，不记录警告
        
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
        asyncio.create_task(delete_message_later(msg, delay=10))
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
        f"🔥 {user.first_name}早安！今天的你注定不凡，一起燃起来吧！",
        f"🌈 {user.first_name}早上好！生活不是等待风来，而是追风的你起飞的样子！",
        f"🌅 {user.first_name}早安！新的一天，新的奇迹，就从你睁眼开始！",
        f"💪 {user.first_name}早上好！拼搏的你，是这个星球最靓的仔！",
        f"🎯 {user.first_name}早安！目标已锁定，今天必定百发百中！",
        f"🚀 {user.first_name}早上好！梦想的引擎已经启动，出发吧～",
        f"🌞 {user.first_name}，太阳都没你闪！今天也要发光发亮～",
        f"🛤️ {user.first_name}早安！别怕路远，坚持一步也算前进！",
        f"🧠 {user.first_name}早上好！今天是头脑发光的一天哦～",
        f"🦁 {user.first_name}早安！内心的猛兽今天也要全力以赴！",
        f"🧗 {user.first_name}早上好！每一个不想起床的清晨，都是成长的阶梯！",
        f"🌟 {user.first_name}早上好！你不是普通人，是宇宙为努力打样的模板！",
        f"🧃 {user.first_name}早安！喝口勇气汁，今天继续冲冲冲！",
        f"🎉 {user.first_name}早上好！你今天肯定会比昨天更强一丢丢～",
        f"🧼 {user.first_name}早安！洗把脸，洗掉懒惰，洗出闪光的自己！",
        f"⚙️ {user.first_name}早上好！今日齿轮已转动，为梦想全速前进！",
        f"🌤️ {user.first_name}早安！天气晴好，心情也要大晴天～",
        f"🎈 {user.first_name}早安！抛掉烦恼，今天就做最轻盈的自己～",
        f"🛡️ {user.first_name}早上好！披上希望的盔甲，开启胜者的一天！",
        f"📈 {user.first_name}早安！今天也要让成长的曲线向上画～",
        f"📚 {user.first_name}早上好！知识在召唤，未来因你而闪耀～",
        f"🧭 {user.first_name}早安！别忘了方向，再远也能到达！",
        f"🔋 {user.first_name}早上好！今日电量已满，准备开挂！",
        f"🌿 {user.first_name}早安！新芽破土的力量，也藏在你心里～",
        f"🏃 {user.first_name}早上好！只要迈出第一步，就已经赢了昨天的你！",
        f"🛠️ {user.first_name}早安！一砖一瓦，今天也为梦想添块砖～",
        f"🎵 {user.first_name}早上好！今天的节奏，由你来主导！",
        f"🔭 {user.first_name}早安！用远见点亮今天，用脚步实现未来！",
        f"📦 {user.first_name}早上好！生活给的每一个挑战，都是你的定制礼包！",
        f"🪄 {user.first_name}早安！别忘了，你就是奇迹的代名词！",
        f"🌈 {user.first_name}早安！你是晴天里最耀眼的那缕光！",
        f"🛫 {user.first_name}早上好！生活已起飞，请系好梦想的安全带！",
        f"🥇 {user.first_name}早安！你注定是属于榜首的那种人～",
        f"🧊 {user.first_name}早安！你今天的冷静值+100，理智通关！",
        f"🏖️ {user.first_name}早上好！别忘了，快乐才是最终目的地！",
        f"🍀 {user.first_name}早安！好运今天一定偷偷跟着你出门了～",
        f"🐾 {user.first_name}早上好！迈出第一步，你就已经领先很多人了！",
        f"🧱 {user.first_name}早安！一点一点地垒，你的梦想终将成塔！",
        f"🧃 {user.first_name}早上好！今天的你就是打工界的冰美式：醒！",
        f"🎯 {user.first_name}早安！今天也要精准输出，让世界记住你！",
        f"📖 {user.first_name}早上好！今天是故事主角的第{random.randint(1,999)}章，请继续精彩演绎！",
        f"🚴 {user.first_name}早安！人生就像骑车，停下来就容易倒，坚持就对了！",
        f"📣 {user.first_name}早上好！宇宙广播站正在为你打 call！",
        f"🎬 {user.first_name}早安！你是这部人生大片的唯一主角！",
        f"🌠 {user.first_name}早上好！今天也要当一颗努力发光的星星～",
        f"🧚 {user.first_name}早安！小仙子/仙男准备施展一天的魔法了吗？",
        f"🗺️ {user.first_name}早上好！世界再大，也阻挡不了你要去的方向～",
        f"🥗 {user.first_name}早安！记得喂饱肚子，也喂饱梦想哦～",
        f"🎿 {user.first_name}早上好！一路向前，不怕翻车！你最稳！",
        f"🔔 {user.first_name}早安！生活的闹钟响了，梦想也该起床啦～",
        f"🖼️ {user.first_name}早安！今天是你人生画布上的又一笔神来之笔！",
        f"🦋 {user.first_name}早安！轻盈出发，哪怕是一点点前进，也是飞翔～",
        f"🪴 {user.first_name}早上好！每天一点光，梦想就能慢慢长大～",
        f"🎖️ {user.first_name}早上好！今天也要以主角的姿态出场！",
        f"🏋️ {user.first_name}早安！你的努力，正在悄悄积蓄力量！",
        f"🔧 {user.first_name}早上好！今天是'打磨更好的自己'特别行动日～",
        f"🧬 {user.first_name}早安！努力是你DNA里的默认基因！",
        f"🎓 {user.first_name}早安！成长不止于书本，而在每一次出发！",
        f"🛹 {user.first_name}早安！生活的节奏由你掌控，滑起来吧！",
        f"🎉 {user.first_name}早安！今天也是你征服世界的练习日！",
        f"🦸 {user.first_name}早上好！披上勇气的斗篷，你无所不能！",
        f"🌋 {user.first_name}早安！就算今天困难像火山，你也是岩浆骑士！",
        f"🌉 {user.first_name}早上好！别怕距离，前路有桥，也有光！",
        f"📀 {user.first_name}早安！今日开启'主角光环'模式！",
        f"🪁 {user.first_name}早上好！逆风也能起飞，你就是那只不服的风筝！",
        f"🍉 {user.first_name}早安！夏天的第一口西瓜，不如你今天的第一个微笑甜！",
        f"🧞 {user.first_name}早上好！今天你许下的愿望，宇宙都听到了！",
        f"📦 {user.first_name}早安！每个清晨都是生活递来的快递，签收好运吧！",
        f"🎲 {user.first_name}早上好！今天你会掷出人生的 6 点！",
        f"📡 {user.first_name}早上好！你已接入宇宙好运频道～",
        f"🎻 {user.first_name}早安！你是这首日常交响曲里最动听的旋律！",
        f"💌 {user.first_name}早上好！早安信已送达，今天也要记得喜欢自己哦～",
        f"🦄 {user.first_name}早安！这个世界因为你才不无聊～",
        f"📎 {user.first_name}早上好！今天的你，稳重又闪亮！",
        f"🫧 {user.first_name}早安！每个梦想都值得被温柔对待～",
        f"🛁 {user.first_name}早上好！洗掉烦恼，涂上勇气，闪亮登场吧！",
        f"🏆 {user.first_name}早安！今天也要为'最棒的我'奖努力哦～",
        f"🍸 {user.first_name}早上好！今天调配的是一杯元气满满！",
        f"🌶️ {user.first_name}早安！今天的你，辣得有点过分了耶～",
        f"💃 {user.first_name}早上好！快节奏也别忘了跳自己喜欢的舞步！",
        f"🕹️ {user.first_name}早安！你就是这局人生游戏的隐藏高手！",
        f"🧗 {user.first_name}早安！别怕难，山顶的风景配得上你的坚持！",
        f"🛞 {user.first_name}早安！人生的方向盘在你手里，转起来！",
        f"🥳 {user.first_name}早上好！不需要特别的理由，也值得开心一整天～",
        f"💼 {user.first_name}早安！今天的你，专业又迷人！",
        f"📌 {user.first_name}早上好！别忘了，把笑容钉在脸上出门～",
        f"💎 {user.first_name}早上好！越打磨越闪耀，今天你也很值钱！",
        f"🧀 {user.first_name}早上好！就算是老鼠，也要勇敢偷走今天的奶酪！",
        f"🧤 {user.first_name}早安！抓住机会，就像戴上了命运的手套！",
        f"🧨 {user.first_name}早上好！今天的你，准备炸翻全场了吗？",
        f"📸 {user.first_name}早安！微笑是你今天最值得记录的表情！",
        f"🌻 {user.first_name}早安！面对阳光，阴影就会在你身后！",
        f"🍰 {user.first_name}早上好！生活苦一点没关系，今天的你够甜！",
        f"🔋 {user.first_name}早安！电力拉满，开工无敌！",
        f"📼 {user.first_name}早安！今天的精彩，已经按下录制键了～",
        f"📅 {user.first_name}早上好！这不是平凡的一天，这是你人生的主线任务！",
        f"🛠️ {user.first_name}早安！今天也是精雕细琢的匠人精神上线！",
        f"🥾 {user.first_name}早上好！脚下有泥，心中有光，继续走！",
        f"🧣 {user.first_name}早安！风再大，你也有温暖包围！",
        f"🧼 {user.first_name}早安！洗净昨日疲惫，迎接今天的荣光！",
        f"🧘 {user.first_name}早上好！身心平衡，才能风生水起！",
        f"🖍️ {user.first_name}早安！今天的篇章，就用你来描绘吧～",
        f"🌕 {user.first_name}早上好！昨夜月光照进心里，今天阳光照进你眼里！",

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
    import random

    NOON_GREETINGS = [
        # 吃饭提醒类
        f"🍱 {{user.first_name}}午安！今天中午吃什么呀？别忘了加点开心的调料！",
        f"🍜 {{user.first_name}}中午好！干饭人准备就位了吗？",
        f"🥢 {{user.first_name}}午安！碳水和蛋白质在等你签收~",
        f"🍛 {{user.first_name}}中午好！你今天的午餐被批准为'快乐套餐'~",
        f"🍔 {{user.first_name}}午安！胃已经开始抗议啦，快去安抚一下它~",
        f"🥗 {{user.first_name}}中午好！吃点清爽的，下午战斗力更强！",
        f"🥘 {{user.first_name}}午安！美味午餐是你前进的燃料~",
        f"🍣 {{user.first_name}}中午好！寿司？火锅？干饭自由由你掌控！",
        f"🍚 {{user.first_name}}午安！今天吃饭别再配寂寞啦~",
        f"🧃 {{user.first_name}}中午好！别忘了来一杯维C满满的果汁哦~",
        f"🍞 {{user.first_name}}午安！吃饭别拖延，饿着可不是英雄行为！",
        f"🥙 {{user.first_name}}中午好！多吃一口，幸福+10点~",
        f"🍕 {{user.first_name}}午安！不吃饭哪来的干劲去追梦呢~",
        f"🍜 {{user.first_name}}中午好！碗里香，心里甜，午安更有味~",
        f"🍱 {{user.first_name}}午安！干饭时间到，碗筷已就绪~",
        f"🍰 {{user.first_name}}中午好！吃饱了才有资格说'我不累'~",
        f"🧂 {{user.first_name}}午安！给你的中饭撒点快乐的盐~",
        f"🍖 {{user.first_name}}中午好！肉肉是人类的好朋友~",
        f"🥩 {{user.first_name}}午安！吃饱了才能拯救银河系~",
        f"🍲 {{user.first_name}}中午好！今天的饭，机器人给打了满分~",
        f"🥪 {{user.first_name}}午安！别总想着减肥，午餐还是要吃好~",
        f"🍜 {{user.first_name}}中午好！面条绕口三圈半，幸福全靠干饭赞~",
        f"🧀 {{user.first_name}}午安！吃饭的时候笑一笑，连奶酪都会变甜~",
        f"🍢 {{user.first_name}}中午好！串串已到位，就等你举箸啦~",
        f"🍇 {{user.first_name}}午安！饭后来点水果，健康又可爱~",

        # 午休提醒类
        f"😴 {{user.first_name}}午安！闭眼10分钟，满血复活不是梦~",
        f"🛏️ {{user.first_name}}中午好！你和床的距离只差一个'躺'字~",
        f"💤 {{user.first_name}}午安！别硬撑啦，躺平才是美德~",
        f"🧸 {{user.first_name}}中午好！午觉时间已到，梦里记得签到~",
        f"🧘 {{user.first_name}}午安！放空大脑，清理缓存中……",
        f"🛋️ {{user.first_name}}中午好！给眼睛放个假，给脑袋充个电~",
        f"🪷 {{user.first_name}}午安！心静自然凉，午休一下刚刚好~",
        f"☁️ {{user.first_name}}中午好！闭上眼，今天的风是奶油味的~",
        f"📵 {{user.first_name}}午安！手机放下，梦乡抱紧~",
        f"🌙 {{user.first_name}}中午好！今天的幸运藏在一场小憩里~",
        f"😌 {{user.first_name}}午安！闭眼10分钟，清醒一整个下午~",
        f"🧠 {{user.first_name}}中午好！大脑需要一杯'安静拿铁'~",
        f"🕯️ {{user.first_name}}午安！静一静，风也温柔~",
        f"🧦 {{user.first_name}}中午好！盖上小毯子，梦里跑个步~",
        f"🍃 {{user.first_name}}午安！静坐半小时，活力一整天~",
        f"🌿 {{user.first_name}}中午好！像植物一样，阳光和休息都要有~",
        f"🧘‍♂️ {{user.first_name}}午安！来一段深呼吸，让午后更轻盈~",
        f"🪑 {{user.first_name}}中午好！靠背一靠，烦恼全跑~",
        f"🧴 {{user.first_name}}午安！给身体抹点'放松防晒霜'~",
        f"⏸️ {{user.first_name}}中午好！暂停，是为了更好地播放~",
        f"🧘‍♀️ {{user.first_name}}午安！和疲惫说拜拜，和活力说hi~",
        f"🪫 {{user.first_name}}中午好！电量不足，正在午间自动充电中~",
        f"🌊 {{user.first_name}}午安！在梦里散个步，也是一种放松~",
        f"🛌 {{user.first_name}}中午好！让身体沉进柔软，唤醒新的力量~",

        # 励志鼓劲类
        f"💪 {{user.first_name}}午安！午后的你依旧是那个宝藏选手！",
        f"🌈 {{user.first_name}}中午好！你已经走得很棒啦，继续加油！",
        f"🌟 {{user.first_name}}午安！每个努力不止的你都在发光~",
        f"🚀 {{user.first_name}}中午好！歇一歇，下午再起飞~",
        f"🌻 {{user.first_name}}午安！就算阳光暂时躲起来，花依旧向光~",
        f"🔋 {{user.first_name}}中午好！给自己满电，别怕下午的任务！",
        f"🛠️ {{user.first_name}}午安！修整一下，再接再厉~",
        f"🌅 {{user.first_name}}中午好！休息，是为了更远的冲刺~",
        f"🧱 {{user.first_name}}午安！每一块砖，都是你在建造的梦想~",
        f"🧭 {{user.first_name}}中午好！方向不变，偶尔停下也是前进的一部分~",
        f"✨ {{user.first_name}}午安！闪光的你，只是小憩一下~",
        f"🕊️ {{user.first_name}}中午好！平静的心，走得更远~",
        f"💼 {{user.first_name}}午安！打工人也要爱自己~",
        f"🔧 {{user.first_name}}中午好！灵魂的维修站，午休营业中~",
        f"🧩 {{user.first_name}}午安！人生拼图，午后续上精彩一块~",
        f"📈 {{user.first_name}}中午好！午餐+午休=效率提升术~",
        f"🛠️ {{user.first_name}}午安！给身体做个保养，下午更顺~",
        f"⛅ {{user.first_name}}中午好！疲惫是成长的注脚~",
        f"🔥 {{user.first_name}}午安！蓄势待发的你，正在升温~",
        f"🌠 {{user.first_name}}中午好！你的一天，值得每一秒被照亮~",
        f"⏳ {{user.first_name}}午安！时间不等人，但人可以暂停~",
        f"📖 {{user.first_name}}中午好！用片刻宁静，翻开人生下一页~",
        f"🔍 {{user.first_name}}午安！休息是为了看得更清更远~",
        f"🎯 {{user.first_name}}中午好！调整好弓，下一箭才会更准~",
        f"🥇 {{user.first_name}}午安！每个中午都在为冠军蓄力！",

        # 小山炮彩蛋类
        f"🤖 {{user.first_name}}午安！我是你中午的'干饭提醒小助手'上线啦！",
        f"🔊 {{user.first_name}}中午好！今日能量语音包已传送，记得充电！",
        f"🎁 {{user.first_name}}午安！你是今天第{random.randint(1,999)}位收到祝福的幸运鹅~",
        f"🧩 {{user.first_name}}中午好！小山炮为你拼凑最安心的中午时光~",
        f"🛎️ {{user.first_name}}午安！友情提醒：可爱的你还没吃饭哦~",
        f"🐣 {{user.first_name}}中午好！吃饱喝足，下午继续快乐营业~",
        f"🧠 {{user.first_name}}午安！中午要让脑袋放个假，不然会罢工哦~",
        f"🎈 {{user.first_name}}中午好！趁阳光正暖，好好爱自己一下~",
        f"🌇 {{user.first_name}}午安！机器人都要休息，人类更要午睡~",
        f"📦 {{user.first_name}}中午好！打开这条消息，收获满满温暖~",
        f"🐼 {{user.first_name}}午安！国宝级的你，该补补觉啦~",
        f"🦉 {{user.first_name}}中午好！午睡十分钟，下午像夜猫一样清醒~",
        f"🎭 {{user.first_name}}午安！中场休息，主角请回后台调整~",
        f"🛸 {{user.first_name}}中午好！你的能量补给飞船已就位~",
        f"📡 {{user.first_name}}午安！正在连接梦境服务器……",
        f"🎮 {{user.first_name}}中午好！午睡=存档，下午=通关！",
        f"🐧 {{user.first_name}}午安！企鹅都要晒太阳了，你还不休息？",
        f"🍄 {{user.first_name}}中午好！蘑菇都知道该午休吸收养分了~",
        f"🧊 {{user.first_name}}午安！冷静一下，喝杯水，关掉脑内会议~",
        f"🧃 {{user.first_name}}中午好！为灵魂灌满维他命~ 午安！",
        f"🧭 {{user.first_name}}午安！前路漫漫，先歇歇再出发！",
        f"🔄 {{user.first_name}}中午好！系统维护中，请放松大脑~",
        f"💽 {{user.first_name}}午安！自动保存已开启，请安心小憩~",
        f"🔑 {{user.first_name}}中午好！你是我中午最期待的用户~",
        f"🧃 {{user.first_name}}午安！今天的饭香+笑容 = 滿分Combo！"
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
        f"🌙 {{user.first_name}}晚安！愿你有个甜美的梦~",
        f"✨ {{user.first_name}}晚安！星星会守护你的梦~",
        f"🌛 {{user.first_name}}晚安！月亮会照亮你的梦~",
        f"🛏️ {{user.first_name}}晚安！被子已经暖好啦~",
        f"🌌 {{user.first_name}}晚安！让疲惫随星光流走，梦里都是美好~",
        f"☁️ {{user.first_name}}晚安！闭上眼，给自己一个温柔的拥抱~",
        f"🕯️ {{user.first_name}}晚安！点一盏心灯，照亮梦的方向~",
        f"🧸 {{user.first_name}}晚安！放下烦恼，把快乐带进梦乡~",
        f"🎐 {{user.first_name}}晚安！风轻云淡的夜里，藏着温柔和力量~",
        f"🪶 {{user.first_name}}晚安！轻轻闭眼，世界也会温柔下来~",
        f"📖 {{user.first_name}}晚安！把今天翻页，明天又是全新的故事~",
        f"🎵 {{user.first_name}}晚安！夜晚是心灵小憩的港湾，愿你安然入梦~",
        f"🌌 {{user.first_name}}晚安！今晚的你，值得最宁静的梦境~",
        f"🪄 {{user.first_name}}晚安！愿你梦见所有想见的人和地方~",
        f"🧘 {{user.first_name}}晚安！放空大脑，身体和心灵一起休息~",
        f"🌃 {{user.first_name}}晚安！城市灯火璀璨，你的梦也不平凡~",
        f"🛏️ {{user.first_name}}晚安！今天已经很棒，早点休息吧~",
        f"📦 {{user.first_name}}晚安！把烦恼封箱，明天再战江湖~",
        f"🫧 {{user.first_name}}晚安！夜晚是给勇敢者的奖赏，睡个好觉吧~",
        f"💫 {{user.first_name}}晚安！梦境列车即将启程，准备出发咯~",
        f"🌠 {{user.first_name}}晚安！你的闪光点，连星星都羡慕~",
        f"🔕 {{user.first_name}}晚安！今天就先这样，明天再全力以赴~",
        f"🌜 {{user.first_name}}晚安！愿今晚月色温柔，也照亮你的心~",
        f"🔮 {{user.first_name}}晚安！梦里预言明天的好运吧~",
        f"🌬️ {{user.first_name}}晚安！风轻云淡，是时候放过自己了~",
        f"📦 {{user.first_name}}晚安！把委屈打包寄走，明早再做主角~",
        f"🛌 {{user.first_name}}晚安！换个姿势，把烦恼留在梦外~",
        f"🪟 {{user.first_name}}晚安！窗外安静了，心也该慢慢沉下来~",
        f"🧚 {{user.first_name}}晚安！愿你梦见童话里的魔法和糖果~",
        f"💤 {{user.first_name}}晚安！明天再来拯救世界，今晚先拯救自己~",
        f"🌙 {{user.first_name}}晚安！你今天的努力，月亮都看在眼里~",
        f"🍷 {{user.first_name}}晚安！今晚的梦境像红酒，微醺又温柔~",
        f"🧠 {{user.first_name}}晚安！大脑已下线，幸福可加载~",
        f"📚 {{user.first_name}}晚安！梦里也有诗和远方等着你~",
        f"🌃 {{user.first_name}}晚安！这城市有千千万万盏灯，最温柔的那一盏属于你~",
        f"🌊 {{user.first_name}}晚安！就像海浪停靠，放松然后入眠~",
        f"🍰 {{user.first_name}}晚安！梦里请享用无限甜品，零卡无罪~",
        f"🐱 {{user.first_name}}晚安！就像猫一样，安心睡在月光下吧~",
        f"🦉 {{user.first_name}}晚安！夜猫子也要按时睡觉哦~",
        f"🪩 {{user.first_name}}晚安！今晚就做梦里的闪闪发光女孩/男孩~",
        f"🪴 {{user.first_name}}晚安！像植物一样，休息是为了更好地成长~",
        f"🎇 {{user.first_name}}晚安！今晚的你，值得被星光点亮~",
        f"🕯️ {{user.first_name}}晚安！灯光渐暗，温暖不减~",
        f"😴 {{user.first_name}}晚安！再不睡就要变成熊猫啦~",
        f"🌙 {{user.first_name}}晚安！梦里记得给我留个位置~",
        f"🛌 {{user.first_name}}晚安！床说它想你了~",
        f"💤 {{user.first_name}}晚安！明天见，小懒虫~",
        f"🌠 {{user.first_name}}晚安！今天的你很棒，明天继续加油~",
        f"🌟 {{user.first_name}}晚安！休息是为了更好的明天~",
        f"🌙 {{user.first_name}}晚安！养精蓄锐，明天再战~",
        f"🌙 {{user.first_name}}晚安！今晚的梦境主题是：{random.choice(['冒险','美食','旅行','童话'])}~",
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

async def toggle_mystonks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """切换 MyStonks 提醒开关"""
    global mystonks_reminder_enabled
    
    if not await check_admin(update, context):
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return
        
    mystonks_reminder_enabled = not mystonks_reminder_enabled
    status = "开启" if mystonks_reminder_enabled else "关闭"
    
    message = await update.message.reply_text(f"✅ MyStonks 提醒已{status}")
    # 5秒后删除消息
    asyncio.create_task(delete_message_later(message, delay=5))

async def check_and_send_daily_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """检查并发送每日提醒"""
    global mystonks_reminder_enabled
    
    # 如果提醒功能已关闭，直接返回
    if not mystonks_reminder_enabled:
        return
        
    if not update.message or not update.message.from_user:
        return
    
    # 其余代码保持不变
    # 检查当前时间是否在凌晨4点后（北京时间）
    current_time = datetime.now(TIMEZONE)
    if current_time.hour < 4:
        return  # 凌晨4点前不发送提醒
        
    user_id = update.message.from_user.id
    current_date = current_time.strftime('%Y-%m-%d')
    
    try:
        # 检查是否已经发送过提醒
        has_reminder = await sheets_storage.check_daily_reminder(user_id, current_date)
        if has_reminder:
            return  # 如果今天已经提醒过，直接返回
            
        # 检查是否是命令消息
        if update.message.text and update.message.text.startswith('/'):
            return
            
        # 检查消息间隔（至少1分钟）
        last_reminder_time = context.user_data.get('last_reminder_time', 0)
        current_timestamp = time_module.time()  # 使用重命名后的 time 模块
        if current_timestamp - last_reminder_time < 60:  # 60秒 = 1分钟
            return
            
        # 随机决定是否发送提醒（20%的概率）
        if random.random() > 0.2:
            return
            
        # 随机选择一条提醒消息
        reminder_messages = [
            "就问一句：你 MyStonks 了吗？\n🔗 https://mystonks.org",
            "MyStonks 都这么好用了，你还不点？\n🔗 https://mystonks.org",
            "每天点一点 MyStonks，机会离你近一点～\n🔗 https://mystonks.org",
            "MyStonks 在等你，不点开它你良心不会痛吗？\n🔗 https://mystonks.org",
            "忘了用 MyStonks？提醒你一下就好。\n🔗 https://mystonks.org",
            "喂～MyStonks喊你来看数据了！\n🔗 https://mystonks.org",
            "喵～今天也要用 MyStonks 才能变有钱哟～\n🔗 https://mystonks.org",
            "MyStonks：你还没来看我吗？🥺\n🔗 https://mystonks.org",
            "亲，今天记得来MyStonks看看哦～\n🔗 https://mystonks.org",
            "喂～你是不是还没打开MyStonks？\n🔗 https://mystonks.org",
            "用MyStonks的，未来都是赢家！所以你用了吗？\n🔗 https://mystonks.org",
            "MyStonks 每天用一下，信息不落后。\n🔗 https://mystonks.org",
            "一天不看 MyStonks，总觉得少点什么。\n🔗 https://mystonks.org",
            "📈 今天用 MyStonks 了吗？市场信息都在这里！\n🔗 https://mystonks.org",
            "💡 打开 MyStonks，掌握市场先机！\n🔗 https://mystonks.org",
            "🚀 用 MyStonks 的人，运气都不会太差～\n🔗 https://mystonks.org",
            "🎯 每日必看 MyStonks，投资不迷路！\n🔗 https://mystonks.org",
            "🌟 今天也要记得打开 MyStonks 哦～\n🔗 https://mystonks.org",
            "MyStonks上线啦，你还没来打卡吗？\n🔗 https://mystonks.org",
            "投资路上不迷路，MyStonks等你来！\n🔗 https://mystonks.org",
            "每天一点点MyStonks，财富离你更近~\n🔗 https://mystonks.org",
            "来MyStonks看看，机会就在指尖！\n🔗 https://mystonks.org",
            "别忘了打开MyStonks，收获更多惊喜！\n🔗 https://mystonks.org",
            "MyStonks提醒：今天的行情你看了吗？\n🔗 https://mystonks.org",
            "MyStonks用起来，投资更自信！\n🔗 https://mystonks.org",
            "别让行情跑了，快打开MyStonks看看！\n🔗 https://mystonks.org",
            "MyStonks在手，财富不愁！\n🔗 https://mystonks.org",
            "你和财富的距离，只差一次打开MyStonks！\n🔗 https://mystonks.org",
            "快来MyStonks，别让机会溜走！\n🔗 https://mystonks.org",
            "MyStonks每天一看，赚钱不发愁！\n🔗 https://mystonks.org",
            "想成为股市高手？先用MyStonks吧！\n🔗 https://mystonks.org",
            "MyStonks带你抓住每一个行情！\n🔗 https://mystonks.org",
            "别让投资盲目，MyStonks帮你把关！\n🔗 https://mystonks.org",
            "MyStonks助你投资路上一路顺风！\n🔗 https://mystonks.org",
            "每天用MyStonks，财富自动到手！\n🔗 https://mystonks.org",
            "来MyStonks看看，财富不再是梦！\n🔗 https://mystonks.org",
            "用MyStonks，做聪明的投资者！\n🔗 https://mystonks.org",
            "想要赢在起点？先用MyStonks！\n🔗 https://mystonks.org",
            "MyStonks在手，行情我有！\n🔗 https://mystonks.org",
            "别犹豫了，MyStonks等你来战！\n🔗 https://mystonks.org",
            "打开MyStonks，让投资更轻松！\n🔗 https://mystonks.org",
            "用MyStonks，天天都是赚钱日！\n🔗 https://mystonks.org",
            "MyStonks帮你捕捉每个赚钱机会！\n🔗 https://mystonks.org",
            "投资路上，有MyStonks相伴更安心！\n🔗 https://mystonks.org",
            "别落伍，MyStonks让你快人一步！\n🔗 https://mystonks.org",
            "MyStonks，让财富触手可及！\n🔗 https://mystonks.org",
            "财富密码就在MyStonks，快来开启！\n🔗 https://mystonks.org",
            "MyStonks，一起见证财富奇迹！\n🔗 https://mystonks.org"
        ]
        
        # 发送提醒消息
        reminder_msg = await update.message.reply_text(random.choice(reminder_messages))
        # 保存提醒记录
        await sheets_storage.save_daily_reminder(user_id, current_date)
        # 更新最后提醒时间
        context.user_data['last_reminder_time'] = current_timestamp
        # 1分钟后删除提醒消息
        asyncio.create_task(delete_message_later(reminder_msg, delay=60))
    except Exception as e:
        logger.error(f"发送提醒消息失败: {e}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    if not update.message or not update.message.text:
        return
        
    # 检查并发送每日提醒
    await check_and_send_daily_reminder(update, context)
        
    text = update.message.text.strip().lower()  # 转换为小写进行比较
    
    # 早安关键词（转换为小写进行比较）
    morning_keywords = [kw.lower() for kw in ["早安", "早上好", "good morning", "morning", "gm", "早"]]
    # 午安关键词
    noon_keywords = [kw.lower() for kw in ["午安", "中午好", "good noon", "noon"]]
    # 晚安关键词
    night_keywords = [kw.lower() for kw in ["晚安", "晚上好", "good night", "night", "gn"]]
    
    # 精确匹配关键词（不区分大小写）
    if text in morning_keywords:
        await morning_greeting_handler(update, context)
    elif text in noon_keywords:
        await noon_greeting_handler(update, context)
    elif text in night_keywords:
        await goodnight_greeting_handler(update, context)
    # 处理命令
    elif text.startswith('/'):
        return

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
            
        # 检查是否已经在处理这个用户
        if "last_ban" in context.chat_data:
            last_ban = context.chat_data["last_ban"]
            if last_ban.get("user_id") == user.id and last_ban.get("operator_id") != message.from_user.id:
                # 如果其他管理员正在处理这个用户，直接返回
                return
                
        # 创建封禁记录
        banned_user_name = user.first_name  # Display name
        banned_username = f"@{user.username}" if user.username else "无"  # Use existing username with @
        context.chat_data["last_ban"] = {
            "operator_id": message.from_user.id,
            "chat_title": chat.title,
            "user_id": user.id,
            "banned_user_name": banned_user_name,
            "banned_username": banned_username,
            "message_id": message.message_id  # 添加消息ID
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
        sent_message = await message.reply_text(
            f"请选择封禁用户 {user.first_name} 的理由：",
            reply_markup=reply_markup
        )
        
        # 30秒后删除消息
        asyncio.create_task(delete_message_later(sent_message, delay=30))
        
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

async def lottery_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理抽奖命令"""
    if not await check_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
        
    try:
        # 检查参数
        if len(context.args) != 2:
            await update.message.reply_text("❌ 请使用正确的格式：/draw <中奖人数> <总人数>")
            return
            
        # 解析参数
        try:
            winners_count = int(context.args[0])
            total_count = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字")
            return
            
        # 验证参数
        if winners_count <= 0 or total_count <= 0:
            await update.message.reply_text("❌ 人数必须大于0")
            return
            
        if winners_count > total_count:
            await update.message.reply_text("❌ 中奖人数不能大于总人数")
            return
            
        # 使用更安全的随机数生成方法
        # 1. 使用系统随机数生成器
        # 2. 使用 Fisher-Yates 洗牌算法
        # 3. 添加时间戳作为随机种子
        numbers = list(range(1, total_count + 1))
        seed = int(time.time() * 1000)  # 使用毫秒级时间戳
        random.seed(seed)
        
        # Fisher-Yates 洗牌算法
        for i in range(len(numbers) - 1, 0, -1):
            j = random.randint(0, i)
            numbers[i], numbers[j] = numbers[j], numbers[i]
            
        # 获取前 winners_count 个数字并排序
        winners = sorted(numbers[:winners_count])
        
        # 构建结果消息
        result_message = (
            f"🎉 抽奖结果 🎉\n\n"
            f"📊 总人数：{total_count}\n"
            f"🎁 中奖人数：{winners_count}\n\n"
            f"🏆 中奖号码：\n"
        )
        
        # 添加中奖号码，每行显示5个
        for i in range(0, len(winners), 5):
            line = winners[i:i+5]
            result_message += " ".join(f"{num:4d}" for num in line) + "\n"
            
        # 添加时间戳和随机种子
        result_message += (
            f"\n⏰ 抽奖时间：{datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🎲 随机种子：{seed}"
        )
        
        # 发送结果
        sent_message = await update.message.reply_text(result_message)
        
        # 5分钟后删除消息
        asyncio.create_task(delete_message_later(sent_message, delay=300))
        
    except Exception as e:
        logger.error(f"处理抽奖命令时出错: {e}")
        await update.message.reply_text("❌ 处理抽奖命令时出错")

async def daka_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理打卡命令，由机器人发送打卡消息"""
    if not update.message or not update.message.from_user:
        return
        
    # 检查是否是管理员
    if not await check_admin(update, context):
        sent_message = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(sent_message, delay=60))
        return
    
    # 打卡消息列表
    daka_messages = [
        "小山炮打卡：坚持，是走向胜利的第一步。",
        "小山炮打卡：今天的努力，都是明天的资本。",
        "小山炮打卡：每一次坚持，都是成长的印记。",
        "小山炮打卡：成功来自不懈的努力和信念。",
        "小山炮打卡：别怕慢，只怕停。",
        "小山炮打卡：行动，是对自己的承诺。",
        "小山炮打卡：每个成功者，都是从开始迈出第一步的。",
        "小山炮打卡：不积跬步，无以至千里。",
        "小山炮打卡：持续努力，就是不断进步。",
        "小山炮打卡：梦想因行动而闪光。",
        "小山炮打卡：你的努力，是别人看不到的力量。",
        "小山炮打卡：只要开始，就永远不会太晚。",
        "小山炮打卡：努力不会骗自己，结果终会证明。",
        "小山炮打卡：今日的汗水，是明日的收获。",
        "小山炮打卡：成功是留给有准备的人。",
        "小山炮打卡：每一次行动，都是自律的表现。",
        "小山炮打卡：别等待完美，完美来自持续。",
        "小山炮打卡：踏实走好每一步，未来自然光明。",
        "小山炮打卡：坚持比天赋更重要。",
        "小山炮打卡：别害怕失败，害怕的是放弃。",
        "小山炮打卡：只要不停下脚步，就能抵达远方。",
        "小山炮打卡：坚持是最好的投资。",
        "小山炮打卡：用行动对抗犹豫和懒惰。",
        "小山炮打卡：未来属于每天努力的人。",
        "小山炮打卡：告诉自己，我依然在奋斗。",
        "小山炮打卡：信念是你最坚实的后盾。",
        "小山炮打卡：生活不会亏待每一个坚持的人。",
        "小山炮打卡：每天进步一点点，积累终将爆发。",
        "小山炮打卡：你种下的每一粒种子，都会发芽。",
        "小山炮打卡：坚持是无声的胜利。",
        "小山炮打卡：每天一点点，汇聚成未来的奇迹。",
        "小山炮打卡：比别人多坚持一秒，就多了一次机会。",
        "小山炮打卡：行动是一种态度，更是一种习惯。",
        "小山炮打卡：心态决定成败，努力决定未来。",
        "小山炮打卡：你的坚持，终将照亮前路。",
        "小山炮打卡：行动胜于空想，努力才是真理。",
        "小山炮打卡：失败不可怕，不努力才可怕。",
        "小山炮打卡：坚持才是最长情的告白。",
        "小山炮打卡：别放弃，你正在创造可能。",
        "小山炮打卡：每一天的努力都是你的资本。",
        "小山炮打卡：耐心耕耘，必有收获。",
        "小山炮打卡：从今天开始，打造最好的自己。",
        "小山炮打卡：坚持，是逆风飞翔的翅膀。",
        "小山炮打卡：不怕慢，就怕停。",
        "小山炮打卡：日积月累，点滴成金。",
        "小山炮打卡：你的努力没人看到，但结果会告诉所有人。",
        "小山炮打卡：只要不停，终会抵达。",
        "小山炮打卡：没有捷径，只有坚持。",
        "小山炮打卡：你今天的努力，都是明天的资本。",
        "小山炮打卡：一切伟大都始于坚持。",
        "小山炮打卡：每一次努力，都是胜利的种子。",
        "小山炮打卡：把每一天当作新的起点。",
        "小山炮打卡：持续发力，收获不负期待。",
        "小山炮打卡：行动，是你对梦想的负责。",
        "小山炮打卡：越努力，越幸运。",
        "小山炮打卡：成功离不开日复一日的坚持。",
        "小山炮打卡：坚持是你最强的武器。",
        "小山炮打卡：用坚持打败拖延和懒惰。",
        "小山炮打卡：只要努力，梦想终会成真。",
        "小山炮打卡：你越坚持，路越宽。",
        "小山炮打卡：成功没有终点，只有不断出发。",
        "小山炮打卡：坚持就是最好的修行。",
        "小山炮打卡：每一次努力都是向目标迈进。",
        "小山炮打卡：每天的努力，都值得被尊重。",
        "小山炮打卡：相信自己，坚持到底。",
        "小山炮打卡：未来属于不轻言放弃的人。",
        "小山炮打卡：别让今天的努力成为明天的遗憾。",
        "小山炮打卡：每一次坚持，都是成长。",
        "小山炮打卡：努力不是说说而已，要行动证明。",
        "小山炮打卡：坚持，是通往成功的桥梁。",
        "小山炮打卡：人生最怕停步不前。",
        "小山炮打卡：今天的努力，是未来的光芒。",
        "小山炮打卡：坚持，是对梦想最好的尊重。",
        "小山炮打卡：用坚持点亮前方的路。",
        "小山炮打卡：每天一点进步，终将非凡。",
        "小山炮打卡：成功没有偶然，只有必然。",
        "小山炮打卡：别轻言放弃，梦想在前方。",
        "小山炮打卡：行动，是梦想的起点。",
        "小山炮打卡：坚持，是成功的秘诀。",
        "小山炮打卡：让坚持成为习惯，而非选择。",
        "小山炮打卡：坚持，是对自己的最好投资。",
        "小山炮打卡：每个坚持的今天，都值得骄傲。",
        "小山炮打卡：失败不可怕，不坚持才可怕。",
        "小山炮打卡：不怕慢，只怕停。",
        "小山炮打卡：用坚持创造未来。",
        "小山炮打卡：梦想属于每天努力的人。",
        "小山炮打卡：坚持，是走向成功的必经之路。",
        "小山炮打卡：把握当下，坚持到底。",
        "小山炮打卡：坚持是最美的语言。",
        "小山炮打卡：没有坚持，就没有成长。",
        "小山炮打卡：成功的秘诀，就是不放弃。",
        "小山炮打卡：把每一天当作新的机会。",
        "小山炮打卡：坚持，是最坚实的力量。",
        "小山炮打卡：用行动说话，用坚持证明。",
        "小山炮打卡：别停下脚步，未来属于你。",
        "小山炮打卡：努力从现在开始。",
        "小山炮打卡：每天进步一点点，终有大成。",
        "小山炮打卡：坚持，是梦想的基石。",
        "小山炮打卡：用坚持点亮未来。",
        "小山炮打卡：今天的努力，是明天的辉煌。"
    ]
    
    # 随机选择一条打卡消息
    daka_message = random.choice(daka_messages)
    
    # 发送打卡消息
    sent_message = await update.message.reply_text(daka_message)
    
    # 1分钟后删除消息
    asyncio.create_task(delete_message_later(sent_message, delay=60))

async def chat_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/chat命令"""
    if not update.message or not update.message.text:
        return
        
    # 获取用户消息（去掉/chat命令）
    user_message = update.message.text.replace('/chat', '').strip()
    if not user_message:
        try:
            sent_message = await update.message.reply_text("请发送要聊天的内容，例如：/chat 你好")
            # 2分钟后删除提示消息
            asyncio.create_task(delete_message_later(sent_message, delay=120))
            # 2分钟后删除用户的命令消息
            asyncio.create_task(delete_message_later(update.message, delay=120))
        except Exception as e:
            logger.error(f"发送提示消息失败: {e}")
        return
        
    try:
        # 2分钟后删除用户的命令消息
        asyncio.create_task(delete_message_later(update.message, delay=120))
        
        # 构建API请求URL
        api_url = f"http://api.qingyunke.com/api.php?key=free&appid=0&msg={user_message}"
        
        # 发送请求
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status == 200:
                    # 获取响应内容
                    content = await response.text()
                    try:
                        # 尝试解析JSON
                        data = json.loads(content)
                        if data.get("result") == 0:
                            # 替换回复内容中的"菲菲"为"小山炮"
                            reply_content = data["content"].replace("菲菲", "小山炮")
                            try:
                                # 发送回复
                                sent_message = await update.message.reply_text(reply_content)
                                # 2分钟后删除回复消息
                                asyncio.create_task(delete_message_later(sent_message, delay=120))
                            except Exception as e:
                                logger.error(f"发送回复消息失败: {e}")
                        else:
                            logger.error(f"API返回错误: {data}")
                    except json.JSONDecodeError:
                        # 如果不是JSON，尝试从HTML中提取内容
                        try:
                            # 使用正则表达式提取JSON部分
                            import re
                            json_match = re.search(r'\{.*\}', content)
                            if json_match:
                                data = json.loads(json_match.group())
                                if data.get("result") == 0:
                                    # 替换回复内容中的"菲菲"为"小山炮"
                                    reply_content = data["content"].replace("菲菲", "小山炮")
                                    try:
                                        sent_message = await update.message.reply_text(reply_content)
                                        # 2分钟后删除回复消息
                                        asyncio.create_task(delete_message_later(sent_message, delay=120))
                                    except Exception as e:
                                        logger.error(f"发送回复消息失败: {e}")
                                else:
                                    logger.error(f"API返回错误: {data}")
                            else:
                                logger.error("无法从响应中提取JSON数据")
                                try:
                                    sent_message = await update.message.reply_text("抱歉，我现在无法回答这个问题")
                                    # 2分钟后删除错误提示消息
                                    asyncio.create_task(delete_message_later(sent_message, delay=120))
                                except Exception as e:
                                    logger.error(f"发送错误提示消息失败: {e}")
                        except Exception as e:
                            logger.error(f"解析响应内容时出错: {e}")
                            try:
                                sent_message = await update.message.reply_text("抱歉，我现在无法回答这个问题")
                                # 2分钟后删除错误提示消息
                                asyncio.create_task(delete_message_later(sent_message, delay=120))
                            except Exception as e:
                                logger.error(f"发送错误提示消息失败: {e}")
                else:
                    logger.error(f"API请求失败: {response.status}")
                    try:
                        sent_message = await update.message.reply_text("抱歉，我现在无法回答这个问题")
                        # 2分钟后删除错误提示消息
                        asyncio.create_task(delete_message_later(sent_message, delay=120))
                    except Exception as e:
                        logger.error(f"发送错误提示消息失败: {e}")
                    
    except Exception as e:
        logger.error(f"处理聊天消息时出错: {e}")
        try:
            sent_message = await update.message.reply_text("抱歉，我现在无法回答这个问题")
            # 2分钟后删除错误提示消息
            asyncio.create_task(delete_message_later(sent_message, delay=120))
        except Exception as e:
            logger.error(f"发送错误提示消息失败: {e}")

async def view_sheet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查看 Google Sheet 内容"""
    if not await check_admin(update, context):
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return

    try:
        # 获取所有记录
        records = sheets_storage.reminder_sheet.get_all_records()
        
        if not records:
            await update.message.reply_text("📊 当前没有记录")
            return
            
        # 格式化记录
        current_date = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
        today_records = [r for r in records if r.get("日期") == current_date]
        
        message = f"📊 今日提醒记录 ({current_date}):\n\n"
        
        if today_records:
            for i, record in enumerate(today_records, 1):
                user_id = record.get("用户ID", "未知")
                date = record.get("日期", "未知")
                message += f"{i}. 用户ID: {user_id}\n   时间: {date}\n\n"
        else:
            message += "暂无今日记录\n"
            
        # 添加统计信息
        message += f"\n📈 统计信息:\n"
        message += f"• 今日记录数: {len(today_records)}\n"
        message += f"• 总记录数: {len(records)}\n"
        
        # 发送消息
        sent_message = await update.message.reply_text(message)
        # 5分钟后删除消息
        asyncio.create_task(delete_message_later(sent_message, delay=300))
        
    except Exception as e:
        logger.error(f"查看记录失败: {e}")
        await update.message.reply_text("❌ 获取记录失败，请稍后重试")

async def export_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """导出群组成员信息"""
    if not await check_admin(update, context):
        await update.message.reply_text("❌ 只有管理员可以使用此命令")
        return
        
    if not update.message or not update.message.chat:
        return
        
    chat_id = update.message.chat.id
    
    # 检查是否是群组
    if update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ 此命令只能在群组中使用")
        return
    
    # 发送处理中的消息
    processing_msg = await update.message.reply_text("⏳ 正在获取群组成员信息，请稍候...")
    
    try:
        # 获取群组成员列表
        members = []
        
        # 获取管理员列表
        admins = await context.bot.get_chat_administrators(chat_id)
        members.extend(admins)
        
        # 获取所有成员列表
        all_members = await context.bot.get_chat_members(chat_id)
        # 过滤掉已经是管理员的成员
        existing_admin_ids = {admin.user.id for admin in admins}
        members.extend([m for m in all_members if m.user.id not in existing_admin_ids])
        
        if not members:
            await processing_msg.edit_text("❌ 无法获取群组成员信息")
            asyncio.create_task(delete_message_later(processing_msg, delay=5))
            return
        
        # 创建CSV文件
        output = io.StringIO()
        writer = csv.writer(output)
        
        # 写入表头
        writer.writerow(['用户ID', '用户名', '昵称', '加入时间', '状态'])
        
        # 写入成员信息
        for member in members:
            try:
                user = member.user
                join_date = member.joined_date.strftime('%Y-%m-%d %H:%M:%S') if member.joined_date else '未知'
                status = '管理员' if member.status in ['creator', 'administrator'] else '成员'
                
                writer.writerow([
                    user.id,
                    user.username or '无',
                    user.full_name,
                    join_date,
                    status
                ])
            except Exception as e:
                logger.error(f"处理成员信息时出错: {str(e)}")
                continue
        
        # 准备发送文件
        output.seek(0)
        csv_data = output.getvalue().encode('utf-8-sig')  # 使用带BOM的UTF-8编码，确保Excel正确显示中文
        
        # 生成文件名
        current_time = datetime.now(TIMEZONE).strftime('%Y%m%d_%H%M%S')
        filename = f"group_members_{current_time}.csv"
        
        # 发送文件
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(csv_data),
            filename=filename,
            caption=f"✅ 群组成员信息导出完成\n共 {len(members)} 名成员"
        )
        
        # 删除处理中的消息
        await processing_msg.delete()
        
    except Exception as e:
        logger.error(f"导出成员信息失败: {str(e)}")
        await processing_msg.edit_text(f"❌ 导出失败：{str(e)}")
        # 5秒后删除错误消息
        asyncio.create_task(delete_message_later(processing_msg, delay=5))

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
        bot_app.add_handler(CommandHandler("draw", lottery_handler))
        bot_app.add_handler(CommandHandler("daka", daka_handler))
        bot_app.add_handler(CommandHandler("chat", chat_command_handler))  # 添加聊天命令处理器
        bot_app.add_handler(CommandHandler("viewsheet", view_sheet_handler))  # 添加新命令
        bot_app.add_handler(CommandHandler("mystonks", toggle_mystonks_handler))  # 添加新命令
        bot_app.add_handler(CommandHandler("exportmembers", export_members_handler))  # 添加新命令
        
        # 添加回调处理器
        bot_app.add_handler(CallbackQueryHandler(ban_reason_handler, pattern="^ban_reason"))
        bot_app.add_handler(CallbackQueryHandler(mute_reason_handler, pattern="^mute_reason"))
        bot_app.add_handler(CallbackQueryHandler(reply_callback_handler, pattern="^reply:"))
        
        # 处理所有文本消息
        bot_app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_reply_flow))
        bot_app.add_handler(MessageHandler(filters.TEXT, message_handler))
        
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
