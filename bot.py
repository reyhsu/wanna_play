import logging
import asyncio
import requests
from datetime import timedelta
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    PollAnswerHandler
)
from apscheduler.schedulers.background import BackgroundScheduler

# === 設定 ===
BOT_TOKEN = ""
GROUP_CHAT_ID = ""
POLL_OPTIONS = ["🏀 打", "❌ nope"]
RADAR_IMAGE_URL = "https://www.cwa.gov.tw/Data/radar/CV1_3600.png"  # 中央氣象局雷達圖

# === 儲存資料 ===
poll_answers = defaultdict(lambda: defaultdict(list))  # {poll_id: {option_index: [user_id]}}
user_display_names = {}  # {user_id: 顯示名稱}
active_poll_info = {"message_id": None, "poll_id": None}

# === Logging 設定 ===
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)

# === /wea 指令：發送雷達圖 ===
async def wea_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = requests.get(RADAR_IMAGE_URL)
        if response.status_code == 200:
            await update.message.reply_photo(photo=response.content, caption="🌧️ 台灣雷達回波圖")
        else:
            await update.message.reply_text("⚠️ 圖片載入失敗，請稍後再試。")
    except Exception as e:
        logging.error(f"錯誤：{e}")
        await update.message.reply_text("⚠️ 發生錯誤，請稍後再試。")

# === 發起投票 ===
async def start_poll_by_bot(bot):
    if active_poll_info["poll_id"] is not None:
        logging.warning("⚠️ 已有一個投票進行中，跳過新投票")

        try:
            # 嘗試發送提示訊息（排程不一定有 chat context）
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

    # 儲存顯示名稱
    user_display_names[user_id] = f"@{user.username}" if user.username else user.full_name

    # 移除舊選擇
    for opt_index in poll_answers[poll_id]:
        if user_id in poll_answers[poll_id][opt_index]:
            poll_answers[poll_id][opt_index].remove(user_id)

    # 加入新選擇
    for i in selected:
        poll_answers[poll_id][i].append(user_id)

    logging.info(f"📥 {user_display_names[user_id]} 投了選項 {selected}")

# === 結束投票 ===
async def stop_poll_by_bot(bot):
    poll_id = active_poll_info["poll_id"]
    message_id = active_poll_info["message_id"]

    if not poll_id or not message_id:
        logging.warning("⚠️ 無投票進行中，跳過結束")
        return

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

        # 清除資料
        del poll_answers[poll_id]
        active_poll_info["poll_id"] = None
        active_poll_info["message_id"] = None

    except Exception as e:
        logging.error(f"❌ 結束投票失敗：{e}")

# === /start 指令 ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot 已啟動")

# === 處理投票指令：檢查是否有進行中的投票 ===
async def poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if active_poll_info["poll_id"] is not None:
        await update.message.reply_text("⚠️ 已有一個投票正在進行中，請先 /close 再發起新的")
        return
    await start_poll_by_bot(context.bot)

# === 主程式 ===
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wea", wea_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(CommandHandler("poll", poll_handler))
    # 測試用手動發起、結束投票指令
    app.add_handler(CommandHandler("poll", lambda update, context: asyncio.create_task(start_poll_by_bot(context.bot))))
    app.add_handler(CommandHandler("close", lambda update, context: asyncio.create_task(stop_poll_by_bot(context.bot))))

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
