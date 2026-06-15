import os
import sys
import logging
import asyncio
import requests
from datetime import timedelta
from collections import defaultdict
from telegram import Update, InputMediaPhoto
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    PollAnswerHandler
)
from playwright.async_api import async_playwright
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv()

# === 設定 (優先從環境變數讀取) ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "")
POLL_OPTIONS = ["🏀 打", "❌ nope"]

# Windy 擷取設定 (預設為台灣地區)
WINDY_RAIN_URL = "https://www.windy.com/24.163/120.648?rain,23.580,120.648,8"
WINDY_RADAR_URL = "https://www.windy.com/24.163/120.648?radar,23.580,120.648,8"
SCREENSHOT_WAIT_TIME = 10
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800

# 建立鎖定機制，防止同時執行多個 Playwright 瀏覽器實例導致系統記憶體與 CPU 載入過重
screenshot_lock = asyncio.Lock()

# === 儲存資料 ===
poll_answers = defaultdict(lambda: defaultdict(list))  # {poll_id: {option_index: [user_id]}}
user_display_names = {}  # {user_id: 顯示名稱}
active_poll_info = {"message_id": None, "poll_id": None}

# === Logging 設定 ===
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)


async def capture_screenshots(rain_path: str, radar_path: str) -> tuple[bool, bool]:
    """
    使用 Playwright 平行擷取 Windy 的雨量與雷達回波網頁畫面並儲存為高品質 JPEG
    """
    rain_success = False
    radar_success = False
    
    async with async_playwright() as p:
        browser = None
        try:
            logging.info("正在啟動 Playwright 瀏覽器...")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu"
                ]
            )
            
            # 建立瀏覽器上下文，設定 Viewport 與擬真的 User Agent
            context = await browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 建立兩個平行載入的分頁
            page_rain = await context.new_page()
            page_radar = await context.new_page()
            
            logging.info("同時連線至累積雨量與雷達回波頁面...")
            # 平行發起請求並等待基本網頁載入
            results = await asyncio.gather(
                page_rain.goto(WINDY_RAIN_URL, wait_until="load", timeout=45000),
                page_radar.goto(WINDY_RADAR_URL, wait_until="load", timeout=45000),
                return_exceptions=True
            )
            
            # 檢查是否有任何一頁連線失敗
            for i, res in enumerate(results):
                if isinstance(res, Exception):
                    page_name = "累積雨量預報" if i == 0 else "即時雷達回波"
                    logging.error(f"連線至 Windy {page_name} 頁面失敗: {res}")
                    return False, False
            
            # 等待氣象圖層、動畫與地圖完全載入
            logging.info(f"等待 {SCREENSHOT_WAIT_TIME} 秒讓地圖細節與雷達動畫載入完成...")
            await asyncio.sleep(SCREENSHOT_WAIT_TIME)
            
            # 嘗試隱藏可能遮擋地圖的 Cookie 同意聲明橫幅
            for name, page in [("雨量頁面", page_rain), ("雷達頁面", page_radar)]:
                try:
                    await page.evaluate("""
                        () => {
                            const consent = document.querySelector('#consent-wall');
                            if (consent) consent.style.display = 'none';
                            
                            const banner = document.querySelector('.fc-consent-root');
                            if (banner) banner.style.display = 'none';
                            
                            const overlay = document.querySelector('.fc-ab-root');
                            if (overlay) overlay.style.display = 'none';
                        }
                    """)
                except Exception as e:
                    logging.warning(f"嘗試隱藏 {name} 的 Cookie 橫幅時發生裝飾性錯誤 (可忽略): {e}")
            
            # 擷取並儲存雨量預報圖
            await page_rain.screenshot(path=rain_path, type="jpeg", quality=85, full_page=False)
            rain_success = True
            logging.info(f"雨量預報圖擷取成功，儲存至: {rain_path}")
            
            # 擷取並儲存雷達回波圖
            await page_radar.screenshot(path=radar_path, type="jpeg", quality=85, full_page=False)
            radar_success = True
            logging.info(f"雷達回波圖擷取成功，儲存至: {radar_path}")
            
            # 關閉分頁
            await page_rain.close()
            await page_radar.close()
            
        except Exception as e:
            logging.error(f"擷取網頁畫面時發生未預期錯誤: {e}", exc_info=True)
        finally:
            if browser:
                await browser.close()
                
    return rain_success, radar_success


