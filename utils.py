# --- START OF FILE utils.py ---

import sqlite3
import time
import os
import logging
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP # Use Decimal for financial calculations
import requests # Added for API calls

# --- Telegram Imports ---
from telegram import Update, Bot
from telegram.constants import ParseMode # Keep import but change default usage
import telegram.error as telegram_error
from telegram.ext import ContextTypes
# -------------------------
from telegram import helpers # Keep for potential other uses, but not escaping
from collections import Counter

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Render Disk Path Configuration ---
RENDER_DISK_MOUNT_PATH = '/mnt/data'
DATABASE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'shop.db')
MEDIA_DIR = os.path.join(RENDER_DISK_MOUNT_PATH, 'media')
BOT_MEDIA_JSON_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'bot_media.json')

# Ensure the base media directory exists on the disk when the script starts
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
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "") # NOWPayments API Key
# *** ADDED: NOWPAYMENTS IPN Secret Key ***
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
# *****************************************
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: ADMIN_ID = int(ADMIN_ID_RAW)
    except (ValueError, TypeError): logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not TOKEN: logger.critical("CRITICAL ERROR: TOKEN environment variable is missing."); raise SystemExit("TOKEN not set.")
if not NOWPAYMENTS_API_KEY: logger.critical("CRITICAL ERROR: NOWPAYMENTS_API_KEY environment variable is missing."); raise SystemExit("NOWPAYMENTS_API_KEY not set.")
# *** ADDED: Warning for missing IPN Secret ***
if not NOWPAYMENTS_IPN_SECRET: logger.warning("WARNING: NOWPAYMENTS_IPN_SECRET environment variable is missing. Webhook verification disabled (less secure).")
# *******************************************
if not WEBHOOK_URL: logger.critical("CRITICAL ERROR: WEBHOOK_URL environment variable is missing."); raise SystemExit("WEBHOOK_URL not set.")
if ADMIN_ID is None: logger.warning("ADMIN_ID not set or invalid. Primary admin features disabled.")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")
logger.info(f"Basket timeout set to {BASKET_TIMEOUT // 60} minutes.")
logger.info(f"NOWPayments IPN expected at: {WEBHOOK_URL}/webhook")
logger.info(f"Telegram webhook expected at: {WEBHOOK_URL}/telegram/{TOKEN}")


