import os
import re
import json
import tweepy
import pytz
import random
import asyncio
import logging
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import pandas as pd
import gspread
from typing import List, Dict, Optional
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

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
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
TWITTER_API_KEY=os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET_KEY=os.getenv("TWITTER_API_SECRET_KEY")
TWITTER_ACCESS_TOKEN=os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET=os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
# 全局变量
bot_app: Optional[Application] = None
bot_initialized: bool = False
ban_records: List[Dict[str, Any]] = []

class TwitterMonitor:
    def __init__(self):
        self.api_key = os.getenv("TWITTER_API_KEY")
        self.api_secret = os.getenv("TWITTER_API_SECRET_KEY")
        self.access_token = os.getenv("TWITTER_ACCESS_TOKEN")
        self.access_token_secret = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        
        # 初始化 Twitter 客户端
        self.client = tweepy.Client(
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            access_token=self.access_token,
            access_token_secret=self.access_token_secret
        )
    def get_latest_tweets(self, username: str, count: int = 5) -> List[Dict]:
        """获取某个用户的最新推文"""
        try:
            user = self.client.get_user(username=username)
            tweets = self.client.get_users_tweets(
                user.data.id,
                max_results=count,
                tweet_fields=["created_at", "public_metrics"]
            )
            return [
                {
                    "text": tweet.text,
                    "created_at": tweet.created_at,
                    "likes": tweet.public_metrics["like_count"],
                    "retweets": tweet.public_metrics["retweet_count"],
                    "url": f"https://twitter.com/{username}/status/{tweet.id}"
                }
                for tweet in tweets.data
            ]
        except Exception as e:
            logger.error(f"获取 Twitter 推文失败: {e}")
            return []
    def monitor_keyword(self, keyword: str, count: int = 5) -> List[Dict]:
        """监控某个关键词的最新推文"""
        try:
            tweets = self.client.search_recent_tweets(
                query=keyword,
                max_results=count,
                tweet_fields=["created_at", "public_metrics", "author_id"]
            )
            return [
                {
                    "text": tweet.text,
                    "author": tweet.author_id,  # 可以进一步获取用户名
                    "created_at": tweet.created_at,
                    "likes": tweet.public_metrics["like_count"],
                    "retweets": tweet.public_metrics["retweet_count"],
                    "url": f"https://twitter.com/user/status/{tweet.id}"
                }
                for tweet in tweets.data
            ]
        except Exception as e:
            logger.error(f"监控 Twitter 关键词失败: {e}")
            return []

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
        return sum(1 for record in ban_records if record.get("banned_user_id") == user_id)

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
    f"☀️ {{}}午安！阳光正好，记得休息一会儿哦~",
    f"🍱 {{}}午安！该享用美味的午餐啦~",
    f"😴 {{}}午安！需要来个午睡充电吗？",
    f"🌤️ {{}}午安！一天已经过半啦，继续加油~",
    
    # 幽默系列
    f"⏰ {{}}午安！你的胃在抗议啦，快去喂它~",
    f"💤 {{}}午安！困了可以学猫咪打个盹~",
    f"🍵 {{}}午安！来杯茶提提神吧~",
    f"🍜 {{}}午安！泡面还是外卖？这是个问题~",
    
    # 励志系列
    f"🚀 {{}}午安！下午也要元气满满~",
    f"💪 {{}}午安！上午表现很棒，下午再接再厉~",
    f"🎯 {{}}午安！上午的目标完成了吗？",
    
    # 特别彩蛋
    f"🍱 {{}}午安！今日午餐推荐：{random.choice(['拉面','寿司','饺子','盖饭','沙拉'])}~",
    f"☕ {{}}午安！咖啡因含量：{random.randint(10,100)}%",
    ]
    greetings = [g.format(user.first_name) for g in NOON_GREETINGS]
    
    reply = random.choice(greetings)
    
    # 10%概率附加彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "🎉", "💫", "🎊"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个说午安的小可爱~"
    
    sent_message = await update.message.reply_text(reply)
    logger.info(f"🌞 向 {user.full_name} 发送了午安问候")
    
    # 1分钟后自动删除
    asyncio.create_task(delete_message_later(sent_message, delay=60))

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
    f"🎪 {user.first_name}早安！马戏团最抢手的明星演员来咯~",
    f"🎆 {user.first_name}早安！烟花秀主火炬手已就位~",
    f"🧿 {user.first_name}早上好！锦鲤本鲤开始散发好运~",
    f"🎨 {user.first_name}早安！梵高看了都点赞的艺术品醒啦~",
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
    f"🌳 {user.first_name}早安！扎根的日子终会开花~",
    f"🦋 {user.first_name}早上好！蜕变需要耐心等待~",
    f"🧲 {user.first_name}早安！正能量吸引更多美好~",
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
    f"🛶 {user.first_name}早上好！掌舵自己的人生~",
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
    f"📱 {user.first_name}早安！科技让爱零距离~",
    f"🍞 {user.first_name}早上好！面包背后有无数双手~",
    f"👣 {user.first_name}早安！感谢双脚带你看世界~",
    f"👀 {user.first_name}早上好！眼睛让你看见美好~",
    f"🌧️ {user.first_name}早安！雨水滋润万物生长~",
    f"🍎 {user.first_name}早上好！苹果里有整个宇宙~",
    f"🚌 {user.first_name}早安！感恩平安的出行~",
    f"📚 {user.first_name}早上好！知识是前人馈赠~",
    f"🛒 {user.first_name}早安！丰盛物资值得珍惜~",
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
    f"🦋 {user.first_name}早上好！破茧时刻即将到来~"
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
    asyncio.create_task(delete_message_later(sent_message, delay=60))