# === /wea 指令：發送雷達與雨量預報圖 ===
async def wea_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("⏳ 正在初始化瀏覽器... 預計需要 10-15 秒，請稍候...")
    
    rain_path = f"/tmp/windy_rain_{chat_id}.jpg"
    radar_path = f"/tmp/windy_radar_{chat_id}.jpg"
    
    if screenshot_lock.locked():
        await status_msg.edit_text("⏳ 系統目前正忙於處理其他使用者的截圖，您的請求已加入佇列排隊中，請耐心等候...")
        
    async with screenshot_lock:
        try:
            await status_msg.edit_text("📸 連線至 Windy 中... 正在載入累積雨量預報與即時雷達圖...")
            rain_success, radar_success = await capture_screenshots(rain_path, radar_path)
            
            if rain_success and radar_success:
                await status_msg.edit_text("📤 擷取成功！正在上傳至 Telegram...")
                with open(rain_path, 'rb') as f_rain, open(radar_path, 'rb') as f_radar:
                    media_group = [
                        InputMediaPhoto(media=f_rain, caption="🌧️ **Windy 累積雨量預報圖** (台灣地區)", parse_mode="Markdown"),
                        InputMediaPhoto(media=f_radar, caption="📡 **Windy 即時雷達回波圖** (台灣地區)", parse_mode="Markdown")
                    ]
                    await update.message.reply_media_group(media=media_group, write_timeout=60, read_timeout=60)
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ 抱歉，擷取 Windy 畫面失敗（連線逾時或官網異常），請稍候再試！")
                
        except Exception as e:
            logging.error(f"處理天氣請求時發生未預期錯誤: {e}", exc_info=True)
            await status_msg.edit_text("❌ 處理您的請求時發生錯誤，請稍候再試。")
        finally:
            for path in [rain_path, radar_path]:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception as e:
                        logging.warning(f"刪除暫存圖檔失敗 {path}: {e}")


# === 發起投票核心邏輯 ===
async def start_poll_by_bot(bot):
    if active_poll_info["poll_id"] is not None:
        logging.warning("⚠️ 已有一個投票進行中，跳過新投票")
        try:
            await bot.send_message(chat_id=GROUP_CHAT_ID, text="⚠️ 已有一個投票進行中，請先結束再發起新投票")
        except Exception as e:
            logging.info(f"排程模式下跳過發送訊息：{e}")
        return

    message = await bot.send_poll(
        chat_id=GROUP_CHAT_ID,
        question="wanna play?",
        options=POLL_OPTIONS,
        is_anonymous=False,
        allows_multiple_answers=False,
    )
    active_poll_info["message_id"] = message.message_id
    active_poll_info["poll_id"] = message.poll.id
    logging.info(f"✅ 發起投票：{message.poll.id}")


# === 投票紀錄 ===
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll_id = update.poll_answer.poll_id
    user = update.poll_answer.user
    user_id = user.id
    selected = update.poll_answer.option_ids

    user_display_names[user_id] = f"@{user.username}" if user.username else user.full_name

    for opt_index in poll_answers[poll_id]:
        if user_id in poll_answers[poll_id][opt_index]:
            poll_answers[poll_id][opt_index].remove(user_id)

    for i in selected:
        poll_answers[poll_id][i].append(user_id)

    logging.info(f"📥 {user_display_names[user_id]} 投了選項 {selected}")


