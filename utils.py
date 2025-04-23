# --- START OF FILE utils.py ---

import sqlite3
import time
import os
import logging
import json
import shutil
import tempfile
import asyncio
import requests # Keep for NOWPayments deposits and CoinGecko
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# --- Telegram Imports ---
from telegram import Update, Bot
from telegram.constants import ParseMode
import telegram.error as telegram_error
from telegram.ext import ContextTypes
# -------------------------
from telegram import helpers
from collections import Counter

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Render Disk Path Configuration ---
RENDER_DISK_MOUNT_PATH = '/mnt/data'
DATABASE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'shop.db')
MEDIA_DIR = os.path.join(RENDER_DISK_MOUNT_PATH, 'media')
BOT_MEDIA_JSON_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'bot_media.json')

try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
    logger.info(f"Ensured media directory exists: {MEDIA_DIR}")
except OSError as e:
    logger.error(f"Could not create media directory {MEDIA_DIR}: {e}")

logger.info(f"Using Database Path: {DATABASE_PATH}")
logger.info(f"Using Media Directory: {MEDIA_DIR}")
logger.info(f"Using Bot Media Config Path: {BOT_MEDIA_JSON_PATH}")

# --- Configuration Loading (from Environment Variables) ---
TOKEN = os.environ.get("TOKEN", "")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # Needed for deposits
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Needed for deposit callbacks
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")
# REMOVED: NOWPAYMENTS_EMAIL, NOWPAYMENTS_PASSWORD

ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: ADMIN_ID = int(ADMIN_ID_RAW)
    except (ValueError, TypeError): logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}")

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values.")

BASKET_TIMEOUT = 15 * 60
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: BASKET_TIMEOUT = 15 * 60; logger.warning("BASKET_TIMEOUT non-positive, using 15min.")
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using 15min.")

# --- Validate essential config ---
if not TOKEN: raise SystemExit("CRITICAL ERROR: TOKEN environment variable is missing.")
if not NOWPAYMENTS_API_KEY: logger.warning("NOWPAYMENTS_API_KEY environment variable is missing. Deposits will fail.") # Deposits need this
if not WEBHOOK_URL: logger.warning("WEBHOOK_URL environment variable is missing. Deposit confirmations via webhook will not work.") # Webhook needed for deposit IPN
if ADMIN_ID is None: logger.warning("ADMIN_ID environment variable not set or invalid.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")
logger.info(f"Basket timeout set to {BASKET_TIMEOUT // 60} minutes.")
logger.info(f"NOWPayments IPN URL expected at: {WEBHOOK_URL}/webhook")

# Fee adjustment percentage for deposits (if desired)
FEE_ADJUSTMENT = Decimal('0.015')

# Price cache
price_cache = {}
CACHE_EXPIRATION_MINUTES = 10

# --- Bot Media Loading ---
BOT_MEDIA = {'type': None, 'path': None}
if os.path.exists(BOT_MEDIA_JSON_PATH):
    try:
        with open(BOT_MEDIA_JSON_PATH, 'r') as f: BOT_MEDIA = json.load(f)
        logger.info(f"Loaded BOT_MEDIA from {BOT_MEDIA_JSON_PATH}")
        if BOT_MEDIA.get("path"):
             filename = os.path.basename(BOT_MEDIA["path"])
             correct_path = os.path.join(MEDIA_DIR, filename)
             if BOT_MEDIA["path"] != correct_path:
                 logger.warning(f"Correcting BOT_MEDIA path from {BOT_MEDIA['path']} to {correct_path}")
                 BOT_MEDIA["path"] = correct_path
    except Exception as e: logger.warning(f"Could not load {BOT_MEDIA_JSON_PATH}: {e}")
else: logger.info(f"{BOT_MEDIA_JSON_PATH} not found.")

