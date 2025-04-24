import sqlite3
import time
import os
import logging
import json
import shutil
import tempfile
import asyncio
import requests # Keep for NOWPayments deposits and CoinGecko
from datetime import datetime, timedelta, timezone # Ensure timezone is imported
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation # Use Decimal for financial calcs

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
FEE_ADJUSTMENT = Decimal('0.015') # Example 1.5% adjustment

# Price cache
price_cache = {}
CACHE_EXPIRATION_MINUTES = 10

# --- Bot Media Loading ---
BOT_MEDIA = {'type': None, 'path': None}
if os.path.exists(BOT_MEDIA_JSON_PATH):
    try:
        with open(BOT_MEDIA_JSON_PATH, 'r') as f: BOT_MEDIA = json.load(f)
        logger.info(f"Loaded BOT_MEDIA from {BOT_MEDIA_JSON_PATH}")
        # --- Path Correction ---
        if BOT_MEDIA.get("path"):
             filename = os.path.basename(BOT_MEDIA["path"])
             # Construct the expected path based on the *current* MEDIA_DIR
             correct_path = os.path.join(MEDIA_DIR, filename)
             if BOT_MEDIA["path"] != correct_path:
                 logger.warning(f"Correcting BOT_MEDIA path from {BOT_MEDIA['path']} to {correct_path}")
                 BOT_MEDIA["path"] = correct_path
                 # Optionally re-save the JSON with the corrected path
                 try:
                     with open(BOT_MEDIA_JSON_PATH, 'w') as wf:
                         json.dump(BOT_MEDIA, wf, indent=4)
                     logger.info(f"Saved corrected BOT_MEDIA path to {BOT_MEDIA_JSON_PATH}")
                 except Exception as save_e:
                     logger.error(f"Failed to save corrected BOT_MEDIA path: {save_e}")
        # --- End Path Correction ---
    except Exception as e: logger.warning(f"Could not load or parse {BOT_MEDIA_JSON_PATH}: {e}")
else: logger.info(f"{BOT_MEDIA_JSON_PATH} not found. Bot media will not be displayed.")

