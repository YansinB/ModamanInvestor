import logging
import asyncio
from io import BytesIO
import os

# --- ส่วนของ Telegram ---
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- ส่วนของเครื่องมือวิเคราะห์ ---
import yfinance as yf
import pandas as pd
import pandas_ta as ta

# กำหนดค่า Matplotlib เพื่อไม่ให้ใช้ GUI (สำคัญสำหรับการรันบน Server)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# -----------------------------------------------------------------
# (1) ส่วนตั้งค่า Logging
# -----------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------
# (2) ฟังก์ชันช่วยเหลือ: คำนวณแนวรับ/แนวต้าน
# -----------------------------------------------------------------
def calculate_support_resistance(data):
    """คำนวณแนวรับแนวต้านง่ายๆ โดยใช้ราคาต่ำสุด/สูงสุด 60 วัน"""
    window = 60
    if len(data) < window:
        window = len(data)
    
    recent_data = data.iloc[-window:]
    support = recent_data['close'].min()
    resistance = recent_data['high'].max()
    last_close = data['close'].iloc[-1]
    
    return support, resistance, last_close

# -----------------------------------------------------------------
# (3) "สมอง": ฟังก์ชันวิเคราะห์ (Logic V43 - Multi-Report Fix)
# -----------------------------------------------------------------
def analyze_stock(ticker_input):
    """ฟังก์ชันสำหรับวิเคราะห์หุ้น/สินทรัพย์ 1 ตัว (ทำงานแบบ Sync)"""
    
    original_ticker = ticker_input.upper()
    ticker = original_ticker
    market_type = "US Stocks/ETFs"
    
    # 1. Ticker List ที่ต้องวิเคราะห์
    tickers_to_analyze = []
    
    if ticker == "XAUUSD":
        tickers_to_analyze.append(("GC=F", "Gold (Commodity Futures)"))
    elif ticker.endswith(".BK"):
        tickers_to_analyze.append((original_ticker, "Thai Stock (SET)"))
    
    # 2. กรณี Ticker สั้นและกำกวม (เช่น NPK, CPALL)
    elif 3 <= len(original_ticker) <= 5 and not any(c.isdigit() for c in original_ticker):
        
        tickers_to_analyze.append((original_ticker, "US Stocks/ETFs")) 
        tickers_to_analyze.append((f"{original_ticker}.BK", "Thai Stock (SET)"))
    
    # 3. กรณี Ticker ทั่วไป (เช่น ETF, Long names)
    else:
        tickers_to_analyze.append((original_ticker, "US Stocks/ETFs"))

    
    # --- V43 NEW: ทำการวิเคราะห์ตามรายการ Tickers ---
    full_report_parts = []
    
    for ticker_to_fetch, current_market_type in tickers_to_analyze:
        try:
            # 1. ดึงข้อมูลหุ้นย้อนหลัง 1 ปี 
            data = yf.download(ticker_to_fetch, period="1y", progress=False, group_by='ticker', auto_adjust=True)
            stock_info = yf.Ticker(ticker_to_fetch).info 

            if data.empty:
                continue

            # --- [Data Normalize] ---
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.droplevel(0)
                
            data.columns = data.columns.str.lower()
            
            required_cols = ['open', 'high', 'low', 'close', 'volume']
            if not all(col in data.columns for col in required_cols):
                 logger.error(f"FATAL: Required columns missing for {ticker_to_fetch}. Available: {data.columns}")
                 continue
                 
            data = data[required_cols]
            data.dropna(inplace=True)
            # --- [END Data Normalize] ---

            # 2. คำนวณ Indicators
            data.ta.rsi(close='close', append=True); data.ta.macd(close='close', append=True)
            data.ta.ema(close='close', length=10, append=True); data.ta.ema(close='close', length=20, append=True) 
            data.ta.ema(close='close', length=50, append=True); data.ta.ema(close='close', length=200, append=True)
            data.ta.obv(close='close', volume='volume', append=True); data.ta.atr(length=14, append=True)
            data.ta.adx(length=14, append=True); data.ta.stochrsi(append=True); data.dropna(inplace=True)
            
            latest_data = data.iloc[-1]
            latest_price = latest_data['close'] 
            latest_atr = latest_data['ATRr_14']; ema_10 = latest_data['EMA_10']; ema_20 = latest_data['EMA_20']
            macd_line = latest_data['MACD_12_26_9']; signal_line = latest_data['MACDs_12_26_9']; adx_val = latest_data['ADX_14']
            stochrsi_k = latest_data['STOCHRSIk_14_14_3_3']

            # 3. ตรรกะการให้คะแนน (Scoring Logic V36)
            buy_score = 0; sell_score = 0; rsi_val = latest_data['RSI_14']
            pe_ratio = stock_info.get('trailingPE', 999.00) 
            
            # Scoring Logic
            if adx_val > 20: buy_score += 1;
            if rsi_val < 35: buy_score += 1; 
            elif rsi_val > 65: sell_score += 1; 
            
            if stochrsi_k < 20: buy_score += 1;
            elif stochrsi_k > 80: sell_score += 1;
                
            if macd_line > signal_line: buy_score += 1
            elif macd_line < signal_line: sell_score += 2 
                
            if ema_10 > ema_20: buy_score += 2
            else: sell_score += 1
                
            if latest_data['EMA_50'] > latest_data['EMA_200']: buy_score += 1 
            else: sell_score += 1
                
            obv_col_name = 'OBV' 
            if data[obv_col_name].iloc[-1] > data[obv_col_name].iloc[-5]: buy_score += 1
            else: sell_score += 1

            # สรุปสัญญาณ
            final_signal = "รอจังหวะ / ถือ 🟡"; signal_score_text = f"(คะแนน {buy_score}:{sell_score})"
            if buy_score >= 4:
                final_signal = f"สัญญาณซื้อ 🟢 {signal_score_text}"
            elif sell_score >= 3:
                final_signal = f"สัญญาณขาย 🔴 {signal_score_text}"
            else:
                final_signal = f"รอจังหวะ / ถือ 🟡 {signal_score_text}"

            # 4. คำนวณจุดเข้าซื้อ/เป้าหมาย (Logic V40 - Enforce Wait Logic)
            support_60d, resistance_60d, _ = calculate_support_resistance(data)
            
            calculated_entry = ema_20 if ema_10 > ema_20 else latest_data['EMA_50']
            calculated_strategy = "Demand Zone (EMA-20)" if ema_10 > ema_20 else "EMA-50 (Conservative)"
            
            entry_price = latest_price
            strategy_name = "กลยุทธ์: WAIT"
            
            # V35 Entry Logic
            MAX_ENTRY_DEVIATION = 1.05 
            
            if calculated_entry > latest_price * MAX_ENTRY_DEVIATION:
                 entry_price = latest_price
                 strategy_name = "Anomaly Entry (High Risk)"
            elif 'สัญญาณซื้อ 🟢' in final_signal and latest_price > ema_20 and adx_val > 25:
                 entry_price = latest_price
                 strategy_name = "Aggressive Breakout (ADX Confirmed)"
            elif 'สัญญาณซื้อ 🟢' in final_signal and latest_price > ema_20:
                 entry_price = calculated_entry
                 strategy_name = "Demand Zone (EMA-20) - Low Risk"
            else:
                entry_price = calculated_entry
                strategy_name = calculated_strategy
                
                # Sub-Fix: ถ้า Entry ที่คำนวณได้ยังสูงกว่าราคาปิด ให้ใช้ราคาปิด (ป้องกันการซื้อแพงเกินไป)
                if entry_price > latest_price:
                     entry_price = latest_price - (latest_atr * 0.2) 
                     strategy_name = "Conservative Entry (Buy Below Close)"
            
            # 5. Stop Loss Calculation (V40 FIX: คำนวณ Label ให้ถูกต้อง)
            sl_atr = entry_price - (1 * latest_atr); sl_pct = entry_price * 0.90
            stop_loss_price = max(sl_atr, sl_pct)
            
            risk_percentage = 100 * (entry_price - stop_loss_price) / entry_price
            risk_label = f"{risk_percentage:.2f}% Risk"
                 
            if stop_loss_price < 0: stop_loss_price = 0.01

            # คำนวณ Take Profit
            take_profit_5 = entry_price * 1.05; take_profit_10 = entry_price * 1.10
            take_profit_15 = entry_price * 1.15; take_profit_20 = entry_price * 1.20
            
            # 6. สร้างข้อความรายงาน (Markdown)
            summary_text = f"📈 **สรุปสัญญาณ {current_market_type} Ticker: {original_ticker.upper()}**\n\n"
            summary_text += f"**ราคาปัจจุบัน:** **`${latest_price:.2f}`**\n"
            
            if pe_ratio < 999:
                 summary_text += f"**P/E Ratio:** **{pe_ratio:.2f}**\n"
            else:
                 summary_text += f"**P/E Ratio:** **N/A (Data Anomaly)**\n"
                 
            summary_text += f"**สรุปสัญญาณ:** **{final_signal}**\n\n"
            
            summary_text += "--- *เป้าหมายราคา (สำหรับฝั่งซื้อ)* ---\n"
            
            if 'สัญญาณซื้อ 🟢' in final_signal:
                summary_text += f"**กลยุทธ์:** **{strategy_name}**\n"
                summary_text += f"**จุดเข้าซื้อ (Buy Entry):** **`${entry_price:.2f}`**\n"
                summary_text += f"**จุดตัดขาดทุน (Stop-Loss):** `${stop_loss_price:.2f}` ({risk_label})\n" 
                summary_text += f"**เป้าหมายทำกำไร 5%:** `${take_profit_5:.2f}`\n"
                summary_text += f"**เป้าหมายทำกำไร 10%:** `${take_profit_10:.2f}`\n"
                summary_text += f"**เป้าหมายทำกำไร 15%:** `${take_profit_15:.2f}`\n"
                summary_text += f"**เป้าหมายทำกำไร 20%:** `${take_profit_20:.2f}`\n\n"
            else:
                summary_text += f"**คำแนะนำ:** **ไม่แนะนำให้เข้าซื้อในขณะนี้**\n"
                summary_text += f"พิจารณาซื้อเมื่อราคาเข้าใกล้แนวรับที่: `{calculated_entry:.2f}` ({calculated_strategy})\n"
                summary_text += f"จุดตัดขาดทุนที่ควรพิจารณา: `{stop_loss_price:.2f}`\n\n"
            
            # รายละเอียด Indicators
            summary_text += "--- *รายละเอียด Indicators* ---\n"
            summary_text += f"**ADX (14):** {adx_val:.2f} ({'Strong Trend' if adx_val > 25 else 'Weak Trend'})\n"
            summary_text += f"**StochRSI (K):** {stochrsi_k:.2f} ({'Oversold' if stochrsi_k < 20 else ('Overbought' if stochrsi_k > 80 else 'Neutral')})\n" 
            summary_text += f"**RSI (14):** {latest_data['RSI_14']:.2f}\n"
            summary_text += f"**MACD:** {'Bullish (ซื้อ)' if macd_line > signal_line else 'Bearish (ขาย)'}\n"
            summary_text += f"**EMA (10/20):** {'Golden Cross (ขึ้น)' if ema_10 > ema_20 else 'Dead Cross (ลง)'}\n"
            summary_text += f"**EMA (50/200):** {'Golden Cross (ขึ้น)' if latest_data['EMA_50'] > latest_data['EMA_200'] else 'Dead Cross (ลง)'}\n"
            summary_text += f"**ATR (14):** ${latest_atr:.2f}\n"
            summary_text += "\n_*ข้อควรระวัง: นี่คือสัญญาณจากสูตรคำนวณอัตโนมัติ ไม่ใช่คำแนะนำการลงทุน*_"


            # 7. สร้างกราฟ
            recent_data = data.tail(60)
            
            plt.style.use('seaborn-v0_8-darkgrid')
            fig, ax = plt.subplots(figsize=(10, 5))
            
            ax.plot(recent_data.index, recent_data['close'], label='Close Price', linewidth=1.5)
            ax.plot(recent_data.index, recent_data['EMA_20'].tail(60), label='EMA 20 (Demand)', color='orange', linestyle='-')
            
            # Targets
            ax.axhline(y=entry_price, color='green', linestyle='--', label=f'Buy Entry: {entry_price:.2f}')
            ax.axhline(y=stop_loss_price, color='red', linestyle='--', label=f'Stop Loss: {stop_loss_price:.2f}', alpha=0.7)
            ax.axhline(y=take_profit_10, color='blue', linestyle=':', label=f'TP 10%: {take_profit_10:.2f}', alpha=0.5)

            ax.set_title(f'{original_ticker.upper()} ({current_market_type}) Price & Targets (last 60 days)', fontsize=14)
            ax.set_xlabel('Date'); ax.set_ylabel('Price')
            plt.xticks(rotation=45); ax.legend(loc='best'); plt.tight_layout()

            buf = BytesIO(); fig.savefig(buf, format='png'); buf.seek(0); plt.close(fig)

            full_report_parts.append({"text": summary_text, "image": buf})
            
        except Exception as e:
            logger.error(f"Error in analyze_stock for {ticker_to_fetch}: {e}")
            full_report_parts.append({"text": f"เกิดข้อผิดพลาดร้ายแรงในการวิเคราะห์ {original_ticker} ({current_market_type}) ครับ: {e}", "image": None})
    
    # ส่งรายงานรวม
    if not full_report_parts:
        return f"ขออภัยครับ ไม่พบข้อมูลสำหรับ Ticker '{original_ticker}' ในตลาดสหรัฐฯ หรือไทยที่ระบุ", None
        
    return full_report_parts # ส่ง list ของรายงานและรูปภาพ

