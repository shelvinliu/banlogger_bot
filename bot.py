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

    # 天气主题 (15条)
    f"☀️ {user.first_name}早安！阳光正好的日子最适合奋斗啦~",
    f"🌧️ {user.first_name}早安！雨天也要保持好心情哦~",
    f"❄️ {user.first_name}早上好！寒冷的日子里请多保暖~",
    f"🌪️ {user.first_name}早安！就算有风暴也阻挡不了你的光芒~",
    f"🌤️ {user.first_name}早上好！今天天气和你一样晴朗~",
    f"🌫️ {user.first_name}早安！迷雾终将散去，美好终会到来~",
    f"🌩️ {user.first_name}早上好！雷雨过后必有彩虹~",
    f"🌬️ {user.first_name}早安！让晨风带走所有烦恼~",
    f"🌡️ {user.first_name}早上好！注意天气变化别感冒哦~",
    f"🌀 {user.first_name}早安！就算台风来了也刮不走你的好心情~",
    f"🌤️ {user.first_name}早上好！多云天气也遮不住你的光芒~",
    f"🌦️ {user.first_name}早安！短暂的阵雨是为了更美的晴天~",
    f"🌤️ {user.first_name}早上好！今天阳光为你定制~",
    f"🌨️ {user.first_name}早安！雪天也要保持温暖的心~",
    f"🌤️ {user.first_name}早上好！天气预报说今天有100%的好运~",

    # 食物主题 (20条)
    f"🍳 {user.first_name}早安！煎蛋和美好的一天更配哦~",
    f"🥐 {user.first_name}早上好！可颂面包和阳光都为你准备好了~",
    f"🍌 {user.first_name}早安！香蕉说它想给你一个拥抱~",
    f"🍎 {user.first_name}早上好！一天一苹果，快乐远离我...才怪！",
    f"🥞 {user.first_name}早安！松饼和微笑都是今天的必需品~",
    f"🍯 {user.first_name}早上好！蜂蜜般甜蜜的一天开始啦~",
    f"🥪 {user.first_name}早安！三明治里夹着我对你的祝福~",
    f"🍩 {user.first_name}早上好！甜甜圈都不如你甜~",
    f"🍓 {user.first_name}早安！草莓味的清晨最适合你~",
    f"🍊 {user.first_name}早上好！橙子说它想和你一样阳光~",
    f"🥗 {user.first_name}早安！健康的一天从早餐开始~",
    f"🍵 {user.first_name}早上好！清茶一杯，烦恼全消~",
    f"🥛 {user.first_name}早安！牛奶补钙，微笑补心~",
    f"🍪 {user.first_name}早上好！曲奇饼干和好心情更配哦~",
    f"🍑 {user.first_name}早安！水蜜桃般的甜蜜一天~",
    f"🥑 {user.first_name}早上好！牛油果说它想和你一样有营养~",
    f"🍍 {user.first_name}早安！菠萝头也比不上你的可爱~",
    f"🥖 {user.first_name}早上好！法棍面包和阳光都为你准备好了~",
    f"🍫 {user.first_name}早安！巧克力般丝滑的一天开始啦~",
    f"🍒 {user.first_name}早上好！樱桃小嘴不如你的笑容甜~",

    # 动物主题 (20条)
    f"🐶 {user.first_name}早安！狗狗说它想和你一起散步~",
    f"🐱 {user.first_name}早上好！猫咪都起床了你还在等什么~",
    f"🐰 {user.first_name}早安！像兔子一样活力四射吧~",
    f"🦊 {user.first_name}早上好！狐狸说你今天会聪明过人~",
    f"🐻 {user.first_name}早安！熊抱一个，给你力量~",
    f"🐼 {user.first_name}早上好！熊猫眼也比不上你的黑眼圈可爱~",
    f"🐯 {user.first_name}早安！像老虎一样勇敢面对今天~",
    f"🦁 {user.first_name}早上好！狮子王也要向你问好~",
    f"🐮 {user.first_name}早安！牛奶会有的，好运也会有的~",
    f"🐷 {user.first_name}早上好！小猪佩奇都起床啦~",
    f"🐸 {user.first_name}早安！青蛙王子在等你的早安吻~",
    f"🐙 {user.first_name}早上好！章鱼哥都没你起得早~",
    f"🦄 {user.first_name}早安！独角兽为你撒下魔法粉末~",
    f"🐧 {user.first_name}早上好！企鹅式摇摆开始新的一天~",
    f"🦉 {user.first_name}早安！猫头鹰都说你起得真早~",
    f"🐝 {user.first_name}早上好！小蜜蜂已经开始采蜜啦~",
    f"🦋 {user.first_name}早安！蝴蝶为你跳支晨舞~",
    f"🐬 {user.first_name}早上好！海豚式跳跃迎接新的一天~",
    f"🦕 {user.first_name}早安！恐龙都灭绝了你还活着真好~",
    f"🐿️ {user.first_name}早上好！松鼠已经囤好今天的坚果啦~",

    # 职业主题 (15条)
    f"👨‍💻 {user.first_name}早安！代码诗人今天也要写出优美代码~",
    f"👩‍🏫 {user.first_name}早上好！最棒的老师今天也要加油~",
    f"👨‍⚕️ {user.first_name}早安！白衣天使今天也要拯救世界~",
    f"👩‍🚀 {user.first_name}早上好！宇航员准备发射正能量~",
    f"👨‍🍳 {user.first_name}早安！大厨今天要烹饪什么美味呢~",
    f"👩‍🎨 {user.first_name}早上好！艺术家今天也要创造美丽~",
    f"👨‍🔧 {user.first_name}早安！工程师今天也要建造奇迹~",
    f"👩‍💼 {user.first_name}早上好！职场精英今天也要slay全场~",
    f"👨‍🔬 {user.first_name}早安！科学家今天又要发现什么新大陆~",
    f"👩‍🚒 {user.first_name}早上好！消防员今天也要勇敢无畏~",
    f"👨‍✈️ {user.first_name}早安！机长准备带领我们飞向成功~",
    f"👩‍🌾 {user.first_name}早上好！农民伯伯说今天会丰收~",
    f"👨‍🎤 {user.first_name}早安！摇滚巨星今天也要闪耀~",
    f"👩‍⚖️ {user.first_name}早上好！法官大人今天也要公正严明~",
    f"👨‍🎓 {user.first_name}早安！学霸今天又要征服哪些知识~",

    # 季节主题 (20条)
    f"🌸 {user.first_name}春日早安！樱花为你绽放~",
    f"☀️ {user.first_name}夏日早晨！阳光沙滩在等你~",
    f"🍁 {user.first_name}秋日早安！枫叶为你变红~",
    f"❄️ {user.first_name}冬日早晨！雪花为你跳舞~",
    f"🌱 {user.first_name}春分早安！万物复苏的好时节~",
    f"🌊 {user.first_name}夏至早上好！一起去海边吧~",
    f"🍂 {user.first_name}秋分早安！收获的季节到啦~",
    f"⛄ {user.first_name}冬至早上好！热汤圆在等你~",
    f"🌷 {user.first_name}春季早安！郁金香为你盛开~",
    f"🌞 {user.first_name}夏季早晨！防晒霜涂好了吗~",
    f"🍎 {user.first_name}秋季早安！苹果园大丰收~",
    f"🧣 {user.first_name}冬季早晨！围巾手套别忘记~",
    f"🪁 {user.first_name}春风早安！风筝飞得真高~",
    f"🏖️ {user.first_name}夏日早上好！椰子树上结满了快乐~",
    f"🌰 {user.first_name}秋晨早安！栗子烤好了快来吃~",
    f"⛷️ {user.first_name}冬日上午好！滑雪场已经开放啦~",
    f"🐛 {user.first_name}春晓早安！毛毛虫都变成蝴蝶啦~",
    f"🍉 {user.first_name}夏晨早上好！西瓜最中间那块留给你~",
    f"🌽 {user.first_name}秋早早安！玉米须都笑得翘起来了~",
    f"🛷 {user.first_name}冬晨早上好！雪橇犬已经迫不及待了~",

    # 节日主题 (15条)
    f"🎄 {user.first_name}圣诞早安！袜子里有惊喜哦~",
    f"🎉 {user.first_name}新年早上好！烟花为你绽放~",
    f"🐲 {user.first_name}春节早安！红包拿来~",
    f"🎃 {user.first_name}万圣早晨！糖果还是捣蛋~",
    f"🦃 {user.first_name}感恩早安！火鸡在桌上等你~",
    f"💖 {user.first_name}情人节早上好！玫瑰为你盛开~",
    f"🎆 {user.first_name}国庆早安！烟花秀即将开始~",
    f"🐇 {user.first_name}复活节早晨！彩蛋藏好了吗~",
    f"🪔 {user.first_name}元宵早安！汤圆甜又圆~",
    f"👻 {user.first_name}中元早上好！记得早点回家~",
    f"🎎 {user.first_name}端午早安！粽子咸甜你选哪个~",
    f"🏮 {user.first_name}中秋早晨！月亮饼分你一半~",
    f"🕊️ {user.first_name}和平日早安！白鸽为你衔来橄榄枝~",
    f"🎂 {user.first_name}生日早上好！蛋糕上的蜡烛都等不及了~",
    f"🍾 {user.first_name}除夕早安！年夜饭准备开动~",

    # 励志主题 (20条)
    f"💪 {user.first_name}早安！今天的你比昨天更强大~",
    f"🚀 {user.first_name}早上好！准备发射你的梦想~",
    f"🌟 {user.first_name}早安！星星都为你让路~",
    f"🏆 {user.first_name}早上好！冠军从晨间开始~",
    f"🌈 {user.first_name}早安！风雨过后必见彩虹~",
    f"🦸 {user.first_name}早上好！超级英雄也需要吃早餐~",
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
    f"👻 {user.first_name}早上好！你的睡意被我吓跑啦~",
    f"🦸 {user.first_name}早安！拯救世界的任务从起床开始~",
    f"🤖 {user.first_name}早上好！机器人也需要充电哦~",
    f"👽 {user.first_name}早安！外星人都在学你的早起~",
    f"🦖 {user.first_name}早上好！恐龙灭绝是因为没早起~",
    f"🧟 {user.first_name}早安！僵尸都比你有活力~",
    f"🤡 {user.first_name}早上好！小丑说你的睡相很可爱~",
    f"👾 {user.first_name}早安！游戏角色都开始新关卡了~",
    f"🦹 {user.first_name}早上好！超级反派都吃早餐了~",
    f"🧌 {user.first_name}早安！巨魔都开始梳头发了~",
    f"🧛 {user.first_name}早上好！吸血鬼都喝完早茶了~",
    f"🧙 {user.first_name}早安！魔法师说早起咒语最灵验~",
    f"🎭 {user.first_name}早上好！人生如戏全靠早起~",
    f"🤺 {user.first_name}早安！击剑手已经热身完毕~",
    f"🏴‍☠️ {user.first_name}早上好！海盗船等你来驾驶~",
    f"🕵️ {user.first_name}早安！侦探发现你昨晚睡得很好~",
    f"🥷 {user.first_name}早上好！忍者都开始晨练了~",
    f"👰 {user.first_name}早安！新娘说早起化妆不赶~",
    f"🤴 {user.first_name}早上好！王子已经吻醒青蛙了~",

    # 文艺风格 (15条)
    f"📖 {user.first_name}晨安！今日诗篇为你而写~",
    f"🎨 {user.first_name}早安！生活是张空白画布~",
    f"🎼 {user.first_name}晨光好！今日谱首欢乐颂~",
    f"✒️ {user.first_name}早安！钢笔已吸饱墨水~",
    f"🎭 {user.first_name}晨安！人生舞台幕布已拉开~",
    f"📷 {user.first_name}早上好！今天的美好值得定格~",
    f"🎻 {user.first_name}晨光熹微！小提琴在等你的旋律~",
    f"🖌️ {user.first_name}早安！调色板已准备好色彩~",
    f"📜 {user.first_name}晨安！羊皮卷轴展开新篇章~",
    f"🎹 {user.first_name}早上好！黑白键奏响晨曲~",
    f"🖋️ {user.first_name}晨光美！羽毛笔蘸满星光~",
    f"🎬 {user.first_name}早安！人生电影今日开机~",
    f"📯 {user.first_name}晨安！号角唤醒沉睡城堡~",
    f"🖼️ {user.first_name}早上好！画框已装好今日风景~",
    f"📚 {user.first_name}晨光好！故事书翻到新章节~",

    # 科幻未来 (15条)
    f"🚀 {user.first_name}星际早安！曲速引擎已启动~",
    f"👽 {user.first_name}银河早晨！外星朋友发来问候~",
    f"🤖 {user.first_name}机器人早安！系统更新完成100%~",
    f"🛸 {user.first_name}太空晨安！UFO停在你家阳台~",
    f"🌌 {user.first_name}宇宙早晨！黑洞也吞噬不了你的好心情~",
    f"⚡ {user.first_name}未来早安！特斯拉线圈为你充电~",
    f"🧪 {user.first_name}实验室早晨！新发明即将诞生~",
    f"🔭 {user.first_name}天文早安！望远镜发现你的星座~",
    f"🪐 {user.first_name}星际早晨！土星环为你闪耀~",
    f"🧬 {user.first_name}基因早安！DNA螺旋为你欢呼~",
    f"⚛️ {user.first_name}量子早晨！薛定谔的猫说早安~",
    f"🛰️ {user.first_name}卫星早安！GPS定位到你的笑容~",
    f"💫 {user.first_name}宇宙晨安！流星雨为你而下~",
    f"🧲 {user.first_name}磁力早晨！正能量吸引好运~",
    f"🌠 {user.first_name}星际早安！彗星尾巴扫走困意~",

    # 奇幻魔法 (15条)
    f"✨ {user.first_name}魔法早安！魔杖已充满能量~",
    f"🦄 {user.first_name}仙境早晨！独角兽等你骑乘~",
    f"🧙 {user.first_name}巫师早安！咒语书翻到祝福页~",
    f"🧚 {user.first_name}精灵晨安！翅膀洒下金粉~",
    f"🐉 {user.first_name}龙族早安！火焰温暖你清晨~",
    f"🏰 {user.first_name}城堡早晨！吊桥为你放下~",
    f"🔮 {user.first_name}水晶球早安！预见今日好运~",
    f"🧝 {user.first_name}精灵晨安！长生树为你开花~",
    f"🪄 {user.first_name}魔杖早安！变出美味早餐~",
    f"🧌 {user.first_name}巨魔早晨！桥下问题已解决~",
    f"🧜 {user.first_name}人鱼早安！珍珠串成祝福~",
    f"🦹 {user.first_name}反派早晨！今日暂停搞破坏~",
    f"🧞 {user.first_name}神灯早安！三个愿望待许~",
    f"🏮 {user.first_name}灯笼晨安！指引你前行~",
    f"🧂 {user.first_name}精灵早晨！撒盐驱散坏运气~",

    # 运动健康 (15条)
    f"🏃 {user.first_name}运动早安！晨跑路线已规划~",
    f"🧘 {user.first_name}瑜伽早晨！呼吸法开始~",
    f"🚴 {user.first_name}骑行早安！风景在前方等你~",
    f"🏊 {user.first_name}游泳早晨！泳池波光粼粼~",
    f"🤸 {user.first_name}体操早安！翻滚开始新一天~",
    f"🏋️ {user.first_name}健身早晨！杠铃已擦亮~",
    f"🤾 {user.first_name}球类早安！团队需要你~",
    f"⛹️ {user.first_name}篮球早晨！投篮百分百~",
    f"🤽 {user.first_name}水球早安！泳池见~",
    f"🏸 {user.first_name}羽毛球晨安！拍线已调好~",
    f"🎾 {user.first_name}网球早晨！发球Ace~",
    f"⚽ {user.first_name}足球早安！绿茵场召唤~",
    f"🏀 {user.first_name}篮球晨安！三分球练习~",
    f"🏐 {user.first_name}排球早晨！一传到位~",
    f"🥊 {user.first_name}拳击早安！出拳如风~",

    # 特殊场合 (10条)
    f"🎂 {user.first_name}生日早安！蜡烛等你吹灭~",
    f"💍 {user.first_name}纪念日早晨！爱意满满~",
    f"🎓 {user.first_name}毕业早安！学位帽抛向天空~",
    f"🏆 {user.first_name}比赛早晨！金牌在招手~",
    f"💼 {user.first_name}入职早安！新同事等你~",
    f"🚗 {user.first_name}旅行早晨！行李箱已装好~",
    f"🏠 {user.first_name}搬家早安！新家温暖~",
    f"💐 {user.first_name}约会早晨！鲜花已备好~",
    f"📝 {user.first_name}考试早安！笔下生花~",
    f"💵 {user.first_name}加薪早晨！钱包要变胖~"
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