# --- Constants ---
THEMES = { # Keep themes as is
    "default": {"product": "💎", "basket": "🛒", "review": "📝"},
    "neon": {"product": "💎", "basket": "🛍️", "review": "✨"},
    "stealth": {"product": "🌑", "basket": "🛒", "review": "🌟"},
    "nature": {"product": "🌿", "basket": "🧺", "review": "🌸"}
}
LANGUAGES = { # Keep languages as is (ensure consistency with provided example)
    # --- English ---
    "en": {
        "native_name": "English",
        "payment_amount_too_low_api": "❌ Payment Amount Too Low: The equivalent of {target_eur_amount} EUR in {currency} \\({crypto_amount}\\) is below the minimum required by the payment provider \\({min_amount} {currency}\\)\\. Please try a higher EUR amount\\.", # Added specific error message & escaping
        "error_min_amount_fetch": "❌ Error: Could not retrieve minimum payment amount for {currency}\\. Please try again later or select a different currency\\.", # Added & escaping
        "invoice_title_refill": "*Top\\-Up Invoice Created*", # Use Markdown
        "min_amount_label": "*Minimum Amount:*", # New key
        "payment_address_label": "*Payment Address:*", # Use Markdown
        "amount_label": "*Amount:*", # Changed label slightly
        "expires_at_label": "*Expires At:*", # Use Markdown
        "send_warning_template": "⚠️ *Important:* Send *exactly* this amount of {asset} to this address\\.", # Updated wording, added Markdown & escaping
        "overpayment_note": "ℹ️ _Sending more than this amount is okay\\! Your balance will be credited based on the amount received after network confirmation\\._", # New key with Markdown & escaping
        "confirmation_note": "✅ Confirmation is automatic via webhook after network confirmation\\.", # Updated wording & escaping
        "error_estimate_failed": "❌ Error: Could not estimate crypto amount. Please try again or select a different currency.", # New error message
        "error_estimate_currency_not_found": "❌ Error: Currency {currency} not supported for estimation. Please select a different currency.", # New error message

        "welcome": "👋 Welcome, {username}!",
        "profile": "🎉 Your Profile\n\n👤 Status: {status} {progress_bar}\n💰 Balance: {balance} EUR\n📦 Total Purchases: {purchases}\n🛒 Basket Items: {basket}",
        "refill": "💸 Top Up Your Balance\n\nChoose a payment method below:",
        "reviews": "📝 Share Your Feedback!\n\nWe’d love to hear your thoughts! 😊",
        "price_list": "🏙️ Choose a City\n\nView available products by location:",
        "language": "🌐 Select Language\n\nPick your preferred language:",
        "added_to_basket": "✅ Item Reserved!\n\n{item} is in your basket for {timeout} minutes! ⏳",
        "pay": "💳 Total to Pay: {amount} EUR",
        "admin_menu": "🔧 Admin Panel\n\nManage the bot from here:",
        "admin_select_city": "🏙️ Select City to Edit\n\nChoose a city:",
        "admin_select_district": "🏘️ Select District in {city}\n\nPick a district:",
        "admin_select_type": "💎 Select Candy Type or Add New\n\nChoose or create a type:",
        "admin_choose_action": "📦 Manage {type} in {city}, {district}\n\nWhat would you like to do?",
        "basket_empty": "🛒 Your Basket is Empty!\n\nAdd items to start shopping! 😊",
        "insufficient_balance": "⚠️ Insufficient Balance!\n\nPlease top up to continue! 💸",
        "purchase_success": "🎉 Purchase Complete!\n\nYour pickup details are below! 🚚",
        "basket_cleared": "🗑️ Basket Cleared!\n\nStart fresh now! ✨",
        "payment_failed": "❌ Payment Failed!\n\nPlease try again or contact {support}. 📞",
        "support": "📞 Need Help?\n\nContact {support} for assistance!",
        "file_download_error": "❌ Error: Failed to Download Media\n\nPlease try again or contact {support}. ",
        "set_media_prompt_plain": "📸 Send a photo, video, or GIF to display above all messages:",
        "state_error": "❌ Error: Invalid State\n\nPlease start the 'Add New Product' process again from the Admin Panel.",
        "review_prompt": "🎉 Thank you for your purchase!\n\nWe’d love to hear your feedback. Would you like to leave a review now or later?",
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
        "your_basket_title": "Your Basket",
        "add_items_prompt": "Add items to start shopping!",
        "items_expired_note": "Items may have expired or were removed.",
        "expires_in_label": "Expires in",
        "remove_button_label": "Remove",
        "discount_applied_label": "Discount Applied",
        "discount_value_label": "Value",
        "discount_removed_note": "Discount code {code} removed: {reason}",
        "subtotal_label": "Subtotal",
        "total_label": "Total",
        "pay_now_button": "Pay Now",
        "clear_all_button": "Clear All",
        "remove_discount_button": "Remove Discount",
        "apply_discount_button": "Apply Discount Code",
        "shop_more_button": "Shop More",
        "home_button": "Home",
        "view_basket_button": "View Basket",
        "clear_basket_button": "Clear Basket",
        "back_options_button": "Back to Options",
        "purchase_history_button": "Purchase History",
        "back_profile_button": "Back to Profile",
        "language_set_answer": "Language set to {lang}!",
        "error_saving_language": "Error saving language preference.",
        "invalid_language_answer": "Invalid language selected.",
        "back_button": "Back",
        "no_cities_for_prices": "No cities available to view prices for.",
        "price_list_title": "Price List",
        "select_city_prices_prompt": "Select a city to view available products and prices:",
        "error_city_not_found": "Error: City not found.",
        "price_list_title_city": "Price List: {city_name}",
        "no_products_in_city": "No products currently available in this city.",
        "available_label": "available",
        "available_label_short": "Av",
        "back_city_list_button": "Back to City List",
        "message_truncated_note": "Message truncated due to length limit. Use 'Shop' for full details.",
        "error_loading_prices_db": "Error: Failed to Load Price List for {city_name}",
        "error_displaying_prices": "Error displaying price list.",
        "error_unexpected_prices": "Error: An unexpected issue occurred while generating the price list.",
        "view_reviews_button": "View Reviews",
        "leave_review_button": "Leave a Review",
        "enter_review_prompt": "Please type your review message and send it.",
        "cancel_button": "Cancel",
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
        "unknown_date_label": "Unknown Date",
        "error_displaying_review": "Error displaying review",
        "error_updating_review_list": "Error updating review list.",
        "discount_no_items": "Your basket is empty. Add items first.",
        "enter_discount_code_prompt": "Please enter your discount code:",
        "enter_code_answer": "Enter code in chat.",
        "no_code_entered": "No code entered.",
        "send_text_please": "Please send the discount code as text.",
        "error_calculating_total": "Error calculating basket total.",
        "returning_to_basket": "Returning to basket.",
        "basket_empty_no_discount": "Your basket is empty. Cannot apply discount code.",
        "success_label": "Success!",
        "basket_already_empty": "Basket is already empty.",
        "crypto_payment_disabled": "Top Up is currently disabled.",
        "top_up_title": "Top Up Balance",
        "enter_refill_amount_prompt": "Please reply with the amount in EUR you wish to add to your balance (e.g., 10 or 25.50).",
        "min_top_up_note": "Minimum top up: {amount} EUR",
        "enter_amount_answer": "Enter the top-up amount.",
        "error_occurred_answer": "An error occurred. Please try again.",
        "send_amount_as_text": "Please send the amount as text (e.g., 10 or 25.50).",
        "amount_too_low_msg": "Amount too low. Minimum top up is {amount} EUR. Please enter a higher amount.",
        "amount_too_high_msg": "Amount too high. Please enter a lower amount.",
        "invalid_amount_format_msg": "Invalid amount format. Please enter a number (e.g., 10 or 25.50).",
        "unexpected_error_msg": "An unexpected error occurred. Please try again later.",
        "choose_crypto_prompt": "You want to top up {amount} EUR. Please choose the cryptocurrency you want to pay with:",
        "cancel_top_up_button": "Cancel Top Up",
        "purchase_history_title": "Purchase History",
        "no_purchases_yet": "You haven't made any purchases yet.",
        "recent_purchases_title": "Your Recent Purchases",
        "back_types_button": "Back to Types",
        "no_districts_available": "No districts available yet for this city.",
        "choose_district_prompt": "Choose a district:",
        "back_cities_button": "Back to Cities",
        "error_location_mismatch": "Error: Location data mismatch.",
        "drop_unavailable": "Drop Unavailable! This option just sold out or was reserved by someone else.",
        "price_label": "Price",
        "available_label_long": "Available",
        "add_to_basket_button": "Add to Basket",
        "error_loading_details": "Error: Failed to Load Product Details",
        "expires_label": "Expires",
        "error_adding_db": "Error: Database issue adding item to basket.",
        "error_adding_unexpected": "Error: An unexpected issue occurred.",
        "profile_title": "Your Profile",
        "no_cities_available": "No cities available at the moment. Please check back later.",
        "select_location_prompt": "Select your location:",
        "choose_city_title": "Choose a City",
        "preparing_invoice": "⏳ Preparing your payment invoice...",
        "failed_invoice_creation": "❌ Failed to create payment invoice. This could be a temporary issue with the payment provider or an API key problem. Please try again later or contact support.",
        "calculating_amount": "⏳ Calculating required amount and preparing invoice...",
        "error_getting_rate": "❌ Error: Could not get exchange rate for {asset}. Please try another currency or contact support.",
        "error_preparing_payment": "❌ An error occurred while preparing the payment. Please try again later.",
        "please_pay_label": "Please pay",
        "target_value_label": "Target Value",
        "pay_now_button_nowpayments": "Pay via NOWPayments",
        "top_up_success_title": "✅ Top Up Successful!",
        "amount_added_label": "Amount Added",
        "new_balance_label": "Your new balance",
        "sold_out_note": "⚠️ Note: The following items became unavailable during processing and were not included: {items}. You were not charged for these.",
        "balance_changed_error": "❌ Transaction failed: Your balance changed. Please check your balance and try again.",
        "order_failed_all_sold_out_balance": "❌ Order Failed: All items in your basket became unavailable during processing. Your balance was not charged.",
        "error_processing_purchase_contact_support": "❌ An error occurred while processing your purchase. Please contact support.",
        "back_basket_button": "Back to Basket",
        "language": "🌐 Select Language:",
        "no_items_of_type": "No items of this type currently available here.",
        "available_options_prompt": "Available options:",
        "error_loading_products": "Error: Failed to Load Products",
        "error_unexpected": "An unexpected error occurred",
        "error_district_city_not_found": "Error: District or city not found.",
        "error_loading_types": "Error: Failed to Load Product Types",
        "no_types_available": "No product types currently available here.",
        "select_type_prompt": "Select product type:",
        "no_districts_available": "No districts available yet for this city.",
        "back_districts_button": "Back to Districts",
        "back_cities_button": "Back to Cities",
        "admin_select_city": "🏙️ Select City to Edit:",
        "admin_select_district": "🏘️ Select District in {city}:",
        "admin_select_type": "💎 Select Product Type:",
        "admin_choose_action": "📦 Manage {type} in {city}/{district}:",
        "error_nowpayments_api": "❌ Payment API Error: Could not create payment. Please try again later or contact support.",
        "error_invalid_nowpayments_response": "❌ Payment API Error: Invalid response received. Please contact support.",
        "error_nowpayments_api_key": "❌ Payment API Error: Invalid API key. Please contact support.",
        "payment_pending_db_error": "❌ Database Error: Could not record pending payment. Please contact support.",
        "webhook_processing_error": "Webhook Error: Could not process payment update {payment_id}.",
        "webhook_db_update_failed": "Critical Error: Payment {payment_id} confirmed, but DB balance update failed for user {user_id}. Manual action required.",
        "webhook_pending_not_found": "Webhook Warning: Received update for payment ID {payment_id}, but no pending deposit found in DB.",
        "webhook_price_fetch_error": "Webhook Error: Could not fetch price for {currency} to confirm EUR value for payment {payment_id}.", # Kept for reference, but shouldn't happen now
        "payment_cancelled_or_expired": "Payment Status: Your payment ({payment_id}) was cancelled or expired.",
        "set_media_prompt_plain": "📸 Send a photo, video, or GIF to display above all messages:",
        "state_error": "❌ Error: Invalid State. Please start the 'Add New Product' process again from the Admin Panel.",
        "review_prompt": "🎉 Thank you for your purchase! We’d love to hear your feedback. Would you like to leave a review now or later?",
        "payment_failed": "❌ Payment Failed! Please try again or contact {support}. 📞",
        "support": "📞 Need Help? Contact {support}!",
        "file_download_error": "❌ Error: Failed to Download Media. Please try again or contact {support}.",
        "admin_enter_type_emoji": "✍️ Please reply with a single emoji for the product type:", # New
        "admin_type_emoji_set": "Emoji set to {emoji}.", # New
        "admin_edit_type_emoji_button": "✏️ Change Emoji", # New
        "admin_invalid_emoji": "❌ Invalid input. Please send a single emoji.", # New
        "admin_type_emoji_updated": "✅ Emoji updated successfully for {type_name}!", # New
        "admin_edit_type_menu": "🧩 Editing Type: {type_name}\n\nCurrent Emoji: {emoji}\n\nWhat would you like to do?", # New
    },
    # --- Add other languages similarly ---
    "lt": {
        "native_name": "Lietuvių",
        # ... existing Lithuanian translations ...
        "admin_enter_type_emoji": "✍️ Atsakykite vienu jaustuku produkto tipui:", # New
        "admin_type_emoji_set": "Jaustukas nustatytas į {emoji}.", # New
        "admin_edit_type_emoji_button": "✏️ Keisti jaustuką", # New
        "admin_invalid_emoji": "❌ Neteisinga įvestis. Prašome siųsti vieną jaustuką.", # New
        "admin_type_emoji_updated": "✅ Jaustukas sėkmingai atnaujintas tipui {type_name}!", # New
        "admin_edit_type_menu": "🧩 Redaguojamas tipas: {type_name}\n\nDabartinis jaustukas: {emoji}\n\nKą norėtumėte daryti?", # New
        # ... rest of Lithuanian translations ...
    },
    "ru": {
        "native_name": "Русский",
        # ... existing Russian translations ...
        "admin_enter_type_emoji": "✍️ Пожалуйста, ответьте одним эмодзи для этого типа товара:", # New
        "admin_type_emoji_set": "Эмодзи установлен на {emoji}.", # New
        "admin_edit_type_emoji_button": "✏️ Изменить эмодзи", # New
        "admin_invalid_emoji": "❌ Неверный ввод. Пожалуйста, отправьте один эмодзи.", # New
        "admin_type_emoji_updated": "✅ Эмодзи успешно обновлен для типа {type_name}!", # New
        "admin_edit_type_menu": "🧩 Редактирование типа: {type_name}\n\nТекущий эмодзи: {emoji}\n\nЧто вы хотите сделать?", # New
        # ... rest of Russian translations ...
    }
}