# === 結束投票核心邏輯 ===
async def stop_poll_by_bot(bot) -> bool:
    poll_id = active_poll_info["poll_id"]
    message_id = active_poll_info["message_id"]

    if not poll_id or not message_id:
        logging.warning("⚠️ 無投票進行中，跳過結束")
        return False

    try:
        result = await bot.stop_poll(
            chat_id=GROUP_CHAT_ID,
            message_id=message_id,
        )

        summary = f"📊 投票結果：「{result.question}」\n\n"
        for i, option in enumerate(result.options):
            user_ids = poll_answers[poll_id].get(i, [])
            names = [user_display_names.get(uid, "未知") for uid in user_ids]
            summary += f"{option.text}（{len(user_ids)}人）：{'、'.join(names) or '無'}\n"

        await bot.send_message(chat_id=GROUP_CHAT_ID, text=summary)

        del poll_answers[poll_id]
        active_poll_info["poll_id"] = None
        active_poll_info["message_id"] = None
        return True

    except Exception as e:
        logging.error(f"❌ 結束投票失敗：{e}")
        return False


# === /start 指令 ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot 已啟動")


# === /help 指令 ===
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🏀 *wanna_play Telegram Bot 幫助選單* 🗳️\n\n"
        "本 Bot 主要提供籃球投票發起與即時 Windy 天氣圖功能，以下是可用的指令清單與用法：\n\n"
        "💬 *一般指令*\n"
        "• /start - 啟動 Bot，確認 Bot 運作狀態。\n"
        "• /help - 顯示此幫助選單，列出所有可用指令與詳細用法。\n\n"
        "📡 *天氣預報*\n"
        "• /wea - 擷取 Windy 台灣累積雨量預報圖與即時雷達回波圖並發送相簿。\n"
        "  _(系統會於背景以 headless 瀏覽器載入 Windy 最新畫面，擷取雙圖預計需 10-15 秒。內建排隊鎖，若多人同時查詢時會加入佇列排隊。)_\n\n"
        "🗳️ *投票功能*\n"
        "• /poll - 手動發起「wanna play?」非匿名投票。\n"
        "• /close - 手動結束當前投票，並發布投票結果（包含各選項的人員統計與人數）。\n\n"
        "⏰ *排程設定 (自動)*\n"
        "• *自動發起*：每週日 18:00 自動於群組發起新投票。\n"
        "• *自動結束*：每週一 07:00 自動結束投票並發送結果統計。\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


# === /poll 指令 ===
async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if active_poll_info["poll_id"] is not None:
        await update.message.reply_text("⚠️ 已有一個投票正在進行中，請先 /close 再發起新的")
        return
    await start_poll_by_bot(context.bot)


# === /close 指令 ===
async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if active_poll_info["poll_id"] is None:
        await update.message.reply_text("⚠️ 目前沒有正在進行中的投票喔！")
        return
    success = await stop_poll_by_bot(context.bot)
    if not success:
        await update.message.reply_text("❌ 結束投票失敗，請檢查後台日誌。")


# === 主程式 ===
def main():
    if not BOT_TOKEN:
        logging.critical("❌ 未設定 BOT_TOKEN 環境變數，程式即將結束。")
        sys.exit(1)

    request_config = HTTPXRequest(connect_timeout=15, read_timeout=60, write_timeout=120)
    app = ApplicationBuilder().token(BOT_TOKEN).request(request_config).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("wea", wea_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(CommandHandler("poll", poll_handler))
    app.add_handler(CommandHandler("close", close_handler))

    # === 建立排程器 ===
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    loop = asyncio.get_event_loop()

    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(start_poll_by_bot(app.bot), loop),
        trigger="cron", day_of_week="sun", hour=18, minute=0,
    )

    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(stop_poll_by_bot(app.bot), loop),
        trigger="cron", day_of_week="mon", hour=7, minute=0,
    )

    scheduler.start()
    app.run_polling()


if __name__ == "__main__":
    main()