# --- Constants ---
THEMES = { "default": {"product": "💎", "basket": "🛒", "review": "📝"} }
LANGUAGES = { # REMOVED withdrawal-related strings
    "en": {
        "native_name": "English",
        "welcome": "👋 Welcome, {username}!",
        "status_label": "Status",
        "balance_label": "Balance",
        "purchases_label": "Total Purchases",
        "basket_label": "Basket Items",
        "shopping_prompt": "Start shopping or explore your options below.",
        "refund_note": "Note: No refunds.",
        "shop_button": "Shop",
        "profile_button": "Profile",
        "top_up_button": "Top Up",
        "reviews_button": "Reviews",
        "price_list_button": "Price List",
        "language_button": "Language",
        "admin_button": "🔧 Admin Panel",
        "no_cities_available": "No cities available at the moment. Please check back later.",
        "choose_city_title": "Choose a City",
        "select_location_prompt": "Select your location:",
        "home_button": "Home",
        "error_city_not_found": "Error: City not found.",
        "back_cities_button": "Back to Cities",
        "no_districts_available": "No districts available yet for this city.",
        "choose_district_prompt": "Choose a district:",
        "error_district_city_not_found": "Error: District or city not found.",
        "back_districts_button": "Back to Districts",
        "no_types_available": "No product types currently available here.",
        "select_type_prompt": "Select product type:",
        "error_loading_types": "Error: Failed to Load Product Types",
        "error_unexpected": "An unexpected error occurred",
        "back_types_button": "Back to Types",
        "no_items_of_type": "No items of this type currently available here.",
        "available_options_prompt": "Available options:",
        "error_loading_products": "Error: Failed to Load Products",
        "available_label_short": "Av",
        "price_label": "Price",
        "available_label_long": "Available",
        "back_options_button": "Back to Options",
        "drop_unavailable": "Drop Unavailable! This option just sold out or was reserved by someone else.",
        "add_to_basket_button": "Add to Basket",
        "error_loading_details": "Error: Failed to Load Product Details",
        "error_location_mismatch": "Error: Location data mismatch.",
        "out_of_stock": "Out of Stock! Sorry, the last one was just taken or reserved.",
        "pay_now_button": "Pay Now",
        "view_basket_button": "View Basket",
        "clear_basket_button": "Clear Basket",
        "shop_more_button": "Shop More",
        "expires_label": "Expires",
        "error_adding_db": "Error: Database issue adding item to basket.",
        "error_adding_unexpected": "Error: An unexpected issue occurred.",
        "added_to_basket": "✅ Item Reserved!\n\n{item} is in your basket for {timeout} minutes! ⏳",
        "pay": "💳 Total to Pay: {amount} EUR",
        "profile_title": "Your Profile",
        "purchase_history_button": "Purchase History",
        "basket_empty": "🛒 Your Basket is Empty!",
        "add_items_prompt": "Add items to start shopping!",
        "your_basket_title": "Your Basket",
        "expires_in_label": "Expires in",
        "remove_button_label": "Remove",
        "items_expired_note": "Items may have expired or were removed.",
        "discount_applied_label": "Discount Applied",
        "discount_removed_note": "Discount code {code} removed: {reason}",
        "subtotal_label": "Subtotal",
        "total_label": "Total",
        "remove_discount_button": "Remove Discount",
        "apply_discount_button": "Apply Discount Code",
        "discount_no_items": "Your basket is empty. Add items first.",
        "cancel_button": "Cancel",
        "enter_discount_code_prompt": "Please enter your discount code:",
        "enter_code_answer": "Enter code in chat.",
        "discount_removed_answer": "Discount removed.",
        "no_discount_answer": "No discount applied.",
        "send_text_please": "Please send the discount code as text.",
        "returning_to_basket": "Returning to basket.",
        "no_code_entered": "No code entered.",
        "error_calculating_total": "Error calculating basket total.",
        "basket_empty_no_discount": "Your basket is empty. Cannot apply discount code.",
        "success_label": "Success!",
        "basket_already_empty": "Basket is already empty.",
        "basket_cleared": "🗑️ Basket Cleared!",
        "purchase_history_title": "Purchase History",
        "no_purchases_yet": "You haven't made any purchases yet.",
        "recent_purchases_title": "Your Recent Purchases",
        "back_profile_button": "Back to Profile",
        "unknown_date_label": "Unknown Date",
        "error_saving_language": "Error saving language preference.",
        "invalid_language_answer": "Invalid language selected.",
        "back_button": "Back",
        "language": "🌐 Select Language:",
        "no_cities_for_prices": "No cities available to view prices for.",
        "price_list_title": "Price List",
        "select_city_prices_prompt": "Select a city to view available products and prices:",
        "price_list_title_city": "Price List: {city_name}",
        "no_products_in_city": "No products currently available in this city.",
        "available_label": "available",
        "back_city_list_button": "Back to City List",
        "message_truncated_note": "Message truncated due to length limit. Use 'Shop' for full details.",
        "error_displaying_prices": "Error displaying price list.",
        "error_loading_prices_db": "Error: Failed to Load Price List for {city_name}",
        "error_unexpected_prices": "Error: An unexpected issue occurred while generating the price list.",
        "reviews": "📝 Reviews Menu",
        "view_reviews_button": "View Reviews",
        "leave_review_button": "Leave a Review",
        "enter_review_prompt": "Please type your review message and send it.",
        "enter_review_answer": "Enter your review in the chat.",
        "send_text_review_please": "Please send text only for your review.",
        "review_not_empty": "Review cannot be empty. Please try again or cancel.",
        "review_too_long": "Review is too long (max 1000 characters). Please shorten it.",
        "review_thanks": "Thank you for your review! Your feedback helps us improve.",
        "error_saving_review_db": "Error: Could not save your review due to a database issue.",
        "error_saving_review_unexpected": "Error: An unexpected issue occurred while saving your review.",
        "user_reviews_title": "User Reviews",
        "no_reviews_yet": "No reviews have been left yet.",
        "no_more_reviews": "No more reviews to display.",
        "prev_button": "Prev",
        "next_button": "Next",
        "back_review_menu_button": "Back to Reviews Menu",
        "error_displaying_review": "Error displaying review",
        "error_updating_review_list": "Error updating review list.",
        "top_up_title": "Top Up Balance",
        "min_top_up_note": "Minimum top up: {amount} EUR",
        "error_occurred_answer": "An error occurred. Please try again.",
        "send_amount_as_text": "Please send the amount as text (e.g., 10 or 25.50).",
        "amount_too_low_msg": "Amount too low. Minimum top up is {amount} EUR. Please enter a higher amount.",
        "amount_too_high_msg": "Amount too high. Please enter a lower amount.",
        "invalid_amount_format_msg": "Invalid amount format. Please enter a number (e.g., 10 or 25.50).",
        "unexpected_error_msg": "An unexpected error occurred. Please try again later.",
        "choose_crypto_prompt": "You want to top up {amount} EUR. Please choose the cryptocurrency you want to pay with:",
        "cancel_top_up_button": "Cancel Top Up",
        "balance_changed_error": "❌ Transaction failed: Balance changed.",
        "order_failed_all_sold_out_balance": "❌ Order Failed: All items sold out.",
        "error_processing_purchase_contact_support": "❌ Error processing purchase. Contact support.",
        "purchase_success": "🎉 Purchase Complete!",
        "sold_out_note": "⚠️ Note: Items unavailable: {items}.",
        "insufficient_balance": "⚠️ Insufficient Balance! Top up needed.",
        "back_basket_button": "Back to Basket",
        "admin_select_city": "Select City to Add Product:",
        "admin_select_district": "Select District in {city}:",
        "admin_select_type": "Select Product Type:",
        "set_media_prompt_plain": "Send a photo, video, or GIF to display above all messages:",
        # --- NOWPAYMENTS Specific ---
        "select_deposit_crypto": "💳 Choose Deposit Method",
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
        "deposit_api_error": "❌ Failed to generate deposit address due to an API error ({error_code}). Please try again later or contact support.",
        "deposit_fetch_price_error": "❌ Failed to fetch current price for {currency}. Cannot calculate deposit amount. Please try again later.",
        "target_top_up_amount": "Target top-up amount",

    },
    # --- Add LT / RU translations later if needed ---
    "lt": { "native_name": "Lietuvių", # ... (rest of LT translations) ...
        "select_deposit_crypto": "💳 Pasirinkite depozito metodą",
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
        "deposit_api_error": "❌ Nepavyko sugeneruoti depozito adreso dėl API klaidos ({error_code}). Bandykite vėliau arba susisiekite su palaikymo tarnyba.",
        "deposit_fetch_price_error": "❌ Nepavyko gauti dabartinės {currency} kainos. Negalima apskaičiuoti depozito sumos. Bandykite vėliau.",
        "target_top_up_amount": "Norima papildymo suma",
    },
    "ru": { "native_name": "Русский", # ... (rest of RU translations) ...
        "select_deposit_crypto": "💳 Выберите метод депозита",
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
        "deposit_api_error": "❌ Не удалось сгенерировать адрес депозита из-за ошибки API ({error_code}). Пожалуйста, попробуйте позже или свяжитесь со службой поддержки.",
        "deposit_fetch_price_error": "❌ Не удалось получить текущую цену для {currency}. Невозможно рассчитать сумму депозита. Пожалуйста, попробуйте позже.",
        "target_top_up_amount": "Целевая сумма пополнения",
     }
}

