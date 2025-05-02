import os
import re
import json
import pytz
import random
import asyncio
import aiohttp
import logging
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import pandas as pd
import gspread
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from oauth2client.service_account import ServiceAccountCredentials
from fastapi import FastAPI, Request, HTTPException, APIRouter
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from bs4 import BeautifulSoup
import urllib.parse

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 全局变量
ADMIN_USER_IDS = [int(id) for id in os.getenv("ADMIN_USER_IDS", "").split(",") if id]

async def nitter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/nitter命令"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not context.args:
        help_text = (
            "🐦 Twitter 监控命令:\n\n"
            "/nitter status - 查看监控状态\n"
            "/nitter monitor <用户名> - 监控指定用户的推文\n"
            "/nitter search <关键词> - 搜索包含关键词的推文\n"
            "/nitter stop - 停止所有监控\n"
        )
        await update.message.reply_text(help_text)
        return

    command = context.args[0].lower()
    global nitter_monitor

    if command == "status":
        if not nitter_monitor:
            await update.message.reply_text("❌ Twitter监控未初始化")
            return
        await update.message.reply_text("✅ Twitter监控运行正常")

    elif command == "monitor":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 请提供要监控的用户名")
            return

        username = context.args[1]
        try:
            tweets = await nitter_monitor.get_latest_tweets(username)
            if tweets:
                message = f"✅ 成功获取@{username}的最新推文:\n\n"
                for tweet in tweets:
                    message += (
                        f"📝 {tweet['text']}\n"
                        f"🕒 {tweet['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
                        f"🔗 {tweet['url']}\n\n"
                    )
            else:
                message = f"❌ 未找到@{username}的推文"
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"❌ 获取推文失败: {str(e)}")

    elif command == "search":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 请提供要搜索的关键词")
            return

        keyword = " ".join(context.args[1:])
        try:
            tweets = await nitter_monitor.search_tweets(keyword)
            if tweets:
                message = f"✅ 找到包含'{keyword}'的推文:\n\n"
                for tweet in tweets:
                    message += (
                        f"📝 {tweet['text']}\n"
                        f"👤 @{tweet['author']}\n"
                        f"🕒 {tweet['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
                        f"🔗 {tweet['url']}\n\n"
                    )
            else:
                message = f"❌ 未找到包含'{keyword}'的推文"
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"❌ 搜索推文失败: {str(e)}")

    elif command == "stop":
        if not nitter_monitor:
            await update.message.reply_text("❌ Twitter监控未初始化")
            return
        await update.message.reply_text("✅ Twitter监控已停止")

    else:
        await update.message.reply_text("❌ 未知命令，请使用 status/monitor/search/stop")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """检查用户是否是管理员"""
    return update.effective_user.id in ADMIN_USER_IDS

