# --- START OF FILE user.py ---

import sqlite3
import time
import logging
import asyncio
import os  # Import os for path joining
from datetime import datetime, timezone
from collections import defaultdict, Counter
from decimal import Decimal  # Use Decimal for financial calculations

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram import helpers
import telegram.error as telegram_error
# -------------------------

# Import from utils
from utils import (
    CITIES, DISTRICTS, PRODUCT_TYPES, THEMES, LANGUAGES, BOT_MEDIA, ADMIN_ID, BASKET_TIMEOUT, MIN_DEPOSIT_EUR,
    format_currency, get_progress_bar, send_message_with_retry, format_discount_value,
    clear_expired_basket, fetch_last_purchases, get_user_status, fetch_reviews,
    NOWPAYMENTS_API_KEY,  # Check if NOWPayments is configured
    get_db_connection, MEDIA_DIR  # Import helper and MEDIA_DIR
)

# Logging setup
logger = logging.getLogger(__name__)

# Emojis (Keep as is)
EMOJI_CITY = "🏙️"
EMOJI_DISTRICT = "🏘️"
EMOJI_PRODUCT = "💎"
EMOJI_HERB = "🌿"
EMOJI_PRICE = "💰"
EMOJI_QUANTITY = "🔢"
EMOJI_BASKET = "🛒"
EMOJI_PROFILE = "👤"
EMOJI_REFILL = "💸"
EMOJI_REVIEW = "📝"
EMOJI_PRICELIST = "📋"
EMOJI_LANG = "🌐"
EMOJI_BACK = "⬅️"
EMOJI_HOME = "🏠"
EMOJI_SHOP = "🛍️"
EMOJI_DISCOUNT = "🏷️"