COMFORT_MESSAGES = [
    "🌧️ 市场下雨了，但别忘了雨后总有彩虹~",
    "📉 短期波动而已，咱们长期主义者笑看风云",
    "💎 钻石手们，握紧你们的筹码！",
    "🐋 大户跑了正好，咱们捡便宜筹码的机会来了",
    "🛌 跌了就睡会，醒来又是新行情",
    "🍃 风会停，雨会住，市场总会回暖",
    "🧘 深呼吸，价格波动只是市场的呼吸节奏",
    "🦉 聪明人都在悄悄加仓呢",
    "📚 历史告诉我们，每次大跌都是财富再分配的机会",
    "🌊 潮起潮落很正常，咱们冲浪手不怕浪",
    "🛡️ 真正的战士经得起市场考验",
    "🍵 淡定喝茶，这点波动不算啥",
    "🎢 坐过山车就要享受刺激过程",
    "🕰️ 时间会奖励耐心的人",
    "🧩 市场拼图少了一块？很快会补上的",
    "🌱 跌下去的都在扎根，为了跳得更高",
    "🎯 目标不变，策略微调，继续前进",
    "🚣 划船不用桨，全靠浪~现在浪来了",
    "🛒 打折促销啦！聪明买家该出手了",
    "📉📈 没有只跌不涨的市场",
    "💪 考验信仰的时候到了",
    "🔄 周期循环，下一站是上涨",
    "🧲 价值终会吸引价格回归",
    "🏗️ 下跌是更好的建仓机会",
    "🎮 游戏难度调高了，但通关奖励更丰厚",
    "🤲 空头抛售，我们接盘，谁更聪明？",
    "🌌 黑夜再长，黎明终会到来",
    "🛎️ 市场闹钟响了，该关注机会了",
    "🧠 情绪化的人恐慌，理性的人布局",
    "🪂 降落是为了更好的起飞",
    "🎲 短期是投票机，长期是称重机",
    "🦚 孔雀开屏前要先收拢羽毛",
    "⚖️ 市场终会回归价值平衡",
    "🏔️ 攀登前总要下到山谷",
    "🔮 水晶球显示：未来会涨回来",
    "🧵 行情像弹簧，压得越狠弹得越高",
    "🎻 市场交响乐也有慢板乐章",
    "🛸 外星人砸盘？正好接点外星筹码",
    "🏆 冠军都是在逆境中练就的",
    "🌪️ 风暴中心最平静，保持冷静",
    "🕵️‍♂️ 价值投资者正在悄悄扫货",
    "🎢 过山车下坡才刺激，上坡在后面",
    "🧗 回调是为了更好的突破前高",
    "🛌 装死策略启动，躺平等反弹",
    "🎯 目标价没变，只是路线曲折了点",
    "🧘‍♂️ 禅定时刻：市场噪音过滤中",
    "🦸 英雄都是在危机中诞生的",
    "🌄 最美的日出前是最暗的夜",
    "🎻 这是市场的休止符，不是终止符",
    "🛡️ 你的止损线设好了吗？没设就不用慌",
    "🧂 这点波动，洒洒水啦~"
]