async def delete_message_later(message, delay: int = 30):
    """在指定时间后删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"删除消息失败: {str(e)}")

async def twitter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/twitter命令"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return

    if not context.args:
        help_text = (
            "🐦 Twitter 监控命令:\n\n"
            "/twitter status - 查看Twitter监控状态\n"
            "/twitter monitor <用户名> - 监控指定用户的推文\n"
            "/twitter keyword <关键词> - 监控包含关键词的推文\n"
            "/twitter stop - 停止所有监控\n"
        )
        await update.message.reply_text(help_text)
        return

    command = context.args[0].lower()
    global twitter_monitor

    if command == "status":
        if not twitter_monitor:
            await update.message.reply_text("❌ Twitter监控未初始化")
            return

        try:
            # 测试Twitter API连接
            async with aiohttp.ClientSession() as session:
                # 使用 Twitter API v2 的示例端点
                api_url = "https://api.twitter.com/2/users/me"
                auth = tweepy.OAuth1UserHandler(
                    TWITTER_API_KEY,
                    TWITTER_API_SECRET_KEY,
                    TWITTER_ACCESS_TOKEN,
                    TWITTER_ACCESS_TOKEN_SECRET
                )
                headers = {
                    "Authorization": f"Bearer {auth.access_token}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }
                async with session.get(api_url, headers=headers) as response:
                    if response.status == 200:
                        status = "✅ Twitter API连接正常"
                    else:
                        status = f"❌ Twitter API连接异常: HTTP {response.status}"
        except Exception as e:
            status = f"❌ Twitter API连接异常: {str(e)}"

        await update.message.reply_text(status)

    elif command == "monitor":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 请提供要监控的Twitter用户名")
            return

        username = context.args[1]
        try:
            tweets = await twitter_monitor.get_latest_tweets(username)
            if tweets:
                message = f"✅ 成功获取@{username}的最新推文:\n\n"
                for tweet in tweets:
                    message += (
                        f"📝 {tweet['text']}\n"
                        f"🕒 {tweet['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
                        f"👍 {tweet['likes']} | 🔁 {tweet['retweets']}\n"
                        f"🔗 {tweet['url']}\n\n"
                    )
            else:
                message = f"❌ 未找到@{username}的推文"
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"❌ 获取推文失败: {str(e)}")

    elif command == "keyword":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 请提供要监控的关键词")
            return

        keyword = " ".join(context.args[1:])
        try:
            tweets = await twitter_monitor.monitor_keyword(keyword)
            if tweets:
                message = f"✅ 找到包含'{keyword}'的推文:\n\n"
                for tweet in tweets:
                    message += (
                        f"📝 {tweet['text']}\n"
                        f"👤 @{tweet['author']}\n"
                        f"🕒 {tweet['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
                        f"👍 {tweet['likes']} | 🔁 {tweet['retweets']}\n"
                        f"🔗 {tweet['url']}\n\n"
                    )
            else:
                message = f"❌ 未找到包含'{keyword}'的推文"
            await update.message.reply_text(message)
        except Exception as e:
            await update.message.reply_text(f"❌ 搜索推文失败: {str(e)}")

    elif command == "stop":
        if not twitter_monitor:
            await update.message.reply_text("❌ Twitter监控未初始化")
            return

        try:
            # 这里可以添加停止监控的逻辑
            await update.message.reply_text("✅ Twitter监控已停止")
        except Exception as e:
            await update.message.reply_text(f"❌ 停止监控失败: {str(e)}")

    else:
        await update.message.reply_text("❌ 未知命令，请使用 status/monitor/keyword/stop")

app = FastAPI()
# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS")  # Base64编码的JSON凭证
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "BanRecords")    # Google Sheet名称
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL', '')}{WEBHOOK_PATH}" if os.getenv("RENDER_EXTERNAL_URL") else None
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))
MAX_RECORDS_DISPLAY = 10
EXCEL_FILE = "ban_records.xlsx"

# 全局变量
bot_app: Optional[Application] = None
bot_initialized: bool = False
ban_records: List[Dict[str, Any]] = []

class GoogleSheetsStorage:
    _last_request_time = 0
    
    @staticmethod
    async def _throttle():
        """Enforce minimum delay between API calls"""
        now = time.time()
        elapsed = now - GoogleSheetsStorage._last_request_time
        if elapsed < 1.1:  # 1.1 second minimum between requests
            await asyncio.sleep(1.1 - elapsed)
        GoogleSheetsStorage._last_request_time = time.time()
    @staticmethod
    async def load_from_sheet() -> List[Dict[str, Any]]:
        """从Google Sheet加载数据"""
        if not GOOGLE_SHEETS_CREDENTIALS:
            logger.warning("未配置GOOGLE_SHEETS_CREDENTIALS，无法从Google Sheet加载数据")
            return []
            
        try:
            worksheet = await GoogleSheetsStorage._get_worksheet()  # 不传参数获取默认工作表
            records = worksheet.get_all_records()
            
            expected_columns = ["操作时间", "电报群组名称", "用户ID", "用户名", "名称", "操作管理", "理由", "操作"]
            
            if not records:
                logger.info("Google Sheet为空，将创建新记录")
                return []
                
            first_record = records[0] if records else {}
            if not all(col in first_record for col in expected_columns):
                logger.warning("Google Sheet列名不匹配，可能需要修复")
                return []
                
            return records
        except Exception as e:
            logger.error(f"从Google Sheet加载数据失败: {e}")
            # Create a local backup file
            try:
                with open("local_backup.json", "r") as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return []
            except Exception as backup_error:
                logger.error(f"本地备份加载失败: {backup_error}")
                return []
    @staticmethod
    async def _get_gspread_client():
        """获取gspread客户端"""
        try:
            # Get credentials with proper padding
            creds_b64 = GOOGLE_SHEETS_CREDENTIALS.strip()
            padding = len(creds_b64) % 4
            if padding:
                creds_b64 += '=' * (4 - padding)
            
            # Decode
            creds_json = base64.b64decode(creds_b64).decode('utf-8')
            creds_dict = json.loads(creds_json)
            
            # Verify we got the private key correctly
            if not creds_dict.get('private_key'):
                raise ValueError("Invalid credentials - missing private key")
                
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            return gspread.authorize(credentials)
        except Exception as e:
            logger.error(f"获取gspread客户端失败: {str(e)}")
            raise

    @staticmethod
    async def _get_worksheet(sheet_name: str = None) -> gspread.Worksheet:
        """获取工作表，默认返回第一个工作表"""
        try:
            # Get credentials with proper padding
            creds_b64 = GOOGLE_SHEETS_CREDENTIALS.strip()
            padding = len(creds_b64) % 4
            if padding:
                creds_b64 += '=' * (4 - padding)
            
            # Decode
            creds_json = base64.b64decode(creds_b64).decode('utf-8')
            creds_dict = json.loads(creds_json)
            
            # Verify we got the private key correctly
            if not creds_dict.get('private_key'):
                raise ValueError("Invalid credentials - missing private key")
                
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gc = gspread.authorize(credentials)
            
            try:
                sh = gc.open(GOOGLE_SHEET_NAME)
                if sheet_name:
                    return sh.worksheet(sheet_name)
                return sh.sheet1
            except gspread.SpreadsheetNotFound:
                sh = gc.create(GOOGLE_SHEET_NAME)
                sh.share(creds_dict["client_email"], perm_type="user", role="writer")
                if sheet_name:
                    return sh.add_worksheet(title=sheet_name, rows=100, cols=20)
                return sh.sheet1
                
        except Exception as e:
            logger.error(f"Google Sheets 初始化失败: {str(e)}")
            raise
    @staticmethod
    def _auth_with_dict(creds_dict: dict) -> gspread.Worksheet:
        """使用字典凭证认证"""
        # More flexible credential type checking
        if not isinstance(creds_dict, dict):
            raise ValueError("Invalid credentials format - expected dictionary")
        
        # Accept either service account or API key
        if creds_dict.get("type") == "service_account":
            scope = ['https://spreadsheets.google.com/feeds',
                    'https://www.googleapis.com/auth/drive']
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        elif "api_key" in creds_dict:  # Alternative authentication method
            gc = gspread.service_account_from_dict(creds_dict)
        else:
            raise ValueError("Unsupported credential type - must be service account or API key")
        
        gc = gspread.authorize(credentials)
        return gc.open(GOOGLE_SHEET_NAME).sheet1

    @staticmethod
    def _auth_with_file(file_path: str) -> gspread.Worksheet:
        """使用文件路径认证"""
        scope = ['https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive']
        credentials = ServiceAccountCredentials.from_json_keyfile_name(file_path, scope)
        gc = gspread.authorize(credentials)
        return gc.open(GOOGLE_SHEET_NAME).sheet1

    @staticmethod
    async def save_to_sheet(records: List[Dict[str, Any]]) -> bool:
        """保存数据到Google Sheet"""
        try:
            worksheet = await GoogleSheetsStorage._get_worksheet()
            
            # 清除现有数据（保留标题行）
            worksheet.clear()
            
            # 准备数据 - 确保所有记录都有所有字段
            expected_columns = ["操作时间", "电报群组名称", "用户ID", 
                              "用户名", "名称", 
                              "操作管理", "理由", "操作"]  # 新增"操作"列
            
            # 添加标题行
            worksheet.append_row(expected_columns)
            
            # 添加数据行
            for record in records:
                row = [str(record.get(col, "")) for col in expected_columns]
                worksheet.append_row(row)
            
            logger.info("数据已保存到Google Sheet")
            return True
        except Exception as e:
            logger.error(f"保存到Google Sheet失败: {e}")
            return False
    @staticmethod
    async def get_keyword_replies() -> List[Dict[str, str]]:
        """获取所有关键词回复配置"""
        try:
            worksheet = await GoogleSheetsStorage.get_keyword_replies_worksheet()
            records = worksheet.get_all_records()
            return records
        except Exception as e:
            logger.error(f"获取关键词回复失败: {e}")
            return []
    @staticmethod
    async def get_keyword_replies_worksheet():
        """获取关键词回复工作表"""
        try:
            worksheet = await GoogleSheetsStorage._get_worksheet("KeywordReplies")
            return worksheet
        except gspread.WorksheetNotFound:
                # 如果工作表不存在则创建
            gc = await GoogleSheetsStorage._get_gspread_client()
            sh = gc.open(GOOGLE_SHEET_NAME)
            worksheet = sh.add_worksheet(title="KeywordReplies", rows=100, cols=5)
                # 添加标题行
            worksheet.append_row(["关键词", "回复内容", "链接", "链接文本", "创建时间"])
            return worksheet
        except Exception as e:
            logger.error(f"获取关键词回复工作表失败: {e}")
            raise

    @staticmethod
    async def add_keyword_reply(keyword: str, reply_text: str, link: str = "", link_text: str = ""):
        """添加关键词回复"""
        try:
            worksheet = await GoogleSheetsStorage.get_keyword_replies_worksheet()
            worksheet.append_row([
                keyword.lower(),
                reply_text,
                link,
                link_text,
                datetime.now(TIMEZONE).isoformat()
            ])
            return True
        except Exception as e:
            logger.error(f"添加关键词回复失败: {e}")
            return False



    @staticmethod
    async def delete_keyword_reply(keyword: str):
        """删除关键词回复"""
        try:
            worksheet = await GoogleSheetsStorage.get_keyword_replies_worksheet()
            records = worksheet.get_all_records()
            
            # 找到匹配的行并删除
            for i, record in enumerate(records, start=2):  # 从第2行开始
                if record["关键词"].lower() == keyword.lower():
                    worksheet.delete_rows(i)
                    return True
            return False
        except Exception as e:
            logger.error(f"删除关键词回复失败: {e}")
            return False
class BanManager:
    """封禁管理工具类"""
    
    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str, action_type: str = "ban") -> InlineKeyboardMarkup:
        """生成封禁/禁言原因选择键盘"""
        action_prefix = "mute_reason" if action_type == "mute" else "ban_reason"
        buttons = [
            [
                InlineKeyboardButton("广告", callback_data=f"{action_prefix}|{banned_user_id}|{banned_user_name}|广告"),
                InlineKeyboardButton("辱骂", callback_data=f"{action_prefix}|{banned_user_id}|{banned_user_name}|辱骂"),
                InlineKeyboardButton("诈骗", callback_data=f"{action_prefix}|{banned_user_id}|{banned_user_name}|诈骗"),
            ],
            [
                InlineKeyboardButton("FUD", callback_data=f"{action_prefix}|{banned_user_id}|{banned_user_name}|FUD"),
                InlineKeyboardButton("带节奏", callback_data=f"{action_prefix}|{banned_user_id}|{banned_user_name}|带节奏"),
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

    @classmethod
    async def get_ban_count(cls, user_id: int) -> int:
        """获取用户被封禁次数"""
        global ban_records
        return sum(1 for record in ban_records if record.get("用户ID") == user_id)

    @staticmethod
    async def save_to_db(
        chat_title: str,
        banned_user_id: int,
        banned_user_name: str,
        admin_name: str,
        reason: str = "未填写",
        banned_username: Optional[str] = None,
        action_type: str = "封禁"  # 新增操作类型参数，默认为"封禁"
    ) -> bool:
        """保存封禁记录到内存并导出到Google Sheet"""
        global ban_records
        
        try:
            record = {
                "操作时间": datetime.now(TIMEZONE).isoformat(),
                "电报群组名称": chat_title,
                "用户ID": banned_user_id,
                "名称": banned_user_name,
                "用户名": f"@{banned_username}" if banned_username else "无",
                "操作管理": admin_name,
                "理由": reason,
                "操作": action_type  # 新增操作类型字段
            }
            
            ban_records.append(record)
            
            # 同步到Google Sheet
            success = await GoogleSheetsStorage.save_to_sheet(ban_records)
            if not success:
                logger.warning("Google Sheet同步失败，数据仅保存在内存中")
            
            logger.info(f"记录已保存: {banned_user_name} | {reason} | {action_type}")
            return True
        except Exception as e:
            logger.error(f"保存记录失败: {e}")
            return False

async def delete_message_later(message, delay: int = 30) -> None:
    """延迟删除消息"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"删除消息失败: {e}")

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
        logger.error(f"检查管理员状态失败: {e}")
        return False