MIN_DEPOSIT_EUR = Decimal('5.00') # Minimum deposit amount in EUR
NOWPAYMENTS_API_URL = "https://api.nowpayments.io"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
# Optional: Adjust this slightly below 1 if NOWPayments takes a fee not reflected in the exchange rate.
# Example: 0.995 = Deduct 0.5% to cover potential fees. Set to 1.0 for no adjustment.
FEE_ADJUSTMENT = Decimal('1.0')

# --- Global Data Variables ---
CITIES = {}
DISTRICTS = {}
# *** CHANGE: PRODUCT_TYPES is now a dictionary: {name: emoji} ***
PRODUCT_TYPES = {}
DEFAULT_PRODUCT_EMOJI = "💎" # Fallback emoji
SIZES = ["2g", "5g"]
BOT_MEDIA = {'type': None, 'path': None}
currency_price_cache = {} # Simple in-memory cache for CoinGecko prices
min_amount_cache = {} # Simple in-memory cache for NOWPayments minimum amounts
CACHE_EXPIRY_SECONDS = 900 # Cache prices/minimums for 15 minutes (Increased from 300)


# --- Database Connection Helper ---
def get_db_connection():
    """Returns a connection to the SQLite database using the configured path."""
    try:
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir:
            try: os.makedirs(db_dir, exist_ok=True)
            except OSError as e: logger.warning(f"Could not create DB dir {db_dir}: {e}")
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row # Use Row factory for dict-like access
        return conn
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR connecting to database at {DATABASE_PATH}: {e}")
        raise SystemExit(f"Failed to connect to database: {e}")