# -----------------------------------------------------------------
# (4) ฟังก์ชัน Telegram Handler (แก้ไขให้รองรับ Multi-Part Report)
# -----------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ส่งข้อความต้อนรับเมื่อพิมพ์ /start"""
    welcome_message = (
        f"👋 สวัสดีครับ! ผมคือบอทวิเคราะห์หุ้น Modaman Investor\n\n"
        "โปรดพิมพ์สัญลักษณ์หุ้น (Ticker) ของสหรัฐฯ เช่น **NVDA**, **AAPL** "
        "หุ้นไทย เช่น **CPALL** หรือ **KBANK.BK** "
        "และราคาทองคำ **XAUUSD** เพื่อให้ผมวิเคราะห์จุดเข้าซื้อที่แม่นยำขึ้นครับ 📈"
    )
    await update.message.reply_html(welcome_message, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """จัดการข้อความที่ผู้ใช้พิมพ์มา (ที่ไม่ใช่คำสั่ง)"""
    ticker = update.message.text.strip().upper()
    
    if not ticker or len(ticker) > 15:
        await update.message.reply_text("🤔 โปรดพิมพ์ Ticker ที่ถูกต้อง (เช่น NVDA, CPALL.BK, XAUUSD) ครับ")
        return

    await update.message.reply_text(f"รับทราบครับ! กำลังวิเคราะห์ข้อมูลหุ้น **{ticker}**... กรุณารอสักครู่ครับ ⏳", parse_mode='Markdown')
    
    # ใช้ asyncio เพื่อเรียกฟังก์ชันวิเคราะห์หุ้นแบบ blocking
    loop = asyncio.get_event_loop()
    full_report_parts = await loop.run_in_executor(None, analyze_stock, ticker)
    
    # *** V43 FIX: วนลูปเพื่อส่งรายงานและรูปภาพทั้งหมด (แก้ BadRequest) ***
    if not isinstance(full_report_parts, list):
         # ถ้าไม่ใช่ list (เป็นข้อผิดพลาด)
         await update.message.reply_text(str(full_report_parts), parse_mode='Markdown')
         return

    for part in full_report_parts:
        text_result = part["text"]
        image_result = part["image"]

        if image_result:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=image_result,
                caption=text_result,
                parse_mode='Markdown'
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text_result,
                parse_mode='Markdown'
            )

    logger.info(f"Analysis for {ticker} completed.")

# -----------------------------------------------------------------
# (5) ฟังก์ชัน Main (สำหรับรัน)
# -----------------------------------------------------------------
def main() -> None:
    """เริ่มการทำงานของบอท"""
    
    # 1. TOKEN: ควรใช้ Environment Variable สำหรับ Production
    TOKEN = os.environ.get("TELEGRAM_TOKEN") 
    if not TOKEN:
        # ใช้ Token ที่คุณฮาร์ดโค้ดไว้สำหรับการทดสอบ
        TOKEN = "8014636341:AAH-lK-pMA9vW5yp76s33b340dcMPojD4ho" 
        logger.warning("WARNING: Using Hardcoded Token. Set TELEGRAM_TOKEN environment variable in production.")

    logger.info(f"Using Token starting with: {TOKEN[:5]}...")
    
    # 2. สร้าง Application
    try:
        application = Application.builder().token(TOKEN).build()
    except Exception as e:
        logger.error(f"FATAL ERROR during Application build: {e}")
        return

    # 3. สร้าง Handler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Application started. Bot is now polling for updates.")
    application.run_polling()


if __name__ == "__main__":
    main()