# --- Constants ---
THEMES = { "default": {"product": "💎", "basket": "🛒", "review": "📝"} } # Add others if needed
LANGUAGES = { # REMOVED withdrawal-related strings
    "en": {
        "native_name": "English",
        # ... (Keep all existing non-withdrawal English translations) ...
        "select_deposit_crypto": "💳 Choose Deposit Method",
        "choose_crypto_prompt": "Please choose the cryptocurrency you want to deposit with:",
        "generating_payment": "⏳ Generating payment details...",
        "payment_generation_failed": "❌ Failed to generate payment details. Please try again or contact support. Reason: {reason}",
        "nowpayments_invoice_title": "Deposit Invoice",
        "nowpayments_pay_amount_label": "Amount to pay",
        "nowpayments_send_to_label": "Send the exact amount to this address:",
        "nowpayments_address_label": "{currency} Address",
        "nowpayments_copy_hint": "(Click to copy)",
        "nowpayments_expires_label": "Expires in",
        "nowpayments_network_warning": "⚠️ Ensure you send {currency} using the correct network.",
        "nowpayments_fee_note": "Note: A small fee adjustment may apply to cover transaction costs.",
        "deposit_confirmed_ipn": "✅ Deposit Confirmed! Amount credited: {amount_usd} EUR. New balance: {new_balance} EUR",
        "deposit_failed_ipn": "❌ Deposit Failed (ID: {payment_id}). Reason: {reason}. Please contact support.",
        "error_processing_ipn": "❌ Error processing deposit confirmation. Please contact support with Payment ID: {payment_id}.",
        "status_note": "Payment status will be updated automatically once confirmed on the blockchain (may take time).",
        "minimum_deposit_warning": "1. ⚠️ Minimum deposit: {min_amount} {currency}. Amounts below this may be lost.",
        "unique_address_note": "3. This address is unique to this payment attempt and expires.",
        "manual_check_note": "If your deposit isn't confirmed automatically after some time, please contact support with your transaction details.",
    },
    "lt": {
        "native_name": "Lietuvių",
        # ... (Keep all existing non-withdrawal Lithuanian translations) ...
        "select_deposit_crypto": "💳 Pasirinkite depozito metodą",
        "choose_crypto_prompt": "Pasirinkite kriptovaliutą, kuria norite atlikti depozitą:",
        "generating_payment": "⏳ Generuojama mokėjimo informacija...",
        "payment_generation_failed": "❌ Nepavyko sugeneruoti mokėjimo informacijos. Bandykite dar kartą arba susisiekite su palaikymo tarnyba. Priežastis: {reason}",
        "nowpayments_invoice_title": "Depozito sąskaita",
        "nowpayments_pay_amount_label": "Mokėtina suma",
        "nowpayments_send_to_label": "Siųskite tikslią sumą šiuo adresu:",
        "nowpayments_address_label": "{currency} adresas",
        "nowpayments_copy_hint": "(Spustelėkite norėdami nukopijuoti)",
        "nowpayments_expires_label": "Galioja iki",
        "nowpayments_network_warning": "⚠️ Įsitikinkite, kad siunčiate {currency} naudodami teisingą tinklą.",
        "nowpayments_fee_note": "Pastaba: Gali būti taikomas nedidelis mokesčio koregavimas transakcijos kaštams padengti.",
        "deposit_confirmed_ipn": "✅ Depozitas patvirtintas! Priskirta suma: {amount_usd} EUR. Naujas likutis: {new_balance} EUR",
        "deposit_failed_ipn": "❌ Depozitas nepavyko (ID: {payment_id}). Priežastis: {reason}. Susisiekite su palaikymo tarnyba.",
        "error_processing_ipn": "❌ Klaida tvarkant depozito patvirtinimą. Susisiekite su palaikymo tarnyba, nurodydami Mokėjimo ID: {payment_id}.",
        "status_note": "Mokėjimo būsena bus atnaujinta automatiškai, kai bus patvirtinta blokų grandinėje (gali užtrukti).",
        "minimum_deposit_warning": "1. ⚠️ Minimalus depozitas: {min_amount} {currency}. Mažesnės sumos gali būti prarastos.",
        "unique_address_note": "3. Šis adresas yra unikalus šiam mokėjimo bandymui ir turi galiojimo laiką.",
        "manual_check_note": "Jei jūsų depozitas nepatvirtinamas automatiškai per kurį laiką, susisiekite su palaikymo tarnyba pateikdami transakcijos duomenis.",
    },
    "ru": {
        "native_name": "Русский",
        # ... (Keep all existing non-withdrawal Russian translations) ...
        "select_deposit_crypto": "💳 Выберите метод депозита",
        "choose_crypto_prompt": "Выберите криптовалюту для депозита:",
        "generating_payment": "⏳ Генерация данных для платежа...",
        "payment_generation_failed": "❌ Не удалось сгенерировать данные для платежа. Пожалуйста, попробуйте еще раз или свяжитесь со службой поддержки. Причина: {reason}",
        "nowpayments_invoice_title": "Счет на пополнение",
        "nowpayments_pay_amount_label": "Сумма к оплате",
        "nowpayments_send_to_label": "Отправьте точную сумму на этот адрес:",
        "nowpayments_address_label": "Адрес {currency}",
        "nowpayments_copy_hint": "(Нажмите для копирования)",
        "nowpayments_expires_label": "Истекает через",
        "nowpayments_network_warning": "⚠️ Убедитесь, что отправляете {currency} через правильную сеть.",
        "nowpayments_fee_note": "Примечание: Может применяться небольшая корректировка комиссии для покрытия транзакционных издержек.",
        "deposit_confirmed_ipn": "✅ Депозит подтвержден! Зачислено: {amount_usd} EUR. Новый баланс: {new_balance} EUR",
        "deposit_failed_ipn": "❌ Депозит не удался (ID: {payment_id}). Причина: {reason}. Пожалуйста, свяжитесь со службой поддержки.",
        "error_processing_ipn": "❌ Ошибка обработки подтверждения депозита. Пожалуйста, свяжитесь со службой поддержки, указав ID платежа: {payment_id}.",
        "status_note": "Статус платежа будет обновлен автоматически после подтверждения в блокчейне (может занять время).",
        "minimum_deposit_warning": "1. ⚠️ Минимальный депозит: {min_amount} {currency}. Суммы меньше этой могут быть потеряны.",
        "unique_address_note": "3. Этот адрес уникален для данной попытки платежа и имеет срок действия.",
        "manual_check_note": "Если ваш депозит не подтвердится автоматически через некоторое время, пожалуйста, свяжитесь со службой поддержки, предоставив детали транзакции.",
    }
}