# --- Database Initialization ---
def init_db():
    """Initializes the database schema ONLY."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # users table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0,
                total_purchases INTEGER DEFAULT 0, basket TEXT DEFAULT '',
                language TEXT DEFAULT 'en', theme TEXT DEFAULT 'default'
            )''')
            # cities table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
            )''')
            # districts table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS districts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city_id INTEGER NOT NULL, name TEXT NOT NULL,
                FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE, UNIQUE (city_id, name)
            )''')

            # *** CHANGE: Add emoji column to product_types ***
            c.execute(f'''CREATE TABLE IF NOT EXISTS product_types (
                name TEXT PRIMARY KEY NOT NULL,
                emoji TEXT DEFAULT '{DEFAULT_PRODUCT_EMOJI}'
            )''')
            # *** Add emoji column if it doesn't exist (for existing databases) ***
            try:
                c.execute(f"ALTER TABLE product_types ADD COLUMN emoji TEXT DEFAULT '{DEFAULT_PRODUCT_EMOJI}'")
                logger.info("Added 'emoji' column to product_types table.")
            except sqlite3.OperationalError as alter_e:
                 if "duplicate column name: emoji" in str(alter_e): pass # Column already exists, ignore
                 else: raise # Re-raise other operational errors

            # products table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT NOT NULL, district TEXT NOT NULL,
                product_type TEXT NOT NULL, size TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL,
                available INTEGER DEFAULT 1, reserved INTEGER DEFAULT 0, original_text TEXT,
                added_by INTEGER, added_date TEXT
                -- No direct FK to product_types.name needed, handled by application logic
            )''')
            # product_media table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS product_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                media_type TEXT NOT NULL, file_path TEXT UNIQUE NOT NULL, telegram_file_id TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
            )''')
            # purchases table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER,
                product_name TEXT NOT NULL, product_type TEXT NOT NULL, product_size TEXT NOT NULL,
                price_paid REAL NOT NULL, city TEXT NOT NULL, district TEXT NOT NULL, purchase_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
            )''')
            # reviews table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                review_text TEXT NOT NULL, review_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # discount_codes table (no changes)
            c.execute('''CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
                discount_type TEXT NOT NULL CHECK(discount_type IN ('percentage', 'fixed')),
                value REAL NOT NULL, is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
                max_uses INTEGER DEFAULT NULL, uses_count INTEGER DEFAULT 0,
                created_date TEXT NOT NULL, expiry_date TEXT DEFAULT NULL
            )''')

            # pending_deposits table (MODIFIED: Added expected_crypto_amount)
            c.execute('''CREATE TABLE IF NOT EXISTS pending_deposits (
                payment_id TEXT PRIMARY KEY NOT NULL,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                target_eur_amount REAL NOT NULL,
                expected_crypto_amount REAL NOT NULL, -- <<< ADDED COLUMN
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # Add column if it doesn't exist (for existing databases)
            try:
                # Try adding with NOT NULL and a default if possible, safer but might fail on old SQLite
                # c.execute("ALTER TABLE pending_deposits ADD COLUMN expected_crypto_amount REAL NOT NULL DEFAULT 0.0")
                # Safer approach: Add column allowing NULL, handle NULL on retrieval
                c.execute("ALTER TABLE pending_deposits ADD COLUMN expected_crypto_amount REAL")
                logger.info("Added 'expected_crypto_amount' column to pending_deposits table.")
            except sqlite3.OperationalError as alter_e:
                 if "duplicate column name: expected_crypto_amount" in str(alter_e): pass # Column already exists
                 else: raise


            # Create Indices (no changes)
            c.execute("CREATE INDEX IF NOT EXISTS idx_product_media_product_id ON product_media(product_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_districts_city_name ON districts(city_id, name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_products_location_type ON products(city, district, product_type)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_discount_code_unique ON discount_codes(code)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_deposits_user_id ON pending_deposits(user_id)") # Index for new table

            conn.commit()
            logger.info(f"Database schema at {DATABASE_PATH} initialized/verified successfully.")
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR: Database initialization failed for {DATABASE_PATH}: {e}", exc_info=True)
        raise SystemExit("Database initialization failed.")


# --- Pending Deposit DB Helpers (Synchronous) ---
# MODIFIED: Add expected_crypto_amount parameter
def add_pending_deposit(payment_id: str, user_id: int, currency: str, target_eur_amount: float, expected_crypto_amount: float):
    """Adds a record for a pending NOWPayments deposit."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO pending_deposits (payment_id, user_id, currency, target_eur_amount, expected_crypto_amount, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (payment_id, user_id, currency.lower(), target_eur_amount, expected_crypto_amount, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            logger.info(f"Added pending deposit {payment_id} for user {user_id} ({target_eur_amount:.2f} EUR / exp: {expected_crypto_amount} {currency}).") # Log expected amount
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Attempted to add duplicate pending deposit ID: {payment_id}")
        return False # Indicate failure due to duplication
    except sqlite3.Error as e:
        logger.error(f"DB error adding pending deposit {payment_id} for user {user_id}: {e}", exc_info=True)
        return False

