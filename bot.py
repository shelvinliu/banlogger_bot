# ---------- 配置部分 ----------
import os
from fastapi import FastAPI

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # 必须通过Render后台设置
WEBHOOK_PATH = "/telegram"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH  # Render自动提供
EXCEL_FILE = "/tmp/ban_records.xlsx"  # 临时存储（建议改用数据库）

app = FastAPI()

# ---------- 启动逻辑 ----------
@app.on_event("startup")
async def startup():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN 环境变量未设置！")
    
    # 初始化机器人
    global bot_app
    bot_app = ApplicationBuilder().token(TOKEN).build()
    
    # 添加处理器（CommandHandler等...）
    
    # 设置Webhook
    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(WEBHOOK_URL)

# ---------- 主程序入口 ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