# --- User Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command and the initial welcome message."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    is_callback = update.callback_query is not None

    # --- Send Bot Media ---
    if not is_callback and BOT_MEDIA.get("type") and BOT_MEDIA.get("path"):
        media_path = BOT_MEDIA["path"]
        media_type = BOT_MEDIA["type"]
        logger.info(f"Attempting to send BOT_MEDIA: type={media_type}, path={media_path}")
        if await asyncio.to_thread(os.path.exists, media_path):
            try:
                async with await asyncio.to_thread(open, media_path, "rb") as file_content:
                    if media_type == "photo": await context.bot.send_photo(chat_id=chat_id, photo=file_content)
                    elif media_type == "video": await context.bot.send_video(chat_id=chat_id, video=file_content)
                    elif media_type == "gif": await context.bot.send_animation(chat_id=chat_id, animation=file_content)
                    else: logger.warning(f"Unsupported BOT_MEDIA type: {media_type}")
            except FileNotFoundError: logger.warning(f"BOT_MEDIA file not found at {media_path} despite initial check.")
            except Exception as e: logger.error(f"Error sending BOT_MEDIA: {e}", exc_info=True)
        else:
             logger.warning(f"BOT_MEDIA path {media_path} not found on disk.")
    # --- End Send Bot Media ---

    user_id = user.id
    username = user.username or user.first_name or f"User_{user_id}"
    # Use Decimal for balance
    lang, theme, balance, purchases, basket_count = 'en', 'default', Decimal('0.0'), 0, 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN TRANSACTION")
        c.execute("""
            INSERT INTO users (user_id, username, balance, total_purchases, language, theme, basket)
            VALUES (?, ?, 0.0, 0, 'en', 'default', '')
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
        """, (user_id, username))
        c.execute("SELECT balance, total_purchases, language, theme, basket FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result:
            balance = Decimal(str(result['balance']))  # Load balance as Decimal
            purchases = result['total_purchases']
            db_lang = result['language']; db_theme = result['theme']
            lang = db_lang if db_lang and db_lang in LANGUAGES else 'en'
            theme = db_theme if db_theme and db_theme in THEMES else 'default'
        conn.commit()
        context.user_data["lang"] = lang
        context.user_data["theme"] = theme
        if 'basket' not in context.user_data: context.user_data['basket'] = []
        clear_expired_basket(context, user_id)  # Uses helper internally now
        basket = context.user_data.get("basket", [])
        basket_count = len(basket)
    except sqlite3.Error as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"Database error initializing user {user_id}: {e}", exc_info=True)
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Unable to load profile data.", parse_mode=None)
        if conn: conn.close()
        return  # Added return on failure
    finally:
        if conn: conn.close()

    # Prepare welcome message (plain text)
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    status = get_user_status(purchases)
    balance_str = format_currency(balance)  # Format Decimal balance
    welcome_template = lang_data.get("welcome", "👋 Welcome, {username}!")
    status_label = lang_data.get("status_label", "Status")
    balance_label = lang_data.get("balance_label", "Balance")
    purchases_label = lang_data.get("purchases_label", "Total Purchases")
    basket_label = lang_data.get("basket_label", "Basket Items")
    shopping_prompt = lang_data.get("shopping_prompt", "Start shopping or explore your options below.")
    refund_note = lang_data.get("refund_note", "Note: No refunds.")
    progress_bar_str = get_progress_bar(purchases)
    status_line = f"{EMOJI_PROFILE} {status_label}: {status} {progress_bar_str}"
    balance_line = f"{EMOJI_PRICE} {balance_label}: {balance_str} EUR"
    purchases_line = f"📦 {purchases_label}: {purchases}"
    basket_line = f"{EMOJI_BASKET} {basket_label}: {basket_count}"
    welcome_part = welcome_template.format(username=username)
    full_welcome = (
        f"{welcome_part}\n\n{status_line}\n{balance_line}\n"
        f"{purchases_line}\n{basket_line}\n\n{shopping_prompt}\n\n⚠️ {refund_note}"
    )
    # Keyboard
    shop_button_text = lang_data.get("shop_button", "Shop")
    profile_button_text = lang_data.get("profile_button", "Profile")
    top_up_button_text = lang_data.get("top_up_button", "Top Up")
    reviews_button_text = lang_data.get("reviews_button", "Reviews")
    price_list_button_text = lang_data.get("price_list_button", "Price List")
    language_button_text = lang_data.get("language_button", "Language")
    admin_button_text = lang_data.get("admin_button", "🔧 Admin Panel")
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI_SHOP} {shop_button_text}", callback_data="shop")],
        [InlineKeyboardButton(f"{EMOJI_PROFILE} {profile_button_text}", callback_data="profile"),
         InlineKeyboardButton(f"{EMOJI_REFILL} {top_up_button_text}", callback_data="refill")],
        [InlineKeyboardButton(f"{EMOJI_REVIEW} {reviews_button_text}", callback_data="reviews"),
         InlineKeyboardButton(f"{EMOJI_PRICELIST} {price_list_button_text}", callback_data="price_list"),
         InlineKeyboardButton(f"{EMOJI_LANG} {language_button_text}", callback_data="language")]
    ]
    if user_id == ADMIN_ID: keyboard.insert(0, [InlineKeyboardButton(admin_button_text, callback_data="admin_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send or edit message
    if is_callback:
        query = update.callback_query
        try:
             if query.message and (query.message.text != full_welcome or query.message.reply_markup != reply_markup):
                  await query.edit_message_text(full_welcome, reply_markup=reply_markup, parse_mode=None)
             elif query: await query.answer()
        except telegram_error.BadRequest as e:
              if "message is not modified" not in str(e).lower():
                  logger.warning(f"Failed to edit start message: {e}. Sending new.")
                  await send_message_with_retry(context.bot, chat_id, full_welcome, reply_markup=reply_markup, parse_mode=None)
              elif query: await query.answer()
        except Exception as e:
             logger.error(f"Unexpected error editing start message: {e}", exc_info=True)
             await send_message_with_retry(context.bot, chat_id, full_welcome, reply_markup=reply_markup, parse_mode=None)
    else:
        await send_message_with_retry(context.bot, chat_id, full_welcome, reply_markup=reply_markup, parse_mode=None)

# --- Other handlers ---
async def handle_back_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Back' button presses that should return to the main start menu."""
    await start(update, context)

# --- Shopping Handlers (Largely unchanged, ensure price is handled as Decimal where relevant) ---

async def handle_shop(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the list of cities for shopping."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    logger.info(f"handle_shop triggered by user {user_id}.")

    no_cities_available_msg = lang_data.get("no_cities_available", "No cities available at the moment. Please check back later.")
    choose_city_title = lang_data.get("choose_city_title", "Choose a City")
    select_location_prompt = lang_data.get("select_location_prompt", "Select your location:")
    home_button_text = lang_data.get("home_button", "Home")

    if not CITIES:
        logger.warning(f"handle_shop: CITIES dictionary is empty for user {user_id}.")
        keyboard = [[InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]]
        await query.edit_message_text(f"{EMOJI_CITY} {no_cities_available_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        return

    try:
        sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
        keyboard = []
        for c_id in sorted_city_ids:
             city_name = CITIES.get(c_id)
             if city_name: keyboard.append([InlineKeyboardButton(f"{EMOJI_CITY} {city_name}", callback_data=f"city|{c_id}")])
             else: logger.warning(f"handle_shop: City name missing for ID {c_id}.")
        keyboard.append([InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = f"{EMOJI_CITY} {choose_city_title}\n\n{select_location_prompt}"
        await query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode=None)
        logger.info(f"handle_shop: Sent city list to user {user_id}.")
    except telegram_error.BadRequest as e:
         if "message is not modified" not in str(e).lower(): logger.error(f"Error editing shop message: {e}"); await query.answer("Error displaying cities.", show_alert=True)
         else: await query.answer()
    except Exception as e:
        logger.error(f"Error in handle_shop for user {user_id}: {e}", exc_info=True)
        try: keyboard = [[InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]]; await query.edit_message_text("❌ An error occurred.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except Exception as inner_e: logger.error(f"Failed fallback in handle_shop: {inner_e}")


async def handle_city_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays districts for the selected city (SHOPPING FLOW)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params: logger.warning("handle_city_selection called without city_id."); await query.answer("Error: City ID missing.", show_alert=True); return
    city_id = params[0]
    city = CITIES.get(city_id)
    if not city: error_city_not_found = lang_data.get("error_city_not_found", "Error: City not found."); await query.edit_message_text(f"❌ {error_city_not_found}", parse_mode=None); return await handle_shop(update, context)

    districts_in_city = DISTRICTS.get(city_id, {})
    back_cities_button = lang_data.get("back_cities_button", "Back to Cities")
    home_button = lang_data.get("home_button", "Home")
    no_districts_msg = lang_data.get("no_districts_available", "No districts available yet for this city.")
    choose_district_prompt = lang_data.get("choose_district_prompt", "Choose a district:")

    if not districts_in_city:
        keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_cities_button}", callback_data="shop"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
        await query.edit_message_text(f"{EMOJI_CITY} {city}\n\n{no_districts_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    else:
        sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id, ''))
        keyboard = []
        for d_id in sorted_district_ids:
            dist_name = districts_in_city.get(d_id)
            if dist_name: keyboard.append([InlineKeyboardButton(f"{EMOJI_DISTRICT} {dist_name}", callback_data=f"dist|{city_id}|{d_id}")])
            else: logger.warning(f"District name missing for ID {d_id} city {city_id}")
        keyboard.append([InlineKeyboardButton(f"{EMOJI_BACK} {back_cities_button}", callback_data="shop"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")])
        await query.edit_message_text(f"{EMOJI_CITY} {city}\n\n{choose_district_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_district_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays product types available in the selected district (SHOPPING FLOW)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params or len(params) < 2: logger.warning("handle_district_selection missing params."); await query.answer("Error: City/District ID missing.", show_alert=True); return
    city_id, dist_id = params[0], params[1]
    city = CITIES.get(city_id); district = DISTRICTS.get(city_id, {}).get(dist_id)

    if not city or not district: error_district_city_not_found = lang_data.get("error_district_city_not_found", "Error: District or city not found."); await query.edit_message_text(f"❌ {error_district_city_not_found}", parse_mode=None); return await handle_shop(update, context)

    theme_name = context.user_data.get("theme", "default"); theme = THEMES.get(theme_name, THEMES["default"]); product_emoji = theme.get('product', EMOJI_PRODUCT)
    back_districts_button = lang_data.get("back_districts_button", "Back to Districts"); home_button = lang_data.get("home_button", "Home")
    no_types_msg = lang_data.get("no_types_available", "No product types currently available here."); select_type_prompt = lang_data.get("select_type_prompt", "Select product type:")
    error_loading_types = lang_data.get("error_loading_types", "Error: Failed to Load Product Types"); error_unexpected = lang_data.get("error_unexpected", "An unexpected error occurred")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT product_type FROM products WHERE city = ? AND district = ? AND available > reserved ORDER BY product_type", (city, district))
        available_types = [row['product_type'] for row in c.fetchall()]

        if not available_types:
            keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_districts_button}", callback_data=f"city|{city_id}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
            await query.edit_message_text(f"{EMOJI_CITY} {city}\n{EMOJI_DISTRICT} {district}\n\n{no_types_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        else:
            keyboard = [[InlineKeyboardButton(f"{product_emoji} {pt}", callback_data=f"type|{city_id}|{dist_id}|{pt}")] for pt in available_types]
            keyboard.append([InlineKeyboardButton(f"{EMOJI_BACK} {back_districts_button}", callback_data=f"city|{city_id}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")])
            await query.edit_message_text(f"{EMOJI_CITY} {city}\n{EMOJI_DISTRICT} {district}\n\n{select_type_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e: logger.error(f"DB error fetching product types {city}/{district}: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_loading_types}", parse_mode=None)
    except Exception as e: logger.error(f"Unexpected error in handle_district_selection: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_unexpected}", parse_mode=None)
    finally:
        if conn: conn.close()


async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays products (size/price variants) of the selected type (SHOPPING FLOW)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params or len(params) < 3: logger.warning("handle_type_selection missing params."); await query.answer("Error: City/District/Type missing.", show_alert=True); return
    city_id, dist_id, p_type = params
    city = CITIES.get(city_id); district = DISTRICTS.get(city_id, {}).get(dist_id)

    if not city or not district: error_district_city_not_found = lang_data.get("error_district_city_not_found", "Error: District or city not found."); await query.edit_message_text(f"❌ {error_district_city_not_found}", parse_mode=None); return await handle_shop(update, context)

    theme_name = context.user_data.get("theme", "default"); theme = THEMES.get(theme_name, THEMES["default"]); product_emoji = theme.get('product', EMOJI_PRODUCT)
    back_types_button = lang_data.get("back_types_button", "Back to Types"); home_button = lang_data.get("home_button", "Home")
    no_items_of_type = lang_data.get("no_items_of_type", "No items of this type currently available here.")
    available_options_prompt = lang_data.get("available_options_prompt", "Available options:")
    error_loading_products = lang_data.get("error_loading_products", "Error: Failed to Load Products"); error_unexpected = lang_data.get("error_unexpected", "An unexpected error occurred")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT size, price, COUNT(*) as count_available FROM products WHERE city = ? AND district = ? AND product_type = ? AND available > reserved GROUP BY size, price ORDER BY price", (city, district, p_type))
        products = c.fetchall()

        if not products:
            keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_types_button}", callback_data=f"dist|{city_id}|{dist_id}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
            await query.edit_message_text(f"{EMOJI_CITY} {city}\n{EMOJI_DISTRICT} {district}\n{product_emoji} {p_type}\n\n{no_items_of_type}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        else:
            keyboard = []
            available_label_short = lang_data.get("available_label_short", "Av")
            for row in products:
                size, price, count = row['size'], Decimal(str(row['price'])), row['count_available']  # Load price as Decimal
                price_str_formatted = format_currency(price)
                # Use consistent decimal places for callback data
                price_str_callback = f"{price:.2f}"
                button_text = f"{product_emoji} {size} ({price_str_formatted}€) - {available_label_short}: {count}"
                callback_data = f"product|{city_id}|{dist_id}|{p_type}|{size}|{price_str_callback}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

            keyboard.append([InlineKeyboardButton(f"{EMOJI_BACK} {back_types_button}", callback_data=f"dist|{city_id}|{dist_id}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")])
            await query.edit_message_text(f"{EMOJI_CITY} {city}\n{EMOJI_DISTRICT} {district}\n{product_emoji} {p_type}\n\n{available_options_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e: logger.error(f"DB error fetching products {city}/{district}/{p_type}: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_loading_products}", parse_mode=None)
    except Exception as e: logger.error(f"Unexpected error in handle_type_selection: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_unexpected}", parse_mode=None)
    finally:
        if conn: conn.close()


async def handle_product_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows details and 'Add to Basket' for a specific product variant (SHOPPING FLOW)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params or len(params) < 5: logger.warning("handle_product_selection missing params."); await query.answer("Error: Incomplete product data.", show_alert=True); return
    city_id, dist_id, p_type, size, price_str = params

    try: price = Decimal(price_str)  # Load price as Decimal
    except ValueError: logger.warning(f"Invalid price format: {price_str}"); await query.edit_message_text("❌ Error: Invalid product data.", parse_mode=None); return

    city = CITIES.get(city_id); district = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city or not district: error_location_mismatch = lang_data.get("error_location_mismatch", "Error: Location data mismatch."); await query.edit_message_text(f"❌ {error_location_mismatch}", parse_mode=None); return await handle_shop(update, context)

    theme_name = context.user_data.get("theme", "default"); theme = THEMES.get(theme_name, THEMES["default"]); product_emoji = theme.get('product', EMOJI_PRODUCT); basket_emoji = theme.get('basket', EMOJI_BASKET)
    price_label = lang_data.get("price_label", "Price"); available_label_long = lang_data.get("available_label_long", "Available")
    back_options_button = lang_data.get("back_options_button", "Back to Options"); home_button = lang_data.get("home_button", "Home")
    drop_unavailable_msg = lang_data.get("drop_unavailable", "Drop Unavailable! This option just sold out or was reserved.")
    add_to_basket_button = lang_data.get("add_to_basket_button", "Add to Basket")
    error_loading_details = lang_data.get("error_loading_details", "Error: Failed to Load Product Details"); error_unexpected = lang_data.get("error_unexpected", "An unexpected error occurred")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use float price for DB query as SQLite stores REAL
        c.execute("SELECT COUNT(*) as count FROM products WHERE city = ? AND district = ? AND product_type = ? AND size = ? AND price = ? AND available > reserved", (city, district, p_type, size, float(price)))
        available_count_result = c.fetchone(); available_count = available_count_result['count'] if available_count_result else 0

        if available_count <= 0:
            keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_options_button}", callback_data=f"type|{city_id}|{dist_id}|{p_type}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
            await query.edit_message_text(f"❌ {drop_unavailable_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        else:
            price_formatted = format_currency(price)  # Format Decimal price
            msg = (f"{EMOJI_CITY} {city} | {EMOJI_DISTRICT} {district}\n"
                   f"{product_emoji} {p_type} - {size}\n"
                   f"{EMOJI_PRICE} {price_label}: {price_formatted} EUR\n"
                   f"{EMOJI_QUANTITY} {available_label_long}: {available_count}")
            add_callback = f"add|{city_id}|{dist_id}|{p_type}|{size}|{price_str}"  # Keep price_str for callback
            back_callback = f"type|{city_id}|{dist_id}|{p_type}"
            keyboard = [
                [InlineKeyboardButton(f"{basket_emoji} {add_to_basket_button}", callback_data=add_callback)],
                [InlineKeyboardButton(f"{EMOJI_BACK} {back_options_button}", callback_data=back_callback), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]
            ]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e: logger.error(f"DB error checking availability {city}/{district}/{p_type}/{size}: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_loading_details}", parse_mode=None)
    except Exception as e: logger.error(f"Unexpected error in handle_product_selection: {e}", exc_info=True); await query.edit_message_text(f"❌ {error_unexpected}", parse_mode=None)
    finally:
        if conn: conn.close()


async def handle_add_to_basket(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Adds a selected product to the user's basket (DB and context)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params or len(params) < 5: logger.warning("handle_add_to_basket missing params."); await query.answer("Error: Incomplete product data.", show_alert=True); return
    city_id, dist_id, p_type, size, price_str = params

    try: price = Decimal(price_str)  # Load price as Decimal
    except ValueError: logger.warning(f"Invalid price format add_to_basket: {price_str}"); await query.edit_message_text("❌ Error: Invalid product data.", parse_mode=None); return

    city = CITIES.get(city_id); district = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city or not district: error_location_mismatch = lang_data.get("error_location_mismatch", "Error: Location data mismatch."); await query.edit_message_text(f"❌ {error_location_mismatch}", parse_mode=None); return await handle_shop(update, context)

    user_id = query.from_user.id
    theme_name = context.user_data.get("theme", "default"); theme = THEMES.get(theme_name, THEMES["default"])
    product_emoji = theme.get('product', EMOJI_PRODUCT); basket_emoji = theme.get('basket', EMOJI_BASKET)
    product_id_reserved = None; conn = None

    back_options_button = lang_data.get("back_options_button", "Back to Options"); home_button = lang_data.get("home_button", "Home")
    out_of_stock_msg = lang_data.get("out_of_stock", "Out of Stock! Sorry, the last one was taken or reserved.")
    pay_now_button_text = lang_data.get("pay_now_button", "Pay Now"); top_up_button_text = lang_data.get("top_up_button", "Top Up")
    view_basket_button_text = lang_data.get("view_basket_button", "View Basket"); clear_basket_button_text = lang_data.get("clear_basket_button", "Clear Basket")
    shop_more_button_text = lang_data.get("shop_more_button", "Shop More"); expires_label = lang_data.get("expires_label", "Expires")
    error_adding_db = lang_data.get("error_ dolaadding_db", "Error: Database issue adding item."); error_adding_unexpected = lang_data.get("error_adding_unexpected", "Error: An unexpected issue occurred.")
    added_msg_template = lang_data.get("added_to_basket", "✅ Item Reserved!\n\n{item} is in your basket for {timeout} minutes! ⏳")
    pay_msg_template = lang_data.get("pay", "💳 Total to Pay: {amount} EUR")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE")
        # Use float price for DB query
        c.execute("SELECT id FROM products WHERE city = ? AND district = ? AND product_type = ? AND size = ? AND price = ? AND available > reserved ORDER BY id LIMIT 1", (city, district, p_type, size, float(price)))
        product_row = c.fetchone()

        if not product_row:
            conn.rollback(); keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_options_button}", callback_data=f"type|{city_id}|{dist_id}|{p_type}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]; await query.edit_message_text(f"❌ {out_of_stock_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); return

        product_id_reserved = product_row['id']
        c.execute("UPDATE products SET reserved = reserved + 1 WHERE id = ?", (product_id_reserved,))
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        user_basket_row = c.fetchone(); current_basket_str = user_basket_row['basket'] if user_basket_row else ''
        timestamp = time.time(); new_item_str = f"{product_id_reserved}:{timestamp}"
        new_basket_str = f"{current_basket_str},{new_item_str}" if current_basket_str else new_item_str
        c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (new_basket_str, user_id))
        conn.commit()

        if "basket" not in context.user_data or not isinstance(context.user_data["basket"], list): context.user_data["basket"] = []
        # Store price as Decimal in context
        context.user_data["basket"].append({"product_id": product_id_reserved, "price": price, "timestamp": timestamp})
        logger.info(f"User {user_id} added product {product_id_reserved} to basket.")

        timeout_minutes = BASKET_TIMEOUT // 60
        current_basket_list = context.user_data["basket"]

        # Recalculate total using Decimal
        original_total = sum(item['price'] for item in current_basket_list)
        final_total = original_total; discount_amount = Decimal('0.0')
        applied_discount_info = context.user_data.get('applied_discount')
        pay_msg_str = ""

        if applied_discount_info:
             code_valid, _, discount_details = validate_discount_code(applied_discount_info['code'], float(original_total))  # Pass float for validation
             if code_valid and discount_details:
                 discount_amount = Decimal(str(discount_details['discount_amount']))  # Convert back to Decimal
                 final_total = Decimal(str(discount_details['final_total']))
                 context.user_data['applied_discount']['amount'] = float(discount_amount)  # Store as float
                 context.user_data['applied_discount']['final_total'] = float(final_total)

        final_total_str = format_currency(final_total)
        pay_msg_str = pay_msg_template.format(amount=final_total_str)
        if discount_amount > 0:
             original_total_str = format_currency(original_total)
             discount_amount_str = format_currency(discount_amount)
             pay_msg_str = f"~{original_total_str} EUR~ - {discount_amount_str} EUR Discount\n{pay_msg_str}"

        item_price_str = format_currency(price)  # Format Decimal price
        item_desc = f"{product_emoji} {p_type} {size} ({item_price_str}€)"
        expiry_dt = datetime.fromtimestamp(timestamp + BASKET_TIMEOUT); expiry_time_str = expiry_dt.strftime('%H:%M:%S')
        reserved_msg = (added_msg_template.format(timeout=timeout_minutes, item=item_desc) + "\n\n" + f"⏳ {expires_label}: {expiry_time_str}\n\n" + f"{pay_msg_str}")
        district_btn_text = district[:15]
        keyboard = [
            [InlineKeyboardButton(f"💳 {pay_now_button_text}", callback_data="confirm_pay"), InlineKeyboardButton(f"{EMOJI_REFILL} {top_up_button_text}", callback_data="refill")],
            [InlineKeyboardButton(f"{basket_emoji} {view_basket_button_text} ({len(current_basket_list)})", callback_data="view_basket"), InlineKeyboardButton(f"{basket_emoji} {clear_basket_button_text}", callback_data="clear_basket")],
            [InlineKeyboardButton(f"➕ {shop_more_button_text} ({district_btn_text})", callback_data=f"dist|{city_id}|{dist_id}")],
            [InlineKeyboardButton(f"{EMOJI_BACK} {back_options_button}", callback_data=f"type|{city_id}|{dist_id}|{p_type}"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]
        ]
        await query.edit_message_text(reserved_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except sqlite3.Error as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"DB error adding product {product_id_reserved if product_id_reserved else 'N/A'} user {user_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ {error_adding_db}", parse_mode=None)
    except Exception as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"Unexpected error adding item user {user_id}: {e}", exc_info=True)
        await query.edit_message_text(f"❌ {error_adding_unexpected}", parse_mode=None)
    finally:
        if conn: conn.close()


# --- Profile Handlers ---

async def handle_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the user's profile information."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    theme_name = context.user_data.get("theme", "default")
    theme = THEMES.get(theme_name, THEMES["default"])
    basket_emoji = theme.get('basket', EMOJI_BASKET)

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT balance, total_purchases FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if not result: logger.error(f"User {user_id} not found in DB for profile."); await query.edit_message_text("❌ Error: Could not load profile.", parse_mode=None); return
        balance, purchases = Decimal(str(result['balance'])), result['total_purchases']  # Load balance as Decimal

        clear_expired_basket(context, user_id)
        basket_count = len(context.user_data.get("basket", []))
        status = get_user_status(purchases); progress_bar = get_progress_bar(purchases); balance_str = format_currency(balance)
        status_label = lang_data.get("status_label", "Status"); balance_label = lang_data.get("balance_label", "Balance")
        purchases_label = lang_data.get("purchases_label", "Total Purchases"); basket_label = lang_data.get("basket_label", "Basket Items")
        profile_title = lang_data.get("profile_title", "Your Profile")
        profile_msg = (f"🎉 {profile_title}\n\n" f"👤 {status_label}: {status} {progress_bar}\n" f"💰 {balance_label}: {balance_str} EUR\n"
                       f"📦 {purchases_label}: {purchases}\n" f"🛒 {basket_label}: {basket_count}")

        top_up_button_text = lang_data.get("top_up_button", "Top Up"); view_basket_button_text = lang_data.get("view_basket_button", "View Basket")
        purchase_history_button_text = lang_data.get("purchase_history_button", "Purchase History"); home_button_text = lang_data.get("home_button", "Home")
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI_REFILL} {top_up_button_text}", callback_data="refill"), InlineKeyboardButton(f"{basket_emoji} {view_basket_button_text} ({basket_count})", callback_data="view_basket")],
            [InlineKeyboardButton(f"📜 {purchase_history_button_text}", callback_data="view_history")],
            [InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]
        ]
        await query.edit_message_text(profile_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except sqlite3.Error as e: logger.error(f"DB error loading profile user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Failed to Load Profile.", parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Unexpected BadRequest handle_profile user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Unexpected issue.", parse_mode=None)
        else: await query.answer()
    except Exception as e: logger.error(f"Unexpected error handle_profile user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Unexpected issue.", parse_mode=None)
    finally:
        if conn: conn.close()


# --- Discount Validation (Synchronous) ---
def validate_discount_code(code_text: str, current_total_float: float) -> tuple[bool, str, dict | None]:
    """
    Validates a discount code against the database. Synchronous.
    Args: current_total_float: The current basket total as a float.
    Returns: (is_valid: bool, message: str, details: dict | None)
             Details dict contains amounts as floats.
    """
    if not code_text: return False, "No code provided.", None
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM discount_codes WHERE code = ?", (code_text,))
        code_data = c.fetchone()

        if not code_data: return False, "Discount code not found.", None
        if not code_data['is_active']: return False, "This discount code is inactive.", None
        if code_data['expiry_date']:
            try:
                expiry_dt = datetime.fromisoformat(code_data['expiry_date'])
                # Make expiry timezone-aware if it isn't (assume local or UTC as appropriate)
                if expiry_dt.tzinfo is None:
                    # Decide on a default timezone, e.g., UTC or local system time
                    # expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)  # Example: Assume UTC
                    expiry_dt = expiry_dt.astimezone()  # Example: Assume local system time

                # Ensure 'now' is also timezone-aware for comparison
                if datetime.now(expiry_dt.tzinfo) > expiry_dt:
                    return False, "This discount code has expired.", None
            except ValueError: logger.warning(f"Invalid expiry_date format DB code {code_data['code']}"); return False, "Invalid code expiry data.", None
        if code_data['max_uses'] is not None and code_data['uses_count'] >= code_data['max_uses']: return False, "Code reached usage limit.", None

        discount_amount = 0.0
        dtype = code_data['discount_type']; value = Decimal(str(code_data['value']))  # Load value as Decimal
        current_total_decimal = Decimal(str(current_total_float))  # Convert input float to Decimal

        if dtype == 'percentage': discount_amount = (current_total_decimal * value) / Decimal('100.0')
        elif dtype == 'fixed': discount_amount = value
        else: logger.error(f"Unknown discount type '{dtype}' code {code_data['code']}"); return False, "Internal error processing discount type.", None

        # Ensure calculations use Decimal, then round final floats
        discount_amount = min(discount_amount, current_total_decimal)
        final_total_decimal = max(Decimal('0.0'), current_total_decimal - discount_amount)
        discount_amount_float = round(float(discount_amount), 2)
        final_total_float = round(float(final_total_decimal), 2)

        details = {'code': code_data['code'], 'type': dtype, 'value': float(value), 'discount_amount': discount_amount_float, 'final_total': final_total_float}
        code_display = code_data['code']; value_str_display = format_discount_value(dtype, float(value))
        amount_str_display = format_currency(discount_amount_float)
        message = f"Code '{code_display}' ({value_str_display}) applied. Discount: -{amount_str_display} EUR"
        return True, message, details

    except sqlite3.Error as e: logger.error(f"DB error validating discount code '{code_text}': {e}", exc_info=True); return False, "Database error validating code.", None
    except Exception as e: logger.error(f"Unexpected error validating code '{code_text}': {e}", exc_info=True); return False, "An unexpected error occurred.", None
    finally:
        if conn: conn.close()


# --- Basket Handlers ---

async def handle_view_basket(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the contents of the user's basket and applied discount."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    theme_name = context.user_data.get("theme", "default"); theme = THEMES.get(theme_name, THEMES["default"]); basket_emoji = theme.get('basket', EMOJI_BASKET)

    clear_expired_basket(context, user_id)  # Sync call
    basket = context.user_data.get("basket", [])
    applied_discount_info = context.user_data.get('applied_discount')
    discount_code_to_revalidate = applied_discount_info.get('code') if applied_discount_info else None

    if not basket:
        context.user_data.pop('applied_discount', None)
        basket_empty_msg = lang_data.get("basket_empty", "🛒 Your Basket is Empty!")
        add_items_prompt = lang_data.get("add_items_prompt", "Add items to start shopping!")
        shop_button_text = lang_data.get("shop_button", "Shop"); home_button_text = lang_data.get("home_button", "Home")
        full_empty_msg = basket_empty_msg + "\n\n" + add_items_prompt + " 😊"
        keyboard = [[InlineKeyboardButton(f"{EMOJI_SHOP} {shop_button_text}", callback_data="shop"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]]
        try: await query.edit_message_text(full_empty_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing empty basket msg: {e}")
             else: await query.answer()
        return

    msg = f"{basket_emoji} {lang_data.get('your_basket_title', 'Your Basket')}\n\n"
    original_total = Decimal('0.0')  # Use Decimal for total
    keyboard_items = []; product_db_details = {}; conn = None

    try:
        product_ids_in_basket = list(set(item['product_id'] for item in basket))
        if product_ids_in_basket:
             conn = get_db_connection()
             c = conn.cursor()
             placeholders = ','.join('?' for _ in product_ids_in_basket)
             c.execute(f"SELECT id, name, price, size, product_type FROM products WHERE id IN ({placeholders})", product_ids_in_basket)
             product_db_details = {row['id']: dict(row) for row in c.fetchall()}

        items_to_display_count = 0
        expires_in_label = lang_data.get("expires_in_label", "Expires in"); remove_button_label = lang_data.get("remove_button_label", "Remove")

        for index, item in enumerate(basket):
            prod_id = item['product_id']; details = product_db_details.get(prod_id)
            if not details: logger.warning(f"P{prod_id} missing DB details for view."); continue

            # Ensure item['price'] is Decimal if it exists in context, otherwise use DB price
            if 'price' in item and isinstance(item['price'], Decimal):
                price = item['price']
            else:
                price = Decimal(str(details['price']))  # Ensure DB price is Decimal

            timestamp = item['timestamp']
            item_desc = f"{details['product_type']} {details['size']}"
            item_price = format_currency(price)  # Format Decimal price
            remaining_time = max(0, int(BASKET_TIMEOUT - (time.time() - timestamp)))
            time_str = f"{remaining_time // 60} min {remaining_time % 60} sec"
            msg += (f"{items_to_display_count + 1}. {item_desc} ({item_price}€)\n" f"   ⏳ {expires_in_label}: {time_str}\n")
            remove_button_text = f"🗑️ {remove_button_label} {item_desc}"[:60]
            keyboard_items.append([InlineKeyboardButton(remove_button_text, callback_data=f"remove|{prod_id}")])
            original_total += price  # Add Decimal prices
            items_to_display_count += 1

        if items_to_display_count == 0:
             context.user_data.pop('applied_discount', None); context.user_data['basket'] = []
             basket_empty_msg = lang_data.get("basket_empty", "🛒 Your Basket is Empty!"); items_expired_note = lang_data.get("items_expired_note", "Items may have expired or were removed.")
             shop_button_text = lang_data.get("shop_button", "Shop"); home_button_text = lang_data.get("home_button", "Home")
             full_empty_msg = basket_empty_msg + "\n\n" + items_expired_note
             keyboard = [[InlineKeyboardButton(f"🛍️ {shop_button_text}", callback_data="shop"), InlineKeyboardButton(f"🏠 {home_button_text}", callback_data="back_start")]]; await query.edit_message_text(full_empty_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); return

        # Apply Discount Logic (using Decimal)
        discount_amount = Decimal('0.0'); final_total = original_total; discount_applied_str = ""
        discount_applied_label = lang_data.get("discount_applied_label", "Discount Applied"); discount_removed_note_template = lang_data.get("discount_removed_note", "Discount code {code} removed: {reason}")

        if discount_code_to_revalidate:
            code_valid, validation_message, discount_details = validate_discount_code(discount_code_to_revalidate, float(original_total))  # Pass float for validation check
            if code_valid and discount_details:
                discount_amount = Decimal(str(discount_details['discount_amount']))  # Convert back to Decimal
                final_total = Decimal(str(discount_details['final_total']))
                discount_code = discount_code_to_revalidate; discount_value = format_discount_value(discount_details['type'], discount_details['value'])
                discount_amount_str = format_currency(discount_amount)
                discount_applied_str = (f"\n{EMOJI_DISCOUNT} {discount_applied_label} ({discount_code}: {discount_value}): -{discount_amount_str} EUR")
                context.user_data['applied_discount'] = {'code': discount_code_to_revalidate, 'amount': float(discount_amount), 'final_total': float(final_total)}  # Store as float
            else:
                context.user_data.pop('applied_discount', None); logger.info(f"Discount '{discount_code_to_revalidate}' invalid user {user_id}. Reason: {validation_message}")
                discount_applied_str = f"\n{discount_removed_note_template.format(code=discount_code_to_revalidate, reason=validation_message)}"

        # Display Totals (using Decimal)
        subtotal_label = lang_data.get("subtotal_label", "Subtotal"); total_label = lang_data.get("total_label", "Total")
        original_total_str = format_currency(original_total); final_total_str = format_currency(final_total)
        msg += f"\n{subtotal_label}: {original_total_str} EUR"
        msg += discount_applied_str if discount_applied_str else ""
        msg += f"\n💳 {total_label}: {final_total_str} EUR"

        # Button Texts
        pay_now_button_text = lang_data.get("pay_now_button", "Pay Now"); clear_all_button_text = lang_data.get("clear_all_button", "Clear All")
        remove_discount_button_text = lang_data.get("remove_discount_button", "Remove Discount"); apply_discount_button_text = lang_data.get("apply_discount_button", "Apply Discount Code")
        shop_more_button_text = lang_data.get("shop_more_button", "Shop More"); home_button_text = lang_data.get("home_button", "Home")

        # Keyboard
        action_buttons = [
            [InlineKeyboardButton(f"💳 {pay_now_button_text}", callback_data="confirm_pay"), InlineKeyboardButton(f"{basket_emoji} {clear_all_button_text}", callback_data="clear_basket")],
            *([[InlineKeyboardButton(f"❌ {remove_discount_button_text}", callback_data="remove_discount")]] if context.user_data.get('applied_discount') else []),
            [InlineKeyboardButton(f"{EMOJI_DISCOUNT} {apply_discount_button_text}", callback_data="apply_discount_start")],
            [InlineKeyboardButton(f"{EMOJI_SHOP} {shop_more_button_text}", callback_data="shop"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]
        ]
        final_keyboard = keyboard_items + action_buttons

        try: await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(final_keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing basket view message: {e}")
             else: await query.answer()

    except sqlite3.Error as e: logger.error(f"DB error viewing basket user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Failed to Load Basket.", parse_mode=None)
    except Exception as e: logger.error(f"Unexpected error viewing basket user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Unexpected issue.", parse_mode=None)
    finally:
         if conn: conn.close()


# --- Discount Application Handlers ---

async def apply_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Prompts the user to enter a discount code."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    clear_expired_basket(context, user_id)
    basket = context.user_data.get("basket", [])
    if not basket: no_items_message = lang_data.get("discount_no_items", "Your basket is empty."); await query.answer(no_items_message, show_alert=True); return await handle_view_basket(update, context)

    context.user_data['state'] = 'awaiting_user_discount_code'
    cancel_button_text = lang_data.get("cancel_button", "Cancel")
    keyboard = [[InlineKeyboardButton(f"❌ {cancel_button_text}", callback_data="view_basket")]]
    enter_code_prompt = lang_data.get("enter_discount_code_prompt", "Please enter your discount code:")
    await query.edit_message_text(f"{EMOJI_DISCOUNT} {enter_code_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer(lang_data.get("enter_code_answer", "Enter code in chat."))


async def remove_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Removes any applied discount code."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if 'applied_discount' in context.user_data:
        removed_code = context.user_data.pop('applied_discount')['code']
        logger.info(f"User {user_id} removed discount code '{removed_code}'.")
        discount_removed_answer = lang_data.get("discount_removed_answer", "Discount removed.")
        await query.answer(discount_removed_answer)
    else: no_discount_answer = lang_data.get("no_discount_answer", "No discount applied."); await query.answer(no_discount_answer, show_alert=False)
    await handle_view_basket(update, context)  # Refresh basket view


async def handle_user_discount_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user entering a discount code, validates, and applies it."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = context.user_data.get("state")
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if state != "awaiting_user_discount_code": return
    if not update.message or not update.message.text: send_text_please = lang_data.get("send_text_please", "Please send the code as text."); await send_message_with_retry(context.bot, chat_id, send_text_please, parse_mode=None); return

    entered_code = update.message.text.strip()
    context.user_data.pop('state', None)
    view_basket_button_text = lang_data.get("view_basket_button", "View Basket"); returning_to_basket_msg = lang_data.get("returning_to_basket", "Returning to basket.")

    if not entered_code: no_code_entered_msg = lang_data.get("no_code_entered", "No code entered."); await send_message_with_retry(context.bot, chat_id, no_code_entered_msg, parse_mode=None); keyboard = [[InlineKeyboardButton(view_basket_button_text, callback_data="view_basket")]]; await send_message_with_retry(context.bot, chat_id, returning_to_basket_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); return

    # Recalculate current basket total using Decimal
    clear_expired_basket(context, user_id)
    basket = context.user_data.get("basket", [])
    original_total_decimal = Decimal('0.0'); conn = None
    if basket:
         try:
            product_ids_in_basket = list(set(item['product_id'] for item in basket))
            conn = get_db_connection()
            c = conn.cursor()
            placeholders = ','.join('?' for _ in product_ids_in_basket)
            c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", product_ids_in_basket)
            prices_dict = {row['id']: Decimal(str(row['price'])) for row in c.fetchall()}
            original_total_decimal = sum(prices_dict.get(item['product_id'], Decimal('0.0')) for item in basket if item['product_id'] in prices_dict)
         except sqlite3.Error as e: logger.error(f"DB error recalculating total user {user_id}: {e}"); error_calc_total = lang_data.get("error_calculating_total", "Error calculating total."); await send_message_with_retry(context.bot, chat_id, f"❌ {error_calc_total}", parse_mode=None); kb = [[InlineKeyboardButton(view_basket_button_text, callback_data="view_basket")]]; await send_message_with_retry(context.bot, chat_id, returning_to_basket_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return
         finally:
              if conn: conn.close()
    else:
        basket_empty_no_discount = lang_data.get("basket_empty_no_discount", "Basket empty. Cannot apply code."); await send_message_with_retry(context.bot, chat_id, basket_empty_no_discount, parse_mode=None); kb = [[InlineKeyboardButton(view_basket_button_text, callback_data="view_basket")]]; await send_message_with_retry(context.bot, chat_id, returning_to_basket_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return

    # Validate (passing float total) and Apply
    code_valid, message, discount_details = validate_discount_code(entered_code, float(original_total_decimal))

    if code_valid and discount_details:
        context.user_data['applied_discount'] = {'code': entered_code, 'amount': discount_details['discount_amount'], 'final_total': discount_details['final_total']}  # Stored as float
        logger.info(f"User {user_id} applied discount code '{entered_code}'.")
        success_label = lang_data.get("success_label", "Success!")
        feedback_msg = f"✅ {success_label} {message}"
    else:
        context.user_data.pop('applied_discount', None)
        logger.warning(f"User {user_id} failed to apply code '{entered_code}': {message}")
        feedback_msg = f"❌ {message}"

    keyboard = [[InlineKeyboardButton(view_basket_button_text, callback_data="view_basket")]]
    await send_message_with_retry(context.bot, chat_id, feedback_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Remove From Basket ---
async def handle_remove_from_basket(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Removes a specific item from the user's basket."""
    query = update.callback_query
    user_id = query.from_user.id

    if not params: logger.warning(f"handle_remove_from_basket no product_id user {user_id}."); await query.answer("Error: Product ID missing.", show_alert=True); return
    try: product_id_to_remove = int(params[0])
    except ValueError: logger.warning(f"Invalid product_id format user {user_id}: {params[0]}"); await query.answer("Error: Invalid product data.", show_alert=True); return

    logger.info(f"Attempting remove product {product_id_to_remove} user {user_id}.")
    item_removed_from_context = False; item_to_remove_str = None; conn = None
    current_basket_context = context.user_data.get("basket", []); new_basket_context = []
    found_item_index = -1

    for index, item in enumerate(current_basket_context):
        if item.get('product_id') == product_id_to_remove:
            found_item_index = index
            try: timestamp_float = float(item['timestamp']); item_to_remove_str = f"{item['product_id']}:{timestamp_float}"
            except (ValueError, TypeError, KeyError) as e: logger.error(f"Invalid format in context item {item}: {e}"); item_to_remove_str = None
            break

    if found_item_index != -1:
        item_removed_from_context = True
        new_basket_context = current_basket_context[:found_item_index] + current_basket_context[found_item_index+1:]
        logger.debug(f"Found item {product_id_to_remove} in context user {user_id}. DB String: {item_to_remove_str}")
    else: logger.warning(f"Product {product_id_to_remove} not in user_data basket user {user_id}."); new_basket_context = list(current_basket_context)

    # DB Update
    try:
        conn = get_db_connection()
        c = conn.cursor(); c.execute("BEGIN")
        if item_removed_from_context:
             update_result = c.execute("UPDATE products SET reserved = MAX(0, reserved - 1) WHERE id = ?", (product_id_to_remove,))
             if update_result.rowcount > 0: logger.debug(f"Decremented reservation P{product_id_to_remove}.")
             else: logger.warning(f"Could not find P{product_id_to_remove} to decrement reservation.")
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        db_basket_result = c.fetchone(); db_basket_str = db_basket_result['basket'] if db_basket_result else ''
        if db_basket_str and item_to_remove_str:
            items_list = db_basket_str.split(',')
            if item_to_remove_str in items_list:
                items_list.remove(item_to_remove_str); new_db_basket_str = ','.join(items_list)
                c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (new_db_basket_str, user_id)); logger.debug(f"Updated DB basket user {user_id} to: {new_db_basket_str}")
            else: logger.warning(f"Item string '{item_to_remove_str}' not found in DB basket '{db_basket_str}' user {user_id}.")
        elif item_removed_from_context and not item_to_remove_str: logger.warning(f"Could not construct item string for DB removal P{product_id_to_remove}.")
        elif not item_removed_from_context: logger.debug(f"Item {product_id_to_remove} not in context, DB basket not modified.")
        conn.commit()
        logger.info(f"DB ops complete remove P{product_id_to_remove} user {user_id}.")

        # Update context AFTER successful DB commit
        context.user_data['basket'] = new_basket_context
        # Revalidate discount after context update
        if not context.user_data['basket']: context.user_data.pop('applied_discount', None)
        elif context.user_data.get('applied_discount'):
             applied_discount_info = context.user_data['applied_discount']
             # Calculate total using Decimal, convert to float for validation
             basket_total_after_removal = float(sum(item.get('price', Decimal('0.0')) for item in context.user_data['basket']))
             code_valid, _, _ = validate_discount_code(applied_discount_info['code'], basket_total_after_removal)
             if not code_valid: context.user_data.pop('applied_discount', None); await query.answer("Discount removed (no longer valid).", show_alert=False)

    except sqlite3.Error as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"DB error removing item {product_id_to_remove} user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Failed to remove item (DB).", parse_mode=None); return
    except Exception as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"Unexpected error removing item {product_id_to_remove} user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Unexpected issue removing item.", parse_mode=None); return
    finally:
        if conn: conn.close()
    await handle_view_basket(update, context)  # Refresh basket view


async def handle_clear_basket(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Clears all items from the user's basket."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    conn = None

    current_basket_context = context.user_data.get("basket", [])
    if not current_basket_context: already_empty_msg = lang_data.get("basket_already_empty", "Basket already empty."); await query.answer(already_empty_msg, show_alert=False); return await handle_view_basket(update, context)

    product_ids_to_release_counts = Counter(item['product_id'] for item in current_basket_context)

    try:
        conn = get_db_connection()
        c = conn.cursor(); c.execute("BEGIN"); c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,))
        if product_ids_to_release_counts:
             decrement_data = [(count, pid) for pid, count in product_ids_to_release_counts.items()]
             c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
             total_items_released = sum(product_ids_to_release_counts.values()); logger.info(f"Released {total_items_released} reservations user {user_id} clear.")
        conn.commit()
        context.user_data["basket"] = []; context.user_data.pop('applied_discount', None)
        logger.info(f"Cleared basket/discount user {user_id}.")
        shop_button_text = lang_data.get("shop_button", "Shop"); home_button_text = lang_data.get("home_button", "Home")
        cleared_msg = lang_data.get("basket_cleared", "🗑️ Basket Cleared!")
        keyboard = [[InlineKeyboardButton(f"{EMOJI_SHOP} {shop_button_text}", callback_data="shop"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")]]
        await query.edit_message_text(cleared_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except sqlite3.Error as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"DB error clearing basket user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: DB issue clearing basket.", parse_mode=None)
    except Exception as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"Unexpected error clearing basket user {user_id}: {e}", exc_info=True); await query.edit_message_text("❌ Error: Unexpected issue.", parse_mode=None)
    finally:
        if conn: conn.close()


# --- Other User Handlers ---

async def handle_view_history(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the user's recent purchase history."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    history = fetch_last_purchases(user_id, limit=10)

    history_title = lang_data.get("purchase_history_title", "Purchase History"); no_history_msg = lang_data.get("no_purchases_yet", "No purchases yet.")
    recent_purchases_title = lang_data.get("recent_purchases_title", "Recent Purchases"); back_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    home_button = lang_data.get("home_button", "Home"); unknown_date_label = lang_data.get("unknown_date_label", "Unknown Date")

    if not history: msg = f"📜 {history_title}\n\n{no_history_msg}"; keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_profile_button}", callback_data="profile"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
    else:
        msg = f"📜 {recent_purchases_title}\n\n"
        for i, purchase in enumerate(history):
            try:
                # Attempt to parse with timezone, fallback if not present
                dt_obj = datetime.fromisoformat(purchase['purchase_date'].replace('Z', '+00:00'))
                date_str = dt_obj.strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError): date_str = unknown_date_label
            name = purchase.get('product_name', 'N/A'); size = purchase.get('product_size', 'N/A')
            price_str = format_currency(Decimal(str(purchase.get('price_paid', 0.0))))  # Ensure Decimal for format
            msg += (f"{i+1}. {date_str} - {name} ({size}) - {price_str} EUR\n")
        keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_profile_button}", callback_data="profile"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]

    try: await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing history msg: {e}")
        else: await query.answer()


async def handle_language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Allows the user to select their preferred language."""
    query = update.callback_query
    user_id = query.from_user.id
    current_lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(current_lang, LANGUAGES['en'])
    conn = None

    if params:  # Language code passed in callback data
        new_lang = params[0]
        if new_lang in LANGUAGES:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("UPDATE users SET language = ? WHERE user_id = ?", (new_lang, user_id))
                conn.commit()
                context.user_data["lang"] = new_lang
                logger.info(f"User {user_id} changed language to {new_lang}")
                await start(update, context)  # Refresh start menu
                language_set_answer = LANGUAGES.get(new_lang, {}).get("language_set_answer", "Language set!")
                await query.answer(language_set_answer.format(lang=new_lang.upper()))
            except sqlite3.Error as e: logger.error(f"DB error updating language user {user_id}: {e}"); error_saving_lang = lang_data.get("error_saving_language", "Error saving."); await query.answer(error_saving_lang, show_alert=True)
            finally:
                if conn: conn.close()
        else: invalid_lang_answer = lang_data.get("invalid_language_answer", "Invalid language."); await query.answer(invalid_lang_answer, show_alert=True)
    else:  # Display language selection menu
        keyboard = []
        for lang_code, lang_dict_for_name in LANGUAGES.items():
            lang_name = lang_dict_for_name.get("native_name", lang_code.upper())
            keyboard.append([InlineKeyboardButton(f"{lang_name} {'✅' if lang_code == current_lang else ''}", callback_data=f"language|{lang_code}")])
        back_button_text = lang_data.get("back_button", "Back")
        keyboard.append([InlineKeyboardButton(f"{EMOJI_BACK} {back_button_text}", callback_data="back_start")])
        lang_select_prompt = lang_data.get("language", "🌐 Select Language:")
        await query.edit_message_text(lang_select_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_price_list(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows the city selection specifically for viewing price lists."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not CITIES: no_cities_msg = lang_data.get("no_cities_for_prices", "No cities available."); keyboard = [[InlineKeyboardButton(f"{EMOJI_HOME} {lang_data.get('home_button', 'Home')}", callback_data="back_start")]]; await query.edit_message_text(f"{EMOJI_CITY} {no_cities_msg}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); return

    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    home_button_text = lang_data.get("home_button", "Home")
    keyboard = [[InlineKeyboardButton(f"{EMOJI_CITY} {CITIES.get(c, 'N/A')}", callback_data=f"price_list_city|{c}")] for c in sorted_city_ids if CITIES.get(c)]
    keyboard.append([InlineKeyboardButton(f"{EMOJI_HOME} {home_button_text}", callback_data="back_start")])
    price_list_title = lang_data.get("price_list_title", "Price List"); select_city_prompt = lang_data.get("select_city_prices_prompt", "Select a city:")
    await query.edit_message_text(f"{EMOJI_PRICELIST} {price_list_title}\n\n{select_city_prompt}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_price_list_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the formatted price list for the selected city."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not params: logger.warning("handle_price_list_city no city_id."); await query.answer("Error: City ID missing.", show_alert=True); return

    city_id = params[0]; city_name = CITIES.get(city_id)
    if not city_name: error_city_not_found = lang_data.get("error_city_not_found", "Error: City not found."); await query.edit_message_text(f"❌ {error_city_not_found}", parse_mode=None); return await handle_price_list(update, context)

    price_list_title_city_template = lang_data.get("price_list_title_city", "Price List: {city_name}"); msg = f"{EMOJI_PRICELIST} {price_list_title_city_template.format(city_name=city_name)}\n\n"
    found_products = False; conn = None

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT product_type, size, price, district, COUNT(*) as quantity FROM products WHERE city = ? AND available > reserved GROUP BY product_type, size, price, district ORDER BY product_type, price, size, district", (city_name,))
        results = c.fetchall()
        no_products_in_city = lang_data.get("no_products_in_city", "No products available here."); available_label = lang_data.get("available_label", "available")

        if not results: msg += no_products_in_city
        else:
            found_products = True
            grouped_data = defaultdict(lambda: defaultdict(list))
            for row in results: price_size_key = (Decimal(str(row['price'])), row['size']); grouped_data[row['product_type']][price_size_key].append((row['district'], row['quantity']))

            for p_type in sorted(grouped_data.keys()):
                type_data = grouped_data[p_type]; sorted_price_size = sorted(type_data.keys(), key=lambda x: (x[0], x[1]))
                for price, size in sorted_price_size:
                    districts_list = type_data[(price, size)]; price_str = format_currency(price)  # Format Decimal
                    prod_emoji = EMOJI_HERB if 'herb' in p_type.lower() else EMOJI_PRODUCT
                    msg += f"\n{prod_emoji} {p_type} {size} ({price_str}€)\n"
                    districts_list.sort(key=lambda x: x[0])
                    for district, quantity in districts_list: msg += f"  • {EMOJI_DISTRICT} {district}: {quantity} {available_label}\n"

        back_city_list_button = lang_data.get("back_city_list_button", "Back to City List"); home_button = lang_data.get("home_button", "Home")
        keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_city_list_button}", callback_data="price_list"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]

        try:
            if len(msg) > 4000: truncated_note = lang_data.get("message_truncated_note", "Message truncated."); msg = msg[:4000] + f"\n\n✂️ ... {truncated_note}"; logger.warning(f"Price list message truncated {city_name}.")
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing price list: {e}. Snippet: {msg[:200]}")
                 error_displaying_prices = lang_data.get("error_displaying_prices", "Error displaying prices.")  # Corrected: Explicit variable
                 await query.answer(error_displaying_prices, show_alert=True)  # Corrected: Explicit variable
             else:
                 await query.answer()

    except sqlite3.Error as e:
        logger.error(f"DB error fetching price list city {city_name}: {e}", exc_info=True)
        error_loading_prices_db_template = lang_data.get("error_loading_prices_db", "Error: DB Load Error {city_name}")
        await query.edit_message_text(f"❌ {error_loading_prices_db_template.format(city_name=city_name)}", parse_mode=None)
    except Exception as e:
        logger.error(f"Unexpected error price list city {city_name}: {e}", exc_info=True)
        error_unexpected_prices = lang_data.get("error_unexpected_prices", "Error: Unexpected issue.")
        await query.edit_message_text(f"❌ {error_unexpected_prices}", parse_mode=None)
    finally:
         if conn: conn.close()


# --- Review Handlers ---

async def handle_reviews_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows the main menu for reviews (View or Leave)."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    review_prompt = lang_data.get("reviews", "📝 Reviews Menu")
    view_reviews_button = lang_data.get("view_reviews_button", "View Reviews")
    leave_review_button = lang_data.get("leave_review_button", "Leave a Review")
    home_button = lang_data.get("home_button", "Home")
    keyboard = [
        [InlineKeyboardButton(f"👀 {view_reviews_button}", callback_data="view_reviews|0")],
        [InlineKeyboardButton(f"✍️ {leave_review_button}", callback_data="leave_review")],
        [InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)  # Define reply_markup explicitly
    await query.edit_message_text(review_prompt, reply_markup=reply_markup, parse_mode=None)  # Pass it here


async def handle_leave_review(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Prompts user for review text."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en"); lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    context.user_data["state"] = "awaiting_review"
    enter_review_prompt = lang_data.get("enter_review_prompt", "Please type your review:"); cancel_button_text = lang_data.get("cancel_button", "Cancel"); prompt_msg = f"✍️ {enter_review_prompt}"
    keyboard = [[InlineKeyboardButton(f"❌ {cancel_button_text}", callback_data="reviews")]]
    try: await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); enter_review_answer = lang_data.get("enter_review_answer", "Enter review in chat."); await query.answer(enter_review_answer)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing leave review prompt: {e}"); await send_message_with_retry(context.bot, update.effective_chat.id, prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); await query.answer()
        else: await query.answer()
    except Exception as e: logger.error(f"Unexpected error handle_leave_review: {e}", exc_info=True); await query.answer("Error occurred.", show_alert=True)


async def handle_leave_review_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the text message containing the user's review."""
    user_id = update.effective_user.id; chat_id = update.effective_chat.id; state = context.user_data.get("state"); lang = context.user_data.get("lang", "en"); lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if state != "awaiting_review": return
    send_text_review_please = lang_data.get("send_text_review_please", "Please send text only."); review_not_empty = lang_data.get("review_not_empty", "Review cannot be empty."); review_too_long = lang_data.get("review_too_long", "Review too long (max 1000 chars)."); review_thanks = lang_data.get("review_thanks", "Thank you for your review!"); error_saving_review_db = lang_data.get("error_saving_review_db", "Error saving review (DB)."); error_saving_review_unexpected = lang_data.get("error_saving_review_unexpected", "Error saving review (unexpected)."); view_reviews_button = lang_data.get("view_reviews_button", "View Reviews"); home_button = lang_data.get("home_button", "Home")
    if not update.message or not update.message.text: await send_message_with_retry(context.bot, chat_id, send_text_review_please, parse_mode=None); return
    review_text = update.message.text.strip()
    if not review_text: await send_message_with_retry(context.bot, chat_id, review_not_empty, parse_mode=None); return
    if len(review_text) > 1000: await send_message_with_retry(context.bot, chat_id, review_too_long, parse_mode=None); return
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO reviews (user_id, review_text, review_date) VALUES (?, ?, ?)", (user_id, review_text, datetime.now(timezone.utc).isoformat()))
        conn.commit(); logger.info(f"User {user_id} left review."); context.user_data.pop("state", None)
        success_msg = f"✅ {review_thanks}"; keyboard = [[InlineKeyboardButton(f"👀 {view_reviews_button}", callback_data="view_reviews|0"), InlineKeyboardButton(f"{EMOJI_HOME} {home_button}", callback_data="back_start")]]
        await send_message_with_retry(context.bot, chat_id, success_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e: logger.error(f"DB error saving review user {user_id}: {e}", exc_info=True); if conn and conn.in_transaction: conn.rollback(); await send_message_with_retry(context.bot, chat_id, f"❌ {error_saving_review_db}", parse_mode=None); context.user_data.pop("state", None)
    except Exception as e: logger.error(f"Unexpected error saving review user {user_id}: {e}", exc_info=True); if conn and conn.in_transaction: conn.rollback(); await send_message_with_retry(context.bot, chat_id, f"❌ {error_saving_review_unexpected}", parse_mode=None); context.user_data.pop("state", None)
    finally:
        if conn: conn.close()

async def handle_view_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays reviews paginated for users."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    offset = 0; reviews_per_page = 5
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])
    reviews_data = fetch_reviews(offset=offset, limit=reviews_per_page + 1)
    user_reviews_title = lang_data.get("user_reviews_title", "User Reviews"); no_reviews_yet = lang_data.get("no_reviews_yet", "No reviews yet."); no_more_reviews = lang_data.get("no_more_reviews", "No more reviews."); prev_button = lang_data.get("prev_button", "Prev"); next_button = lang_data.get("next_button", "Next"); back_review_menu_button = lang_data.get("back_review_menu_button", "Back to Reviews"); unknown_date_label = lang_data.get("unknown_date_label", "Unknown Date"); error_displaying_review = lang_data.get("error_displaying_review", "Error display"); error_updating_review_list = lang_data.get("error_updating_review_list", "Error updating list.")
    msg = f"{EMOJI_REVIEW} {user_reviews_title}\n\n"; keyboard = []
    if not reviews_data:
        if offset == 0: msg += no_reviews_yet; keyboard = [[InlineKeyboardButton(f"{EMOJI_BACK} {back_review_menu_button}", callback_data="reviews")]]
        else: msg += no_more_reviews; keyboard = [[InlineKeyboardButton(f"⬅️ {prev_button}", callback_data=f"view_reviews|{max(0, offset - reviews_per_page)}")], [InlineKeyboardButton(f"{EMOJI_BACK} {back_review_menu_button}", callback_data="reviews")]]
    else:
        has_more = len(reviews_data) > reviews_per_page; reviews_to_show = reviews_data[:reviews_per_page]
        for review in reviews_to_show:
            try:
                date_str = review.get('review_date', ''); formatted_date = unknown_date_label
                if date_str: try: formatted_date = datetime.fromisoformat(date_str.replace('Z', '+00:00')).strftime("%Y-%m-%d") except ValueError: pass
                username = review.get('username', 'anonymous'); username_display = f"@{username}" if username and username != 'anonymous' else username
                review_text = review.get('review_text', ''); msg += f"{EMOJI_PROFILE} {username_display} ({formatted_date}):\n{review_text}\n\n"
            except Exception as e: logger.error(f"Error formatting review: {review}, Error: {e}"); msg += f"({error_displaying_review})\n\n"
        nav_buttons = []
        if offset > 0: nav_buttons.append(InlineKeyboardButton(f"⬅️ {prev_button}", callback_data=f"view_reviews|{max(0, offset - reviews_per_page)}"))
        if has_more: nav_buttons.append(InlineKeyboardButton(f"➡️ {next_button}", callback_data=f"view_reviews|{offset + reviews_per_page}"))
        if nav_buttons: keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton(f"{EMOJI_BACK} {back_review_menu_button}", callback_data="reviews")])
    try: await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Failed edit view_reviews: {e}"); await query.answer(error_updating_review_list, show_alert=True)
        else: await query.answer()

async def handle_leave_review_now(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Callback handler specifically for the 'Leave Review Now' button after purchase."""
    await handle_leave_review(update, context, params)

# --- Refill Handlers (Modified for NOWPayments) ---
async def handle_refill(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the refill/top-up button press."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not NOWPAYMENTS_API_KEY:  # Check if NOWPayments is configured
        crypto_disabled_msg = lang_data.get("crypto_payment_disabled", "Top Up is currently disabled.")
        await query.answer(crypto_disabled_msg, show_alert=True)
        logger.warning(f"User {user_id} tried to refill, but NOWPAYMENTS_API_KEY is not set.")
        return

    context.user_data['state'] = 'awaiting_refill_amount'
    logger.info(f"User {user_id} initiated refill process. State -> awaiting_refill_amount.")

    top_up_title = lang_data.get("top_up_title", "Top Up Balance")
    enter_refill_amount_prompt = lang_data.get("enter_refill_amount_prompt", "Please reply with the amount in EUR you wish to add (e.g., 10 or 25.50).")
    min_top_up_note_template = lang_data.get("min_top_up_note", "Minimum top up: {amount} EUR")
    cancel_button_text = lang_data.get("cancel_button", "Cancel")
    enter_amount_answer = lang_data.get("enter_amount_answer", "Enter the top-up amount.")

    min_amount_str = format_currency(MIN_DEPOSIT_EUR)  # Use constant
    min_top_up_note = min_top_up_note_template.format(amount=min_amount_str)
    prompt_msg = (f"{EMOJI_REFILL} {top_up_title}\n\n{enter_refill_amount_prompt}\n\n{min_top_up_note}")
    keyboard = [[InlineKeyboardButton(f"❌ {cancel_button_text}", callback_data="profile")]]

    try:
        await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer(enter_amount_answer)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing refill prompt: {e}"); await send_message_with_retry(context.bot, chat_id, prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None); await query.answer()
        else: await query.answer(enter_amount_answer)
    except Exception as e: logger.error(f"Unexpected error handle_refill: {e}", exc_info=True); error_occurred_answer = lang_data.get("error_occurred_answer", "An error occurred."); await query.answer(error_occurred_answer, show_alert=True)


async def handle_refill_amount_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user entering the top-up amount and shows crypto choices."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = context.user_data.get("state")
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if state != "awaiting_refill_amount": logger.debug(f"Ignore msg user {user_id}, state: {state}"); return

    send_amount_as_text = lang_data.get("send_amount_as_text", "Send amount as text (e.g., 10).")
    amount_too_low_msg_template = lang_data.get("amount_too_low_msg", "Amount too low. Min: {amount} EUR.")
    amount_too_high_msg = lang_data.get("amount_too_high_msg", "Amount too high. Max: 10000 EUR.")
    invalid_amount_format_msg = lang_data.get("invalid_amount_format_msg", "Invalid amount format (e.g., 10.50).")
    unexpected_error_msg = lang_data.get("unexpected_error_msg", "Unexpected error. Try again.")
    choose_crypto_prompt_template = lang_data.get("choose_crypto_prompt", "Top up {amount} EUR. Choose crypto:")
    cancel_top_up_button = lang_data.get("cancel_top_up_button", "Cancel Top Up")

    if not update.message or not update.message.text: 
        await send_message_with_retry(context.bot, chat_id, f"❌ {send_amount_as_text}", parse_mode=None)
        return

    amount_text = update.message.text.strip().replace(',', '.')

    try:
        refill_amount_decimal = Decimal(amount_text)
        if refill_amount_decimal < MIN_DEPOSIT_EUR:
            min_amount_str = format_currency(MIN_DEPOSIT_EUR)
            amount_too_low_msg = amount_too_low_msg_template.format(amount=min_amount_str)
            await send_message_with_retry(context.bot, chat_id, f"❌ {amount_too_low_msg}", parse_mode=None)
            return
        # Add a reasonable upper limit
        if refill_amount_decimal > Decimal('10000.00'):
            await send_message_with_retry(context.bot, chat_id, f"❌ {amount_too_high_msg}", parse_mode=None)
            return

        # Store amount as float in context (as used by payment creation later)
        context.user_data['refill_eur_amount'] = float(refill_amount_decimal)
        context.user_data['state'] = 'awaiting_refill_crypto_choice'
        logger.info(f"User {user_id} entered refill EUR: {refill_amount_decimal:.2f}. State -> awaiting_refill_crypto_choice")

        # Define supported crypto buttons (Uppercase for display, lowercase for callback)
        supported_currencies = {
            'BTC': 'btc', 'LTC': 'ltc', 'ETH': 'eth', 'SOL': 'sol',
            'USDT': 'usdt', 'USDC': 'usdc', 'TON': 'ton'
        }
        asset_buttons = []
        row = []
        for display, code in supported_currencies.items():
            row.append(InlineKeyboardButton(display, callback_data=f"select_refill_crypto|{code}"))
            if len(row) >= 3: 
                asset_buttons.append(row)
                row = []
        if row: 
            asset_buttons.append(row)
        asset_buttons.append([InlineKeyboardButton(f"❌ {cancel_top_up_button}", callback_data="profile")])

        refill_amount_str = format_currency(refill_amount_decimal)
        choose_crypto_msg = choose_crypto_prompt_template.format(amount=refill_amount_str)

        await send_message_with_retry(context.bot, chat_id, choose_crypto_msg, reply_markup=InlineKeyboardMarkup(asset_buttons), parse_mode=None)

    except ValueError:
        await send_message_with_retry(context.bot, chat_id, f"❌ {invalid_amount_format_msg}", parse_mode=None)
        return  # Keep state for retry
    except Exception as e:
        logger.error(f"Error processing refill amount user {user_id}: {e}", exc_info=True)
        await send_message_with_retry(context.bot, chat_id, f"❌ {unexpected_error_msg}", parse_mode=None)
        context.user_data.pop('state', None)
        context.user_data.pop('refill_eur_amount', None)  # Reset state

# --- END OF FILE user.py ---