# MODIFIED: Retrieve the new column
def get_pending_deposit(payment_id: str):
    """Retrieves pending deposit details by payment ID."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # Select the new column as well
            c.execute("SELECT user_id, currency, target_eur_amount, expected_crypto_amount FROM pending_deposits WHERE payment_id = ?", (payment_id,))
            row = c.fetchone()
            # Handle potential NULL for expected_crypto_amount if ALTER ADD was recent
            if row:
                row_dict = dict(row)
                if row_dict.get('expected_crypto_amount') is None:
                    logger.warning(f"Pending deposit {payment_id} has NULL expected_crypto_amount. Using 0.0.")
                    row_dict['expected_crypto_amount'] = 0.0
                return row_dict
            else:
                return None
    except sqlite3.Error as e:
        logger.error(f"DB error fetching pending deposit {payment_id}: {e}", exc_info=True)
        return None


def remove_pending_deposit(payment_id: str):
    """Removes a pending deposit record by payment ID."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            result = c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,))
            conn.commit()
            if result.rowcount > 0:
                logger.info(f"Removed pending deposit record for payment ID: {payment_id}")
                return True
            else:
                # This isn't necessarily an error, could have been removed already
                logger.info(f"No pending deposit record found to remove for payment ID: {payment_id}")
                return False
    except sqlite3.Error as e:
        logger.error(f"DB error removing pending deposit {payment_id}: {e}", exc_info=True)
        return False


# --- Data Loading Functions (Synchronous) ---
def load_cities():
    cities_data = {}
    try:
        with get_db_connection() as conn: c = conn.cursor(); c.execute("SELECT id, name FROM cities ORDER BY name"); cities_data = {str(row['id']): row['name'] for row in c.fetchall()}
    except sqlite3.Error as e: logger.error(f"Failed to load cities: {e}")
    return cities_data

def load_districts():
    districts_data = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT d.city_id, d.id, d.name FROM districts d ORDER BY d.city_id, d.name")
            for row in c.fetchall(): city_id_str = str(row['city_id']); districts_data.setdefault(city_id_str, {})[str(row['id'])] = row['name']
    except sqlite3.Error as e: logger.error(f"Failed to load districts: {e}")
    return districts_data

# *** CHANGE: Load product types AND emojis ***
def load_product_types():
    product_types_dict = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # Fetch both name and emoji, providing a default if emoji is NULL
            c.execute(f"SELECT name, COALESCE(emoji, '{DEFAULT_PRODUCT_EMOJI}') as emoji FROM product_types ORDER BY name")
            product_types_dict = {row['name']: row['emoji'] for row in c.fetchall()}
    except sqlite3.Error as e:
        logger.error(f"Failed to load product types and emojis: {e}")
    return product_types_dict