# --- Global Data Variables ---
CITIES = {}; DISTRICTS = {}; PRODUCT_TYPES = []; SIZES = ["2g", "5g"]

# --- Database Connection Helper ---
def get_db_connection():
    """Establishes a connection to the SQLite database."""
    try:
        # Ensure the directory exists
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
            except OSError as e:
                # Log a warning but don't necessarily exit, connect might still work if file exists
                logger.warning(f"Could not create database directory {db_dir}: {e}")

        # Connect to the database
        conn = sqlite3.connect(DATABASE_PATH, timeout=10) # Increased timeout slightly
        conn.execute("PRAGMA foreign_keys = ON;") # Enforce foreign key constraints
        conn.row_factory = sqlite3.Row # Return rows as dictionary-like objects
        return conn
    except sqlite3.Error as e:
        # Log critical error and exit if connection fails
        logger.critical(f"CRITICAL ERROR connecting to database at {DATABASE_PATH}: {e}")
        # Optionally: raise SystemExit to stop the bot if DB is essential
        raise SystemExit(f"Failed to connect to database: {e}")

# --- Data Loading Functions ---
def load_cities(): # Keep as is
    cities_data = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, name FROM cities ORDER BY name")
        cities_data = {str(row['id']): row['name'] for row in c.fetchall()}
    except sqlite3.Error as e: logger.error(f"Failed to load cities: {e}")
    finally:
        if conn: conn.close()
    return cities_data