# --- Global Data Variables ---
CITIES = {}; DISTRICTS = {}; PRODUCT_TYPES = []; SIZES = ["2g", "5g"]

# --- Database Connection Helper ---
def get_db_connection():
    try:
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir:
            try: os.makedirs(db_dir, exist_ok=True)
            except OSError as e: logger.warning(f"Could not create database directory {db_dir}: {e}")
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR connecting to database at {DATABASE_PATH}: {e}")
        raise SystemExit(f"Failed to connect to database: {e}")

# --- Data Loading Functions ---
def load_cities(): # Keep as is
    cities_data = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, name FROM cities ORDER BY name")
            cities_data = {str(row['id']): row['name'] for row in c.fetchall()}
    except sqlite3.Error as e: logger.error(f"Failed to load cities: {e}")
    return cities_data

def load_districts(): # Keep as is
    districts_data = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT d.city_id, d.id, d.name FROM districts d ORDER BY d.city_id, d.name")
            for row in c.fetchall():
                city_id_str = str(row['city_id'])
                if city_id_str not in districts_data: districts_data[city_id_str] = {}
                districts_data[city_id_str][str(row['id'])] = row['name']
    except sqlite3.Error as e: logger.error(f"Failed to load districts: {e}")
    return districts_data

def load_product_types(): # Keep as is
    product_types_list = []
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM product_types ORDER BY name")
            product_types_list = [row['name'] for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to load product types: {e}")
    return product_types_list

def load_all_data(): # Keep as is
    global CITIES, DISTRICTS, PRODUCT_TYPES
    logger.info("Starting load_all_data (in-place update)...")
    try:
        cities_data = load_cities(); districts_data = load_districts(); product_types_list = load_product_types()
        CITIES.clear(); CITIES.update(cities_data)
        DISTRICTS.clear(); DISTRICTS.update(districts_data)
        PRODUCT_TYPES[:] = product_types_list
        logger.info(f"Loaded (in-place) {len(CITIES)} cities, {sum(len(d) for d in DISTRICTS.values())} districts, {len(PRODUCT_TYPES)} product types.")
    except Exception as e:
        logger.error(f"Error during load_all_data (in-place): {e}", exc_info=True)
        CITIES.clear(); DISTRICTS.clear(); PRODUCT_TYPES[:] = []

# --- Database Initialization ---
def init_db(): # Keep as is (pending_deposits table already added)
    """Initializes the database schema including the pending_deposits table."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # ... (keep all existing CREATE TABLE statements) ...
             # users table
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0,
                total_purchases INTEGER DEFAULT 0, basket TEXT DEFAULT '',
                language TEXT DEFAULT 'en', theme TEXT DEFAULT 'default'
            )''')
            # ... other tables ...
            # pending_deposits table (already added)
            c.execute('''CREATE TABLE IF NOT EXISTS pending_deposits (
                            payment_id TEXT PRIMARY KEY,
                            user_id INTEGER NOT NULL,
                            currency TEXT NOT NULL,
                            target_eur_amount REAL,
                            created_at TEXT NOT NULL,
                            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                        )''')
            # ... (keep CREATE INDEX statements, including for pending_deposits) ...
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_deposits_user ON pending_deposits(user_id)")
            conn.commit()
            logger.info(f"Database schema at {DATABASE_PATH} initialized/verified successfully.")
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR: Database initialization failed for {DATABASE_PATH}: {e}", exc_info=True)
        raise SystemExit("Database initialization failed.")

# --- Database functions for pending deposits (Keep as is) ---
def add_pending_deposit(payment_id, user_id, currency, eur_amount): # Keep as is
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO pending_deposits (payment_id, user_id, currency, target_eur_amount, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (payment_id, user_id, currency, float(eur_amount), datetime.now().isoformat()))
        conn.commit()
        logger.info(f"Added pending deposit: payment_id={payment_id}, user_id={user_id}, currency={currency}, eur_amount={eur_amount}")

def get_pending_deposit(payment_id): # Keep as is
    """Fetches user_id and originally intended EUR amount for a pending deposit."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, currency, target_eur_amount FROM pending_deposits WHERE payment_id = ?", (payment_id,))
        result = c.fetchone()
        if result: return {"user_id": result['user_id'], "currency": result['currency'], "eur_amount": result['target_eur_amount']}
        return None