def load_all_data():
    """Loads all dynamic data, modifying global variables IN PLACE."""
    global CITIES, DISTRICTS, PRODUCT_TYPES
    logger.info("Starting load_all_data (in-place update)...")
    try:
        cities_data = load_cities()
        districts_data = load_districts()
        # *** CHANGE: Load the dict ***
        product_types_dict = load_product_types()

        CITIES.clear(); CITIES.update(cities_data)
        DISTRICTS.clear(); DISTRICTS.update(districts_data)
        # *** CHANGE: Update the dict ***
        PRODUCT_TYPES.clear(); PRODUCT_TYPES.update(product_types_dict)

        logger.info(f"Loaded (in-place) {len(CITIES)} cities, {sum(len(d) for d in DISTRICTS.values())} districts, {len(PRODUCT_TYPES)} product types.")
    except Exception as e:
        logger.error(f"Error during load_all_data (in-place): {e}", exc_info=True)
        CITIES.clear(); DISTRICTS.clear(); PRODUCT_TYPES.clear()


# --- Bot Media Loading (from specified path on disk) ---
# Try to load from the persistent disk path
if os.path.exists(BOT_MEDIA_JSON_PATH):
    try:
        with open(BOT_MEDIA_JSON_PATH, 'r') as f: BOT_MEDIA = json.load(f)
        logger.info(f"Loaded BOT_MEDIA from {BOT_MEDIA_JSON_PATH}: {BOT_MEDIA}")
        if BOT_MEDIA.get("path"):
            filename = os.path.basename(BOT_MEDIA["path"]); correct_path = os.path.join(MEDIA_DIR, filename)
            if BOT_MEDIA["path"] != correct_path: logger.warning(f"Correcting BOT_MEDIA path from {BOT_MEDIA['path']} to {correct_path}"); BOT_MEDIA["path"] = correct_path
    except Exception as e: logger.warning(f"Could not load/parse {BOT_MEDIA_JSON_PATH}: {e}. Using default BOT_MEDIA.")
else: logger.info(f"{BOT_MEDIA_JSON_PATH} not found. Bot starting without default media.")


# --- Utility Functions ---
def format_currency(value):
    """Formats a numeric value into a currency string (EUR)."""
    try: return f"{Decimal(str(value)):.2f}"
    except (ValueError, TypeError): logger.warning(f"Could format currency {value}"); return "0.00"

def format_discount_value(dtype, value):
    """Formats discount value for display (PLAIN TEXT)."""
    try:
        if dtype == 'percentage': return f"{Decimal(str(value)):.1f}%"
        elif dtype == 'fixed': return f"{format_currency(value)} EUR"
        return str(value)
    except (ValueError, TypeError): logger.warning(f"Could not format discount {dtype} {value}"); return "N/A"

def get_progress_bar(purchases):
    """Generates a simple text progress bar for user status (PLAIN TEXT)."""
    try:
        p_int = int(purchases); thresholds = [0, 2, 5, 8, 10]
        filled = min(sum(1 for t in thresholds if p_int >= t), 5)
        return '[' + '🟩' * filled + '⬜️' * (5 - filled) + ']'
    except (ValueError, TypeError): return '[⬜️⬜️⬜️⬜️⬜️]'