def load_districts(): # Keep as is
    districts_data = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT d.city_id, d.id, d.name FROM districts d ORDER BY d.city_id, d.name")
        for row in c.fetchall():
            city_id_str = str(row['city_id'])
            if city_id_str not in districts_data: districts_data[city_id_str] = {}
            districts_data[city_id_str][str(row['id'])] = row['name']
    except sqlite3.Error as e: logger.error(f"Failed to load districts: {e}")
    finally:
        if conn: conn.close()
    return districts_data

def load_product_types(): # Keep as is
    product_types_list = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM product_types ORDER BY name")
        product_types_list = [row['name'] for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to load product types: {e}")
    finally:
        if conn: conn.close()
    return product_types_list

def load_all_data(): # Keep as is
    global CITIES, DISTRICTS, PRODUCT_TYPES
    logger.info("Starting load_all_data (in-place update)...")
    try:
        cities_data = load_cities()
        districts_data = load_districts()
        product_types_list = load_product_types()

        CITIES.clear(); CITIES.update(cities_data)
        DISTRICTS.clear(); DISTRICTS.update(districts_data)
        PRODUCT_TYPES[:] = product_types_list # Update list in-place

        logger.info(f"Loaded (in-place) {len(CITIES)} cities, {sum(len(d) for d in DISTRICTS.values())} districts, {len(PRODUCT_TYPES)} product types.")
    except Exception as e:
        logger.error(f"Error during load_all_data (in-place): {e}", exc_info=True)
        # Clear globals on error to prevent using stale data
        CITIES.clear()
        DISTRICTS.clear()
        PRODUCT_TYPES[:] = []

# --- Database Initialization ---
def init_db():
    """Initializes the database schema including the pending_deposits table."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0.0,
            total_purchases INTEGER DEFAULT 0,
            basket TEXT DEFAULT '',
            language TEXT DEFAULT 'en',
            theme TEXT DEFAULT 'default'
        )''')
        # cities table
        c.execute('''CREATE TABLE IF NOT EXISTS cities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL
                    )''')
        # districts table
        c.execute('''CREATE TABLE IF NOT EXISTS districts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        city_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE,
                        UNIQUE(city_id, name)
                    )''')
        # product_types table
        c.execute('''CREATE TABLE IF NOT EXISTS product_types (
                        name TEXT PRIMARY KEY
                    )''')
        # products table
        c.execute('''CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL, district TEXT NOT NULL, product_type TEXT NOT NULL,
            size TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL,
            available INTEGER DEFAULT 1, reserved INTEGER DEFAULT 0,
            original_text TEXT, added_by INTEGER, added_date TEXT,
            FOREIGN KEY(product_type) REFERENCES product_types(name) ON DELETE RESTRICT
        )''')
        # product_media table
        c.execute('''CREATE TABLE IF NOT EXISTS product_media (
            media_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            media_type TEXT NOT NULL, file_path TEXT NOT NULL,
            telegram_file_id TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )''')
        # purchases table
        c.execute('''CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            product_name TEXT, product_type TEXT, product_size TEXT,
            price_paid REAL NOT NULL, city TEXT, district TEXT,
            purchase_date TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE SET NULL
        )''')
        # reviews table
        c.execute('''CREATE TABLE IF NOT EXISTS reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, review_text TEXT NOT NULL, review_date TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE SET NULL
        )''')
        # discount_codes table
        c.execute('''CREATE TABLE IF NOT EXISTS discount_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_type TEXT NOT NULL CHECK(discount_type IN ('percentage', 'fixed')),
            value REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            max_uses INTEGER,
            uses_count INTEGER DEFAULT 0,
            expiry_date TEXT,
            created_date TEXT NOT NULL
        )''')
        # pending_deposits table (Important for NOWPayments webhook)
        c.execute('''CREATE TABLE IF NOT EXISTS pending_deposits (
                        payment_id TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        currency TEXT NOT NULL,
                        target_eur_amount REAL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    )''')

        # Add Indexes for performance
        c.execute("CREATE INDEX IF NOT EXISTS idx_products_location_type ON products(city, district, product_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_products_availability ON products(available, reserved)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, purchase_date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_date ON reviews(review_date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_discount_codes_code ON discount_codes(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_product_media_product ON product_media(product_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_deposits_user ON pending_deposits(user_id)")

        conn.commit()
        logger.info(f"Database schema at {DATABASE_PATH} initialized/verified successfully.")
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR: Database initialization failed for {DATABASE_PATH}: {e}", exc_info=True)
        # Ensure connection is closed if initialization fails
        if conn: conn.close()
        raise SystemExit("Database initialization failed.")
    finally:
        if conn: conn.close()


# --- Database functions for pending deposits (Keep as is) ---
def add_pending_deposit(payment_id, user_id, currency, eur_amount): # Keep as is
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO pending_deposits (payment_id, user_id, currency, target_eur_amount, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (payment_id, user_id, currency, float(eur_amount), datetime.now().isoformat()))
        conn.commit()
        logger.info(f"Added pending deposit: payment_id={payment_id}, user_id={user_id}, currency={currency}, eur_amount={eur_amount}")
    except sqlite3.Error as e:
        logger.error(f"Failed to add pending deposit {payment_id} for user {user_id}: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()


def get_pending_deposit(payment_id): # Keep as is
    """Fetches user_id and originally intended EUR amount for a pending deposit."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, currency, target_eur_amount FROM pending_deposits WHERE payment_id = ?", (payment_id,))
        result = c.fetchone()
        if result: return {"user_id": result['user_id'], "currency": result['currency'], "eur_amount": result['target_eur_amount']}
        return None
    except sqlite3.Error as e:
        logger.error(f"Failed to get pending deposit {payment_id}: {e}")
        return None
    finally:
        if conn: conn.close()


