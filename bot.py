import os
import re
import json
import pytz
import asyncio
import logging
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager
import pandas as pd
from github import Github, GithubException
from fastapi import FastAPI, Request, HTTPException
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

# 配置
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # GitHub Personal Access Token
GITHUB_REPO = os.getenv("GITHUB_REPO")    # 格式：username/repo
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = f"{os.getenv('RENDER_EXTERNAL_URL', '')}{WEBHOOK_PATH}" if os.getenv("RENDER_EXTERNAL_URL") else None
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Asia/Shanghai"))
MAX_RECORDS_DISPLAY = 10
EXCEL_FILE = "ban_records.xlsx"

# 全局变量
bot_app: Optional[Application] = None
bot_initialized: bool = False
ban_records: List[Dict[str, Any]] = []

class GitHubStorage:
    """GitHub 存储管理类"""
    
    @staticmethod
    async def load_from_github() -> List[Dict[str, Any]]:
        """从GitHub加载Excel数据"""
        if not GITHUB_TOKEN or not GITHUB_REPO:
            logger.warning("未配置GITHUB_TOKEN或GITHUB_REPO，无法从GitHub加载数据")
            return []
            
        try:
            g = Github(GITHUB_TOKEN)
            repo = g.get_repo(GITHUB_REPO)
            try:
                contents = repo.get_contents(EXCEL_FILE)
                file_data = base64.b64decode(contents.content)
                
                # 临时保存到本地
                with open(EXCEL_FILE, "wb") as f:
                    f.write(file_data)
                
                # 读取Excel到内存
                df = pd.read_excel(EXCEL_FILE)
                return df.to_dict('records')
            except GithubException as e:
                if e.status == 404:
                    logger.info("GitHub上未找到历史记录文件，将创建新文件")
                return []
        except Exception as e:
            logger.error(f"从GitHub加载数据失败: {e}")
            return []

    @staticmethod
    async def save_to_github(records: List[Dict[str, Any]]) -> bool:
        """保存数据到GitHub"""
        if not GITHUB_TOKEN or not GITHUB_REPO:
            logger.error("未配置GITHUB_TOKEN或GITHUB_REPO，无法保存到GitHub")
            return False
            
        try:
            # 先保存到本地Excel
            df = pd.DataFrame(records)
            df.to_excel(EXCEL_FILE, index=False, engine="openpyxl")
            
            # 读取Excel内容
            with open(EXCEL_FILE, "rb") as f:
                content = base64.b64encode(f.read()).decode("utf-8")
            
            # 上传到GitHub
            g = Github(GITHUB_TOKEN)
            repo = g.get_repo(GITHUB_REPO)
            
            try:
                # 尝试获取现有文件（更新模式）
                contents = repo.get_contents(EXCEL_FILE)
                repo.update_file(
                    path=EXCEL_FILE,
                    message="Update ban records",
                    content=content,
                    sha=contents.sha
                )
            except GithubException as e:
                if e.status == 404:
                    # 文件不存在，创建新文件
                    repo.create_file(
                        path=EXCEL_FILE,
                        message="Create ban records",
                        content=content
                    )
                else:
                    raise
            
            logger.info("数据已保存到GitHub")
            return True
        except Exception as e:
            logger.error(f"保存到GitHub失败: {e}")
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
        reason: str = "未填写"
    ) -> bool:
        """保存封禁记录到内存并同步到GitHub"""
        global ban_records
        
        try:
            record = {
                "time": datetime.now(TIMEZONE).isoformat(),
                "group_name": chat_title,
                "banned_user_id": banned_user_id,
                "banned_user_name": banned_user_name,
                "admin_name": admin_name,
                "reason": reason
            }
            
            ban_records.append(record)
            
            # 同步到GitHub
            success = await GitHubStorage.save_to_github(ban_records)
            if not success:
                logger.warning("GitHub同步失败，数据仅保存在内存中")
            
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
        "/kick - 踢出用户(回复消息使用)\n"
        "/mute - 禁言用户(回复消息并指定时间)\n"
        "/unmute - 解除禁言\n"
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
            "operator_id": update.effective_user.id
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
        logger.warning(f"禁言时间格式错误: {e}")
    except Exception as e:
        error_msg = await update.message.reply_text(f"❌ 禁言失败: {str(e)}")
        asyncio.create_task(delete_message_later(error_msg))
        logger.error(f"禁言用户失败: {e}")

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
                f"👤 用户: {record.get('banned_user_name', '未知')} (ID: {record.get('banned_user_id', '未知')})\n"
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
                f"👤 用户: {record.get('banned_user_name', '未知')} (ID: {record.get('banned_user_id', '未知')})\n"
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
    """FastAPI生命周期管理"""
    global bot_app, bot_initialized

    if not bot_initialized:
        # 初始化时从GitHub加载数据
        global ban_records
        ban_records = await GitHubStorage.load_from_github()
        logger.info(f"从GitHub加载了 {len(ban_records)} 条历史记录")

        bot_app = ApplicationBuilder().token(TOKEN).build()

        # 注册处理器
        bot_app.add_handler(CommandHandler("start", start_handler))
        bot_app.add_handler(CommandHandler("kick", kick_handler))
        bot_app.add_handler(CommandHandler("mute", mute_handler))
        bot_app.add_handler(CommandHandler("unmute", unmute_handler))
        bot_app.add_handler(CommandHandler("records", records_handler))
        bot_app.add_handler(CommandHandler("search", search_handler))
        bot_app.add_handler(CommandHandler("export", export_handler))
        bot_app.add_handler(CallbackQueryHandler(ban_reason_handler))
        bot_app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, custom_reason_handler))

        await bot_app.initialize()
        await bot_app.start()
        if WEBHOOK_URL:
            await bot_app.bot.set_webhook(url=WEBHOOK_URL)

        bot_initialized = True
        logger.info("✅ Bot 已成功初始化并启动")
    
    yield

    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("✅ Bot 已停止")

# FastAPI应用实例
app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    """Telegram Webhook入口"""
    if not bot_app:
        raise HTTPException(status_code=503, detail="Bot未初始化")

    try:
        data = await req.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"处理更新失败: {e}")
        raise HTTPException(status_code=400, detail="处理更新失败")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