async def noon_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    NOON_GREETINGS = [
        # 温馨系列
        f"☀️ {user.first_name}午安！阳光正好，记得休息一会儿哦~",
        f"🍱 {user.first_name}午安！该享用美味的午餐啦~",
        f"😴 {user.first_name}午安！需要来个午睡充电吗？",
        f"🌤️ {user.first_name}午安！一天已经过半啦，继续加油~",
        
        # 幽默系列
        f"⏰ {user.first_name}午安！你的胃在抗议啦，快去喂它~",
        f"💤 {user.first_name}午安！困了可以学猫咪打个盹~",
        f"🍵 {user.first_name}午安！来杯茶提提神吧~",
        f"🍜 {user.first_name}午安！泡面还是外卖？这是个问题~",
        
        # 励志系列
        f"🚀 {user.first_name}午安！下午也要元气满满~",
        f"💪 {user.first_name}午安！上午表现很棒，下午再接再厉~",
        f"🎯 {user.first_name}午安！上午的目标完成了吗？",
        
        # 特别彩蛋
        f"🍱 {user.first_name}午安！今日午餐推荐：{random.choice(['拉面','寿司','饺子','盖饭','沙拉'])}~",
        f"☕ {user.first_name}午安！咖啡因含量：{random.randint(10,100)}%",
    ]
    
    # 随机选择一条问候语
    reply = random.choice(NOON_GREETINGS)
    
    # 10%概率附加彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "🎉", "💫", "🎊"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个说午安的小可爱~"
    
    sent_message = await update.message.reply_text(reply)
    logger.info(f"🌞 向 {user.full_name} 发送了午安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    
    """处理/start命令"""
    user = update.effective_user
    help_text = (
        "👮 封禁管理机器人使用说明:\n\n"
        "/k - 踢出用户(回复消息使用)\n"
        "/m - 禁言用户(回复消息并指定时间)\n"
        "/um - 解除禁言\n"
        "/records - 查看封禁记录\n"
        "/search <关键词> - 搜索封禁记录\n"
        "/export - 导出封禁记录为Excel文件\n\n"
        "请确保机器人有管理员权限!"
    )
    
    await update.message.reply_text(help_text)
    logger.info(f"新用户启动: {user.full_name if user else 'Unknown'}")

async def kick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/kick命令"""
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
        
        # 获取用户被封禁次数
        ban_count = await BanManager.get_ban_count(target_user.id)
        
        kick_msg = await update.message.reply_text(
            f"🚨 用户 [{target_user.full_name}](tg://user?id={target_user.id}) 已被踢出\n"
            f"📌 历史封禁次数: {ban_count}",
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
        
        # 保存操作上下文
        context.chat_data["last_ban"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id,
            "target_username": target_user.username  # 存储username用于后续处理
        }
        
        # 设置自动删除
        asyncio.create_task(delete_message_later(kick_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 踢出失败: 踢管理员失败。建议你先踢自己冷静一下。")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"踢出用户失败: {e}")

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理封禁/禁言原因选择"""
    query = update.callback_query
    await query.answer()
    
    try:
        action, user_id_str, user_name, reason = query.data.split("|")
        banned_user_id = int(user_id_str)
    except ValueError:
        error_msg = await query.message.reply_text("⚠️ 无效的回调数据")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 获取操作上下文
    if action == "ban_reason":
        last_action = context.chat_data.get("last_ban", {})
        action_type = "封禁"
    elif action == "mute_reason":
        last_action = context.chat_data.get("last_mute", {})
        action_type = "禁言"
    else:
        error_msg = await query.message.reply_text("⚠️ 未知的操作类型")
        asyncio.create_task(delete_message_later(error_msg))
        return
    
    # 验证操作权限
    if query.from_user.id != last_action.get("operator_id"):
        error_msg = await query.message.reply_text("⚠️ 只有执行操作的管理员能选择原因")
        asyncio.create_task(delete_message_later(error_msg))
        return    
    
    # 保存记录
    try:
        success = await BanManager.save_to_db(
            chat_title=last_action.get("chat_title", query.message.chat.title),
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            banned_username=last_action.get("target_username"),
            admin_name=query.from_user.full_name,
            reason=f"{'禁言' if action == 'mute_reason' else '封禁'}: {reason}" + 
                  (f" ({last_action.get('duration')})" if action == "mute_reason" else ""),
            action_type="禁言" if action == "mute_reason" else "封禁"  # 添加这行
        )
        
        if success:
            confirm_msg = await query.message.reply_text(f"✅ 已记录: {user_name} - {reason}")
            asyncio.create_task(delete_message_later(confirm_msg))
        else:
            error_msg = await query.message.reply_text("❌ 保存记录失败")
            asyncio.create_task(delete_message_later(error_msg))
        
        asyncio.create_task(delete_message_later(query.message))
        
    except Exception as e:
        error_msg = await query.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"保存原因失败: {e}")