def remove_pending_deposit(payment_id): # Keep as is
    with get_db_connection() as conn:
        c = conn.cursor()
        deleted = c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,)).rowcount
        conn.commit()
        if deleted > 0: logger.info(f"Removed pending deposit: payment_id={payment_id}")
        else: logger.warning(f"Attempted to remove pending deposit, but payment_id={payment_id} not found.")

# --- NOWPayments/CoinGecko Price Fetching (Keep as is) ---
def get_currency_to_eur_price(currency_code): # Keep as is
    """Fetches the price of a cryptocurrency in EUR using CoinGecko API."""
    global price_cache
    currency_code = currency_code.lower(); current_time = datetime.now()
    if currency_code in price_cache:
        price, timestamp = price_cache[currency_code]
        if current_time - timestamp < timedelta(minutes=CACHE_EXPIRATION_MINUTES):
            logger.info(f"Using cached EUR price for {currency_code}: {price}")
            return Decimal(str(price))
    currency_map = {'btc': 'bitcoin', 'eth': 'ethereum', 'ltc': 'litecoin', 'sol': 'solana', 'usdt': 'tether', 'usdc': 'usd-coin', 'ton': 'the-open-network'}
    coingecko_id = currency_map.get(currency_code)
    if not coingecko_id: logger.error(f"No CoinGecko ID for {currency_code}"); return None
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=eur"
    logger.info(f"Fetching EUR price for {currency_code} ({coingecko_id})...")
    try:
        response = requests.get(url, timeout=10); response.raise_for_status()
        data = response.json()
        if coingecko_id in data and 'eur' in data[coingecko_id]:
            price = Decimal(str(data[coingecko_id]['eur']))
            price_cache[currency_code] = (float(price), current_time)
            logger.info(f"Fetched EUR price for {currency_code}: €{price}")
            return price
        logger.error(f"EUR price not found in CoinGecko response for {coingecko_id}: {data}"); return None
    except requests.exceptions.RequestException as e: logger.error(f"Failed CoinGecko request for {currency_code}: {e}"); return None
    except (KeyError, ValueError, InvalidOperation) as e: logger.error(f"Error parsing CoinGecko response for {currency_code}: {e}"); return None