def remove_pending_deposit(payment_id): # Keep as is
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        deleted = c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,)).rowcount
        conn.commit()
        if deleted > 0: logger.info(f"Removed pending deposit: payment_id={payment_id}")
        else: logger.warning(f"Attempted to remove pending deposit, but payment_id={payment_id} not found.")
    except sqlite3.Error as e:
        logger.error(f"Failed to remove pending deposit {payment_id}: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()

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
    # --- CoinGecko IDs ---
    # You might need to find the correct IDs on coingecko.com
    currency_map = {
        'btc': 'bitcoin', 'eth': 'ethereum', 'ltc': 'litecoin',
        'sol': 'solana', 'usdt': 'tether', 'usdc': 'usd-coin',
        'ton': 'the-open-network' # Example for TON
    }
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

# --- Time Formatting ---
def format_expiration_time(expiration_date_str):
    """Formats ISO UTC expiration string into H:MM:SS remaining."""
    if not expiration_date_str: return "N/A"
    try:
        # Handle potential milliseconds and the 'Z' UTC marker
        expiration_time = datetime.fromisoformat(expiration_date_str.replace('Z', '+00:00'))
        now_utc = datetime.now(timezone.utc) # Use timezone-aware now
        time_left = expiration_time - now_utc

        if time_left.total_seconds() <= 0: return "Expired"

        total_seconds = int(time_left.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        # Show H:MM:SS format
        return f"{hours:01d}:{minutes:02d}:{seconds:02d}"
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse expiration time '{expiration_date_str}': {e}")
        return "N/A" # Fallback

# --- Moved from user.py ---
def validate_discount_code(code_text, current_total):
    """
    Validates a discount code against the database. Synchronous.
    Returns: (is_valid: bool, message: str, details: dict | None)
    """
    if not code_text: return False, "No code provided.", None
    conn = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column names because row_factory is set
        c.execute("SELECT * FROM discount_codes WHERE code = ?", (code_text,))
        code_data = c.fetchone()

        if not code_data: return False, "Discount code not found.", None
        if not code_data['is_active']: return False, "This discount code is inactive.", None
        if code_data['expiry_date']:
            try:
                expiry_dt = datetime.fromisoformat(code_data['expiry_date'])
                if datetime.now() > expiry_dt:
                    return False, "This discount code has expired.", None
            except ValueError:
                logger.warning(f"Invalid expiry_date format in DB for code {code_data['code']}")
                return False, "Invalid code expiry data.", None
        if code_data['max_uses'] is not None and code_data['uses_count'] >= code_data['max_uses']:
            return False, "This discount code has reached its usage limit.", None

        discount_amount = Decimal('0.0') # Use Decimal for precision
        dtype = code_data['discount_type']
        value = Decimal(str(code_data['value'])) # Ensure value is Decimal
        current_total_decimal = Decimal(str(current_total)) # Ensure total is Decimal

        if dtype == 'percentage':
            discount_amount = (current_total_decimal * value) / Decimal('100.0')
        elif dtype == 'fixed':
            discount_amount = value
        else:
            logger.error(f"Unknown discount type '{dtype}' for code {code_data['code']}")
            return False, "Internal error processing discount type.", None

        # Clamp discount to not exceed total and round
        discount_amount = min(discount_amount, current_total_decimal)
        final_total = current_total_decimal - discount_amount

        # Round final amounts to 2 decimal places for currency
        discount_amount = discount_amount.quantize(Decimal("0.01"))
        final_total = final_total.quantize(Decimal("0.01"))

        details = {
            'code': code_data['code'], 'type': dtype, 'value': float(value),
            'discount_amount': float(discount_amount), # return float for compatibility
            'final_total': float(final_total) # return float
        }
        # Success message (Plain text)
        code_display = code_data['code']
        value_str_display = format_discount_value(dtype, float(value))
        amount_str_display = format_currency(float(discount_amount))
        message = f"Code '{code_display}' ({value_str_display}) applied. Discount: -{amount_str_display} EUR"
        return True, message, details

    except sqlite3.Error as e:
        logger.error(f"DB error validating discount code '{code_text}': {e}", exc_info=True)
        return False, "Database error validating code.", None
    except Exception as e:
         logger.error(f"Unexpected error validating code '{code_text}': {e}", exc_info=True)
         return False, "An unexpected error occurred.", None
    finally:
        if conn: conn.close() # Close connection if opened

# --- Other Utility Functions (Keep as is) ---
def format_currency(value):
    """Formats a number as currency (EUR) with 2 decimal places."""
    try:
        decimal_value = Decimal(str(value))
        # Round to 2 decimal places using standard rounding
        rounded_value = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # Format as string with 2 decimal places
        return f"{rounded_value:.2f}"
    except (ValueError, TypeError, InvalidOperation) as e:
        logger.warning(f"Could not format currency: {value}, Error: {e}")
        return "0.00" # Default fallback

def format_discount_value(dtype, value): # Keep as is
    """Formats a discount value based on its type."""
    try:
        float_value = float(value)
        if dtype == 'percentage': return f"{float_value:.1f}%"
        elif dtype == 'fixed': return f"{format_currency(float_value)} EUR"
        return str(float_value)
    except (ValueError, TypeError): return "N/A"

def get_progress_bar(purchases): # Keep as is
    """Generates a simple text-based progress bar based on purchase count."""
    try:
        p = int(purchases)
        thresholds = [0, 2, 5, 8, 10] # Example thresholds for status/bar
        filled = min(sum(1 for t in thresholds if p >= t), 5) # Max 5 blocks
        return '[' + '🟩' * filled + '⬜️' * (5 - filled) + ']'
    except (ValueError, TypeError):
        return '[⬜️⬜️⬜️⬜️⬜️]' # Default bar

async def send_message_with_retry(bot: Bot, chat_id: int, text: str, **kwargs): # Keep as is
    """Sends a Telegram message with retries on rate limit errors."""
    max_retries = kwargs.pop('max_retries', 3) # Default 3 retries
    initial_delay = 1.0 # Seconds

    for attempt in range(max_retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except telegram_error.RetryAfter as e:
            # Specific handling for RetryAfter (rate limiting)
            retry_seconds = e.retry_after + 1 # Add a small buffer
            logger.warning(f"Rate limit hit sending to {chat_id}. Retrying after {retry_seconds}s. (Attempt {attempt + 1}/{max_retries})")
            if retry_seconds > 60: # Don't wait excessively long
                 logger.error(f"RetryAfter ({retry_seconds}s) > 60s. Aborting send to {chat_id}.")
                 return None
            await asyncio.sleep(retry_seconds)
        except (telegram_error.BadRequest, telegram_error.Unauthorized, telegram_error.NetworkError) as e:
            # Handle other common, potentially retryable errors or terminal errors
            logger.warning(f"{type(e).__name__} sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            # Unauthorized/BadRequest usually mean we can't send to this user, stop retrying
            if isinstance(e, (telegram_error.BadRequest, telegram_error.Unauthorized)):
                return None
            # For NetworkError, retry with exponential backoff
            if attempt < max_retries - 1:
                delay = initial_delay * (2 ** attempt)
                await asyncio.sleep(delay)
            else:
                logger.error(f"NetworkError persisted after {max_retries} attempts sending to {chat_id}.")
                return None # Failed after retries
        except Exception as e:
            # Handle unexpected errors
            logger.error(f"Unexpected error sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1:
                 delay = initial_delay * (2 ** attempt)
                 await asyncio.sleep(delay) # Exponential backoff for unexpected errors too
            else:
                 return None # Failed after retries

    logger.error(f"Failed to send message to {chat_id} after {max_retries} attempts.")
    return None

def get_date_range(period_key): # Keep as is
    """Calculates start and end ISO format datetime strings for reporting periods."""
    now = datetime.now()
    start, end = None, None
    try:
        if period_key == 'today':
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif period_key == 'yesterday':
            yesterday = now - timedelta(days=1)
            start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
            end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'week': # Current week (Mon-Now)
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif period_key == 'last_week': # Previous full week (Mon-Sun)
            start_of_this_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_last_week = start_of_this_week - timedelta(microseconds=1)
            start = (end_of_last_week - timedelta(days=end_of_last_week.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = end_of_last_week.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'month': # Current month (1st-Now)
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif period_key == 'last_month': # Previous full month
            start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_of_last_month = start_of_this_month - timedelta(microseconds=1)
            start = end_of_last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = end_of_last_month.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'year': # Current year (Jan 1st - Now)
            start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
        else: return None, None # Invalid period key

        # Return ISO formatted strings
        return start.isoformat(), end.isoformat()
    except Exception as e:
        logger.error(f"Error calculating date range for '{period_key}': {e}")
        return None, None

def get_user_status(purchases): # Keep as is
    """Determines user status based on purchase count."""
    try:
        p = int(purchases)
        if p >= 10: return "VIP 👑"
        elif p >= 5: return "Regular ⭐"
        else: return "New 🌱"
    except (ValueError, TypeError):
        return "New 🌱" # Default status

def clear_expired_basket(context: ContextTypes.DEFAULT_TYPE, user_id: int): # Keep as is
    """Clears expired items from a user's basket (DB and context). Synchronous DB access."""
    if 'basket' not in context.user_data: context.user_data['basket'] = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        basket_str = result['basket'] if result else ''

        if not basket_str:
            # Ensure context is also cleared if DB is empty
            if context.user_data.get('basket'): context.user_data['basket'] = []
            if context.user_data.get('applied_discount'): context.user_data.pop('applied_discount', None)
            c.execute("COMMIT") # Commit even if no changes needed to end transaction
            return

        items = basket_str.split(',')
        current_time = time.time()
        valid_items_str = []
        valid_items_ctx = [] # Build new context list
        expired_ids = Counter() # Count how many reservations to release per product ID
        expired = False

        # Fetch prices for items potentially in basket to store in context
        potential_prod_ids = []
        for item_str in items:
             if ':' in item_str:
                 try: potential_prod_ids.append(int(item_str.split(':')[0]))
                 except ValueError: pass
        product_prices = {}
        if potential_prod_ids:
             # Deduplicate IDs before querying
             unique_ids = list(set(potential_prod_ids))
             placeholders = ','.join('?' * len(unique_ids))
             # Use column names
             c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", unique_ids)
             product_prices = {row['id']: Decimal(str(row['price'])) for row in c.fetchall()} # Use Decimal

        for item_str in items:
            if not item_str: continue
            try:
                pid_str, t_str = item_str.split(':')
                pid = int(pid_str)
                timestamp = float(t_str)

                if current_time - timestamp <= BASKET_TIMEOUT:
                    valid_items_str.append(item_str)
                    # Add to new context list if price was found
                    if pid in product_prices:
                        valid_items_ctx.append({
                            "product_id": pid,
                            "price": float(product_prices[pid]), # Store price in context
                            "timestamp": timestamp
                        })
                    else:
                        logger.warning(f"clear_expired_basket: Price for product ID {pid} not found in DB for user {user_id}, item excluded from new context.")
                else:
                    expired_ids[pid] += 1
                    expired = True
            except (ValueError, IndexError):
                logger.warning(f"Malformed basket item '{item_str}' for user {user_id}, skipping.")

        if expired:
            # Update DB only if items actually expired
            c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (','.join(valid_items_str), user_id))
            if expired_ids:
                 # Release reservations for expired items
                 decrement_data = [(count, p_id) for p_id, count in expired_ids.items()]
                 c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
                 logger.info(f"User {user_id}: Released {sum(expired_ids.values())} expired reservations.")

        c.execute("COMMIT") # Commit changes (or lack thereof)

        # Update context with the valid items
        context.user_data['basket'] = valid_items_ctx

        # Clear discount if basket becomes empty
        if not valid_items_ctx and context.user_data.get('applied_discount'):
            context.user_data.pop('applied_discount', None)
            logger.info(f"Removed discount for user {user_id} as basket became empty after expiry check.")

    except sqlite3.Error as e:
        logger.error(f"SQLite error clearing expired basket for user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error clearing expired basket for user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback() # Rollback on unexpected error too
    finally:
        if conn: conn.close()


def clear_all_expired_baskets(): # Keep as is
    """Scheduled job: Clears expired items from all users' baskets."""
    logger.info("Running scheduled job: clear_all_expired_baskets")
    expired_reservations = Counter() # {product_id: count_to_decrement}
    users_to_update = [] # [(new_basket_string, user_id)]
    conn = None

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")

        # Fetch users with non-empty baskets
        c.execute("SELECT user_id, basket FROM users WHERE basket IS NOT NULL AND basket != ''")
        users_with_baskets = c.fetchall() # Fetches list of Row objects
        current_time = time.time()

        for user_row in users_with_baskets:
            user_id, basket_str = user_row['user_id'], user_row['basket']
            items = basket_str.split(',')
            valid_items = []
            user_expired = False

            for item_str in items:
                if not item_str: continue
                try:
                    pid_str, t_str = item_str.split(':')
                    pid = int(pid_str)
                    timestamp = float(t_str)

                    if current_time - timestamp <= BASKET_TIMEOUT:
                        valid_items.append(item_str)
                    else:
                        # Item expired
                        expired_reservations[pid] += 1
                        user_expired = True
                except (ValueError, IndexError):
                    logger.warning(f"Scheduled Job: Malformed basket item '{item_str}' for user {user_id}, skipping.")
                    # Keep malformed items? For now, yes, to avoid data loss. Or remove? Removing seems safer.
                    # valid_items.append(item_str) # Option: Keep malformed
                    pass # Option: Remove malformed

            if user_expired:
                # Add user to the list for batch update if their basket changed
                users_to_update.append((','.join(valid_items), user_id))

        # --- Batch Updates ---
        if users_to_update:
            c.executemany("UPDATE users SET basket = ? WHERE user_id = ?", users_to_update)
            logger.info(f"Scheduled clear: Updated {len(users_to_update)} user baskets.")

        if expired_reservations:
            decrement_data = [(count, p_id) for p_id, count in expired_reservations.items()]
            if decrement_data:
                c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
                logger.info(f"Scheduled clear: Released {sum(expired_reservations.values())} total expired reservations.")

        conn.commit() # Commit all changes together

    except sqlite3.Error as e:
        logger.error(f"SQLite error in scheduled job clear_all_expired_baskets: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error in scheduled job clear_all_expired_baskets: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()


def fetch_last_purchases(user_id, limit=10): # Keep as is
    """Fetches the last N purchases for a specific user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use column names
        c.execute("""
            SELECT purchase_date, product_name, product_size, price_paid
            FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC LIMIT ?
        """, (user_id, limit))
        # Return list of dicts directly
        return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"DB error fetching history for user {user_id}: {e}", exc_info=True)
        return []
    finally:
        if conn: conn.close()


def fetch_reviews(offset=0, limit=5): # Keep as is
    """Fetches reviews with usernames for display, handling pagination."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use column names
        c.execute("""
            SELECT r.review_id, r.user_id, r.review_text, r.review_date,
                   COALESCE(u.username, 'anonymous') as username
            FROM reviews r LEFT JOIN users u ON r.user_id = u.user_id
            ORDER BY r.review_date DESC LIMIT ? OFFSET ?
        """, (limit, offset))
        # Return list of dicts
        return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch reviews (offset={offset}, limit={limit}): {e}", exc_info=True)
        return []
    finally:
        if conn: conn.close()


# --- Placeholder Handler ---
async def handle_coming_soon(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): # Keep as is
    query = update.callback_query
    if query:
        try: await query.answer("This feature is coming soon!", show_alert=True)
        except Exception as e: logger.error(f"Error answering 'coming soon' callback: {e}")


# --- Initial Data Load (runs when module is imported) ---
# Ensure DB exists and schema is up-to-date before loading data
init_db()
# Load data into global variables
load_all_data()