# 在命令处理部分添加
async def comfort_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/comfort安慰指令"""
    try:
        # 随机选择3条不同的安慰语
        selected = random.sample(COMFORT_MESSAGES, min(3, len(COMFORT_MESSAGES)))
        reply = "💖 市场下跌安慰包 💖\n\n" + "\n\n".join(selected)
        reply += "\n\n✨ 记住：市场周期往复，保持良好心态最重要"
        
        await update.message.reply_text(reply)
        logger.info(f"发送安慰消息给 {update.effective_user.full_name}")
        asyncio.create_task(delete_message_later(sent_message, delay=60))

    except Exception as e:
        logger.error(f"发送安慰消息失败: {e}")
        await update.message.reply_text("😔 安慰服务暂时不可用，先抱抱~")
async def twitter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """获取 Twitter 最新动态"""
    if not context.args:
        await update.message.reply_text("用法: /twitter <用户名> 或 /twitter search <关键词>")
        return
    
    if context.args[0] == "search":
        keyword = " ".join(context.args[1:])
        tweets = twitter_monitor.monitor_keyword(keyword)
        if not tweets:
            await update.message.reply_text("未找到相关推文")
            return
        response = "🔍 最新相关推文:\n\n" + "\n\n".join(
            f"{tweet['text']}\n👍 {tweet['likes']} | 🔁 {tweet['retweets']}\n🔗 {tweet['url']}"
            for tweet in tweets
        )
    else:
        username = context.args[0]
        tweets = twitter_monitor.get_latest_tweets(username)
        if not tweets:
            await update.message.reply_text(f"未找到 @{username} 的推文")
            return
        response = f"🐦 @{username} 的最新推文:\n\n" + "\n\n".join(
            f"{tweet['text']}\n🕒 {tweet['created_at']}\n👍 {tweet['likes']} | 🔁 {tweet['retweets']}\n🔗 {tweet['url']}"
            for tweet in tweets
        )
    
    await update.message.reply_text(response[:4000])  # Telegram 消息限制 4096 字符
async def goodnight_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    greetings = [
        # 温馨祝福系列
        f"🌙 {user.first_name}晚安，愿你今夜好梦~",
        f"✨ {user.first_name}晚安，星星会守护你的梦境",
        f"🛌 {user.first_name}晚安，被子已经帮你暖好啦",
        f"🌜 {user.first_name}晚安，月亮说它会陪你到天亮",
        f"💤 {user.first_name}晚安，充电时间到！明天满血复活~",
        f"🦉 {user.first_name}晚安，猫头鹰会替你站岗的",
        f"🌠 {user.first_name}晚安，流星会实现你梦中的愿望",
        f"🧸 {user.first_name}晚安，抱紧你的小熊做个甜梦吧",
        f"🍃 {user.first_name}晚安，晚风会为你唱摇篮曲",
        f"🌌 {user.first_name}晚安，银河已为你铺好梦境之路",
        # 可爱幽默系列
        f"🐑 {user.first_name}晚安，快去数羊吧！1只羊...2只羊...zzz",
        f"🦇 {user.first_name}晚安，蝙蝠侠说你该睡觉了",
        f"🍵 {user.first_name}晚安，睡前记得喝杯热牛奶哦",
        f"📚 {user.first_name}晚安，明天再看更多精彩故事~",
        f"🎮 {user.first_name}晚安，游戏角色也需要休息啦",
        f"🐱 {user.first_name}晚安，猫咪已经在你床上占好位置了",
        f"🌛 {user.first_name}晚安，月亮姐姐给你盖被子啦",
        f"🛏️ {user.first_name}晚安，床说它想你了",
        f"🧦 {user.first_name}晚安，记得把袜子挂在床边（说不定有惊喜）",
        f"🦄 {user.first_name}晚安，独角兽会带你去梦幻仙境",
        
        # 诗意浪漫系列
        f"🌹 {user.first_name}晚安，让玫瑰的芬芳伴你入眠",
        f"🎶 {user.first_name}晚安，让夜曲轻抚你的梦境",
        f"🖼️ {user.first_name}晚安，今晚的梦会是幅什么画呢？",
        f"📝 {user.first_name}晚安，把今天的烦恼折成纸飞机放飞吧",
        f"🍂 {user.first_name}晚安，落叶会为你铺就柔软的梦乡",
        f"🕯️ {user.first_name}晚安，烛光会守护你到黎明",
        f"🎻 {user.first_name}晚安，让月光小夜曲伴你入睡",
        f"🌉 {user.first_name}晚安，梦境之桥已为你架好",
        f"📖 {user.first_name}晚安，今天的故事就翻到这一页",
        f"🪔 {user.first_name}晚安，愿你的梦境如灯火般温暖",
        
        # 特别彩蛋系列
        f"🎁 {user.first_name}晚安！你是今天第{random.randint(1,100)}个说晚安的天使~",
        f"🔮 {user.first_name}晚安！水晶球显示你明天会有好运！",
        f"🧙 {user.first_name}晚安！魔法师已经为你的梦境施了快乐咒语",
        f"🏰 {user.first_name}晚安！城堡里的公主/王子该就寝啦",
        f"🚀 {user.first_name}晚安！梦境飞船即将发射~",
        f"🌙 {user.first_name}晚安，愿星光轻轻吻你的梦境~",
        f"🛏️ {user.first_name}今晚睡个好觉，是对明天最好的投资~",
        f"🌠 {user.first_name}晚安，流星已替你藏好所有烦恼~",
        f"🛌 {user.first_name}钻进被窝吧，今天辛苦了~",
        f"🌜 {user.first_name}月亮开始值班了，放心入睡吧~",
        f"💤 {user.first_name}晚安，枕头已充满好梦能量~",
        f"🪔 {user.first_name}夜灯温柔，祝你一夜安眠~",
        f"🌃 {user.first_name}城市入睡时，你的梦要开始冒险啦~",
        f"🛋️ {user.first_name}卸下疲惫，沙发为你记着今天的努力~",
        f"📖 {user.first_name}晚安，今日故事存档完毕~",
        f"🌉 {user.first_name}晚安，桥梁都亮起温柔的引路灯~",
        f"🌙 {user.first_name}被子魔法启动，三秒入睡倒计时~",
        f"🛁 {user.first_name}洗去尘埃，换上星星织的睡衣吧~",
        f"🌛 {user.first_name}晚安，月亮会守护你的窗台~",
        f"🪟 {user.first_name}窗帘拉好，梦境快递正在派送~",
        f"🌌 {user.first_name}银河铺好绒毯，等你来遨游~",
        f"🛏️ {user.first_name}床已暖好，请查收今日份安心~",
        f"🌠 {user.first_name}晚安，所有星星都在对你眨眼睛~",
        f"🛋️ {user.first_name}辛苦一天的身体该充电啦~",
        f"🌉 {user.first_name}晚安，江面倒映着为你准备的星光~",
        f"🌙 {user.first_name}闭上眼睛，宇宙开始播放专属梦境~",
        f"🛌 {user.first_name}晚安，羽绒云朵已装满你的被窝~",
        f"🌜 {user.first_name}月亮船来接你去童话世界啦~",
        f"💤 {user.first_name}睡眠金币已存入，明天利息是活力~",
        f"🪔 {user.first_name}床头小灯，像不像守夜的萤火虫？",
        f"🌃 {user.first_name}晚安，霓虹都调成助眠模式了~",
        f"🛋️ {user.first_name}今日剧情播放完毕，请休息~",
        f"🌉 {user.first_name}晚安，跨江大桥变成摇篮曲五线谱~",
        f"🌙 {user.first_name}睫毛落下时，会有天使来盖章~",
        f"🛏️ {user.first_name}床是成年人的游乐场，去玩吧~",
        f"🌠 {user.first_name}晚安，所有噩梦已转交给奥特曼~",
    ]
    
    # 随机选择一条问候语
    reply = random.choice(greetings)
    
    # 10%概率附加特别彩蛋
    if random.random() < 0.1:
        emojis = ["✨", "🌟", "🎉", "💫", "🎊"]
        reply += f"\n\n{random.choice(emojis)} 彩蛋：你是今天第{random.randint(1,100)}个获得晚安祝福的幸运儿~"
    
    sent_message=await update.message.reply_text(reply)
    logger.info(f"🌃 向 {user.full_name} 发送了晚安问候")
    asyncio.create_task(delete_message_later(sent_message, delay=60))

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

    if not context.args or len(context.args) < 2:
        help_text = (
            "📝 关键词回复管理命令:\n\n"
            "/reply add <关键词> <回复内容> [链接] [链接文本] - 添加关键词回复\n"
            "/reply del <关键词> - 删除关键词回复\n"
            "/reply list - 查看所有关键词回复\n\n"
            "示例:\n"
            "/reply add 帮助 这是帮助信息 https://example.com 点击这里"
        )
        await update.message.reply_text(help_text)
        return

    action = context.args[0].lower()
    
    if action == "add":
        if len(context.args) < 3:
            await update.message.reply_text("❌ 格式错误，需要至少提供关键词和回复内容")
            return
            
        keyword = context.args[1]
        reply_text = " ".join(context.args[2:])
        
        # 解析链接和链接文本
        link = ""
        link_text = ""
        if "[链接]" in reply_text and "[链接文本]" in reply_text:
            parts = reply_text.split("[链接]")
            reply_text = parts[0].strip()
            link_parts = parts[1].split("[链接文本]")
            link = link_parts[0].strip()
            link_text = link_parts[1].strip() if len(link_parts) > 1 else "点击这里"
        
        success = await GoogleSheetsStorage.add_keyword_reply(
            keyword=keyword,
            reply_text=reply_text,
            link=link,
            link_text=link_text
        )
        
        if success:
            await update.message.reply_text(f"✅ 已添加关键词回复: {keyword}")
        else:
            await update.message.reply_text("❌ 添加关键词回复失败")
            
    elif action == "del":
        if len(context.args) < 2:
            await update.message.reply_text("❌ 请提供要删除的关键词")
            return
            
        keyword = context.args[1]
        success = await GoogleSheetsStorage.delete_keyword_reply(keyword)
        
        if success:
            await update.message.reply_text(f"✅ 已删除关键词回复: {keyword}")
        else:
            await update.message.reply_text(f"❌ 未找到关键词: {keyword}")
            
    elif action == "list":
        replies = await GoogleSheetsStorage.get_keyword_replies()
        
        if not replies:
            await update.message.reply_text("暂无关键词回复配置")
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
            
        await update.message.reply_text(message)
        
    else:
        await update.message.reply_text("❌ 未知操作，请使用 add/del/list")
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
                await update.message.reply_text(
                    reply_text,
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(reply_text)
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
        recent_records = sorted(ban_records, key=lambda x: x.get("time", ""), reverse=True)[:MAX_RECORDS_DISPLAY]
        
        message = "📊 最近封禁记录:\n\n"
        for record in recent_records:
            record_time = datetime.fromisoformat(record["time"]).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
            message += (
                f"🕒 {record.get('操作时间', '未知')}\n"
                f"👤 用户: {record.get('名称', '未知')} "
                f"(ID: {record.get('用户ID', '未知')}) "
                f"[{record.get('用户名', '无')}]\n"
                f"👮 管理员: {record.get('操作管理', '未知')}\n"
                f"📝 原因: {record.get('理由', '未填写')}\n"
                f"💬 群组: {record.get('电报群组名称', '未知')}\n"
                f"🔧 操作: {record.get('操作', '未知')}\n"  # 新增操作类型显示
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
            if keyword.lower() in record.get("reason", "").lower()
        ]

        if not matched_records:
            msg = await update.message.reply_text("未找到匹配的封禁记录")
            asyncio.create_task(delete_message_later(msg, delay=10))
            return

        message = f"🔍 搜索结果 (关键词: {keyword}):\n\n"
        for record in matched_records[:MAX_RECORDS_DISPLAY]:
            record_time = datetime.fromisoformat(record["time"]).astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M")
            message += (
                f"🕒 {record_time}\n"
                f"👤 用户: {record.get('banned_user_name', '未知')} "
                f"(ID: {record.get('banned_user_id', '未知')}) "
                f"[{record.get('banned_username', '无')}]\n"
                f"👮 管理员: {record.get('admin_name', '未知')}\n"
                f"📝 原因: {record.get('reason', '未填写')}\n"
                f"💬 群组: {record.get('group_name', '未知')}\n"
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
        if "banned_username" not in df.columns:
            df["banned_username"] = "无"
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app, bot_initialized, ban_records
    
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable未设置")
    
    # Try Google Sheets connection only if credentials exist
    if GOOGLE_SHEETS_CREDENTIALS:
        try:
            logger.info("正在验证Google Sheets连接...")
            ban_records = await GoogleSheetsStorage.load_from_sheet()
            logger.info(f"从Google Sheet加载了 {len(ban_records)} 条历史记录")
        except Exception as e:
            logger.error(f"Google Sheets连接失败: {e}")
            logger.warning("将仅使用内存存储")
            ban_records = []
    else:
        logger.warning("未配置GOOGLE_SHEETS_CREDENTIALS，将仅使用内存存储")
        ban_records = []

    # Initialize bot
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_handler))
    bot_app.add_handler(CommandHandler("k", kick_handler))
    bot_app.add_handler(CommandHandler("m", mute_handler))
    bot_app.add_handler(CommandHandler("um", unmute_handler))
    bot_app.add_handler(CommandHandler("records", records_handler))
    bot_app.add_handler(CommandHandler("search", search_handler))
    bot_app.add_handler(CommandHandler("export", export_handler))
    bot_app.add_handler(CallbackQueryHandler(ban_reason_handler))
    bot_app.add_handler(CommandHandler("twitter", twitter_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r'(?i)^(gm|早|早上好|早安|good morning)$'), morning_greeting_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r'(?i)^(gn|晚安|晚上好|good night|night|nighty night|晚安安|睡觉啦|睡啦|去睡了)$'), goodnight_greeting_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r'(?i)^(午安|中午好|good afternoon|noon)$'),noon_greeting_handler))
    bot_app.add_handler(CommandHandler("comfort", comfort_handler))
    bot_app.add_handler(CommandHandler("reply", keyword_reply_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), auto_reply_handler))
    await bot_app.initialize()
    await bot_app.start()
    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(url=WEBHOOK_URL)

    bot_initialized = True
    yield
    
    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()
router = APIRouter()
twitter_monitor = TwitterMonitor()
@router.get("/health")
async def health_check():
    return {
        "status": "running",
        "bot_initialized": bot_initialized,
        "ban_records_count": len(ban_records),
        "google_sheets_connected": bool(GOOGLE_SHEETS_CREDENTIALS)
    }
app = FastAPI(lifespan=lifespan)

# Include your router if you have one
app.include_router(router)
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Telegram Webhook入口"""
    if not bot_app or not bot_initialized:
        raise HTTPException(status_code=503, detail="Bot未初始化")
    
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"处理更新失败: {e}")
        raise HTTPException(status_code=400, detail="处理更新失败")
# This is important for Render to detect your ASGI app
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