# --- Other Utility Functions (Keep as is) ---
def format_currency(value): # Keep as is
    try:
        decimal_value = Decimal(str(value))
        rounded_value = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{rounded_value:.2f}"
    except (ValueError, TypeError, InvalidOperation) as e:
        logger.warning(f"Could not format currency: {value}, Error: {e}")
        return "0.00"

def format_discount_value(dtype, value): # Keep as is
    try:
        float_value = float(value)
        if dtype == 'percentage': return f"{float_value:.1f}%"
        elif dtype == 'fixed': return f"{format_currency(float_value)} EUR"
        return str(float_value)
    except (ValueError, TypeError): return "N/A"

def get_progress_bar(purchases): # Keep as is
    try:
        p = int(purchases); thresholds = [0, 2, 5, 8, 10]; filled = min(sum(1 for t in thresholds if p >= t), 5)
        return '[' + '🟩' * filled + '⬜️' * (5 - filled) + ']'
    except (ValueError, TypeError): return '[⬜️⬜️⬜️⬜️⬜️]'

async def send_message_with_retry(bot: Bot, chat_id: int, text: str, **kwargs): # Keep as is
    """Sends a Telegram message with retries."""
    max_retries = kwargs.pop('max_retries', 3)
    for attempt in range(max_retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except telegram_error.RetryAfter as e:
            s = e.retry_after + 1; logger.warning(f"Rate limit hit: Retrying after {s}s.")
            if s > 60: logger.error("RetryAfter > 60s. Aborting."); return None
            await asyncio.sleep(s)
        except (telegram_error.BadRequest, telegram_error.Unauthorized, telegram_error.NetworkError) as e:
            logger.warning(f"{type(e).__name__} (Attempt {attempt+1}): {e}")
            if isinstance(e, (telegram_error.BadRequest, telegram_error.Unauthorized)) or attempt >= max_retries - 1: return None
            await asyncio.sleep(1 * (2 ** attempt))
        except Exception as e:
            logger.error(f"Unexpected error sending (Attempt {attempt+1}): {e}", exc_info=True)
            if attempt >= max_retries - 1: return None
            await asyncio.sleep(1 * (2 ** attempt))
    logger.error(f"Failed to send message to {chat_id} after {max_retries} attempts.")
    return None

def get_date_range(period_key): # Keep as is
    """Calculates start and end ISO format datetime strings."""
    # ... (implementation unchanged) ...
    now = datetime.now(); start, end = None, None
    try:
        if period_key == 'today': start, end = now.replace(hour=0, minute=0, second=0, microsecond=0), now
        elif period_key == 'yesterday': y = now - timedelta(days=1); start, end = y.replace(hour=0, minute=0, second=0, microsecond=0), y.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'week': start, end = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0), now
        elif period_key == 'last_week': s_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); e_last = s_week - timedelta(microseconds=1); start = (e_last - timedelta(days=e_last.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end = e_last.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'month': start, end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now
        elif period_key == 'last_month': s_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0); e_last = s_month - timedelta(microseconds=1); start = e_last.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end = e_last.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'year': start, end = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), now
        else: return None, None
        return start.isoformat(), end.isoformat()
    except Exception as e: logger.error(f"Error calculating date range for '{period_key}': {e}"); return None, None

def get_user_status(purchases): # Keep as is
    try: p = int(purchases); return "VIP 👑" if p >= 10 else "Regular ⭐" if p >= 5 else "New 🌱"
    except (ValueError, TypeError): return "New 🌱"