# --- CORRECTED send_message_with_retry ---
async def send_message_with_retry(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    max_retries=3,
    parse_mode=None,
    disable_web_page_preview=False
):
    """Sends a Telegram message with retries (defaults to plain text)."""
    for attempt in range(max_retries):
        try:
            # Successful send, return the message object
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        except telegram_error.BadRequest as e:
            logger.warning(f"BadRequest sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}. Text: {text[:100]}...")
            if "chat not found" in str(e).lower() or "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                logger.error(f"Unrecoverable BadRequest sending to {chat_id}: {e}. Aborting retries.")
                return None # Unrecoverable error, stop retrying
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (2 ** attempt))
                continue # Go to the next attempt
            else:
                logger.error(f"Max retries reached for BadRequest sending to {chat_id}: {e}")
                break # Exit loop after max retries for this exception
        except telegram_error.RetryAfter as e:
            retry_seconds = e.retry_after + 1
            logger.warning(f"Rate limit hit sending to {chat_id}. Retrying after {retry_seconds} seconds.")
            if retry_seconds > 60:
                 logger.error(f"RetryAfter requested > 60s ({retry_seconds}s). Aborting for chat {chat_id}.")
                 return None # Abort if retry time is too long
            await asyncio.sleep(retry_seconds)
            continue # Go to the next attempt (implicitly handled by loop)
        except telegram_error.NetworkError as e:
            logger.warning(f"NetworkError sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (2 ** attempt))
                continue # Go to the next attempt
            else:
                logger.error(f"Max retries reached for NetworkError sending to {chat_id}: {e}")
                break # Exit loop after max retries
        except telegram_error.Unauthorized:
            logger.warning(f"Unauthorized error sending to {chat_id}. User may have blocked the bot. Aborting.")
            return None # Unrecoverable error
        except Exception as e:
            logger.error(f"Unexpected error sending message to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (2 ** attempt))
                continue # Go to the next attempt
            else:
                logger.error(f"Max retries reached after unexpected error sending to {chat_id}: {e}")
                break # Exit loop after max retries

    # If the loop completes without returning successfully
    logger.error(f"Failed to send message to {chat_id} after {max_retries} attempts: {text[:100]}...")
    return None
# --- END CORRECTED send_message_with_retry ---

def get_date_range(period_key):
    """Calculates start and end ISO format datetime strings based on a period key."""
    now = datetime.now()
    try:
        if period_key == 'today': start = now.replace(hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'yesterday': yesterday = now - timedelta(days=1); start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0); end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'week': start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'last_week': start_of_this_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end_of_last_week = start_of_this_week - timedelta(microseconds=1); start = (end_of_last_week - timedelta(days=end_of_last_week.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end = end_of_last_week.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'month': start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'last_month': first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end_of_last_month = first_of_this_month - timedelta(microseconds=1); start = end_of_last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end = end_of_last_month.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'year': start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0); end = now
        else: return None, None
        # Convert to UTC ISO format for DB comparison
        # Make sure start and end are timezone-aware before converting
        if start.tzinfo is None: start = start.astimezone()
        if end.tzinfo is None: end = end.astimezone()
        return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
    except Exception as e: logger.error(f"Error calculating date range for '{period_key}': {e}"); return None, None

def get_user_status(purchases):
    """Determines user status ('New', 'Regular', 'VIP') based on purchase count."""
    try:
        p_int = int(purchases)
        if p_int >= 10: return "VIP 👑"
        elif p_int >= 5: return "Regular ⭐"
        else: return "New 🌱"
    except (ValueError, TypeError): return "New 🌱"

def clear_expired_basket(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Clears expired items from a user's basket in DB and user_data. (Synchronous)"""
    if 'basket' not in context.user_data: context.user_data['basket'] = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone(); basket_str = result['basket'] if result else ''
        if not basket_str:
            if context.user_data.get('basket'): context.user_data['basket'] = []
            if context.user_data.get('applied_discount'): context.user_data.pop('applied_discount', None)
            c.execute("COMMIT"); return
        items = basket_str.split(',')
        current_time = time.time(); valid_items_str_list = []; valid_items_userdata_list = []
        expired_product_ids_counts = Counter(); expired_items_found = False
        potential_prod_ids = []
        # Safely parse potential product IDs
        for item_part in items:
            if item_part and ':' in item_part:
                try:
                    potential_prod_ids.append(int(item_part.split(':')[0]))
                except ValueError:
                    logger.warning(f"Invalid product ID format in basket string '{item_part}' for user {user_id}")
        product_prices = {}
        if potential_prod_ids:
             placeholders = ','.join('?' * len(potential_prod_ids))
             c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", potential_prod_ids)
             # Ensure prices are Decimal
             product_prices = {row['id']: Decimal(str(row['price'])) for row in c.fetchall()}
        for item_str in items:
            if not item_str: continue
            try:
                prod_id_str, ts_str = item_str.split(':'); prod_id = int(prod_id_str); ts = float(ts_str)
                if current_time - ts <= BASKET_TIMEOUT:
                    valid_items_str_list.append(item_str)
                    if prod_id in product_prices: valid_items_userdata_list.append({"product_id": prod_id, "price": product_prices[prod_id], "timestamp": ts})
                    else: logger.warning(f"P{prod_id} price not found during basket validation (user {user_id}).")
                else: expired_product_ids_counts[prod_id] += 1; expired_items_found = True
            except (ValueError, IndexError) as e: logger.warning(f"Malformed item '{item_str}' in basket for user {user_id}: {e}")
        if expired_items_found:
            new_basket_str = ','.join(valid_items_str_list)
            c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (new_basket_str, user_id))
            if expired_product_ids_counts:
                decrement_data = [(count, pid) for pid, count in expired_product_ids_counts.items()]
                c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
        c.execute("COMMIT")
        context.user_data['basket'] = valid_items_userdata_list
        if not valid_items_userdata_list and context.user_data.get('applied_discount'):
            context.user_data.pop('applied_discount', None); logger.info(f"Cleared discount for user {user_id} as basket became empty.")
    except sqlite3.Error as e: logger.error(f"SQLite error clearing basket user {user_id}: {e}", exc_info=True); conn.rollback() if conn and conn.in_transaction else None
    except Exception as e: logger.error(f"Unexpected error clearing basket user {user_id}: {e}", exc_info=True)
    finally: conn.close() if conn else None

def clear_all_expired_baskets():
    """Scheduled job: Clears expired items from all users' baskets. (Synchronous)"""
    logger.info("Running scheduled job: clear_all_expired_baskets")
    all_expired_product_counts = Counter(); user_basket_updates = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor(); c.execute("BEGIN"); c.execute("SELECT user_id, basket FROM users WHERE basket IS NOT NULL AND basket != ''")
        users_with_baskets = c.fetchall(); current_time = time.time()
        for user_row in users_with_baskets:
            user_id = user_row['user_id']; basket_str = user_row['basket']; items = basket_str.split(','); valid_items_str_list = []; user_had_expired = False
            for item_str in items:
                if not item_str: continue
                try:
                    prod_id_str, ts_str = item_str.split(':'); prod_id = int(prod_id_str); ts = float(ts_str)
                    if current_time - ts <= BASKET_TIMEOUT: valid_items_str_list.append(item_str)
                    else: all_expired_product_counts[prod_id] += 1; user_had_expired = True
                except (ValueError, IndexError) as e: logger.warning(f"Malformed item '{item_str}' user {user_id} global clear: {e}")
            if user_had_expired: new_basket_str = ','.join(valid_items_str_list); user_basket_updates.append((new_basket_str, user_id))
        if user_basket_updates: c.executemany("UPDATE users SET basket = ? WHERE user_id = ?", user_basket_updates); logger.info(f"Scheduled clear: Updated baskets for {len(user_basket_updates)} users.")
        if all_expired_product_counts:
            decrement_data = [(count, pid) for pid, count in all_expired_product_counts.items()]
            if decrement_data: c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data); total_released = sum(all_expired_product_counts.values()); logger.info(f"Scheduled clear: Released {total_released} expired product reservations.")
        conn.commit()
    except sqlite3.Error as e: logger.error(f"SQLite error in scheduled job clear_all_expired_baskets: {e}", exc_info=True); conn.rollback() if conn and conn.in_transaction else None
    except Exception as e: logger.error(f"Unexpected error in clear_all_expired_baskets: {e}", exc_info=True)
    finally: conn.close() if conn else None

def fetch_last_purchases(user_id, limit=10):
    """Fetches the last N purchases for a specific user. (Synchronous)"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT purchase_date, product_name, product_size, price_paid FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC LIMIT ?", (user_id, limit))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"DB error fetching purchase history user {user_id}: {e}", exc_info=True); return []

def fetch_reviews(offset=0, limit=5):
    """Fetches reviews with usernames for display, handling pagination. (Synchronous)"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT r.review_id, r.user_id, r.review_text, r.review_date, COALESCE(u.username, 'anonymous') as username FROM reviews r LEFT JOIN users u ON r.user_id = u.user_id ORDER BY r.review_date DESC LIMIT ? OFFSET ?", (limit, offset))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to fetch reviews (offset={offset}, limit={limit}): {e}", exc_info=True); return []


# --- API Helpers ---

# --- Get NOWPayments Minimum Amount ---
def get_nowpayments_min_amount(currency_code: str) -> Decimal | None:
    """Gets the minimum payment amount for a specific currency from NOWPayments API with caching."""
    currency_code_lower = currency_code.lower()
    now = time.time()

    # Check cache first
    if currency_code_lower in min_amount_cache:
        min_amount, timestamp = min_amount_cache[currency_code_lower]
        if now - timestamp < CACHE_EXPIRY_SECONDS * 2: # Cache min amount longer (e.g., 30 min)
            logger.debug(f"Cache hit for {currency_code_lower} min amount: {min_amount}")
            return min_amount

    if not NOWPAYMENTS_API_KEY:
        logger.error("NOWPayments API key is missing, cannot fetch minimum amount.")
        return None

    # Fetch from NOWPayments API
    try:
        url = f"{NOWPAYMENTS_API_URL}/v1/min-amount"
        # Parameters might depend on the exact endpoint; check NOWPayments docs if needed.
        # Often it's just the crypto currency.
        params = {'currency_from': currency_code_lower}
        headers = {'x-api-key': NOWPAYMENTS_API_KEY}

        logger.debug(f"Fetching min amount for {currency_code_lower} from {url} with params {params}")
        response = requests.get(url, params=params, headers=headers, timeout=10)
        logger.debug(f"NOWPayments min-amount response status: {response.status_code}, content: {response.text[:200]}")
        response.raise_for_status() # Check for HTTP errors
        data = response.json()

        # NOWPayments response structure might vary slightly, check keys carefully
        min_amount_key = 'min_amount' # Common key name
        if min_amount_key in data and data[min_amount_key] is not None:
            min_amount = Decimal(str(data[min_amount_key]))
            min_amount_cache[currency_code_lower] = (min_amount, now) # Update cache
            logger.info(f"Fetched minimum amount for {currency_code_lower}: {min_amount} from NOWPayments.")
            return min_amount
        else:
            # Log the actual response if the expected key is missing
            logger.warning(f"Could not find '{min_amount_key}' key or it was null for {currency_code_lower} in NOWPayments response: {data}")
            return None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching minimum amount for {currency_code_lower} from NOWPayments.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching minimum amount for {currency_code_lower} from NOWPayments: {e}")
        if e.response is not None:
            logger.error(f"NOWPayments min-amount error response ({e.response.status_code}): {e.response.text}")
        return None
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"Error parsing NOWPayments min amount response for {currency_code_lower}: {e}")
        return None
# --- END NEW FUNCTION ---

def format_expiration_time(expiration_date_str: str | None) -> str:
    """Formats an ISO expiration date string into a human-readable HH:MM:SS format."""
    if not expiration_date_str:
        return "N/A"
    try:
        # Parse the ISO 8601 string with timezone info
        dt_obj = datetime.fromisoformat(expiration_date_str)
        # Format the time part
        return dt_obj.strftime("%H:%M:%S %Z") # Example: include timezone abbreviation
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse expiration date string '{expiration_date_str}': {e}")
        return "Invalid Date"


# --- Placeholder Handler ---
async def handle_coming_soon(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query:
        try: await query.answer("This feature is coming soon!", show_alert=True); logger.info(f"User {query.from_user.id} clicked coming soon (data: {query.data})")
        except Exception as e: logger.error(f"Error answering 'coming soon' callback: {e}")

# --- Initial Data Load ---
init_db() # Ensure DB schema exists before loading
load_all_data() # Load cities, districts, types

# --- END OF FILE utils.py ---