async def mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/mute命令"""
    if not await is_admin(update, context):
        msg = await update.message.reply_text("❌ 只有管理员可以使用此命令")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not update.message.reply_to_message:
        msg = await update.message.reply_text("请回复要禁言的用户消息")
        asyncio.create_task(delete_message_later(msg))
        return
    
    if not context.args:
        msg = await update.message.reply_text("请指定禁言时间，例如: /mute 1d2h30m")
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
            until_date=until_date
        )
        
        # 获取用户被封禁次数
        ban_count = await BanManager.get_ban_count(target_user.id)
        
        mute_msg = await update.message.reply_text(
            f"⏳ 用户 [{target_user.full_name}](tg://user?id={target_user.id}) "
            f"已被禁言 {duration}\n"
            f"📌 历史封禁次数: {ban_count}",
            parse_mode="Markdown"
        )
        
        # 添加封禁原因选择
        reply_markup = BanManager.get_ban_reasons_keyboard(
            banned_user_id=target_user.id,
            banned_user_name=target_user.full_name,
            action_type="mute"
        )
        
        reason_msg = await update.message.reply_text(
            "请选择禁言原因：",
            reply_markup=reply_markup
        )
        
        # 保存操作上下文
        context.chat_data["last_mute"] = {
            "target_id": target_user.id,
            "operator_id": update.effective_user.id,
            "target_username": target_user.username,  # 存储username用于后续处理
            "duration": str(duration),
            "chat_title": update.effective_chat.title
        }
        
        # 设置自动删除
        asyncio.create_task(delete_message_later(mute_msg))
        asyncio.create_task(delete_message_later(reason_msg))
        
    except ValueError as e:
        error_msg = await update.message.reply_text(f"❌ 时间格式错误: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.warning(f"禁言时间格式错误: {e}")
    except Exception as e:
        error_msg = await update.message.reply_text(f"⚠️ 系统检测到珍贵同事光环 ⚠️本次禁言操作已被【职场生存法则】拦截")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"禁言用户失败: {e}")
        
async def morning_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"🌷 {user.first_name}早安！今天的你比花儿还美丽~",
        f"🍯 甜甜的早安送给甜甜的{user.first_name}！",
        f"🌤️ {user.first_name}早上好！乌云后面依然是灿烂的晴天~",
        f"🦋 {user.first_name}早安！愿你今天像蝴蝶一样轻盈自在~",
        f"🎵 叮~{user.first_name}的专属早安闹钟已送达！",
        f"🍓 草莓味的早晨送给可爱的{user.first_name}！",
        f"🌈 {user.first_name}早安！今天会有彩虹般的好运哦~",
        f"🐶 汪汪！{user.first_name}早上好！要像狗狗一样活力满满~",
        f"🌿 {user.first_name}早安！新的一天从呼吸新鲜空气开始~",
        f"🦄 {user.first_name}早上好！今天是属于你的魔法日~",
        f"🌞 {user.first_name}早安！你今天的气色真好~",
        # 阳光正能量系列 (30条)
        f"🌞 {user.first_name}早安！今天的阳光为你而来~",
        f"🌻 {user.first_name}早上好！像向日葵一样追逐光明吧~",
        f"✨ {user.first_name}早安！你值得世间所有美好~",
        f"💖 {user.first_name}早上好！爱自己是终生浪漫的开始~",
        f"🌈 {user.first_name}早安！风雨后总会有彩虹~",
        f"🌱 {user.first_name}早上好！每个清晨都是新的成长机会~",
        f"🕊️ {user.first_name}早安！让烦恼如白鸽飞走~",
        f"🌄 {user.first_name}早上好！晨光会温柔拥抱努力的人~",
        f"🌊 {user.first_name}早安！像海浪一样保持前进的勇气~",
        f"🍃 {user.first_name}早上好！生命如春风永远充满可能~",
        f"🌟 {user.first_name}早安！你本来就是闪耀的星辰~",
        f"🌸 {user.first_name}早上好！美好会如约而至~",
        f"☀️ {user.first_name}早安！心里有光哪里都明亮~",
        f"🌿 {user.first_name}早上好！保持简单纯粹的快乐~",
        f"💫 {user.first_name}早安！宇宙正在为你安排惊喜~",
        f"🌼 {user.first_name}早上好！野花也有春天的权利~",
        f"🌞 {user.first_name}早安！让温暖从心底升起~",
        f"🌻 {user.first_name}早上好！面向阳光阴影就在身后~",
        f"✨ {user.first_name}早安！平凡日子里也有星光~",
        f"💖 {user.first_name}早上好！你给世界的温柔会回馈你~",
        f"🌈 {user.first_name}早安！生活是块调色板由你主宰~",
        f"🌱 {user.first_name}早上好！破土而出的勇气最美~",
        f"🕊️ {user.first_name}早安！平和的心是最好归宿~",
        f"🌄 {user.first_name}早上好！站在高处看风景更美~",
        f"🌊 {user.first_name}早安！潮起潮落都是人生乐章~",
        f"🍃 {user.first_name}早上好！轻盈的心才能飞得更高~",
        f"🌟 {user.first_name}早安！黑暗只是暂时的过客~",
        f"🌸 {user.first_name}早上好！花期不同不必着急~",
        f"☀️ {user.first_name}早安！自带光芒的人永不孤单~",
        f"🌿 {user.first_name}早上好！像植物一样安静生长~",
        # 励志成长系列 (30条)
        f"💪 {user.first_name}早安！今天的你比昨天更强大~",
        f"🚀 {user.first_name}早上好！梦想需要行动来灌溉~",
        f"🏆 {user.first_name}早安！每个坚持都算数~",
        f"📈 {user.first_name}早上好！进步哪怕1%也是胜利~",
        f"🧗 {user.first_name}早安！上坡路虽然累但值得~",
        f"🛤️ {user.first_name}早上好！人生没有白走的路~",
        f"🌋 {user.first_name}早安！压力会让你更璀璨~",
        f"⚓ {user.first_name}早上好！稳住心态才能远航~",
        f"🛡️ {user.first_name}早安！挫折是成长的铠甲~",
        f"🔦 {user.first_name}早上好！黑暗中也别熄灭心灯~",
        f"🧭 {user.first_name}早安！内心指南针永不迷路~",
        f"🛠️ {user.first_name}早上好！生活需要主动创造~",
        f"⏳ {user.first_name}早安！时间会奖励坚持的人~",
        f"📚 {user.first_name}早上好！知识是最忠实的伙伴~",
        f"🌳 {user.first_name}早上好！扎根的日子终会开花~",
        f"🦋 {user.first_name}早上好！蜕变需要耐心等待~",
        f"🧲 {user.first_name}早上好！正能量吸引更多美好~",
        f"⚡ {user.first_name}早上好！突破舒适区的感觉超棒~",
        f"🌠 {user.first_name}早安！许下的愿望正在路上~",
        f"🛫 {user.first_name}早上好！准备好迎接新旅程~",
        f"🧗‍♀️ {user.first_name}早安！山顶的风景在等你~",
        f"🛤️ {user.first_name}早上好！弯路也有独特风景~",
        f"🌄 {user.first_name}早安！黎明前的黑暗最短暂~",
        f"⛵ {user.first_name}早上好！逆风更适合飞翔~",
        f"🔑 {user.first_name}早安！答案就在你手中~",
        f"🏔️ {user.first_name}早上好！高山让人变得更强大~",
        f"🛎️ {user.first_name}早安！机会在敲门你听见了吗~",
        f"📅 {user.first_name}早上好！今天是最年轻的一天~",
        f"🌌 {user.first_name}早安！你的潜力如宇宙浩瀚~",
        f"🏅 {user.first_name}早上好！人生马拉松贵在坚持~",
        # 心灵治愈系列 (30条)
        f"🤗 {user.first_name}早安！给自己一个温暖的拥抱~",
        f"🛌 {user.first_name}早上好！好好休息也是种能力~",
        f"🍵 {user.first_name}早安！慢下来品生活的滋味~",
        f"📿 {user.first_name}早上好！平和的心最珍贵~",
        f"🎐 {user.first_name}早安！让烦恼如风铃飘走~",
        f"🛀 {user.first_name}早上好！洗净疲惫重新出发~",
        f"🌙 {user.first_name}早安！昨夜星辰已为你祝福~",
        f"🧸 {user.first_name}早上好！保持童心也很美好~",
        f"🕯️ {user.first_name}早安！做自己的那盏明灯~",
        f"🎈 {user.first_name}早上好！放下执念才能轻盈~",
        f"🌉 {user.first_name}早安！桥的那头有新希望~",
        f"🛋️ {user.first_name}早上好！家是充电的港湾~",
        f"🌃 {user.first_name}早安！星光不负夜归人~",
        f"🪔 {user.first_name}早上好！温暖的光永不熄灭~",
        f"🌫️ {user.first_name}早安！迷雾终会散去~",
        f"🛁 {user.first_name}早上好！洗去昨日的疲惫~",
        f"🌲 {user.first_name}早安！森林在为你深呼吸~",
        f"🪑 {user.first_name}早上好！停下来欣赏风景吧~",
        f"🌧️ {user.first_name}早安！雨水会滋养新生命~",
        f"☕ {user.first_name}早上好！苦涩后才有回甘~",
        f"🛎️ {user.first_name}早安！幸福在细微处等你~",
        f"🪞 {user.first_name}早上好！镜中的你值得被爱~",
        f"🌠 {user.first_name}早安！许个愿吧会实现的~",
        f"🛌 {user.first_name}早上好！好好爱自己最重要~",
        f"🌙 {user.first_name}早安！月亮守护你的梦境~",
        f"🧘 {user.first_name}早上好！静心聆听内在声音~",
        f"🕊️ {user.first_name}早安！宽恕是给自己的礼物~",
        f"🎼 {user.first_name}早上好！生活是首温柔的歌~",
        f"🌁 {user.first_name}早安！云层之上永远晴朗~",
        f"🛀 {user.first_name}早上好！新的一天从净化开始~",
        # 人生智慧系列 (30条)
        f"📖 {user.first_name}早安！生活是本最好的教科书~",
        f"🖋️ {user.first_name}早上好！你正在书写独特故事~",
        f"🎭 {user.first_name}早安！人生如戏但你是主角~",
        f"🧩 {user.first_name}早上好！每段经历都有意义~",
        f"🛤️ {user.first_name}早安！岔路口也是风景~",
        f"🕰️ {user.first_name}早上好！珍惜当下的礼物~",
        f"🌊 {user.first_name}早安！退潮时才知道谁在裸泳~",
        f"🍂 {user.first_name}早上好！落叶教会我们放下~",
        f"🦋 {user.first_name}早安！改变是美丽的开始~",
        f"🌳 {user.first_name}早上好！年轮里藏着智慧~",
        f"🪶 {user.first_name}早安！轻装上阵才能飞远~",
        f"🌌 {user.first_name}早上好！渺小让我们更勇敢~",
        f"🛶 {user.first_name}早安！顺流逆流都是旅程~",
        f"🗝️ {user.first_name}早上好！答案往往很简单~",
        f"🌄 {user.first_name}早安！视野决定境界~",
        f"🪁 {user.first_name}早上好！线握在自己手中~",
        f"🌫️ {user.first_name}早安！看不清时更要静心~",
        f"🛤️ {user.first_name}早上好！弯路也是必经之路~",
        f"🎻 {user.first_name}早安！生命需要节奏感~",
        f"🧭 {user.first_name}早上好！直觉是最好的指南针~",
        f"🌠 {user.first_name}早安！流星教会我们刹那即永恒~",
        f"🪶 {user.first_name}早上好！羽毛也能承载梦想~",
        f"🌉 {user.first_name}早安！连接过去与未来~",
        f"🛎️ {user.first_name}早上好！觉醒从此刻开始~",
        f"📜 {user.first_name}早安！每个选择都是伏笔~",
        f"🪔 {user.first_name}早上好！智慧之光永不灭~",
        f"🌲 {user.first_name}早安！森林知道所有答案~",
        f"🛶 {user.first_name}早安！掌舵自己的人生~",
        f"🎎 {user.first_name}早安！缘分是奇妙的礼物~",
        f"🌅 {user.first_name}早上好！日出是希望的象征~",
        # 感恩珍惜系列 (30条)
        f"🙏 {user.first_name}早安！感谢呼吸的每一秒~",
        f"🌍 {user.first_name}早上好！地球因你更美好~",
        f"💞 {user.first_name}早安！珍惜身边的温暖~",
        f"👨‍👩‍👧‍👦 {user.first_name}早上好！家人的爱是无价宝~",
        f"🤝 {user.first_name}早安！感恩每个相遇~",
        f"🌾 {user.first_name}早上好！一粥一饭当思来之不易~",
        f"🛏️ {user.first_name}早安！感恩温暖的被窝~",
        f"🚰 {user.first_name}早上好！清水也是恩赐~",
        f"🌞 {user.first_name}早安！感谢阳光免费照耀~",
        f"🌳 {user.first_name}早上好！向大树学习奉献~",
        f"📱 {user.first_name}早上好！科技让爱零距离~",
        f"🍞 {user.first_name}早上好！面包背后有无数双手~",
        f"👣 {user.first_name}早上好！感谢双脚带你看世界~",
        f"👀 {user.first_name}早上好！眼睛让你看见美好~",
        f"🌧️ {user.first_name}早上好！雨水滋润万物生长~",
        f"🍎 {user.first_name}早上好！苹果里有整个宇宙~",
        f"🚌 {user.first_name}早上好！感恩平安的出行~",
        f"📚 {user.first_name}早上好！知识是前人馈赠~",
        f"🛒 {user.first_name}早上好！丰盛物资值得珍惜~",
        f"💐 {user.first_name}早上好！花朵无私绽放美丽~",
        f"🐦 {user.first_name}早安！鸟鸣是自然闹钟~",
        f"☕ {user.first_name}早上好！咖啡香里有故事~",
        f"👕 {user.first_name}早安！衣物承载他人劳动~",
        f"🏠 {user.first_name}早上好！家是温暖的堡垒~",
        f"🛋️ {user.first_name}早安！沙发见证美好时光~",
        f"🌙 {user.first_name}早上好！月亮守护每个夜归人~",
        f"🍽️ {user.first_name}早安！食物是生命的礼物~",
        f"🚿 {user.first_name}早上好！清水洗去尘埃~",
        f"🛏️ {user.first_name}早安！床铺承载甜美梦境~",
        f"🌅 {user.first_name}早上好！日出是希望的承诺~",
        # 希望憧憬系列 (20条)
        f"🌠 {user.first_name}早安！今天的你会遇见惊喜~",
        f"🦋 {user.first_name}早上好！蜕变后的你更美丽~",
        f"🌱 {user.first_name}早安！种子正在悄悄发芽~",
        f"🛤️ {user.first_name}早上好！前方有美好等候~",
        f"🎁 {user.first_name}早安！生活准备了很多礼物~",
        f"🌈 {user.first_name}早上好！转角可能遇见彩虹~",
        f"🪄 {user.first_name}早安！魔法就在平凡日子里~",
        f"🌻 {user.first_name}早上好！阳光总会追随你~",
        f"🎈 {user.first_name}早安！让梦想飞得更高~",
        f"🌉 {user.first_name}早上好！桥的那头是希望~",
        f"🛫 {user.first_name}早安！新的旅程即将开始~",
        f"🌌 {user.first_name}早上好！星辰大海在等你~",
        f"🌄 {user.first_name}早安！山顶的风景值得期待~",
        f"🪁 {user.first_name}早上好！让理想乘风飞翔~",
        f"🎼 {user.first_name}早安！生命乐章正在谱写~",
        f"🌊 {user.first_name}早上好！潮水带来新机遇~",
        f"🛎️ {user.first_name}早安！幸福正在敲门~",
        f"🌠 {user.first_name}早上好！流星听见你的愿望~",
        f"🌱 {user.first_name}早安！新芽代表无限可能~",
        f"🦋 {user.first_name}早上好！破茧时刻即将到来~",
        # 天气主题 (15条)
        f"🌧️ {user.first_name}早安！雨天也要保持好心情哦~",
        f"❄️ {user.first_name}早上好！寒冷的日子里请多保暖~",
        f"🌪️ {user.first_name}早安！就算有风暴也阻挡不了你的光芒~",
        f"🌤️ {user.first_name}早上好！今天天气和你一样晴朗~",
        f"🌫️ {user.first_name}早安！迷雾终将散去，美好终会到来~",
        f"🌩️ {user.first_name}早上好！雷雨过后必有彩虹~",
        f"🌡️ {user.first_name}早上好！注意天气变化别感冒哦~",
        f"🌦️ {user.first_name}早安！短暂的阵雨是为了更美的晴天~",
        f"🌤️ {user.first_name}早上好！今天阳光为你定制~",
        f"🌤️ {user.first_name}早上好！天气预报说今天有100%的好运~",
        # 食物主题 (20条)
        f"🍩 {user.first_name}早上好！甜甜圈都不如你甜~",
        f"🍫 {user.first_name}早安！巧克力般丝滑的一天开始啦~",
        f"🍒 {user.first_name}早上好！樱桃小嘴不如你的笑容甜~",
        # 励志主题 (20条)
        f"💪 {user.first_name}早安！今天的你比昨天更强大~",
        f"🚀 {user.first_name}早上好！准备发射你的梦想~",
        f"🌟 {user.first_name}早安！星星都为你让路~",
        f"🏆 {user.first_name}早上好！冠军从晨间开始~",
        f"🌈 {user.first_name}早安！风雨过后必见彩虹~",
        f"🧗 {user.first_name}早安！今天要攀登新的高峰~",
        f"🏃 {user.first_name}早上好！人生马拉松继续加油~",
        f"🧠 {user.first_name}早安！最强大脑今天也要全速运转~",
        f"🛡️ {user.first_name}早上好！带上勇气盾牌出发吧~",
        f"⚡ {user.first_name}早安！闪电般的效率从早晨开始~",
        f"🏅 {user.first_name}早上好！金牌属于早起的人~",
        f"🛎️ {user.first_name}早安！机会在敲门你听到了吗~",
        f"🔑 {user.first_name}早上好！成功之钥就在你手中~",
        f"📈 {user.first_name}早安！今天K线图会为你上涨~",
        f"🛫 {user.first_name}早上好！梦想航班即将起飞~",
        f"🧩 {user.first_name}早安！人生拼图又完成一块~",
        f"🛠️ {user.first_name}早上好！开始建造你的理想国~",
        f"🧭 {user.first_name}早安！指南针指向成功方向~",
        f"⚓ {user.first_name}早上好！抛下锚开始今天的航行~",
        # 幽默搞笑 (20条)
        f"🤪 {user.first_name}早安！床说它不想放开你~",
        f"🦸 {user.first_name}早安！拯救世界的任务从起床开始~",
    ]
    
    # 随机选择一条问候语
    reply = random.choice(greetings)
    
    # 10%概率附加特别彩蛋
    if random.random() < 0.1:
        reply += "\n\n🎁 彩蛋：你是今天第{}个说早安的天使~".format(random.randint(1,100))
    sent_message = await update.message.reply_text(reply)  # Store the sent message
    logger.info(f"🌅 向 {user.full_name} 发送了早安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=300))  # 改为5分钟

async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/unmute命令"""
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
        # 更新为新的ChatPermissions参数格式
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_polls=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_send_audios=True,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_other_messages=False,
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
        logger.error(f"解除禁言失败: {e}")