def clear_expired_basket(context: ContextTypes.DEFAULT_TYPE, user_id: int): # Keep as is
    """Clears expired items from a user's basket (DB and context)."""
    # ... (implementation unchanged) ...
    if 'basket' not in context.user_data: context.user_data['basket'] = []
    conn = None
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("BEGIN")
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone(); basket_str = result['basket'] if result else ''
        if not basket_str:
            if context.user_data.get('basket'): context.user_data['basket'] = []
            if context.user_data.get('applied_discount'): context.user_data.pop('applied_discount', None)
            c.execute("COMMIT"); return
        items = basket_str.split(','); current_time = time.time(); valid_items_str = []; valid_items_ctx = []; expired_ids = Counter(); expired = False
        potential_prod_ids = [int(i.split(':')[0]) for i in items if ':' in i]
        product_prices = {};
        if potential_prod_ids:
             placeholders = ','.join('?' * len(potential_prod_ids)); c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", potential_prod_ids)
             product_prices = {r['id']: Decimal(str(r['price'])) for r in c.fetchall()}
        for item_str in items:
            if not item_str: continue
            try:
                pid, t_str = item_str.split(':'); pid = int(pid); timestamp = float(t_str)
                if current_time - timestamp <= BASKET_TIMEOUT:
                    valid_items_str.append(item_str)
                    if pid in product_prices: valid_items_ctx.append({"product_id": pid, "price": float(product_prices[pid]), "timestamp": timestamp})
                    else: logger.warning(f"P{pid} price not found (user {user_id}).")
                else: expired_ids[pid] += 1; expired = True
            except (ValueError, IndexError): logger.warning(f"Malformed item '{item_str}' (user {user_id})")
        if expired:
            c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (','.join(valid_items_str), user_id))
            if expired_ids: c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", [(cnt, p_id) for p_id, cnt in expired_ids.items()])
        c.execute("COMMIT"); context.user_data['basket'] = valid_items_ctx
        if not valid_items_ctx and context.user_data.get('applied_discount'): context.user_data.pop('applied_discount', None)
    except sqlite3.Error as e: logger.error(f"SQLite error clearing basket for {user_id}: {e}"); conn.rollback() if conn else None
    except Exception as e: logger.error(f"Unexpected error clearing basket for {user_id}: {e}")
    finally: conn.close() if conn else None

def clear_all_expired_baskets(): # Keep as is
    """Scheduled job: Clears expired items from all users' baskets."""
    # ... (implementation unchanged) ...
    logger.info("Running scheduled job: clear_all_expired_baskets")
    expired_counts = Counter(); updates = []; conn = None
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("BEGIN")
        c.execute("SELECT user_id, basket FROM users WHERE basket IS NOT NULL AND basket != ''")
        users = c.fetchall(); current_time = time.time()
        for user in users:
            uid, b_str = user['user_id'], user['basket']; items = b_str.split(','); valid_items = []; expired = False
            for item_str in items:
                if not item_str: continue
                try:
                    pid, t_str = item_str.split(':'); pid = int(pid); timestamp = float(t_str)
                    if current_time - timestamp <= BASKET_TIMEOUT: valid_items.append(item_str)
                    else: expired_counts[pid] += 1; expired = True
                except (ValueError, IndexError): pass
            if expired: updates.append((','.join(valid_items), uid))
        if updates: c.executemany("UPDATE users SET basket = ? WHERE user_id = ?", updates); logger.info(f"Scheduled clear: Updated {len(updates)} baskets.")
        if expired_counts:
            decrement_data = [(cnt, p_id) for p_id, cnt in expired_counts.items()]
            if decrement_data: c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data); logger.info(f"Scheduled clear: Released {sum(expired_counts.values())} reservations.")
        conn.commit()
    except sqlite3.Error as e: logger.error(f"SQLite error in clear_all_expired_baskets: {e}"); conn.rollback() if conn else None
    except Exception as e: logger.error(f"Unexpected error in clear_all_expired_baskets: {e}")
    finally: conn.close() if conn else None

def fetch_last_purchases(user_id, limit=10): # Keep as is
    """Fetches the last N purchases for a specific user."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT purchase_date, product_name, product_size, price_paid FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC LIMIT ?", (user_id, limit))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"DB error fetching history for {user_id}: {e}"); return []

def fetch_reviews(offset=0, limit=5): # Keep as is
    """Fetches reviews with usernames for display, handling pagination."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT r.review_id, r.user_id, r.review_text, r.review_date, COALESCE(u.username, 'anonymous') as username FROM reviews r LEFT JOIN users u ON r.user_id = u.user_id ORDER BY r.review_date DESC LIMIT ? OFFSET ?", (limit, offset))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to fetch reviews (offset={offset}, limit={limit}): {e}"); return []

# --- Placeholder Handler ---
async def handle_coming_soon(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): # Keep as is
    query = update.callback_query
    if query:
        try: await query.answer("This feature is coming soon!", show_alert=True)
        except Exception as e: logger.error(f"Error answering 'coming soon' callback: {e}")

# --- Initial Data Load ---
init_db()
load_all_data()

# --- END OF FILE utils.py ---