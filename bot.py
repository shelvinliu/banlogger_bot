import os
import re
import json
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

# 全局变量
bot_app: Optional[Application] = None
bot_initialized: bool = False
ban_records: List[Dict[str, Any]] = []

class GoogleSheetsStorage:
    @staticmethod
    async def load_from_sheet() -> List[Dict[str, Any]]:
        """从Google Sheet加载数据"""
        if not GOOGLE_SHEETS_CREDENTIALS:
            logger.warning("未配置GOOGLE_SHEETS_CREDENTIALS，无法从Google Sheet加载数据")
            return []
            
        try:
            worksheet = await GoogleSheetsStorage._get_worksheet()
            records = worksheet.get_all_records()
            
            expected_columns = ["time", "group_name", "banned_user_id", 
                              "banned_user_name", "banned_username", 
                              "admin_name", "reason"]
            
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
    async def _get_worksheet():
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
                return gc.open(GOOGLE_SHEET_NAME).sheet1
            except gspread.SpreadsheetNotFound:
                sh = gc.create(GOOGLE_SHEET_NAME)
                sh.share(creds_dict["client_email"], perm_type="user", role="writer")
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
            expected_columns = ["time", "group_name", "banned_user_id", 
                              "banned_user_name", "banned_username", 
                              "admin_name", "reason"]
            
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
class BanManager:
    """封禁管理工具类"""
    
    @staticmethod
    def get_ban_reasons_keyboard(banned_user_id: int, banned_user_name: str) -> InlineKeyboardMarkup:
        """生成封禁原因选择键盘"""
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
        banned_username: Optional[str] = None
    ) -> bool:
        """保存封禁记录到内存并导出到Google Sheet"""
        global ban_records
        
        try:
            record = {
                "time": datetime.now(TIMEZONE).isoformat(),
                "group_name": chat_title,
                "banned_user_id": banned_user_id,
                "banned_user_name": banned_user_name,
                "banned_username": f"@{banned_username}" if banned_username else "无",
                "admin_name": admin_name,
                "reason": reason
            }
            
            ban_records.append(record)
            
            # 同步到Google Sheet
            success = await GoogleSheetsStorage.save_to_sheet(ban_records)
            if not success:
                logger.warning("Google Sheet同步失败，数据仅保存在内存中")
            
            logger.info(f"记录已保存: {banned_user_name} | {reason}")
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
        error_msg = await update.message.reply_text(f"❌ 踢出失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"踢出用户失败: {e}")

async def ban_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            "banned_username": last_ban.get("target_username"),
            "chat_title": query.message.chat.title,
            "admin_name": query.from_user.full_name
        }
        msg = await query.message.reply_text("请输入自定义封禁原因:")
        asyncio.create_task(delete_message_later(msg))
        return
    
    # 保存封禁记录
    try:
        success = await BanManager.save_to_db(
            chat_title=query.message.chat.title,
            banned_user_id=banned_user_id,
            banned_user_name=user_name,
            banned_username=last_ban.get("target_username"),
            admin_name=query.from_user.full_name,
            reason=reason
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
        logger.error(f"保存封禁原因失败: {e}")

async def custom_reason_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        success = await BanManager.save_to_db(
            chat_title=pending_data["chat_title"],
            banned_user_id=pending_data["banned_user_id"],
            banned_user_name=pending_data["banned_user_name"],
            banned_username=pending_data["banned_username"],
            admin_name=pending_data["admin_name"],
            reason=reason
        )
        
        if success:
            confirm_msg = await update.message.reply_text(f"✅ 已记录自定义原因: {reason}")
            asyncio.create_task(delete_message_later(confirm_msg))
        else:
            error_msg = await update.message.reply_text("❌ 保存记录失败")
            asyncio.create_task(delete_message_later(error_msg))
        
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 保存失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"保存自定义原因失败: {e}")
    
    context.user_data.pop("pending_reason", None)
    await update.message.delete()

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
            can_send_media_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False
    ),
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
        logger.warning(f"禁言时间格式错误: {e}")
    except Exception as e:
        error_msg = await update.message.reply_text(f"⚠️ 系统检测到珍贵同事光环 ⚠️本次禁言操作已被【职场生存法则】拦截")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"禁言用户失败: {e}")
        
async def morning_greeting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    greetings = [
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
    f"👗 {user.first_name}早上好！这件衣服很适合你~",
    f"💇 {user.first_name}早安！新发型让你更有精神了~",
    f"👀 {user.first_name}早上好！你的眼睛特别有神采~",
    f"😊 {user.first_name}早安！你的笑容总是很有感染力~",
    f"👔 {user.first_name}早上好！今天的穿搭很得体~",
    f"💅 {user.first_name}早安！指甲颜色选得真好看~",
    f"🏃 {user.first_name}早上好！坚持运动的效果很明显呢~",
    f"🌺 {user.first_name}早安！你选的香水味道很清新~",
    f"👟 {user.first_name}早上好！这双鞋和裤子很配哦~",
    f"🧢 {user.first_name}早安！帽子戴在你头上特别有型~",
    f"👓 {user.first_name}早上好！眼镜框很适合你的脸型~",
    f"💄 {user.first_name}早安！今天的唇色很提气色~",
    f"🧥 {user.first_name}早上好！这件外套的质感很棒~",
    f"💍 {user.first_name}早安！饰品搭配得很精致~",
    f"🧴 {user.first_name}早上好！你总是把自己收拾得很清爽~",
    f"👜 {user.first_name}早安！包包和整体造型很协调~",
    f"👞 {user.first_name}早上好！皮鞋擦得真亮~",
    f"🧣 {user.first_name}早安！围巾的系法很有创意~",
    f"👚 {user.first_name}早上好！衣服颜色衬得你肤色很亮~",
    f"💇‍♂️ {user.first_name}早安！胡子修剪得很整齐~",
    f"👒 {user.first_name}早上好！草帽很有夏日气息~",
    f"🧦 {user.first_name}早安！袜子的花纹很有趣~",
    f"👖 {user.first_name}早上好！牛仔裤的版型很修身~",
    f"🕶️ {user.first_name}早安！墨镜戴起来很有范儿~",
    f"👗 {user.first_name}早上好！裙摆的剪裁很别致~",
    f"🧤 {user.first_name}早安！手套的颜色很温暖~",
    f"👔 {user.first_name}早上好！领带打得真标准~",
    f"👠 {user.first_name}早安！高跟鞋走得很稳呢~",
    f"🧦 {user.first_name}早上好！袜子和鞋子搭配得很有心思~",

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
    
    await update.message.reply_text(reply)
    logger.info(f"🌅 向 {user.full_name} 发送了早安问候")
    
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
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
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
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND) & filters.Regex(r'(?i)^(gm|早|早上好|早安|good morning)$'), morning_greeting_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), custom_reason_handler))
    
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