async def keyword_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词回复命令"""
    if not await is_admin(update, context):
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
    
    if not await is_admin(update, context):
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
            await query.message.edit_text(
                "📝 添加关键词回复\n\n"
                "第1步：请输入关键词\n"
                "输入 /cancel 取消操作"
            )
            
        elif action == "edit":
            # 获取所有关键词
            replies = await GoogleSheetsStorage.get_keyword_replies()
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
            replies = await GoogleSheetsStorage.get_keyword_replies()
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
            replies = await GoogleSheetsStorage.get_keyword_replies()
            
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
            replies = await GoogleSheetsStorage.get_keyword_replies()
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
            success = await GoogleSheetsStorage.delete_keyword_reply(keyword)
            
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
    if not update.message or not update.message.text:
        return
        
    if "reply_flow" not in context.user_data:
        return
        
    flow = context.user_data["reply_flow"]
    text = update.message.text
    
    if text.startswith("/"):
        return
        
    if flow["step"] == 1:
        # 第一步：获取关键词
        flow["keyword"] = text
        flow["step"] = 2
        await update.message.reply_text(
            f"📝 关键词: {text}\n\n"
            "第2步：请输入回复内容\n"
            "输入 /cancel 取消操作"
        )
        
    elif flow["step"] == 2:
        # 第二步：获取回复内容
        flow["reply_text"] = text
        flow["step"] = 3
        await update.message.reply_text(
            f"📝 关键词: {flow['keyword']}\n"
            f"💬 回复内容: {text}\n\n"
            "第3步：请输入链接和链接文本（可选）\n"
            "格式：链接 [链接文本]文本\n"
            "例如：https://example.com [链接文本]点击这里\n"
            "直接发送 /skip 跳过此步\n"
            "输入 /cancel 取消操作"
        )
        
    elif flow["step"] == 3:
        # 第三步：获取链接信息
        if text.lower() == "/skip":
            link = ""
            link_text = ""
        else:
            # 解析链接和链接文本
            if "[链接文本]" in text:
                parts = text.split("[链接文本]")
                link = parts[0].strip()
                link_text = parts[1].strip() if len(parts) > 1 else "点击这里"
            else:
                link = text.strip()
                link_text = "点击这里"
        
        # 保存回复
        if flow["action"] == "edit":
            # 修改时先删除旧的
            await GoogleSheetsStorage.delete_keyword_reply(flow["keyword"])
            
        success = await GoogleSheetsStorage.add_keyword_reply(
            keyword=flow["keyword"],
            reply_text=flow["reply_text"],
            link=link,
            link_text=link_text
        )
        
        if success:
            action_text = "修改" if flow["action"] == "edit" else "添加"
            await update.message.reply_text(
                f"✅ 已{action_text}关键词回复:\n\n"
                f"🔑 关键词: {flow['keyword']}\n"
                f"💬 回复: {flow['reply_text']}\n"
                f"🔗 链接: {link if link else '无'}\n"
                f"📝 链接文本: {link_text if link else '无'}"
            )
        else:
            await update.message.reply_text(f"❌ {action_text}关键词回复失败")
            
        # 清理流程数据
        del context.user_data["reply_flow"]

async def auto_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """自动回复关键词消息"""
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.lower()
    replies = await GoogleSheetsStorage.get_keyword_replies()
    
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
                
                await update.message.reply_text(
                    formatted_reply,
                    reply_markup=reply_markup
                )
            else:
                # 没有链接时也添加一些美化
                formatted_reply = (
                    f"✨ {reply_text}\n\n"
                    f"💫 需要帮助可以随时问我哦~"
                )
                await update.message.reply_text(formatted_reply)
            break
async def records_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理/records命令"""
    if not await is_admin(update, context):
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
    if not await is_admin(update, context):
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
    if not await is_admin(update, context):
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

