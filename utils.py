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
from decimal import Decimal, ROUND_DOWN # Use Decimal for financial calculations
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
    "en": {
        "native_name": "English",
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
        "admin_select_district": "🏙️ Select District in {city}\n\nPick a district:",
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
        "crypto_payment_disabled": "Crypto payment (Top Up) is currently disabled.", # Keep this for when NOWPayments is disabled
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
        "preparing_invoice": "⏳ Preparing your payment invoice...", # NowPayments version
        "failed_invoice_creation": "❌ Failed to create payment invoice. This could be a temporary issue with the payment provider or an API key problem. Please try again later or contact support.", # NowPayments version
        "calculating_amount": "⏳ Calculating required amount and preparing invoice...", # NowPayments version
        "error_getting_rate": "❌ Error: Could not get exchange rate for {asset}. Please try another currency or contact support.",
        "error_preparing_payment": "❌ An error occurred while preparing the payment. Please try again later.",
        "invoice_title_refill": "Top-Up Invoice Created", # Keep this title
        "please_pay_label": "Please pay",
        "target_value_label": "Target Value",
        "payment_address_label": "Payment Address",
        "amount_label": "Amount",
        "expires_at_label": "Expires At",
        "send_warning_template": "⚠️ Send only {asset}. Ensure you send the exact amount.",
        "confirmation_note": "✅ Confirmation is automatic. Please wait a few minutes after sending.",
        "pay_now_button_nowpayments": "Pay via NOWPayments", # New button text if needed
        "check_status_button": "Check Payment Status", # Removed
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
        "payment_amount_too_low_api": "❌ Payment Amount Too Low: The required crypto amount is below the minimum allowed by the payment provider for {currency}. Please try a higher EUR amount.",
        "webhook_processing_error": "Webhook Error: Could not process payment update {payment_id}.",
        "webhook_db_update_failed": "Critical Error: Payment {payment_id} confirmed, but DB balance update failed for user {user_id}. Manual action required.",
        "webhook_pending_not_found": "Webhook Warning: Received update for payment ID {payment_id}, but no pending deposit found in DB.",
        "webhook_price_fetch_error": "Webhook Error: Could not fetch price for {currency} to confirm EUR value for payment {payment_id}.",
        "payment_cancelled_or_expired": "Payment Status: Your payment ({payment_id}) was cancelled or expired.",
    },
    "lt": { # --- Lithuanian translations ---
        "native_name": "Lietuvių",
        "welcome": "👋 Sveiki, {username}!",
        "status_label": "Statusas",
        "balance_label": "Balansas",
        "purchases_label": "Iš viso pirkimų",
        "basket_label": "Krepšelio prekės",
        "shopping_prompt": "Pradėkite apsipirkti arba naršykite parinktis žemiau.",
        "refund_note": "Pastaba: Pinigai negrąžinami.",
        "shop_button": "Parduotuvė",
        "profile_button": "Profilis",
        "top_up_button": "Papildyti",
        "reviews_button": "Atsiliepimai",
        "price_list_button": "Kainoraštis",
        "language_button": "Kalba",
        "admin_button": "🔧 Administratoriaus Panelė",
        "pay": "💳 Mokėti iš viso: {amount} EUR",
        "added_to_basket": "✅ Prekė rezervuota!\n\n{item} yra jūsų krepšelyje {timeout} min.! ⏳",
        "basket_empty": "🛒 Jūsų krepšelis tuščias!\n\nPridėkite prekių, kad pradėtumėte apsipirkti! 😊",
        "insufficient_balance": "⚠️ Nepakankamas likutis!\n\nPrašome papildyti sąskaitą, kad tęstumėte! 💸",
        "purchase_success": "🎉 Pirkimas sėkmingas!\n\nJūsų atsiėmimo informacija žemiau! 🚚",
        "basket_cleared": "🗑️ Krepšelis išvalytas!\n\nPradėkite iš naujo! ✨",
        "your_basket_title": "Jūsų krepšelis",
        "add_items_prompt": "Pridėkite prekių, kad pradėtumėte apsipirkti!",
        "items_expired_note": "Prekės galėjo baigtis arba buvo pašalintos.",
        "expires_in_label": "Galioja iki",
        "remove_button_label": "Pašalinti",
        "discount_applied_label": "Pritaikyta nuolaida",
        "discount_removed_note": "Nuolaidos kodas {code} pašalintas: {reason}",
        "subtotal_label": "Tarpinė suma",
        "total_label": "Iš viso",
        "pay_now_button": "Mokėti dabar",
        "clear_all_button": "Išvalyti viską",
        "remove_discount_button": "Pašalinti nuolaidą",
        "apply_discount_button": "Pritaikyti nuolaidos kodą",
        "shop_more_button": "Pirkti daugiau",
        "home_button": "Pradžia",
        "view_basket_button": "Peržiūrėti krepšelį",
        "clear_basket_button": "Išvalyti krepšelį",
        "back_options_button": "Atgal į parinktis",
        "purchase_history_button": "Pirkimų istorija",
        "back_profile_button": "Atgal į profilį",
        "language_set_answer": "Kalba nustatyta į {lang}!",
        "error_saving_language": "Klaida išsaugant kalbos nustatymą.",
        "invalid_language_answer": "Pasirinkta neteisinga kalba.",
        "back_button": "Atgal",
        "no_cities_for_prices": "Nėra miestų, kurių kainoraščius būtų galima peržiūrėti.",
        "price_list_title": "Kainoraštis",
        "select_city_prices_prompt": "Pasirinkite miestą, kad pamatytumėte galimas prekes ir kainas:",
        "error_city_not_found": "Klaida: Miestas nerastas.",
        "price_list_title_city": "Kainoraštis: {city_name}",
        "no_products_in_city": "Šiuo metu šiame mieste prekių nėra.",
        "available_label": "prieinama",
        "available_label_short": "Priein.",
        "back_city_list_button": "Atgal į miestų sąrašą",
        "message_truncated_note": "Žinutė sutrumpinta dėl ilgio limito. Naudokite 'Parduotuvė' pilnai informacijai.",
        "error_loading_prices_db": "Klaida: Nepavyko įkelti kainoraščio {city_name}",
        "error_displaying_prices": "Klaida rodant kainoraštį.",
        "error_unexpected_prices": "Klaida: Įvyko netikėta problema generuojant kainoraštį.",
        "reviews": "📝 Atsiliepimai",
        "view_reviews_button": "Žiūrėti atsiliepimus",
        "leave_review_button": "Palikti atsiliepimą",
        "enter_review_prompt": "Įveskite savo atsiliepimą ir išsiųskite.",
        "cancel_button": "Atšaukti",
        "enter_review_answer": "Įveskite atsiliepimą pokalbyje.",
        "send_text_review_please": "Prašome siųsti tik tekstą savo atsiliepimui.",
        "review_not_empty": "Atsiliepimas negali būti tuščias. Bandykite dar kartą arba atšaukite.",
        "review_too_long": "Atsiliepimas per ilgas (maks. 1000 simbolių). Sutrumpinkite.",
        "review_thanks": "Ačiū už jūsų atsiliepimą! Jūsų nuomonė padeda mums tobulėti.",
        "error_saving_review_db": "Klaida: Nepavyko išsaugoti atsiliepimo dėl duomenų bazės problemos.",
        "error_saving_review_unexpected": "Klaida: Įvyko netikėta problema saugant atsiliepimą.",
        "user_reviews_title": "Vartotojų atsiliepimai",
        "no_reviews_yet": "Kol kas nepalikta jokių atsiliepimų.",
        "no_more_reviews": "Daugiau atsiliepimų nėra.",
        "prev_button": "Ankst.",
        "next_button": "Kitas",
        "back_review_menu_button": "Atgal į atsiliepimų meniu",
        "unknown_date_label": "Nežinoma data",
        "error_displaying_review": "Klaida rodant atsiliepimą",
        "error_updating_review_list": "Klaida atnaujinant atsiliepimų sąrašą.",
        "discount_no_items": "Jūsų krepšelis tuščias. Pirmiausia pridėkite prekių.",
        "enter_discount_code_prompt": "Įveskite nuolaidos kodą:",
        "enter_code_answer": "Įveskite kodą pokalbyje.",
        "no_code_entered": "Kodas neįvestas.",
        "send_text_please": "Prašome siųsti nuolaidos kodą tekstu.",
        "error_calculating_total": "Klaida skaičiuojant krepšelio sumą.",
        "returning_to_basket": "Grįžtama į krepšelį.",
        "basket_empty_no_discount": "Jūsų krepšelis tuščias. Negalima pritaikyti nuolaidos kodo.",
        "success_label": "Sėkmingai!",
        "basket_already_empty": "Krepšelis jau tuščias.",
        "crypto_payment_disabled": "Kriptovaliutų mokėjimai (papildymas) šiuo metu išjungti.",
        "top_up_title": "Papildyti balansą",
        "enter_refill_amount_prompt": "Atsakykite nurodydami sumą EUR, kurią norite pridėti prie balanso (pvz., 10 arba 25.50).",
        "min_top_up_note": "Minimalus papildymas: {amount} EUR",
        "enter_amount_answer": "Įveskite papildymo sumą.",
        "error_occurred_answer": "Įvyko klaida. Bandykite dar kartą.",
        "send_amount_as_text": "Prašome siųsti sumą tekstu (pvz., 10 arba 25.50).",
        "amount_too_low_msg": "Suma per maža. Minimalus papildymas yra {amount} EUR. Įveskite didesnę sumą.",
        "amount_too_high_msg": "Suma per didelė. Įveskite mažesnę sumą.",
        "invalid_amount_format_msg": "Neteisingas sumos formatas. Įveskite skaičių (pvz., 10 arba 25.50).",
        "unexpected_error_msg": "Įvyko netikėta klaida. Bandykite dar kartą vėliau.",
        "choose_crypto_prompt": "Norite papildyti {amount} EUR. Pasirinkite kriptovaliutą, kuria norite mokėti:",
        "cancel_top_up_button": "Atšaukti papildymą",
        "purchase_history_title": "Pirkimų istorija",
        "no_purchases_yet": "Kol kas neatlikote jokių pirkimų.",
        "recent_purchases_title": "Jūsų paskutiniai pirkimai",
        "error_location_mismatch": "Klaida: Vietos duomenys nesutampa.",
        "drop_unavailable": "Prekė neprieinama! Ši parinktis ką tik buvo parduota arba rezervuota.",
        "price_label": "Kaina",
        "available_label_long": "Prieinama",
        "add_to_basket_button": "Į krepšelį",
        "error_loading_details": "Klaida: Nepavyko įkelti prekės informacijos",
        "expires_label": "Galioja iki",
        "error_adding_db": "Klaida: Duomenų bazės problema pridedant prekę į krepšelį.",
        "error_adding_unexpected": "Klaida: Įvyko netikėta problema pridedant prekę.",
        "profile_title": "Jūsų profilis",
        "no_cities_available": "Šiuo metu nėra galimų miestų. Patikrinkite vėliau.",
        "select_location_prompt": "Pasirinkite savo vietą:",
        "choose_city_title": "Pasirinkite miestą",
        "preparing_invoice": "⏳ Ruošiama jūsų mokėjimo sąskaita...",
        "failed_invoice_creation": "❌ Nepavyko sukurti mokėjimo sąskaitos. Tai gali būti laikina mokėjimo tiekėjo problema arba API rakto problema. Bandykite dar kartą vėliau arba susisiekite su palaikymo tarnyba.",
        "calculating_amount": "⏳ Skaičiuojama reikiama suma ir ruošiama sąskaita...",
        "error_getting_rate": "❌ Klaida: Nepavyko gauti {asset} keitimo kurso. Bandykite kitą valiutą arba susisiekite su palaikymo tarnyba.",
        "error_preparing_payment": "❌ Ruošiant mokėjimą įvyko klaida. Bandykite dar kartą vėliau.",
        "invoice_title_refill": "Sukurta papildymo sąskaita",
        "please_pay_label": "Prašome sumokėti",
        "target_value_label": "Numatytoji vertė",
        "payment_address_label": "Mokėjimo adresas",
        "amount_label": "Suma",
        "expires_at_label": "Galioja iki",
        "send_warning_template": "⚠️ Siųskite tik {asset}. Įsitikinkite, kad siunčiate tikslią sumą.",
        "confirmation_note": "✅ Patvirtinimas automatinis. Palaukite kelias minutes po siuntimo.",
        "pay_now_button_nowpayments": "Mokėti per NOWPayments",
        "top_up_success_title": "✅ Papildymas sėkmingas!",
        "amount_added_label": "Pridėta suma",
        "new_balance_label": "Jūsų naujas likutis",
        "sold_out_note": "⚠️ Pastaba: Šios prekės tapo neprieinamos apdorojimo metu ir nebuvo įtrauktos: {items}. Už jas nebuvo sumokėta.",
        "balance_changed_error": "❌ Transakcija nepavyko: Jūsų likutis pasikeitė. Patikrinkite likutį ir bandykite dar kartą.",
        "order_failed_all_sold_out_balance": "❌ Užsakymas nepavyko: Visos jūsų krepšelio prekės tapo neprieinamos apdorojimo metu. Jūsų likutis nebuvo nuskaičiuotas.",
        "error_processing_purchase_contact_support": "❌ Apdorojant jūsų pirkimą įvyko klaida. Susisiekite su palaikymo tarnyba.",
        "back_basket_button": "Atgal į krepšelį",
        "discount_value_label": "Vertė",
        "language": "🌐 Pasirinkite kalbą:",
        "no_items_of_type": "Šiuo metu čia nėra šio tipo prekių.",
        "available_options_prompt": "Galimos parinktys:",
        "error_loading_products": "Klaida: Nepavyko įkelti produktų",
        "error_unexpected": "Įvyko netikėta klaida",
        "error_district_city_not_found": "Klaida: Rajonas ar miestas nerastas.",
        "error_loading_types": "Klaida: Nepavyko įkelti produktų tipų",
        "no_types_available": "Šiuo metu čia nėra produktų tipų.",
        "select_type_prompt": "Pasirinkite produkto tipą:",
        "no_districts_available": "Šiam miestui kol kas nėra rajonų.",
        "back_districts_button": "Atgal į rajonus",
        "back_cities_button": "Atgal į miestus",
        "admin_select_city": "🏙️ Pasirinkite miestą redaguoti:",
        "admin_select_district": "🏘️ Pasirinkite rajoną mieste {city}:",
        "admin_select_type": "💎 Pasirinkite saldainių tipą arba pridėkite naują:",
        "admin_choose_action": "📦 Tvarkyti {type} mieste {city}, rajone {district}. Ką norėtumėte daryti?",
        "error_nowpayments_api": "❌ Mokėjimo API klaida: Nepavyko sukurti mokėjimo. Bandykite dar kartą vėliau arba susisiekite su palaikymo tarnyba.",
        "error_invalid_nowpayments_response": "❌ Mokėjimo API klaida: Gautas neteisingas atsakymas. Susisiekite su palaikymo tarnyba.",
        "error_nowpayments_api_key": "❌ Mokėjimo API klaida: Neteisingas API raktas. Susisiekite su palaikymo tarnyba.",
        "payment_pending_db_error": "❌ Duomenų bazės klaida: Nepavyko įrašyti laukiančio mokėjimo. Susisiekite su palaikymo tarnyba.",
        "payment_amount_too_low_api": "❌ Mokėjimo suma per maža: Reikalinga kriptovaliutos suma yra mažesnė už minimalią leistiną mokėjimo tiekėjo {currency}. Bandykite didesnę EUR sumą.",
        "webhook_processing_error": "Webhook klaida: Nepavyko apdoroti mokėjimo atnaujinimo {payment_id}.",
        "webhook_db_update_failed": "Kritinė klaida: Mokėjimas {payment_id} patvirtintas, bet DB balanso atnaujinimas nepavyko vartotojui {user_id}. Reikalingas rankinis veiksmas.",
        "webhook_pending_not_found": "Webhook įspėjimas: Gautas atnaujinimas mokėjimo ID {payment_id}, bet DB nerastas laukiantis indėlis.",
        "webhook_price_fetch_error": "Webhook klaida: Nepavyko gauti {currency} kainos EUR vertei patvirtinti mokėjimui {payment_id}.",
        "payment_cancelled_or_expired": "Mokėjimo būsena: Jūsų mokėjimas ({payment_id}) buvo atšauktas arba baigėsi galiojimas.",
        "set_media_prompt_plain": "📸 Atsiųskite nuotrauką, vaizdo įrašą ar GIF, kad būtų rodoma virš visų pranešimų:",
        "state_error": "❌ Klaida: Neteisinga būsena. Pradėkite 'Pridėti naują produktą' procesą iš naujo per administratoriaus panelę.",
        "review_prompt": "🎉 Ačiū už pirkinį! Norėtume išgirsti jūsų atsiliepimą. Ar norėtumėte palikti atsiliepimą dabar ar vėliau?",
        "payment_failed": "❌ Mokėjimas nepavyko! Bandykite dar kartą arba susisiekite su {support}. 📞",
        "support": "📞 Reikia pagalbos? Susisiekite su {support}!",
        "file_download_error": "❌ Klaida: Nepavyko atsisiųsti medijos. Bandykite dar kartą arba susisiekite su {support}.",
    },
    "ru": { # --- Russian translations ---
        "native_name": "Русский",
        "welcome": "👋 Добро пожаловать, {username}!",
        "status_label": "Статус",
        "balance_label": "Баланс",
        "purchases_label": "Всего покупок",
        "basket_label": "Товары в корзине",
        "shopping_prompt": "Начните покупки или изучите опции ниже.",
        "refund_note": "Примечание: Возврат средств не производится.",
        "shop_button": "Магазин",
        "profile_button": "Профиль",
        "top_up_button": "Пополнить",
        "reviews_button": "Отзывы",
        "price_list_button": "Прайс-лист",
        "language_button": "Язык",
        "admin_button": "🔧 Панель администратора",
        "pay": "💳 Итого к оплате: {amount} EUR",
        "added_to_basket": "✅ Товар зарезервирован!\n\n{item} в вашей корзине на {timeout} минут! ⏳",
        "basket_empty": "🛒 Ваша корзина пуста!\n\nДобавьте товары, чтобы начать покупки! 😊",
        "insufficient_balance": "⚠️ Недостаточно средств!\n\nПожалуйста, пополните баланс, чтобы продолжить! 💸",
        "purchase_success": "🎉 Покупка завершена!\n\nИнформация для получения ниже! 🚚",
        "basket_cleared": "🗑️ Корзина очищена!\n\nНачните сначала! ✨",
        "your_basket_title": "Ваша корзина",
        "add_items_prompt": "Добавьте товары, чтобы начать покупки!",
        "items_expired_note": "Товары могли закончиться или были удалены.",
        "expires_in_label": "Истекает через",
        "remove_button_label": "Удалить",
        "discount_applied_label": "Скидка применена",
        "discount_removed_note": "Промокод {code} удален: {reason}",
        "subtotal_label": "Подытог",
        "total_label": "Итого",
        "pay_now_button": "Оплатить сейчас",
        "clear_all_button": "Очистить все",
        "remove_discount_button": "Удалить скидку",
        "apply_discount_button": "Применить промокод",
        "shop_more_button": "Купить еще",
        "home_button": "Главная",
        "view_basket_button": "Посмотреть корзину",
        "clear_basket_button": "Очистить корзину",
        "back_options_button": "Назад к опциям",
        "purchase_history_button": "История покупок",
        "back_profile_button": "Назад в профиль",
        "language_set_answer": "Язык установлен на {lang}!",
        "error_saving_language": "Ошибка сохранения настроек языка.",
        "invalid_language_answer": "Выбран неверный язык.",
        "back_button": "Назад",
        "no_cities_for_prices": "Нет доступных городов для просмотра цен.",
        "price_list_title": "Прайс-лист",
        "select_city_prices_prompt": "Выберите город для просмотра доступных товаров и цен:",
        "error_city_not_found": "Ошибка: Город не найден.",
        "price_list_title_city": "Прайс-лист: {city_name}",
        "no_products_in_city": "В этом городе пока нет товаров.",
        "available_label": "доступно",
        "available_label_short": "Дост.",
        "back_city_list_button": "Назад к списку городов",
        "message_truncated_note": "Сообщение усечено из-за ограничения длины. Используйте 'Магазин' для полной информации.",
        "error_loading_prices_db": "Ошибка: Не удалось загрузить прайс-лист для {city_name}",
        "error_displaying_prices": "Ошибка отображения прайс-листа.",
        "error_unexpected_prices": "Ошибка: Произошла непредвиденная ошибка при генерации прайс-листа.",
        "reviews": "📝 Отзывы",
        "view_reviews_button": "Смотреть отзывы",
        "leave_review_button": "Оставить отзыв",
        "enter_review_prompt": "Введите текст вашего отзыва и отправьте.",
        "cancel_button": "Отмена",
        "enter_review_answer": "Введите ваш отзыв в чат.",
        "send_text_review_please": "Пожалуйста, отправьте отзыв только текстом.",
        "review_not_empty": "Отзыв не может быть пустым. Попробуйте еще раз или отмените.",
        "review_too_long": "Отзыв слишком длинный (макс. 1000 символов). Пожалуйста, сократите его.",
        "review_thanks": "Спасибо за ваш отзыв! Ваше мнение помогает нам стать лучше.",
        "error_saving_review_db": "Ошибка: Не удалось сохранить ваш отзыв из-за проблемы с базой данных.",
        "error_saving_review_unexpected": "Ошибка: Произошла непредвиденная ошибка при сохранении вашего отзыва.",
        "user_reviews_title": "Отзывы пользователей",
        "no_reviews_yet": "Пока нет ни одного отзыва.",
        "no_more_reviews": "Больше отзывов нет.",
        "prev_button": "Пред.",
        "next_button": "След.",
        "back_review_menu_button": "Назад в меню отзывов",
        "unknown_date_label": "Неизвестная дата",
        "error_displaying_review": "Ошибка отображения отзыва",
        "error_updating_review_list": "Ошибка обновления списка отзывов.",
        "discount_no_items": "Ваша корзина пуста. Сначала добавьте товары.",
        "enter_discount_code_prompt": "Пожалуйста, введите ваш промокод:",
        "enter_code_answer": "Введите код в чат.",
        "no_code_entered": "Код не введен.",
        "send_text_please": "Пожалуйста, отправьте промокод текстом.",
        "error_calculating_total": "Ошибка расчета суммы корзины.",
        "returning_to_basket": "Возвращение в корзину.",
        "basket_empty_no_discount": "Ваша корзина пуста. Невозможно применить промокод.",
        "success_label": "Успешно!",
        "basket_already_empty": "Корзина уже пуста.",
        "crypto_payment_disabled": "Оплата криптовалютой (Пополнение) временно отключена.",
        "top_up_title": "Пополнить баланс",
        "enter_refill_amount_prompt": "Ответьте суммой в EUR, на которую хотите пополнить баланс (например, 10 или 25.50).",
        "min_top_up_note": "Минимальное пополнение: {amount} EUR",
        "enter_amount_answer": "Введите сумму пополнения.",
        "error_occurred_answer": "Произошла ошибка. Пожалуйста, попробуйте еще раз.",
        "send_amount_as_text": "Пожалуйста, отправьте сумму текстом (например, 10 или 25.50).",
        "amount_too_low_msg": "Сумма слишком мала. Минимальное пополнение {amount} EUR. Введите большую сумму.",
        "amount_too_high_msg": "Сумма слишком велика. Введите меньшую сумму.",
        "invalid_amount_format_msg": "Неверный формат суммы. Введите число (например, 10 или 25.50).",
        "unexpected_error_msg": "Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже.",
        "choose_crypto_prompt": "Вы хотите пополнить на {amount} EUR. Выберите криптовалюту для оплаты:",
        "cancel_top_up_button": "Отменить пополнение",
        "purchase_history_title": "История покупок",
        "no_purchases_yet": "У вас еще нет покупок.",
        "recent_purchases_title": "Ваши последние покупки",
        "error_location_mismatch": "Ошибка: Данные о местоположении не совпадают.",
        "drop_unavailable": "Товар недоступен! Этот вариант только что был продан или зарезервирован.",
        "price_label": "Цена",
        "available_label_long": "Доступно",
        "add_to_basket_button": "Добавить в корзину",
        "error_loading_details": "Ошибка: Не удалось загрузить информацию о товаре",
        "expires_label": "Истекает через",
        "error_adding_db": "Ошибка: Проблема с базой данных при добавлении товара в корзину.",
        "error_adding_unexpected": "Ошибка: Произошла непредвиденная ошибка при добавлении товара.",
        "profile_title": "Ваш профиль",
        "no_cities_available": "В данный момент нет доступных городов. Пожалуйста, проверьте позже.",
        "select_location_prompt": "Выберите ваше местоположение:",
        "choose_city_title": "Выберите город",
        "preparing_invoice": "⏳ Готовим ваш счет к оплате...",
        "failed_invoice_creation": "❌ Не удалось создать счет на оплату. Возможно, это временная проблема с платежным провайдером или проблема с ключом API. Пожалуйста, попробуйте позже или свяжитесь со службой поддержки.",
        "calculating_amount": "⏳ Рассчитываем необходимую сумму и готовим счет...",
        "error_getting_rate": "❌ Ошибка: Не удалось получить обменный курс для {asset}. Пожалуйста, попробуйте другую валюту или свяжитесь со службой поддержки.",
        "error_preparing_payment": "❌ Произошла ошибка при подготовке платежа. Пожалуйста, попробуйте позже.",
        "invoice_title_refill": "Счет на пополнение создан",
        "please_pay_label": "Пожалуйста, оплатите",
        "target_value_label": "Целевая стоимость",
        "payment_address_label": "Адрес для оплаты",
        "amount_label": "Сумма",
        "expires_at_label": "Истекает",
        "send_warning_template": "⚠️ Отправляйте только {asset}. Убедитесь, что отправляете точную сумму.",
        "confirmation_note": "✅ Подтверждение автоматическое. Пожалуйста, подождите несколько минут после отправки.",
        "pay_now_button_nowpayments": "Оплатить через NOWPayments",
        "top_up_success_title": "✅ Пополнение успешно!",
        "amount_added_label": "Добавлено",
        "new_balance_label": "Ваш новый баланс",
        "sold_out_note": "⚠️ Примечание: Следующие товары стали недоступны во время обработки и не были включены: {items}. Вы не были списаны за них.",
        "balance_changed_error": "❌ Транзакция не удалась: Ваш баланс изменился. Пожалуйста, проверьте баланс и попробуйте снова.",
        "order_failed_all_sold_out_balance": "❌ Заказ не удался: Все товары в вашей корзине стали недоступны во время обработки. Ваш баланс не был списан.",
        "error_processing_purchase_contact_support": "❌ Произошла ошибка при обработке вашей покупки. Пожалуйста, свяжитесь со службой поддержки.",
        "back_basket_button": "Назад в корзину",
        "discount_value_label": "Значение",
        "language": "🌐 Выберите язык:",
        "no_items_of_type": "Товаров этого типа здесь сейчас нет.",
        "available_options_prompt": "Доступные варианты:",
        "error_loading_products": "Ошибка: Не удалось загрузить товары",
        "error_unexpected": "Произошла непредвиденная ошибка",
        "error_district_city_not_found": "Ошибка: Район или город не найден.",
        "error_loading_types": "Ошибка: Не удалось загрузить типы товаров",
        "no_types_available": "В настоящее время здесь нет типов товаров.",
        "select_type_prompt": "Выберите тип товара:",
        "no_districts_available": "Для этого города пока нет районов.",
        "back_districts_button": "Назад к районам",
        "back_cities_button": "Назад к городам",
        "admin_select_city": "🏙️ Выберите город для редактирования:",
        "admin_select_district": "🏘️ Выберите район в городе {city}:",
        "admin_select_type": "💎 Выберите тип конфет или добавьте новый:",
        "admin_choose_action": "📦 Управление {type} в {city}, {district}. Что бы вы хотели сделать?",
        "error_nowpayments_api": "❌ Ошибка API платежей: Не удалось создать платеж. Попробуйте позже или свяжитесь с поддержкой.",
        "error_invalid_nowpayments_response": "❌ Ошибка API платежей: Получен неверный ответ. Свяжитесь с поддержкой.",
        "error_nowpayments_api_key": "❌ Ошибка API платежей: Неверный API ключ. Свяжитесь с поддержкой.",
        "payment_pending_db_error": "❌ Ошибка БД: Не удалось записать ожидающий платеж. Свяжитесь с поддержкой.",
        "payment_amount_too_low_api": "❌ Сумма платежа слишком мала: Требуемая сумма в криптовалюте ниже минимума, допустимого платежным провайдером для {currency}. Попробуйте большую сумму в EUR.",
        "webhook_processing_error": "Ошибка Webhook: Не удалось обработать обновление платежа {payment_id}.",
        "webhook_db_update_failed": "Критическая ошибка: Платеж {payment_id} подтвержден, но обновление баланса в БД не удалось для пользователя {user_id}. Требуется ручное вмешательство.",
        "webhook_pending_not_found": "Предупреждение Webhook: Получено обновление для платежа ID {payment_id}, но ожидающий депозит не найден в БД.",
        "webhook_price_fetch_error": "Ошибка Webhook: Не удалось получить цену для {currency} для подтверждения стоимости в EUR для платежа {payment_id}.",
        "payment_cancelled_or_expired": "Статус платежа: Ваш платеж ({payment_id}) был отменен или истек.",
        "set_media_prompt_plain": "📸 Отправьте фото, видео или GIF для отображения над всеми сообщениями:",
        "state_error": "❌ Ошибка: Недопустимое состояние. Пожалуйста, начните процесс 'Добавить новый товар' снова из Панели администратора.",
        "review_prompt": "🎉 Спасибо за покупку! Мы хотели бы услышать ваше мнение. Хотите оставить отзыв сейчас или позже?",
        "payment_failed": "❌ Платеж не удался! Пожалуйста, попробуйте еще раз или свяжитесь с {support}. 📞",
        "support": "📞 Нужна помощь? Свяжитесь с {support}!",
        "file_download_error": "❌ Ошибка: Не удалось загрузить медиа. Пожалуйста, попробуйте еще раз или свяжитесь с {support}.",
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
PRODUCT_TYPES = []
SIZES = ["2g", "5g"]
BOT_MEDIA = {'type': None, 'path': None}
currency_price_cache = {} # Simple in-memory cache for CoinGecko prices
CACHE_EXPIRY_SECONDS = 300 # Cache prices for 5 minutes


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
            # users table
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0,
                total_purchases INTEGER DEFAULT 0, basket TEXT DEFAULT '',
                language TEXT DEFAULT 'en', theme TEXT DEFAULT 'default'
            )''')
            # cities table
            c.execute('''CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
            )''')
            # districts table
            c.execute('''CREATE TABLE IF NOT EXISTS districts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city_id INTEGER NOT NULL, name TEXT NOT NULL,
                FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE, UNIQUE (city_id, name)
            )''')
            # product_types table
            c.execute('''CREATE TABLE IF NOT EXISTS product_types ( name TEXT PRIMARY KEY NOT NULL )''')
            # products table
            c.execute('''CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT NOT NULL, district TEXT NOT NULL,
                product_type TEXT NOT NULL, size TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL,
                available INTEGER DEFAULT 1, reserved INTEGER DEFAULT 0, original_text TEXT,
                added_by INTEGER, added_date TEXT
            )''')
            # product_media table
            c.execute('''CREATE TABLE IF NOT EXISTS product_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                media_type TEXT NOT NULL, file_path TEXT UNIQUE NOT NULL, telegram_file_id TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
            )''')
            # purchases table
            c.execute('''CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER,
                product_name TEXT NOT NULL, product_type TEXT NOT NULL, product_size TEXT NOT NULL,
                price_paid REAL NOT NULL, city TEXT NOT NULL, district TEXT NOT NULL, purchase_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
            )''')
            # reviews table
            c.execute('''CREATE TABLE IF NOT EXISTS reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                review_text TEXT NOT NULL, review_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # discount_codes table
            c.execute('''CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
                discount_type TEXT NOT NULL CHECK(discount_type IN ('percentage', 'fixed')),
                value REAL NOT NULL, is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
                max_uses INTEGER DEFAULT NULL, uses_count INTEGER DEFAULT 0,
                created_date TEXT NOT NULL, expiry_date TEXT DEFAULT NULL
            )''')
            # --- NEW: pending_deposits table ---
            c.execute('''CREATE TABLE IF NOT EXISTS pending_deposits (
                payment_id TEXT PRIMARY KEY NOT NULL,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                target_eur_amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # --- END NEW TABLE ---

            # Create Indices
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


# --- NEW: Pending Deposit DB Helpers (Synchronous) ---
def add_pending_deposit(payment_id: str, user_id: int, currency: str, target_eur_amount: float):
    """Adds a record for a pending NOWPayments deposit."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO pending_deposits (payment_id, user_id, currency, target_eur_amount, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (payment_id, user_id, currency.lower(), target_eur_amount, datetime.now(timezone.utc).isoformat()))
            conn.commit()
            logger.info(f"Added pending deposit {payment_id} for user {user_id} ({target_eur_amount:.2f} EUR in {currency}).")
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Attempted to add duplicate pending deposit ID: {payment_id}")
        return False # Indicate failure due to duplication
    except sqlite3.Error as e:
        logger.error(f"DB error adding pending deposit {payment_id} for user {user_id}: {e}", exc_info=True)
        return False

def get_pending_deposit(payment_id: str):
    """Retrieves pending deposit details by payment ID."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, currency, target_eur_amount FROM pending_deposits WHERE payment_id = ?", (payment_id,))
            row = c.fetchone()
            return dict(row) if row else None
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

def load_product_types():
    product_types_list = []
    try:
        with get_db_connection() as conn: c = conn.cursor(); c.execute("SELECT name FROM product_types ORDER BY name"); product_types_list = [row['name'] for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to load product types: {e}")
    return product_types_list

def load_all_data():
    """Loads all dynamic data, modifying global variables IN PLACE."""
    global CITIES, DISTRICTS, PRODUCT_TYPES
    logger.info("Starting load_all_data (in-place update)...")
    try:
        cities_data = load_cities(); districts_data = load_districts(); product_types_list = load_product_types()
        CITIES.clear(); CITIES.update(cities_data)
        DISTRICTS.clear(); DISTRICTS.update(districts_data)
        PRODUCT_TYPES[:] = product_types_list
        logger.info(f"Loaded (in-place) {len(CITIES)} cities, {sum(len(d) for d in DISTRICTS.values())} districts, {len(PRODUCT_TYPES)} product types.")
    except Exception as e: logger.error(f"Error during load_all_data (in-place): {e}", exc_info=True); CITIES.clear(); DISTRICTS.clear(); PRODUCT_TYPES[:] = []


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
        p_int = int(purchases);
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

def get_currency_to_eur_price(currency_code: str) -> Decimal | None:
    """Gets the price of a cryptocurrency in EUR from CoinGecko with caching."""
    currency_code_lower = currency_code.lower()
    now = time.time()

    # Check cache first
    if currency_code_lower in currency_price_cache:
        price, timestamp = currency_price_cache[currency_code_lower]
        if now - timestamp < CACHE_EXPIRY_SECONDS:
            logger.debug(f"Cache hit for {currency_code_lower} price: {price}")
            return price

    # Fetch from CoinGecko
    try:
        # Map common codes to CoinGecko IDs if necessary
        coingecko_id_map = {
            'btc': 'bitcoin', 'eth': 'ethereum', 'ltc': 'litecoin',
            'sol': 'solana', 'ton': 'the-open-network',
            'usdt': 'tether', 'usdc': 'usd-coin'
            # Add more mappings if needed
        }
        coingecko_id = coingecko_id_map.get(currency_code_lower, currency_code_lower)

        url = f"{COINGECKO_API_URL}/simple/price"
        params = {'ids': coingecko_id, 'vs_currencies': 'eur'}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()

        if coingecko_id in data and 'eur' in data[coingecko_id]:
            price = Decimal(str(data[coingecko_id]['eur']))
            currency_price_cache[currency_code_lower] = (price, now) # Update cache
            logger.info(f"Fetched price for {currency_code_lower}: {price} EUR from CoinGecko.")
            return price
        else:
            logger.warning(f"Could not find EUR price for CoinGecko ID '{coingecko_id}' ({currency_code_lower}) in API response: {data}")
            return None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching price for {currency_code_lower} from CoinGecko.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching price for {currency_code_lower} from CoinGecko: {e}")
        return None
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"Error parsing CoinGecko response for {currency_code_lower}: {e}")
        return None

def format_expiration_time(expiration_date_str: str | None) -> str:
    """Formats an ISO expiration date string into a human-readable HH:MM:SS format."""
    if not expiration_date_str:
        return "N/A"
    try:
        # Parse the ISO 8601 string with timezone info
        dt_obj = datetime.fromisoformat(expiration_date_str)
        # Convert to local timezone if needed (or keep as UTC)
        # dt_local = dt_obj.astimezone() # Example: convert to local
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