class TwitterScraper:
    def __init__(self):
        self.max_retries = 3
        self.retry_delay = 5
        self.logger = logging.getLogger(__name__)
        self.session = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        }
        self.monitored_accounts = {
            'MyStonksCN': {'last_tweet_id': None},
            'MyStonks_Org': {'last_tweet_id': None}
        }
        self.monitoring_task = None
        self.group_chats = set()  # 存储机器人所在的群组ID

    async def get_latest_tweets(self, username, count=5):
        """获取指定用户的最新推文"""
        if not username:
            raise ValueError("用户名不能为空")

        username = username.lstrip('@')
        self.logger.info(f"Fetching tweets for @{username}")
        
        tweets = []
        retry_count = 0
        
        while retry_count < self.max_retries:
            try:
                session = await self._get_session()
                # 使用 nitter.net
                url = f"https://nitter.net/{username}"
                
                async with session.get(url) as response:
                    if response.status == 404:
                        raise ValueError(f"用户 @{username} 不存在")
                    elif response.status != 200:
                        raise Exception(f"获取推文失败: HTTP {response.status}")
                    
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    # 解析推文
                    tweet_elements = soup.select('.timeline-item')
                    for tweet in tweet_elements[:count]:
                        try:
                            # 获取推文内容
                            content = tweet.select_one('.tweet-content')
                            if not content:
                                continue
                                
                            # 获取推文ID
                            tweet_link = tweet.select_one('.tweet-link')
                            if not tweet_link:
                                continue
                            tweet_id = tweet_link['href'].split('/')[-1]
                            
                            # 获取发布时间
                            time_element = tweet.select_one('.tweet-date')
                            if not time_element:
                                continue
                            time_str = time_element['title']
                            created_at = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S %z")
                            
                            tweets.append({
                                'text': content.get_text(strip=True),
                                'created_at': created_at,
                                'url': f"https://twitter.com/{username}/status/{tweet_id}",
                                'author': username
                            })
                        except Exception as e:
                            self.logger.warning(f"解析推文时出错: {str(e)}")
                            continue
                
                if tweets:
                    return tweets
                else:
                    self.logger.warning(f"No tweets found for @{username}")
                    return []
                    
            except aiohttp.ClientError as e:
                retry_count += 1
                error_msg = str(e)
                self.logger.error(f"Error fetching tweets for @{username} (attempt {retry_count}/{self.max_retries}): {error_msg}")
                
                if retry_count < self.max_retries:
                    await asyncio.sleep(self.retry_delay * retry_count)
                else:
                    raise Exception(f"获取推文失败: {error_msg}")
            
            except Exception as e:
                raise e
        
        return []

    async def _get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(headers=self.headers)
        return self.session

    async def close(self):
        """关闭会话"""
        if self.session:
            await self.session.close()
            self.session = None
        if self.monitoring_task:
            await self.stop_monitoring()

    async def start_monitoring(self, bot_app):
        """开始监控指定账号的推文"""
        if self.monitoring_task is not None:
            return
            
        # 获取机器人所在的所有群组
        try:
            updates = await bot_app.bot.get_updates()
            for update in updates:
                if update.message and update.message.chat.type in ['group', 'supergroup']:
                    self.group_chats.add(update.message.chat.id)
            
            if not self.group_chats:
                self.logger.warning("机器人未加入任何群组，无法发送推文通知")
                return
                
            self.logger.info(f"机器人已加入 {len(self.group_chats)} 个群组")
        except Exception as e:
            self.logger.error(f"获取群组信息失败: {e}")
            return
            
        self.monitoring_task = asyncio.create_task(self._monitor_tweets(bot_app))
        
    async def stop_monitoring(self):
        """停止监控"""
        if self.monitoring_task is not None:
            self.monitoring_task.cancel()
            self.monitoring_task = None
            
    async def _monitor_tweets(self, bot_app):
        """监控推文的主循环"""
        while True:
            try:
                for username in self.monitored_accounts:
                    try:
                        tweets = await self.get_latest_tweets(username, count=1)
                        if tweets:
                            latest_tweet = tweets[0]
                            last_tweet_id = self.monitored_accounts[username]['last_tweet_id']
                            
                            if last_tweet_id is None or latest_tweet['url'].split('/')[-1] != last_tweet_id:
                                # 新推文，发送通知到所有群组
                                message = (
                                    f"🐦 新推文通知\n\n"
                                    f"👤 @{username}\n"
                                    f"📝 {latest_tweet['text']}\n"
                                    f"🕒 {latest_tweet['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
                                    f"🔗 {latest_tweet['url']}"
                                )
                                
                                # 向所有群组发送通知
                                for chat_id in self.group_chats:
                                    try:
                                        await bot_app.bot.send_message(
                                            chat_id=chat_id,
                                            text=message,
                                            disable_web_page_preview=True
                                        )
                                    except Exception as e:
                                        self.logger.error(f"向群组 {chat_id} 发送推文通知失败: {e}")
                                        # 如果发送失败，可能是机器人被移出群组，从列表中移除
                                        self.group_chats.discard(chat_id)
                                
                                # 更新最后一条推文ID
                                self.monitored_accounts[username]['last_tweet_id'] = latest_tweet['url'].split('/')[-1]
                    except Exception as e:
                        self.logger.error(f"获取 @{username} 的推文失败: {e}")
                
                # 每5分钟检查一次
                await asyncio.sleep(300)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"监控推文时出错: {e}")
                await asyncio.sleep(60)  # 出错后等待1分钟再重试

# 替换原来的 NitterMonitor 实例
nitter_monitor = TwitterScraper()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有消息"""
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.lower()
    
    # 早安关键词（不区分大小写）
    morning_keywords = ["gm", "早", "早安", "早上好", "morning", "good morning", "GM", "Morning", "Good Morning", "GOOD MORNING"]
    # 午安关键词（不区分大小写）
    noon_keywords = ["午安", "中午好", "午好", "noon", "good noon", "Noon", "Good Noon", "GOOD NOON"]
    # 晚安关键词（不区分大小写）
    night_keywords = ["gn", "晚安", "晚上好", "night", "good night", "GN", "Night", "Good Night", "GOOD NIGHT"]
    
    if any(keyword.lower() in text for keyword in morning_keywords):
        await morning_greeting_handler(update, context)
    elif any(keyword.lower() in text for keyword in noon_keywords):
        await noon_greeting_handler(update, context)
    elif any(keyword.lower() in text for keyword in night_keywords):
        await goodnight_greeting_handler(update, context)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global bot_app, bot_initialized, ban_records, nitter_monitor
    
    try:
        # 初始化 Telegram Bot
        bot_app = (
            ApplicationBuilder()
            .token(TOKEN)
            .build()
        )
        
        # 添加命令处理器
        bot_app.add_handler(CommandHandler("start", start_handler))
        bot_app.add_handler(CommandHandler("k", kick_handler))
        bot_app.add_handler(CommandHandler("m", mute_handler))
        bot_app.add_handler(CommandHandler("um", unmute_handler))
        bot_app.add_handler(CommandHandler("records", records_handler))
        bot_app.add_handler(CommandHandler("search", search_handler))
        bot_app.add_handler(CommandHandler("export", export_handler))
        bot_app.add_handler(CommandHandler("nitter", nitter_handler))
        bot_app.add_handler(CommandHandler("twitter", twitter_handler))
        bot_app.add_handler(CommandHandler("keyword", keyword_reply_handler))
        bot_app.add_handler(CommandHandler("morning", morning_greeting_handler))
        bot_app.add_handler(CommandHandler("noon", noon_greeting_handler))
        bot_app.add_handler(CommandHandler("night", goodnight_greeting_handler))
        bot_app.add_handler(CommandHandler("comfort", comfort_handler))
        
        # 添加回调处理器
        bot_app.add_handler(CallbackQueryHandler(ban_reason_handler, pattern="^ban_reason"))
        bot_app.add_handler(CallbackQueryHandler(ban_reason_handler, pattern="^mute_reason"))
        bot_app.add_handler(CallbackQueryHandler(reply_callback_handler, pattern="^reply:"))
        
        # 添加消息处理器
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reply_handler))
        bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_flow))
        
        # 从 Google Sheet 加载数据
        ban_records = await GoogleSheetsStorage.load_from_sheet()
        logger.info(f"Loaded {len(ban_records)} records from Google Sheet")
        
        # 启动 bot
        await bot_app.initialize()
        await bot_app.start()
        bot_initialized = True
        
        # 初始化并启动 Twitter 监控
        nitter_monitor = TwitterScraper()
        await nitter_monitor.start_monitoring(bot_app)
        
        yield
        
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise
        
    finally:
        # 清理资源
        if bot_app:
            await bot_app.stop()
            await bot_app.shutdown()
        if nitter_monitor:
            await nitter_monitor.close()

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
