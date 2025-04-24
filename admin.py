import sqlite3
import os
import logging
import json
import tempfile
import shutil
import time
import secrets # For generating random codes
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
import math # Add math for pagination calculation

# --- Telegram Imports ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAnimation
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram import helpers
import telegram.error as telegram_error

# --- Local Imports ---
from utils import (
    CITIES, DISTRICTS, PRODUCT_TYPES, ADMIN_ID, LANGUAGES, THEMES,
    BOT_MEDIA, SIZES, fetch_reviews, format_currency, send_message_with_retry,
    get_date_range, TOKEN, load_all_data, format_discount_value,
    SECONDARY_ADMIN_IDS,
    get_db_connection, MEDIA_DIR, BOT_MEDIA_JSON_PATH # Import helpers/paths
)
# Import viewer admin handlers
try: from viewer_admin import handle_viewer_admin_menu
except ImportError:
    logger_dummy = logging.getLogger(__name__ + "_dummy_viewer")
    logger_dummy.error("Could not import handle_viewer_admin_menu from viewer_admin.py.")
    async def handle_viewer_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query # Corrected variable name
        msg = "Secondary admin menu handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)
# Import stock handler
try: from stock import handle_view_stock
except ImportError:
    logger_dummy_stock = logging.getLogger(__name__ + "_dummy_stock")
    logger_dummy_stock.error("Could not import handle_view_stock from stock.py.")
    async def handle_view_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query # Corrected variable name
        msg = "Stock viewing handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)

# Logging setup
logger = logging.getLogger(__name__)

# --- Admin Callback Handlers ---
async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the main admin dashboard, handling both command and callback."""
    user = update.effective_user
    query = update.callback_query # Will be None if called by command
    if not user:
        logger.warning("handle_admin_menu triggered without effective_user.")
        if query: await query.answer("Error: Could not identify user.", show_alert=True)
        return

    user_id = user.id
    chat_id = update.effective_chat.id # Needed for sending new message
    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)

    if not is_primary_admin and not is_secondary_admin:
        logger.warning(f"Non-admin user {user_id} attempted to access admin menu via {'command' if not query else 'callback'}.")
        msg = "Access denied."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return

    # Redirect secondary admins
    if is_secondary_admin and not is_primary_admin:
        logger.info(f"Redirecting secondary admin {user_id} to viewer admin menu.")
        return await handle_viewer_admin_menu(update, context)

    # Primary Admin Dashboard
    total_users, total_balance, active_products = 0, 0.0, 0
    conn = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column names
        c.execute("SELECT COUNT(*) as count FROM users")
        res_users = c.fetchone(); total_users = res_users['count'] if res_users else 0
        c.execute("SELECT COALESCE(SUM(balance), 0.0) as total_bal FROM users")
        res_balance = c.fetchone(); total_balance = res_balance['total_bal'] if res_balance else 0.0
        c.execute("SELECT COUNT(*) as count FROM products WHERE available > reserved")
        res_products = c.fetchone(); active_products = res_products['count'] if res_products else 0
    except sqlite3.Error as e:
        logger.error(f"DB error fetching admin dashboard data: {e}", exc_info=True)
        error_message = "❌ Error loading admin data."
        if query:
            try: await query.edit_message_text(error_message, parse_mode=None)
            except Exception: pass
        else: await send_message_with_retry(context.bot, chat_id, error_message, parse_mode=None)
        return
    finally:
        if conn: conn.close()

    total_balance_str = format_currency(total_balance)
    msg = (
       f"🔧 Admin Dashboard (Primary)\n\n"
       f"👥 Total Users: {total_users}\n"
       f"💰 Total Balance: {total_balance_str} EUR\n"
       f"📦 Active Products: {active_products}\n\n"
       "Select an action:"
    )
    keyboard = [
        [InlineKeyboardButton("📊 Sales Analytics", callback_data="sales_analytics_menu")],
        [InlineKeyboardButton("➕ Add Products", callback_data="adm_city")],
        [InlineKeyboardButton("🗑️ Manage Products", callback_data="adm_manage_products")],
        [InlineKeyboardButton("🏷️ Manage Discounts", callback_data="adm_manage_discounts")],
        [InlineKeyboardButton("📦 View Bot Stock", callback_data="view_stock")],
        [InlineKeyboardButton("🗺️ Manage Districts", callback_data="adm_manage_districts")],
        [InlineKeyboardButton("🏙️ Manage Cities", callback_data="adm_manage_cities")],
        [InlineKeyboardButton("🧩 Manage Product Types", callback_data="adm_manage_types")],
        [InlineKeyboardButton("🚫 Manage Reviews", callback_data="adm_manage_reviews|0")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast_start")],
        [InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")],
        [InlineKeyboardButton("📸 Set Bot Media", callback_data="adm_set_media")],
        [InlineKeyboardButton("🏠 User Home Menu", callback_data="back_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        try:
            await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode=None)
        except telegram_error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error editing admin menu message: {e}")
                await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
            else: await query.answer()
        except Exception as e:
            logger.error(f"Unexpected error editing admin menu: {e}", exc_info=True)
            await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
    else:
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)


# --- Sales Analytics Handlers ---
async def handle_sales_analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the sales analytics submenu."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    msg = "📊 Sales Analytics\n\nSelect a report or view:"
    keyboard = [
        [InlineKeyboardButton("📈 View Dashboard", callback_data="sales_dashboard")],
        [InlineKeyboardButton("📅 Generate Report", callback_data="sales_select_period|main")],
        [InlineKeyboardButton("🏙️ Sales by City", callback_data="sales_select_period|by_city")],
        [InlineKeyboardButton("💎 Sales by Type", callback_data="sales_select_period|by_type")],
        [InlineKeyboardButton("🏆 Top Products", callback_data="sales_select_period|top_prod")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_sales_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays a quick sales dashboard for today, this week, this month."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    periods = {
        "today": ("☀️ Today ({})", datetime.now().strftime("%Y-%m-%d")),
        "week": ("🗓️ This Week (Mon-Sun)", None),
        "month": ("📆 This Month", None)
    }
    msg = "📊 Sales Dashboard\n\n"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        for period_key, (label_template, date_str) in periods.items():
            start, end = get_date_range(period_key)
            if not start or not end:
                msg += f"Could not calculate range for {period_key}.\n\n"
                continue
            # Use column names
            c.execute("SELECT COALESCE(SUM(price_paid), 0.0) as total_revenue, COUNT(*) as total_units FROM purchases WHERE purchase_date BETWEEN ? AND ?", (start, end))
            result = c.fetchone()
            revenue = result['total_revenue'] if result else 0.0
            units = result['total_units'] if result else 0
            aov = revenue / units if units > 0 else 0.0
            revenue_str = format_currency(revenue)
            aov_str = format_currency(aov)
            label_formatted = label_template.format(date_str) if date_str else label_template
            msg += f"{label_formatted}\n"
            msg += f"    Revenue: {revenue_str} EUR\n"
            msg += f"    Units Sold: {units}\n"
            msg += f"    Avg Order Value: {aov_str} EUR\n\n"
    except sqlite3.Error as e:
        logger.error(f"DB error generating sales dashboard: {e}", exc_info=True)
        msg += "\n❌ Error fetching dashboard data."
    except Exception as e:
        logger.error(f"Unexpected error in sales dashboard: {e}", exc_info=True)
        msg += "\n❌ An unexpected error occurred."
    finally:
         if conn: conn.close() # Close connection if opened
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="sales_analytics_menu")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing sales dashboard: {e}")
        else: await query.answer()

async def handle_sales_select_period(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows options for selecting a reporting period."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params:
        logger.warning("handle_sales_select_period called without report_type.")
        return await query.answer("Error: Report type missing.", show_alert=True)
    report_type = params[0]
    context.user_data['sales_report_type'] = report_type
    keyboard = [
        [InlineKeyboardButton("Today", callback_data=f"sales_run|{report_type}|today"),
         InlineKeyboardButton("Yesterday", callback_data=f"sales_run|{report_type}|yesterday")],
        [InlineKeyboardButton("This Week", callback_data=f"sales_run|{report_type}|week"),
         InlineKeyboardButton("Last Week", callback_data=f"sales_run|{report_type}|last_week")],
        [InlineKeyboardButton("This Month", callback_data=f"sales_run|{report_type}|month"),
         InlineKeyboardButton("Last Month", callback_data=f"sales_run|{report_type}|last_month")],
        [InlineKeyboardButton("Year To Date", callback_data=f"sales_run|{report_type}|year")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sales_analytics_menu")]
    ]
    await query.edit_message_text("📅 Select Reporting Period", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_sales_run(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Generates and displays the selected sales report."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2:
        logger.warning("handle_sales_run called with insufficient parameters.")
        return await query.answer("Error: Report type or period missing.", show_alert=True)
    report_type, period_key = params[0], params[1]
    start_time, end_time = get_date_range(period_key)
    if not start_time or not end_time:
        return await query.edit_message_text("❌ Error: Invalid period selected.", parse_mode=None)
    period_title = period_key.replace('_', ' ').title()
    msg = ""
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()
        base_query = "FROM purchases WHERE purchase_date BETWEEN ? AND ?"
        base_params = (start_time, end_time)
        if report_type == "main":
            c.execute(f"SELECT COALESCE(SUM(price_paid), 0.0) as total_revenue, COUNT(*) as total_units {base_query}", base_params)
            result = c.fetchone()
            revenue = result['total_revenue'] if result else 0.0
            units = result['total_units'] if result else 0
            aov = revenue / units if units > 0 else 0.0
            revenue_str = format_currency(revenue)
            aov_str = format_currency(aov)
            msg = (f"📊 Sales Report: {period_title}\n\nRevenue: {revenue_str} EUR\n"
                   f"Units Sold: {units}\nAvg Order Value: {aov_str} EUR")
        elif report_type == "by_city":
            c.execute(f"SELECT city, COALESCE(SUM(price_paid), 0.0) as city_revenue, COUNT(*) as city_units {base_query} GROUP BY city ORDER BY city_revenue DESC", base_params)
            results = c.fetchall()
            msg = f"🏙️ Sales by City: {period_title}\n\n"
            if results:
                for row in results:
                    msg += f"{row['city'] or 'N/A'}: {format_currency(row['city_revenue'])} EUR ({row['city_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        elif report_type == "by_type":
            c.execute(f"SELECT product_type, COALESCE(SUM(price_paid), 0.0) as type_revenue, COUNT(*) as type_units {base_query} GROUP by product_type ORDER BY type_revenue DESC", base_params)
            results = c.fetchall()
            msg = f"💎 Sales by Type: {period_title}\n\n"
            if results:
                for row in results:
                    msg += f"{row['product_type'] or 'N/A'}: {format_currency(row['type_revenue'])} EUR ({row['type_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        elif report_type == "top_prod":
            c.execute(f"""
                SELECT p.name as product_name, p.size as product_size, COALESCE(SUM(pu.price_paid), 0.0) as prod_revenue, COUNT(pu.id) as prod_units
                FROM purchases pu JOIN products p ON pu.product_id = p.id
                WHERE pu.purchase_date BETWEEN ? AND ?
                GROUP BY p.name, p.size ORDER BY prod_revenue DESC LIMIT 10
            """, base_params)
            results = c.fetchall()
            msg = f"🏆 Top Products: {period_title}\n\n"
            if results:
                for i, row in enumerate(results):
                    msg += f"{i+1}. {row['product_name'] or 'N/A'} ({row['product_size'] or 'N/A'}): {format_currency(row['prod_revenue'])} EUR ({row['prod_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        else: msg = "❌ Unknown report type requested."
    except sqlite3.Error as e:
        logger.error(f"DB error generating sales report '{report_type}' for '{period_key}': {e}", exc_info=True)
        msg = "❌ Error generating report due to database issue."
    except Exception as e:
        logger.error(f"Unexpected error generating sales report: {e}", exc_info=True)
        msg = "❌ An unexpected error occurred."
    finally:
         if conn: conn.close() # Close connection if opened
    keyboard = [[InlineKeyboardButton("⬅️ Back to Period", callback_data=f"sales_select_period|{report_type}"),
                 InlineKeyboardButton("📊 Analytics Menu", callback_data="sales_analytics_menu")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing sales report: {e}")
        else: await query.answer()


# --- Add Product Flow Handlers ---
async def handle_adm_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects city to add product to."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    lang = context.user_data.get("lang", "en")
    if not CITIES:
        return await query.edit_message_text("No cities configured. Please add a city first via 'Manage Cities'.", parse_mode=None)
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_dist|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])
    select_city_text = LANGUAGES.get(lang, {}).get("admin_select_city", "Select City to Add Product:")
    await query.edit_message_text(select_city_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_dist(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects district within the chosen city."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found. Please select again.", parse_mode=None)
    districts_in_city = DISTRICTS.get(city_id, {})
    lang = context.user_data.get("lang", "en")
    select_district_template = LANGUAGES.get(lang, {}).get("admin_select_district", "Select District in {city}:")
    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_city")]]
        return await query.edit_message_text(f"No districts found for {city_name}. Please add districts via 'Manage Districts'.",
                                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
    keyboard = []
    for d in sorted_district_ids:
        dist_name = districts_in_city.get(d)
        if dist_name:
            keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=f"adm_type|{city_id}|{d}")])
        else: logger.warning(f"District name missing for ID {d} in city {city_id}")
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_city")])
    select_district_text = select_district_template.format(city=city_name)
    await query.edit_message_text(select_district_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects product type."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City or District ID missing.", show_alert=True)
    city_id, dist_id = params[0], params[1]
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found. Please select again.", parse_mode=None)
    lang = context.user_data.get("lang", "en")
    select_type_text = LANGUAGES.get(lang, {}).get("admin_select_type", "Select Product Type:")
    if not PRODUCT_TYPES:
        return await query.edit_message_text("No product types configured. Add types via 'Manage Product Types'.", parse_mode=None)
    keyboard = [[InlineKeyboardButton(f"💎 {pt}", callback_data=f"adm_add|{city_id}|{dist_id}|{pt}")] for pt in sorted(PRODUCT_TYPES)]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_dist|{city_id}")])
    await query.edit_message_text(select_type_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_add(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects size for the new product."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 3: return await query.answer("Error: Location/Type info missing.", show_alert=True)
    city_id, dist_id, p_type = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found. Please select again.", parse_mode=None)
    context.user_data["admin_city_id"] = city_id
    context.user_data["admin_district_id"] = dist_id
    context.user_data["admin_product_type"] = p_type
    context.user_data["admin_city"] = city_name
    context.user_data["admin_district"] = district_name
    keyboard = [[InlineKeyboardButton(f"📏 {s}", callback_data=f"adm_size|{s}")] for s in SIZES]
    keyboard.append([InlineKeyboardButton("📏 Custom Size", callback_data="adm_custom_size")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Types", callback_data=f"adm_type|{city_id}|{dist_id}")])
    await query.edit_message_text(f"📦 Adding {p_type} in {city_name} / {district_name}\n\nSelect size:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_size(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles selection of a predefined size."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Size missing.", show_alert=True)
    size = params[0]
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        return await query.edit_message_text("❌ Error: Context lost. Please start adding the product again.", parse_mode=None)
    context.user_data["pending_drop_size"] = size
    context.user_data["state"] = "awaiting_price"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await query.edit_message_text(f"Size set to {size}. Please reply with the price (e.g., 12.50 or 12.5):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter price in chat.")

async def handle_adm_custom_size(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Custom Size' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        return await query.edit_message_text("❌ Error: Context lost. Please start adding the product again.", parse_mode=None)
    context.user_data["state"] = "awaiting_custom_size"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await query.edit_message_text("Please reply with the custom size (e.g., 10g, 1/4 oz):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter custom size in chat.")

async def handle_confirm_add_drop(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles confirmation (Yes/No) for adding the drop."""
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    chat_id = query.message.chat_id
    pending_drop = context.user_data.get("pending_drop")
    if not pending_drop:
        logger.error(f"Confirmation 'yes' received for add drop, but no pending_drop data found for user {user_id}.")
        context.user_data.pop("state", None)
        return await query.edit_message_text("❌ Error: No pending drop data found. Please start again.", parse_mode=None)

    city = pending_drop.get("city")
    district = pending_drop.get("district")
    p_type = pending_drop.get("product_type")
    size = pending_drop.get("size")
    price = pending_drop.get("price")
    original_text = pending_drop.get("original_text", "")
    media_list = pending_drop.get("media", [])
    temp_dir = pending_drop.get("temp_dir") # Temp dir used for download

    if not all([city, district, p_type, size, price is not None]):
        logger.error(f"Missing data in pending_drop for user {user_id}: {pending_drop}")
        if temp_dir and os.path.exists(temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price", "admin_city_id", "admin_district_id", "admin_product_type", "admin_city", "admin_district"]
        for key in keys_to_clear: context.user_data.pop(key, None)
        return await query.edit_message_text("❌ Error: Incomplete drop data. Please start again.", parse_mode=None)

    product_name = f"{p_type} {size} {int(time.time())}"
    conn = None
    product_id = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute(
            """INSERT INTO products
                (city, district, product_type, size, name, price, available, reserved, original_text, added_by, added_date)
                VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""",
            (city, district, p_type, size, product_name, price, original_text, ADMIN_ID, datetime.now().isoformat())
        )
        product_id = c.lastrowid

        if product_id and media_list and temp_dir:
            # Define final media directory on persistent disk
            final_media_dir = os.path.join(MEDIA_DIR, str(product_id))
            await asyncio.to_thread(os.makedirs, final_media_dir, exist_ok=True) # Ensure dir exists
            logger.info(f"Created/verified final media directory: {final_media_dir}")

            media_inserts = []
            for media_item in media_list:
                if "path" in media_item and "type" in media_item and "file_id" in media_item:
                    temp_file_path = media_item["path"] # This is the path in the temporary directory
                    if await asyncio.to_thread(os.path.exists, temp_file_path):
                        new_filename = os.path.basename(temp_file_path)
                        # Create the final path on the persistent disk
                        final_persistent_path = os.path.join(final_media_dir, new_filename)
                        try:
                            # Move from temp dir to persistent disk
                            await asyncio.to_thread(shutil.move, temp_file_path, final_persistent_path)
                            # Store the persistent path in the DB
                            media_inserts.append((product_id, media_item["type"], final_persistent_path, media_item["file_id"]))
                            logger.info(f"Moved media file to {final_persistent_path}")
                        except OSError as move_err:
                            logger.error(f"Error moving media file {temp_file_path} to {final_persistent_path}: {move_err}")
                    else:
                        logger.warning(f"Media file not found at temp path: {temp_file_path}")
                else:
                    logger.warning(f"Incomplete media item data: {media_item}")

            if media_inserts:
                c.executemany(
                    "INSERT INTO product_media (product_id, media_type, file_path, telegram_file_id) VALUES (?, ?, ?, ?)",
                    media_inserts
                )
        conn.commit()
        logger.info(f"Successfully added product {product_id} ({product_name}) to database.")

        # Clean up the temporary directory AFTER successful commit and move
        if temp_dir and os.path.exists(temp_dir):
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temporary directory: {temp_dir}")

        await query.edit_message_text("✅ Drop Added Successfully!", parse_mode=None)

        # Prepare "Add Another" button
        ctx_city_id = context.user_data.get('admin_city_id')
        ctx_dist_id = context.user_data.get('admin_district_id')
        ctx_p_type = context.user_data.get('admin_product_type')
        add_another_callback = f"adm_add|{ctx_city_id}|{ctx_dist_id}|{ctx_p_type}" if all([ctx_city_id, ctx_dist_id, ctx_p_type]) else "admin_menu"

        keyboard = [
            [InlineKeyboardButton("➕ Add Another Same Type", callback_data=add_another_callback)],
            [InlineKeyboardButton("🔧 Admin Menu", callback_data="admin_menu"),
             InlineKeyboardButton("🏠 User Home", callback_data="back_start")]
        ]
        await send_message_with_retry(context.bot, chat_id, "What next?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except (sqlite3.Error, OSError, Exception) as e:
        try:
            if conn and conn.in_transaction: conn.rollback()
        except Exception as rb_err: logger.error(f"Rollback failed during drop add error handling: {rb_err}")
        logger.error(f"Error saving confirmed drop for user {user_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ Error: Failed to save the drop. Please check logs and try again.", parse_mode=None)
        # Clean up temp dir on error as well
        if temp_dir and os.path.exists(temp_dir):
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temporary directory after error: {temp_dir}")
    finally:
        if conn: conn.close()
        # Clear context data related to adding this specific drop
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price"]
        for key in keys_to_clear: context.user_data.pop(key, None)
        # Keep admin location context for "Add Another"
        # context.user_data.pop("admin_city_id", None)
        # context.user_data.pop("admin_district_id", None)
        # context.user_data.pop("admin_product_type", None)
        # context.user_data.pop("admin_city", None)
        # context.user_data.pop("admin_district", None)


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Cancels the add product flow and cleans up."""
    query = update.callback_query
    pending_drop = context.user_data.get("pending_drop")

    # Clean up temporary directory if it exists
    if pending_drop and "temp_dir" in pending_drop and pending_drop["temp_dir"]:
        temp_dir_path = pending_drop["temp_dir"]
        if os.path.exists(temp_dir_path):
            try:
                await asyncio.to_thread(shutil.rmtree, temp_dir_path, ignore_errors=True)
                logger.info(f"Cleaned up temp dir on cancel: {temp_dir_path}")
            except Exception as e:
                logger.error(f"Error cleaning up temp dir {temp_dir_path} on cancel: {e}")

    # Clear all related context keys
    keys_to_clear = [
        "state", "pending_drop", "pending_drop_size", "pending_drop_price",
        "admin_city_id", "admin_district_id", "admin_product_type",
        "admin_city", "admin_district"
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)

    if query:
        try:
            await query.edit_message_text("❌ Add Product Cancelled", parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing cancel message: {e}")
             else: pass # Ignore if not modified
        keyboard = [[InlineKeyboardButton("🔧 Admin Menu", callback_data="admin_menu"),
                     InlineKeyboardButton("🏠 User Home", callback_data="back_start")]]
        await send_message_with_retry(
            context.bot, query.message.chat_id,
            "Returning to Admin Menu.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None
        )
    elif update.message: # If cancelled via message handler perhaps
         await send_message_with_retry(context.bot, update.message.chat_id, "Add product cancelled.", parse_mode=None)
    else:
         logger.info("Add product flow cancelled internally (no query/message object).")


# --- Manage Geography Handlers ---
async def handle_adm_manage_cities(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows options to manage existing cities."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Use 'Add New City'.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")],
                                                                      [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = []
    for c in sorted_city_ids:
        city_name = CITIES.get(c,'N/A')
        keyboard.append([
             InlineKeyboardButton(f"🏙️ {city_name}", callback_data=f"adm_edit_city|{c}"),
             InlineKeyboardButton(f"🗑️ Delete", callback_data=f"adm_delete_city|{c}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🏙️ Manage Cities\n\nSelect a city or action:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_add_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Add New City' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    context.user_data["state"] = "awaiting_new_city_name"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text("🏙️ Please reply with the name for the new city:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter city name in chat.")

async def handle_adm_edit_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Edit City' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_edit_city_name"
    context.user_data["edit_city_id"] = city_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text(f"✏️ Editing city: {city_name}\n\nPlease reply with the new name for this city:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new city name in chat.")

async def handle_adm_delete_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete City' button press, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["confirm_action"] = f"delete_city|{city_id}"
    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Are you sure you want to delete city: {city_name}?\n\n"
           f"🚨 This will permanently delete this city, all its districts, and all products listed within those districts!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete City", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_manage_districts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows list of cities to choose from for managing districts."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Add a city first.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id,''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c, 'N/A')}", callback_data=f"adm_manage_districts_city|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🗺️ Manage Districts\n\nSelect the city whose districts you want to manage:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_manage_districts_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows districts for the selected city and management options."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    districts_in_city = {}
    conn = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column names
        c.execute("SELECT id, name FROM districts WHERE city_id = ? ORDER BY name", (int(city_id),))
        districts_in_city = {str(row['id']): row['name'] for row in c.fetchall()}
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Failed to reload districts for city {city_id}: {e}")
        districts_in_city = DISTRICTS.get(city_id, {}) # Fallback to potentially outdated global
    finally:
        if conn: conn.close()

    msg = f"🗺️ Districts in {city_name}\n\n"
    keyboard = []
    if not districts_in_city: msg += "No districts found for this city."
    else:
        sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
        for d_id in sorted_district_ids:
            dist_name = districts_in_city.get(d_id)
            if dist_name:
                 keyboard.append([
                     InlineKeyboardButton(f"✏️ Edit {dist_name}", callback_data=f"adm_edit_district|{city_id}|{d_id}"),
                     InlineKeyboardButton(f"🗑️ Delete {dist_name}", callback_data=f"adm_remove_district|{city_id}|{d_id}")
                 ])
            else: logger.warning(f"District name missing for ID {d_id} in city {city_id} (manage view)")
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New District", callback_data=f"adm_add_district|{city_id}")],
        [InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_districts")]
    ])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing manage districts city message: {e}")
        else: await query.answer()

async def handle_adm_add_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Add New District' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_new_district_name"
    context.user_data["admin_add_district_city_id"] = city_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(f"➕ Adding district to {city_name}\n\nPlease reply with the name for the new district:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter district name in chat.")

async def handle_adm_edit_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Edit District' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = None
    conn = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id), int(city_id)))
        res = c.fetchone(); district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch district name for edit: {e}")
    finally:
         if conn: conn.close()
    if not city_name or district_name is None:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_edit_district_name"
    context.user_data["edit_city_id"] = city_id
    context.user_data["edit_district_id"] = dist_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(f"✏️ Editing district: {district_name} in {city_name}\n\nPlease reply with the new name for this district:",
                           reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new district name in chat.")

async def handle_adm_remove_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete District' button press, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = None
    conn = None
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id), int(city_id)))
        res = c.fetchone(); district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch district name for delete confirmation: {e}")
    finally:
        if conn: conn.close()
    if not city_name or district_name is None:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    context.user_data["confirm_action"] = f"remove_district|{city_id}|{dist_id}"
    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Are you sure you want to delete district: {district_name} from {city_name}?\n\n"
           f"🚨 This will permanently delete this district and all products listed within it!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete District", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Manage Products Handlers ---
async def handle_adm_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects city to manage products in."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Add a city first.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id,''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_manage_products_city|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🗑️ Manage Products\n\nSelect the city where the products are located:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_manage_products_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects district to manage products in."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
         keyboard = [[InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_products")]]
         return await query.edit_message_text(f"No districts found for {city_name}. Cannot manage products.",
                                 reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda d_id: districts_in_city.get(d_id,''))
    keyboard = []
    for d in sorted_district_ids:
         dist_name = districts_in_city.get(d)
         if dist_name:
             keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=f"adm_manage_products_dist|{city_id}|{d}")])
         else: logger.warning(f"District name missing for ID {d} in city {city_id} (manage products)")
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_products")])
    await query.edit_message_text(f"🗑️ Manage Products in {city_name}\n\nSelect district:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_manage_products_dist(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects product type to manage within the district."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT DISTINCT product_type FROM products WHERE city = ? AND district = ? ORDER BY product_type", (city_name, district_name))
        product_types_in_dist = sorted([row['product_type'] for row in c.fetchall()])
        if not product_types_in_dist:
             keyboard = [[InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_manage_products_city|{city_id}")]]
             return await query.edit_message_text(f"No product types found in {city_name} / {district_name}.",
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        keyboard = [[InlineKeyboardButton(f"💎 {pt}", callback_data=f"adm_manage_products_type|{city_id}|{dist_id}|{pt}")] for pt in product_types_in_dist]
        keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_manage_products_city|{city_id}")])
        await query.edit_message_text(f"🗑️ Manage Products in {city_name} / {district_name}\n\nSelect product type:",
                                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error fetching product types for managing in {city_name}/{district_name}: {e}", exc_info=True)
        await query.edit_message_text("❌ Error fetching product types.", parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_manage_products_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows specific products of a type and allows deletion."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 3: return await query.answer("Error: Location/Type info missing.", show_alert=True)
    city_id, dist_id, p_type = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()
        # Use column names
        c.execute("""
            SELECT id, size, price, available, reserved, name
            FROM products WHERE city = ? AND district = ? AND product_type = ?
            ORDER BY size, price, id
        """, (city_name, district_name, p_type))
        products = c.fetchall()
        msg = f"🗑️ Products: {p_type} in {city_name} / {district_name}\n\n"
        keyboard = []
        if not products:
            msg += "No products of this type found here."
        else:
             msg += "ID | Size | Price | Status (Avail/Reserved)\n"
             msg += "----------------------------------------\n"
             for prod in products:
                # Access by column name
                prod_id, size_str, price_str = prod['id'], prod['size'], format_currency(prod['price'])
                status_str = f"{prod['available']}/{prod['reserved']}"
                msg += f"{prod_id} | {size_str} | {price_str}€ | {status_str}\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ Delete ID {prod_id}", callback_data=f"adm_delete_prod|{prod_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Types", callback_data=f"adm_manage_products_dist|{city_id}|{dist_id}")])
        try:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing manage products type: {e}.")
             else: await query.answer() # Acknowledge if not modified
    except sqlite3.Error as e:
        logger.error(f"DB error fetching products for deletion: {e}", exc_info=True)
        await query.edit_message_text("❌ Error fetching products.", parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_delete_prod(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Product' button press, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Product ID missing.", show_alert=True)
    try: product_id = int(params[0])
    except ValueError: return await query.answer("Error: Invalid Product ID.", show_alert=True)
    product_name = f"Product ID {product_id}"
    product_details = ""
    back_callback_data = "adm_manage_products"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()
        # Use column names
        c.execute("""
            SELECT p.name, p.city, p.district, p.product_type, p.size, p.price, ci.id as city_id, di.id as dist_id
            FROM products p LEFT JOIN cities ci ON p.city = ci.name
            LEFT JOIN districts di ON p.district = di.name AND ci.id = di.city_id
            WHERE p.id = ?
        """, (product_id,))
        result = c.fetchone()
        if result:
            product_name = result['name'] or product_name
            product_details = f"{result['product_type']} {result['size']} ({format_currency(result['price'])}€) in {result['city']}/{result['district']}"
            if result['city_id'] and result['dist_id'] and result['product_type']:
                back_callback_data = f"adm_manage_products_type|{result['city_id']}|{result['dist_id']}|{result['product_type']}"
            else: logger.warning(f"Could not retrieve full details for product {product_id} during delete confirmation.")
        else:
            return await query.edit_message_text("Error: Product not found.", parse_mode=None)
    except sqlite3.Error as e:
         logger.warning(f"Could not fetch full details for product {product_id} for delete confirmation: {e}")
    finally:
        if conn: conn.close() # Close connection if opened

    context.user_data["confirm_action"] = f"confirm_remove_product|{product_id}"
    msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to delete this specific product instance?\n"
           f"Product ID: {product_id}\nDetails: {product_details}\n\n🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Product", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=back_callback_data)]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Manage Product Types Handlers ---
async def handle_adm_manage_types(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows options to manage product types."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    load_all_data() # Ensure PRODUCT_TYPES is up-to-date
    if not PRODUCT_TYPES: msg = "🧩 Manage Product Types\n\nNo product types configured."
    else: msg = "🧩 Manage Product Types\n\nSelect a type to delete (only possible if no products use it):"
    keyboard = []
    for pt in sorted(PRODUCT_TYPES):
         keyboard.append([InlineKeyboardButton(f"🗑️ Delete {pt}", callback_data=f"adm_delete_type|{pt}")])
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New Type", callback_data="adm_add_type")],
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
    ])
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Add New Type' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    context.user_data["state"] = "awaiting_new_type_name"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_types")]]
    await query.edit_message_text("🧩 Please reply with the name for the new product type:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter type name in chat.")

async def handle_adm_delete_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Type' button, checks usage, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Type name missing.", show_alert=True)
    type_name = params[0]
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products WHERE product_type = ?", (type_name,))
        product_count = c.fetchone()[0] # Fetch first column
        if product_count > 0:
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="adm_manage_types")]]
            await query.edit_message_text(f"⚠️ Cannot Delete Type\n\nType {type_name} is currently used by {product_count} product(s). Please delete or reassign those products first.",
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        else:
            context.user_data["confirm_action"] = f"delete_type|{type_name}"
            msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to delete product type: {type_name}?\n\n"
                   f"🚨 This action is irreversible!")
            keyboard = [[InlineKeyboardButton("✅ Yes, Delete Type", callback_data="confirm_yes"),
                         InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_types")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error checking product type usage for '{type_name}': {e}", exc_info=True)
        await query.edit_message_text("❌ Error checking type usage.", parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened

# --- Discount Management Handlers ---
async def handle_adm_manage_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays existing discount codes and management options."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()
        c.execute("""
            SELECT id, code, discount_type, value, is_active, max_uses, uses_count, expiry_date
            FROM discount_codes ORDER BY created_date DESC
        """)
        codes = c.fetchall()
        msg = "🏷️ Manage Discount Codes\n\n"
        keyboard = []
        if not codes: msg += "No discount codes found."
        else:
            for code in codes: # Access by column name
                status = "✅ Active" if code['is_active'] else "❌ Inactive"
                value_str = format_discount_value(code['discount_type'], code['value'])
                usage_limit = f"/{code['max_uses']}" if code['max_uses'] is not None else "/∞"
                usage = f"{code['uses_count']}{usage_limit}"
                expiry_info = ""
                if code['expiry_date']:
                     try:
                         expiry_dt = datetime.fromisoformat(code['expiry_date'])
                         expiry_info = f" | Expires: {expiry_dt.strftime('%Y-%m-%d')}"
                         if datetime.now() > expiry_dt and code['is_active']: status = "⏳ Expired"
                     except ValueError: expiry_info = " | Invalid Date"
                toggle_text = "Deactivate" if code['is_active'] else "Activate"
                delete_text = "🗑️ Delete"
                code_text = code['code']
                msg += f"{code_text} ({value_str} {code['discount_type']}) | {status} | Used: {usage}{expiry_info}\n"
                keyboard.append([
                    InlineKeyboardButton(f"{'❌' if code['is_active'] else '✅'} {toggle_text}", callback_data=f"adm_toggle_discount|{code['id']}"),
                    InlineKeyboardButton(f"{delete_text}", callback_data=f"adm_delete_discount|{code['id']}")
                ])
        keyboard.extend([
            [InlineKeyboardButton("➕ Add New Discount Code", callback_data="adm_add_discount_start")],
            [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
        ])
        try:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing discount list: {e}.")
             else: await query.answer()
    except sqlite3.Error as e:
        logger.error(f"DB error loading discount codes: {e}", exc_info=True)
        await query.edit_message_text("❌ Error loading discount codes.", parse_mode=None)
    except Exception as e:
         logger.error(f"Unexpected error managing discounts: {e}", exc_info=True)
         await query.edit_message_text("❌ An unexpected error occurred.", parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_toggle_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Activates or deactivates a specific discount code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Code ID missing.", show_alert=True)
    conn = None # Initialize conn
    try:
        code_id = int(params[0])
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT is_active FROM discount_codes WHERE id = ?", (code_id,))
        result = c.fetchone()
        if not result: return await query.answer("Code not found.", show_alert=True)
        current_status = result['is_active']
        new_status = 0 if current_status == 1 else 1
        c.execute("UPDATE discount_codes SET is_active = ? WHERE id = ?", (new_status, code_id))
        conn.commit()
        action = 'deactivated' if new_status == 0 else 'activated'
        logger.info(f"Admin {query.from_user.id} {action} discount code ID {code_id}.")
        await query.answer(f"Code {action} successfully.")
        await handle_adm_manage_discounts(update, context) # Refresh list
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Error toggling discount code {params[0]}: {e}", exc_info=True)
        await query.answer("Error updating code status.", show_alert=True)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_delete_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles delete button press for discount code, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Code ID missing.", show_alert=True)
    conn = None # Initialize conn
    try:
        code_id = int(params[0])
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT code FROM discount_codes WHERE id = ?", (code_id,))
        result = c.fetchone()
        if not result: return await query.answer("Code not found.", show_alert=True)
        code_text = result['code']
        context.user_data["confirm_action"] = f"delete_discount|{code_id}"
        msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to permanently delete discount code: {code_text}?\n\n"
               f"🚨 This action is irreversible!")
        keyboard = [[InlineKeyboardButton("✅ Yes, Delete Code", callback_data="confirm_yes"),
                     InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_discounts")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Error preparing delete confirmation for discount code {params[0]}: {e}", exc_info=True)
        await query.answer("Error fetching code details.", show_alert=True)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_add_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the process of adding a new discount code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    context.user_data['state'] = 'awaiting_discount_code'
    context.user_data['new_discount_info'] = {} # Initialize dict
    random_code = secrets.token_urlsafe(8).upper().replace('-', '').replace('_', '')[:8]
    keyboard = [
        [InlineKeyboardButton(f"Use Generated: {random_code}", callback_data=f"adm_use_generated_code|{random_code}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]
    ]
    await query.edit_message_text(
        "🏷️ Add New Discount Code\n\nPlease reply with the code text you want to use (e.g., SUMMER20), or use the generated one below.\n"
        "Codes are case-sensitive.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None
    )
    await query.answer("Enter code text or use generated.")


async def handle_adm_use_generated_code(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles using the suggested random code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Generated code missing.", show_alert=True)
    code_text = params[0]
    await process_discount_code_input(update, context, code_text) # This function will handle message editing


async def handle_adm_set_discount_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Sets the discount type and asks for the value."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Discount type missing.", show_alert=True)
    current_state = context.user_data.get("state")
    if current_state not in ['awaiting_discount_type', 'awaiting_discount_code']: # Check if state is valid
         logger.warning(f"handle_adm_set_discount_type called in wrong state: {current_state}")
         # Try to recover state if possible, otherwise go back
         if context.user_data and 'new_discount_info' in context.user_data and 'code' in context.user_data['new_discount_info']:
             context.user_data['state'] = 'awaiting_discount_type'
             logger.info("Forcing state back to awaiting_discount_type")
         else:
             return await handle_adm_manage_discounts(update, context)

    discount_type = params[0]
    if discount_type not in ['percentage', 'fixed']:
        return await query.answer("Invalid discount type.", show_alert=True)
    if 'new_discount_info' not in context.user_data: context.user_data['new_discount_info'] = {}
    context.user_data['new_discount_info']['type'] = discount_type
    context.user_data['state'] = 'awaiting_discount_value'
    value_prompt = ("Enter the percentage value (e.g., 10 for 10%):" if discount_type == 'percentage' else
                    "Enter the fixed discount amount in EUR (e.g., 5.50):")
    code_text = context.user_data.get('new_discount_info', {}).get('code', 'N/A')
    msg = f"Code: {code_text} | Type: {discount_type.capitalize()}\n\n{value_prompt}"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer("Enter the discount value.")
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message in handle_adm_set_discount_type: {e}. Message: {msg}")
             await query.answer("Error updating prompt. Please try again.", show_alert=True)
        else: await query.answer()

# --- Set Bot Media Handlers ---

async def handle_adm_set_media(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Set Bot Media' button press."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    lang = context.user_data.get("lang", "en")
    set_media_prompt_text = LANGUAGES.get(lang, {}).get("set_media_prompt_plain", "Send a photo, video, or GIF to display above all messages:")
    context.user_data["state"] = "awaiting_bot_media"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]
    await query.edit_message_text(set_media_prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send photo, video, or GIF.")


# --- Review Management Handlers ---

async def handle_adm_manage_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays reviews paginated for the admin with delete options."""
    query = update.callback_query
    user_id = query.from_user.id
    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)
    if not is_primary_admin and not is_secondary_admin: return await query.answer("Access Denied.", show_alert=True)
    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])
    reviews_per_page = 5
    reviews_data = fetch_reviews(offset=offset, limit=reviews_per_page + 1) # Sync function uses helper
    msg = "🚫 Manage Reviews\n\n"
    keyboard = []
    item_buttons = []
    if not reviews_data:
        if offset == 0: msg += "No reviews have been left yet."
        else: msg += "No more reviews to display."
    else:
        has_more = len(reviews_data) > reviews_per_page
        reviews_to_show = reviews_data[:reviews_per_page]
        for review in reviews_to_show:
            review_id = review.get('review_id', 'N/A')
            try:
                date_str = review.get('review_date', '')
                formatted_date = "???"
                if date_str:
                    try: formatted_date = datetime.fromisoformat(date_str).strftime("%Y-%m-%d")
                    except ValueError: pass
                username = review.get('username', 'anonymous')
                username_display = f"@{username}" if username and username != 'anonymous' else username
                review_text = review.get('review_text', '')
                review_text_preview = review_text[:100] + ('...' if len(review_text) > 100 else '')
                msg += f"ID {review_id} | {username_display} ({formatted_date}):\n{review_text_preview}\n\n"
                if is_primary_admin: # Only primary admin can delete
                     item_buttons.append([InlineKeyboardButton(f"🗑️ Delete Review #{review_id}", callback_data=f"adm_delete_review_confirm|{review_id}")])
            except Exception as e:
                 logger.error(f"Error formatting review item #{review_id} for admin view: {review}, Error: {e}")
                 msg += f"ID {review_id} | (Error displaying review)\n\n"
                 if is_primary_admin: item_buttons.append([InlineKeyboardButton(f"🗑️ Delete Review #{review_id}", callback_data=f"adm_delete_review_confirm|{review_id}")])
        keyboard.extend(item_buttons)
        nav_buttons = []
        if offset > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_manage_reviews|{max(0, offset - reviews_per_page)}"))
        if has_more: nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"adm_manage_reviews|{offset + reviews_per_page}"))
        if nav_buttons: keyboard.append(nav_buttons)
    back_callback = "admin_menu" if is_primary_admin else "viewer_admin_menu"
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data=back_callback)])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" in str(e).lower(): await query.answer()
        else: logger.warning(f"Failed to edit message for adm_manage_reviews: {e}"); await query.answer("Error updating review list.", show_alert=True)
    except Exception as e:
        logger.error(f"Unexpected error in adm_manage_reviews: {e}", exc_info=True)
        await query.edit_message_text("❌ An unexpected error occurred while loading reviews.", parse_mode=None)


async def handle_adm_delete_review_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Review' button press, shows confirmation."""
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Review ID missing.", show_alert=True)
    try: review_id = int(params[0])
    except ValueError: return await query.answer("Error: Invalid Review ID.", show_alert=True)
    review_text_snippet = "N/A"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT review_text FROM reviews WHERE review_id = ?", (review_id,))
        result = c.fetchone()
        if result: review_text_snippet = result['review_text'][:100]
        else:
            await query.answer("Review not found.", show_alert=True)
            try: await query.edit_message_text("Error: Review not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Reviews", callback_data="adm_manage_reviews|0")]]), parse_mode=None)
            except telegram_error.BadRequest: pass
            return
    except sqlite3.Error as e: logger.warning(f"Could not fetch review text for confirmation (ID {review_id}): {e}")
    finally:
        if conn: conn.close() # Close connection if opened
    context.user_data["confirm_action"] = f"delete_review|{review_id}"
    msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to permanently delete review ID {review_id}?\n\n"
           f"Preview: {review_text_snippet}{'...' if len(review_text_snippet) >= 100 else ''}\n\n"
           f"🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Review", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_reviews|0")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Broadcast Handlers ---

async def handle_adm_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the broadcast message process."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    context.user_data['state'] = 'awaiting_broadcast_message'
    context.user_data.pop('broadcast_content', None)
    keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
    await query.edit_message_text(
        "📢 Broadcast Message\n\nPlease send the message you want to broadcast to all users. You can include text, one photo, video, or GIF.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None
    )
    await query.answer("Send the message content.")

async def handle_confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the 'Yes' confirmation for the broadcast."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if 'broadcast_content' not in context.user_data:
        return await query.edit_message_text("❌ Error: Broadcast content not found.", parse_mode=None)
    broadcast_content = context.user_data.pop('broadcast_content')
    text, media_file_id, media_type = broadcast_content['text'], broadcast_content['media_file_id'], broadcast_content['media_type']
    admin_chat_id = query.message.chat_id
    try:
        await query.edit_message_text("⏳ Broadcast initiated. Sending messages...", parse_mode=None)
    except telegram_error.BadRequest: await query.answer() # Ignore if not modified
    asyncio.create_task(send_broadcast(context, text, media_file_id, media_type, admin_chat_id))


async def handle_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Cancels the broadcast process."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    context.user_data.pop('state', None)
    context.user_data.pop('broadcast_content', None)
    try:
        await query.edit_message_text("❌ Broadcast cancelled.", parse_mode=None)
    except telegram_error.BadRequest: await query.answer() # Ignore if not modified
    keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]
    await send_message_with_retry(context.bot, query.message.chat_id, "Returning to Admin Menu.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def send_broadcast(context: ContextTypes.DEFAULT_TYPE, text: str, media_file_id: str | None, media_type: str | None, admin_chat_id: int):
    """Sends the broadcast message to all users in the background (plain text)."""
    bot = context.bot
    users = []
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT user_id FROM users")
        users = [row['user_id'] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"DB error fetching users for broadcast: {e}")
        await send_message_with_retry(bot, admin_chat_id, f"❌ Broadcast failed: Could not fetch user list. Error: {e}", parse_mode=None)
        return
    finally:
        if conn: conn.close() # Close connection if opened
    if not users:
        logger.warning("No users found in DB to broadcast to.")
        await send_message_with_retry(bot, admin_chat_id, "⚠️ Broadcast Warning: No users found.", parse_mode=None)
        return
    success_count, fail_count, block_count, total_users = 0, 0, 0, len(users)
    logger.info(f"Starting broadcast to {total_users} users...")
    for i, user_id in enumerate(users):
        try:
            send_kwargs = {'chat_id': user_id, 'caption': text, 'parse_mode': None}
            if media_file_id and media_type == "photo": await bot.send_photo(photo=media_file_id, **send_kwargs)
            elif media_file_id and media_type == "video": await bot.send_video(video=media_file_id, **send_kwargs)
            elif media_file_id and media_type == "gif": await bot.send_animation(animation=media_file_id, **send_kwargs)
            else: await bot.send_message(chat_id=user_id, text=text, parse_mode=None, disable_web_page_preview=True)
            success_count += 1
        except telegram_error.BadRequest as e:
             error_str = str(e).lower()
             if "chat not found" in error_str or "user is deactivated" in error_str or "bot was blocked" in error_str:
                  logger.warning(f"Broadcast fail/block for user {user_id}: {e}")
                  fail_count += 1; block_count += 1
             else: logger.error(f"Broadcast BadRequest for {user_id}: {e}"); fail_count += 1
        except telegram_error.Unauthorized: logger.info(f"Broadcast skipped for {user_id}: Bot blocked."); fail_count += 1; block_count += 1
        except telegram_error.RetryAfter as e:
             retry_seconds = e.retry_after + 1
             logger.warning(f"Rate limit hit during broadcast. Sleeping {retry_seconds}s.")
             if retry_seconds > 300: logger.error(f"RetryAfter > 5 min. Aborting for {user_id}."); fail_count += 1; continue
             await asyncio.sleep(retry_seconds)
             try: # Retry send after sleep
                 send_kwargs = {'chat_id': user_id, 'caption': text, 'parse_mode': None}
                 if media_file_id and media_type == "photo": await bot.send_photo(photo=media_file_id, **send_kwargs)
                 elif media_file_id and media_type == "video": await bot.send_video(video=media_file_id, **send_kwargs)
                 elif media_file_id and media_type == "gif": await bot.send_animation(animation=media_file_id, **send_kwargs)
                 else: await bot.send_message(chat_id=user_id, text=text, parse_mode=None, disable_web_page_preview=True)
                 success_count += 1
             except Exception as retry_e: logger.error(f"Broadcast fail after retry for {user_id}: {retry_e}"); fail_count += 1;
             if isinstance(retry_e, (telegram_error.Unauthorized, telegram_error.BadRequest)): block_count +=1 # Count as blocked if retry fails with these
        except Exception as e: logger.error(f"Broadcast fail (Unexpected) for {user_id}: {e}", exc_info=True); fail_count += 1
        # Basic rate limiting
        if i > 0 and i % 10 == 0 : await asyncio.sleep(0.5)
        elif i > 0 and i % 3 == 0 : await asyncio.sleep(0.1)
    summary_msg = (f"✅ Broadcast Complete\n\nSent to: {success_count}/{total_users}\n"
                   f"Failed: {fail_count}\n(Blocked/Deactivated: {block_count})")
    await send_message_with_retry(bot, admin_chat_id, summary_msg, parse_mode=None)
    logger.info(f"Broadcast finished. Success: {success_count}, Failed: {fail_count}, Blocked: {block_count}")


# --- Confirmation Handler ---
async def handle_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles generic 'Yes' confirmation based on stored action in user_data."""
    query = update.callback_query
    user_id = query.from_user.id
    is_primary_admin = (user_id == ADMIN_ID)
    # Secondary admins cannot perform destructive confirm actions handled here
    if not is_primary_admin:
        logger.warning(f"Non-primary admin {user_id} tried to confirm a destructive action.")
        await query.answer("Permission denied for this action.", show_alert=True)
        return
    action = context.user_data.pop("confirm_action", None)
    if not action:
        try: await query.edit_message_text("❌ Error: No action pending confirmation.", parse_mode=None)
        except telegram_error.BadRequest: pass # Ignore if not modified
        return
    chat_id = query.message.chat_id
    action_parts = action.split("|")
    action_type = action_parts[0]
    action_params = action_parts[1:]
    logger.info(f"Admin {user_id} confirmed action: {action_type} with params: {action_params}")
    success_msg, next_callback = "✅ Action completed successfully!", "admin_menu"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        # --- Delete City Logic ---
        if action_type == "delete_city":
             if not action_params: raise ValueError("Missing city_id")
             city_id_str = action_params[0]; city_id_int = int(city_id_str)
             city_name = CITIES.get(city_id_str)
             if city_name:
                 # Find products to delete associated media first
                 c.execute("SELECT id FROM products WHERE city = ?", (city_name,))
                 product_ids_to_delete = [row[0] for row in c.fetchall()]
                 if product_ids_to_delete:
                     placeholders = ','.join('?' * len(product_ids_to_delete))
                     c.execute(f"DELETE FROM product_media WHERE product_id IN ({placeholders})", product_ids_to_delete)
                     # Delete associated media folders (run in background)
                     for pid in product_ids_to_delete:
                          media_dir_to_del = os.path.join(MEDIA_DIR, str(pid))
                          if os.path.exists(media_dir_to_del):
                              asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_del, ignore_errors=True))
                              logger.info(f"Scheduled deletion of media dir: {media_dir_to_del}")
                 c.execute("DELETE FROM products WHERE city = ?", (city_name,))
                 c.execute("DELETE FROM districts WHERE city_id = ?", (city_id_int,))
                 delete_city_result = c.execute("DELETE FROM cities WHERE id = ?", (city_id_int,))
                 if delete_city_result.rowcount > 0:
                     conn.commit(); load_all_data()
                     success_msg = f"✅ City '{city_name}' and contents deleted!"
                     next_callback = "adm_manage_cities"
                 else: conn.rollback(); success_msg = f"❌ Error: City '{city_name}' not found."
             else: conn.rollback(); success_msg = "❌ Error: City not found (already deleted?)."
        # --- Delete District Logic ---
        elif action_type == "remove_district":
             if len(action_params) < 2: raise ValueError("Missing city/dist_id")
             city_id_str, dist_id_str = action_params[0], action_params[1]
             city_id_int, dist_id_int = int(city_id_str), int(dist_id_str)
             city_name = CITIES.get(city_id_str)
             # Use column name
             c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (dist_id_int, city_id_int))
             dist_res = c.fetchone(); district_name = dist_res['name'] if dist_res else None
             if city_name and district_name:
                 # Find products to delete media
                 c.execute("SELECT id FROM products WHERE city = ? AND district = ?", (city_name, district_name))
                 product_ids_to_delete = [row[0] for row in c.fetchall()]
                 if product_ids_to_delete:
                     placeholders = ','.join('?' * len(product_ids_to_delete))
                     c.execute(f"DELETE FROM product_media WHERE product_id IN ({placeholders})", product_ids_to_delete)
                     # Delete associated media folders
                     for pid in product_ids_to_delete:
                          media_dir_to_del = os.path.join(MEDIA_DIR, str(pid))
                          if os.path.exists(media_dir_to_del):
                              asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_del, ignore_errors=True))
                              logger.info(f"Scheduled deletion of media dir: {media_dir_to_del}")
                 c.execute("DELETE FROM products WHERE city = ? AND district = ?", (city_name, district_name))
                 delete_dist_result = c.execute("DELETE FROM districts WHERE id = ? AND city_id = ?", (dist_id_int, city_id_int))
                 if delete_dist_result.rowcount > 0:
                     conn.commit(); load_all_data()
                     success_msg = f"✅ District '{district_name}' removed from {city_name}!"
                     next_callback = f"adm_manage_districts_city|{city_id_str}"
                 else: conn.rollback(); success_msg = f"❌ Error: District '{district_name}' not found."
             else: conn.rollback(); success_msg = "❌ Error: City or District not found."
        # --- Delete Product Logic ---
        elif action_type == "confirm_remove_product":
             if not action_params: raise ValueError("Missing product_id")
             product_id = int(action_params[0])
             # Use column names
             c.execute("SELECT ci.id as city_id, di.id as dist_id, p.product_type FROM products p LEFT JOIN cities ci ON p.city = ci.name LEFT JOIN districts di ON p.district = di.name AND ci.id = di.city_id WHERE p.id = ?", (product_id,))
             back_details_tuple = c.fetchone()
             c.execute("DELETE FROM product_media WHERE product_id = ?", (product_id,))
             delete_prod_result = c.execute("DELETE FROM products WHERE id = ?", (product_id,))
             if delete_prod_result.rowcount > 0:
                  conn.commit()
                  success_msg = f"✅ Product ID {product_id} removed!"
                  # Schedule media directory deletion (using MEDIA_DIR)
                  media_dir_to_delete = os.path.join(MEDIA_DIR, str(product_id))
                  if os.path.exists(media_dir_to_delete):
                       asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_delete, ignore_errors=True))
                       logger.info(f"Scheduled deletion of media dir: {media_dir_to_delete}")
                  if back_details_tuple and all([back_details_tuple['city_id'], back_details_tuple['dist_id'], back_details_tuple['product_type']]):
                      next_callback = f"adm_manage_products_type|{back_details_tuple['city_id']}|{back_details_tuple['dist_id']}|{back_details_tuple['product_type']}"
                  else: next_callback = "adm_manage_products"
             else: conn.rollback(); success_msg = f"❌ Error: Product ID {product_id} not found."
        # --- Delete Product Type Logic ---
        elif action_type == "delete_type":
              if not action_params: raise ValueError("Missing type_name")
              type_name = action_params[0]
              c.execute("SELECT COUNT(*) FROM products WHERE product_type = ?", (type_name,))
              count = c.fetchone()[0]
              if count == 0:
                  delete_type_result = c.execute("DELETE FROM product_types WHERE name = ?", (type_name,))
                  if delete_type_result.rowcount > 0:
                       conn.commit(); load_all_data()
                       success_msg = f"✅ Type '{type_name}' deleted!"
                       next_callback = "adm_manage_types"
                  else: conn.rollback(); success_msg = f"❌ Error: Type '{type_name}' not found."
              else: conn.rollback(); success_msg = f"❌ Error: Cannot delete type '{type_name}' as it is used by {count} product(s)."
        # --- Delete Discount Code Logic ---
        elif action_type == "delete_discount":
             if not action_params: raise ValueError("Missing discount_id")
             code_id = int(action_params[0])
             # Use column name
             c.execute("SELECT code FROM discount_codes WHERE id = ?", (code_id,))
             code_res = c.fetchone(); code_text = code_res['code'] if code_res else f"ID {code_id}"
             delete_disc_result = c.execute("DELETE FROM discount_codes WHERE id = ?", (code_id,))
             if delete_disc_result.rowcount > 0:
                 conn.commit()
                 success_msg = f"✅ Discount code {code_text} deleted!"
                 next_callback = "adm_manage_discounts"
             else: conn.rollback(); success_msg = f"❌ Error: Discount code {code_text} not found."
        # --- Delete Review Logic ---
        elif action_type == "delete_review":
            if not action_params: raise ValueError("Missing review_id")
            review_id = int(action_params[0])
            delete_rev_result = c.execute("DELETE FROM reviews WHERE review_id = ?", (review_id,))
            if delete_rev_result.rowcount > 0:
                conn.commit()
                success_msg = f"✅ Review ID {review_id} deleted!"
                next_callback = "adm_manage_reviews|0"
            else: conn.rollback(); success_msg = f"❌ Error: Review ID {review_id} not found."
        else: # Unknown action type
            logger.error(f"Unknown confirmation action type: {action_type}")
            conn.rollback()
            success_msg = "❌ Unknown action confirmed."
            next_callback = "admin_menu"

        # Edit the original confirmation message
        try: await query.edit_message_text(success_msg, parse_mode=None)
        except telegram_error.BadRequest: pass # Ignore if not modified

        # Send follow-up message with navigation
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=next_callback)]]
        await send_message_with_retry(context.bot, chat_id, "Action complete. What next?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except (sqlite3.Error, ValueError, OSError, Exception) as e:
        logger.error(f"Error executing confirmed action '{action}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        error_text = str(e)
        try: await query.edit_message_text(f"❌ An error occurred: {error_text}", parse_mode=None)
        except Exception as edit_err: logger.error(f"Failed to edit message with error: {edit_err}")
    finally:
        if conn: conn.close() # Close connection if opened


# --- Admin Message Handlers (Used when state is set) ---

async def handle_adm_add_city_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_new_city_name'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_city_name": return
    text = update.message.text.strip()
    if not text: return await send_message_with_retry(context.bot, chat_id, "City name cannot be empty.", parse_mode=None)
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("INSERT INTO cities (name) VALUES (?)", (text,))
        new_city_id = c.lastrowid
        conn.commit()
        load_all_data() # Reload global data
        context.user_data.pop("state", None)
        success_text = f"✅ City '{text}' added successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: City '{text}' already exists.", parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error adding city '{text}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add city.", parse_mode=None)
        context.user_data.pop("state", None)
    finally:
        if conn: conn.close() # Close connection if opened

async def handle_adm_add_district_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_new_district_name'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_district_name": return
    text = update.message.text.strip()
    city_id_str = context.user_data.get("admin_add_district_city_id")
    city_name = CITIES.get(city_id_str)
    if not city_id_str or not city_name:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not determine city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
        return
    if not text: return await send_message_with_retry(context.bot, chat_id, "District name cannot be empty.", parse_mode=None)
    conn = None # Initialize conn
    try:
        city_id_int = int(city_id_str)
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("INSERT INTO districts (city_id, name) VALUES (?, ?)", (city_id_int, text))
        conn.commit()
        load_all_data() # Reload global data
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
        success_text = f"✅ District '{text}' added to {city_name}!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: District '{text}' already exists in {city_name}.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error adding district '{text}' to city {city_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add district.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
    finally:
        if conn: conn.close() # Close connection if opened

async def handle_adm_edit_district_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_edit_district_name'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_edit_district_name": return
    new_name = update.message.text.strip()
    city_id_str = context.user_data.get("edit_city_id")
    dist_id_str = context.user_data.get("edit_district_id")
    city_name = CITIES.get(city_id_str)
    old_district_name = None
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id_str), int(city_id_str)))
        res = c.fetchone(); old_district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch old district name for edit: {e}")
    finally:
        if conn: conn.close() # Close connection if opened
    if not city_id_str or not dist_id_str or not city_name or old_district_name is None:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not find district/city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        return
    if not new_name: return await send_message_with_retry(context.bot, chat_id, "New district name cannot be empty.", parse_mode=None)
    if new_name == old_district_name:
        await send_message_with_retry(context.bot, chat_id, "New name is the same. No changes.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        return await send_message_with_retry(context.bot, chat_id, "No changes detected.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    conn = None # Re-initialize for update transaction
    try:
        city_id_int, dist_id_int = int(city_id_str), int(dist_id_str)
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("UPDATE districts SET name = ? WHERE id = ? AND city_id = ?", (new_name, dist_id_int, city_id_int))
        # Update products table as well
        c.execute("UPDATE products SET district = ? WHERE district = ? AND city = ?", (new_name, old_district_name, city_name))
        conn.commit()
        load_all_data() # Reload global data
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        success_text = f"✅ District updated to '{new_name}' successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: District '{new_name}' already exists.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error updating district {dist_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to update district.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
    finally:
         if conn: conn.close() # Close connection if opened


async def handle_adm_edit_city_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_edit_city_name'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_edit_city_name": return
    new_name = update.message.text.strip()
    city_id_str = context.user_data.get("edit_city_id")
    old_name = None
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT name FROM cities WHERE id = ?", (int(city_id_str),))
        res = c.fetchone(); old_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch old city name for edit: {e}")
    finally:
        if conn: conn.close() # Close connection if opened
    if not city_id_str or old_name is None:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not find city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        return
    if not new_name: return await send_message_with_retry(context.bot, chat_id, "New city name cannot be empty.", parse_mode=None)
    if new_name == old_name:
        await send_message_with_retry(context.bot, chat_id, "New name is the same. No changes.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        return await send_message_with_retry(context.bot, chat_id, "No changes detected.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    conn = None # Re-initialize for update transaction
    try:
        city_id_int = int(city_id_str)
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("UPDATE cities SET name = ? WHERE id = ?", (new_name, city_id_int))
        # Update products table as well
        c.execute("UPDATE products SET city = ? WHERE city = ?", (new_name, old_name))
        conn.commit()
        load_all_data() # Reload global data
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        success_text = f"✅ City updated to '{new_name}' successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: City '{new_name}' already exists.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error updating city {city_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to update city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
    finally:
         if conn: conn.close() # Close connection if opened


async def handle_adm_custom_size_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_custom_size'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_custom_size": return
    custom_size = update.message.text.strip()
    if not custom_size: return await send_message_with_retry(context.bot, chat_id, "Custom size cannot be empty.", parse_mode=None)
    if len(custom_size) > 50: return await send_message_with_retry(context.bot, chat_id, "Custom size too long (max 50 chars).", parse_mode=None)
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        context.user_data.pop("state", None)
        return
    context.user_data["pending_drop_size"] = custom_size
    context.user_data["state"] = "awaiting_price"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await send_message_with_retry(context.bot, chat_id, f"Custom size set to '{custom_size}'. Reply with the price (e.g., 12.50):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_price_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_price'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_price": return
    price_text = update.message.text.strip().replace(',', '.')
    try:
        price = round(float(price_text), 2)
        if price <= 0: raise ValueError("Price must be positive")
    except ValueError:
        return await send_message_with_retry(context.bot, chat_id, "❌ Invalid Price Format. Enter positive number (e.g., 12.50):", parse_mode=None)
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type", "pending_drop_size"]):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("pending_drop_size", None)
        return
    context.user_data["pending_drop_price"] = price
    context.user_data["state"] = "awaiting_drop_details"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    price_f = format_currency(price)
    await send_message_with_retry(context.bot, chat_id, f"Price set to {price_f} EUR. Now send drop details text (optional: attach one photo/video/gif).",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_drop_details_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the message containing drop text and optional media."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message: return
    if context.user_data.get("state") != "awaiting_drop_details": return
    drop_text = (update.message.caption or update.message.text or "").strip()
    required_context = ["admin_city", "admin_district", "admin_product_type", "pending_drop_size", "pending_drop_price"]
    if not all(k in context.user_data for k in required_context):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price"]
        for key in keys_to_clear: context.user_data.pop(key, None)
        return

    temp_dir = None
    media_list = []
    download_error = False
    try:
        media_file, media_type, file_id_to_download, temp_file_path = None, None, None, None
        has_media = update.message.photo or update.message.video or update.message.animation
        if has_media:
            # Create a unique temporary directory for this download
            temp_dir = await asyncio.to_thread(tempfile.mkdtemp)
            logger.info(f"Created temp dir for media download: {temp_dir}")
            if update.message.photo: media_file, file_id_to_download, media_type, temp_file_path = update.message.photo[-1], update.message.photo[-1].file_id, "photo", os.path.join(temp_dir, f"{update.message.photo[-1].file_id}.jpg")
            elif update.message.video: media_file, file_id_to_download, media_type, temp_file_path = update.message.video, update.message.video.file_id, "video", os.path.join(temp_dir, f"{update.message.video.file_id}.mp4")
            elif update.message.animation: media_file, file_id_to_download, media_type, temp_file_path = update.message.animation, update.message.animation.file_id, "gif", os.path.join(temp_dir, f"{update.message.animation.file_id}.mp4")
            if file_id_to_download and temp_file_path and media_type:
                try:
                    logger.info(f"Downloading media {file_id_to_download} to {temp_file_path}")
                    file_obj = await context.bot.get_file(file_id_to_download)
                    await file_obj.download_to_drive(custom_path=temp_file_path)
                    if not await asyncio.to_thread(os.path.exists, temp_file_path) or await asyncio.to_thread(os.path.getsize, temp_file_path) == 0:
                        raise IOError(f"Downloaded file {temp_file_path} is missing or empty.")
                    # Store the temp path for now, it will be moved later if confirmed
                    media_list.append({"type": media_type, "path": temp_file_path, "file_id": file_id_to_download})
                    logger.info("Media download successful.")
                except (telegram_error.TelegramError, IOError, OSError) as e:
                    logger.error(f"Error downloading/verifying media: {e}")
                    await send_message_with_retry(context.bot, chat_id, "⚠️ Warning: Error downloading media. Drop will be added without media.", parse_mode=None)
                    download_error = True
                    if temp_dir and os.path.exists(temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True); temp_dir = None # Clean up temp dir on download error
                except Exception as e:
                    logger.error(f"Unexpected error downloading media: {e}", exc_info=True)
                    await send_message_with_retry(context.bot, chat_id, "⚠️ Warning: Unexpected error downloading media. Drop will be added without media.", parse_mode=None)
                    download_error = True
                    if temp_dir and os.path.exists(temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True); temp_dir = None # Clean up temp dir on download error

        context.user_data["pending_drop"] = {
            "city": context.user_data["admin_city"], "district": context.user_data["admin_district"],
            "product_type": context.user_data["admin_product_type"], "size": context.user_data["pending_drop_size"],
            "price": context.user_data["pending_drop_price"], "original_text": drop_text,
            "media": media_list if not download_error else [],
            "temp_dir": temp_dir # Store temp_dir path (or None if download failed)
        }
        context.user_data.pop("state", None)
        # Prepare confirmation message
        city_name = context.user_data['admin_city']
        dist_name = context.user_data['admin_district']
        type_name = context.user_data['admin_product_type']
        size_name = context.user_data['pending_drop_size']
        price_str = format_currency(context.user_data['pending_drop_price'])
        text_preview = drop_text[:200] + ("..." if len(drop_text) > 200 else "")
        text_display = text_preview if text_preview else "No details text provided"
        media_count = len(context.user_data["pending_drop"]["media"])
        download_status = " (Download Failed)" if download_error else ""
        msg = (f"📦 Confirm New Drop\n\n🏙️ City: {city_name}\n🏘️ District: {dist_name}\n💎 Type: {type_name}\n"
               f"📏 Size: {size_name}\n💰 Price: {price_str} EUR\n📝 Details: {text_display}\n"
               f"📸 Media Attached: {'Yes' if media_count > 0 else 'No'}{download_status}\n\nAdd this drop?")
        keyboard = [[InlineKeyboardButton("✅ Yes, Add Drop", callback_data="confirm_add_drop|yes"),
                    InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_add")]]
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except Exception as e:
         logger.error(f"Unexpected error processing drop details: {e}", exc_info=True)
         await send_message_with_retry(context.bot, chat_id, "❌ An unexpected error occurred.", parse_mode=None)
         if temp_dir and os.path.exists(temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
         keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price"]
         for key in keys_to_clear: context.user_data.pop(key, None)


async def handle_adm_bot_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the media message when state is 'awaiting_bot_media'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message: return
    if context.user_data.get("state") != "awaiting_bot_media": return

    new_media_type, file_to_download, file_extension, file_id = None, None, None, None
    if update.message.photo: file_to_download, new_media_type, file_extension, file_id = update.message.photo[-1], "photo", ".jpg", update.message.photo[-1].file_id
    elif update.message.video: file_to_download, new_media_type, file_extension, file_id = update.message.video, "video", ".mp4", update.message.video.file_id
    elif update.message.animation: file_to_download, new_media_type, file_extension, file_id = update.message.animation, "gif", ".mp4", update.message.animation.file_id
    elif update.message.document and update.message.document.mime_type and 'gif' in update.message.document.mime_type.lower():
         file_to_download, new_media_type, file_extension, file_id = update.message.document, "gif", ".gif", update.message.document.file_id
    else: return await send_message_with_retry(context.bot, chat_id, "❌ Invalid Media Type. Send photo, video, or GIF.", parse_mode=None)
    if not file_to_download or not file_id: return await send_message_with_retry(context.bot, chat_id, "❌ Could not identify media file.", parse_mode=None)

    context.user_data.pop("state", None)
    await send_message_with_retry(context.bot, chat_id, "⏳ Downloading and saving new media...", parse_mode=None)

    # Save directly to the persistent disk media directory
    # MEDIA_DIR is imported from utils and points to /mnt/data/media
    final_media_path = os.path.join(MEDIA_DIR, f"bot_media{file_extension}")
    temp_download_path = final_media_path + ".tmp"

    try:
        logger.info(f"Downloading new bot media ({new_media_type}) ID {file_id} to {temp_download_path}")
        file_obj = await context.bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=temp_download_path)
        logger.info("Media download successful to temp path.")

        if not await asyncio.to_thread(os.path.exists, temp_download_path) or await asyncio.to_thread(os.path.getsize, temp_download_path) == 0:
             raise IOError("Downloaded file is empty or missing.")

        # Get old path from global BOT_MEDIA dict (already points to MEDIA_DIR if loaded)
        old_media_path_global = BOT_MEDIA.get("path")
        if old_media_path_global and old_media_path_global != final_media_path and await asyncio.to_thread(os.path.exists, old_media_path_global):
            try:
                await asyncio.to_thread(os.remove, old_media_path_global)
                logger.info(f"Removed old bot media file: {old_media_path_global}")
            except OSError as e:
                logger.warning(f"Could not remove old bot media file '{old_media_path_global}': {e}")

        # Move temp file to final location on persistent disk
        await asyncio.to_thread(shutil.move, temp_download_path, final_media_path)
        logger.info(f"Moved media to final path: {final_media_path}")

        # Update global BOT_MEDIA dict and save to JSON on persistent disk
        BOT_MEDIA["type"] = new_media_type
        BOT_MEDIA["path"] = final_media_path # Store the correct persistent path

        # Use BOT_MEDIA_JSON_PATH from utils
        try:
            # Use asyncio.to_thread for file writing
            async def write_json_async(path, data):
                with open(path, 'w') as f:
                    json.dump(data, f, indent=4)
            await asyncio.to_thread(write_json_async, BOT_MEDIA_JSON_PATH, BOT_MEDIA)
            logger.info(f"Successfully wrote updated BOT_MEDIA to {BOT_MEDIA_JSON_PATH}: {BOT_MEDIA}")
        except Exception as e:
            logger.error(f"Failed to write {BOT_MEDIA_JSON_PATH}: {e}")

        await send_message_with_retry(context.bot, chat_id, "✅ Bot Media Updated Successfully!", parse_mode=None)
        keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]
        await send_message_with_retry(context.bot, chat_id, "Changes applied.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except (telegram_error.TelegramError, IOError, OSError) as e:
        logger.error(f"Error downloading/saving bot media: {e}")
        await send_message_with_retry(context.bot, chat_id, "❌ Error downloading or saving media. Please try again.", parse_mode=None)
        if await asyncio.to_thread(os.path.exists, temp_download_path):
            try: await asyncio.to_thread(os.remove, temp_download_path)
            except OSError: pass
    except Exception as e:
        logger.error(f"Unexpected error updating bot media: {e}", exc_info=True)
        await send_message_with_retry(context.bot, chat_id, "❌ An unexpected error occurred.", parse_mode=None)
    finally:
        # Clean up temp file if it still exists in the final block
        if 'temp_download_path' in locals() and await asyncio.to_thread(os.path.exists, temp_download_path):
             try: await asyncio.to_thread(os.remove, temp_download_path)
             except OSError as e: logger.warning(f"Could not remove temp dl file '{temp_download_path}': {e}")


async def handle_adm_add_type_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text reply when state is 'awaiting_new_type_name'."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_type_name": return
    type_name = update.message.text.strip()
    if not type_name: return await send_message_with_retry(context.bot, chat_id, "Product type name cannot be empty.", parse_mode=None)
    if len(type_name) > 100: return await send_message_with_retry(context.bot, chat_id, "Product type name too long (max 100 chars).", parse_mode=None)
    if type_name.lower() in [pt.lower() for pt in PRODUCT_TYPES]:
        return await send_message_with_retry(context.bot, chat_id, f"❌ Error: Type '{type_name}' already exists.", parse_mode=None)
    conn=None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("INSERT INTO product_types (name) VALUES (?)", (type_name,))
        conn.commit()
        load_all_data() # Reload global data
        context.user_data.pop("state", None)
        success_text = f"✅ Product Type '{type_name}' added!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Types", callback_data="adm_manage_types")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: Product type '{type_name}' already exists.", parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error adding product type '{type_name}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add type.", parse_mode=None)
        context.user_data.pop("state", None)
    finally:
        if conn: conn.close() # Close connection if opened


# --- Message Handlers for Discount Creation ---

async def process_discount_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE, code_text: str):
    """Shared logic to process entered/generated discount code and ask for type."""
    chat_id = update.effective_chat.id
    query = update.callback_query
    if not code_text:
        msg = "Code cannot be empty. Please try again."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return
    if len(code_text) > 50:
        msg = "Code too long (max 50 chars)."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("SELECT 1 FROM discount_codes WHERE code = ?", (code_text,))
        if c.fetchone():
            error_msg = f"❌ Error: Discount code '{code_text}' already exists."
            if query:
                try: await query.edit_message_text(error_msg, parse_mode=None)
                except telegram_error.BadRequest: await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
            else: await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
            return
    except sqlite3.Error as e:
        logger.error(f"DB error checking discount code uniqueness: {e}")
        error_msg = "❌ Database error checking code uniqueness."
        if query: await query.answer("DB Error.", show_alert=True)
        await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
        context.user_data.pop('state', None)
        return
    finally:
        if conn: conn.close() # Close connection if opened
    if 'new_discount_info' not in context.user_data: context.user_data['new_discount_info'] = {}
    context.user_data['new_discount_info']['code'] = code_text
    context.user_data['state'] = 'awaiting_discount_type'
    keyboard = [
        [InlineKeyboardButton("％ Percentage", callback_data="adm_set_discount_type|percentage"),
         InlineKeyboardButton("€ Fixed Amount", callback_data="adm_set_discount_type|fixed")],
        [InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]
    ]
    prompt_msg = f"Code set to: {code_text}\n\nSelect the discount type:"
    if query:
        try: await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest: await query.answer() # Ignore if not modified
    else: await send_message_with_retry(context.bot, chat_id, prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_discount_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount code text via message."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != "awaiting_discount_code": return
    if not update.message or not update.message.text: return
    code_text = update.message.text.strip()
    await process_discount_code_input(update, context, code_text)


async def handle_adm_discount_value_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount value and saves the code."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != "awaiting_discount_value": return
    if not update.message or not update.message.text: return
    value_text = update.message.text.strip().replace(',', '.')
    discount_info = context.user_data.get('new_discount_info', {})
    code = discount_info.get('code'); dtype = discount_info.get('type')
    if not code or not dtype:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Discount context lost.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
        return
    conn = None # Initialize conn
    try:
        value = float(value_text)
        if value <= 0: raise ValueError("Discount value must be positive.")
        if dtype == 'percentage' and (value > 100): raise ValueError("Percentage cannot exceed 100.")
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("INSERT INTO discount_codes (code, discount_type, value, created_date, is_active) VALUES (?, ?, ?, ?, 1)",
                  (code, dtype, value, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"Admin {user_id} added discount code: {code} ({dtype}, {value})")
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
        await send_message_with_retry(context.bot, chat_id, f"✅ Discount code '{code}' added!", parse_mode=None)
        keyboard = [[InlineKeyboardButton("🏷️ View Discount Codes", callback_data="adm_manage_discounts")]]
        await send_message_with_retry(context.bot, chat_id, "Returning to discount management.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except ValueError as e:
        await send_message_with_retry(context.bot, chat_id, f"❌ Invalid Value: {e}. Enter valid positive number.", parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error saving discount code '{code}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Database error saving code.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
    finally:
        if conn: conn.close() # Close connection if opened


# --- Message Handler for Broadcast ---
async def handle_adm_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles receiving the message content for the broadcast."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_broadcast_message': return
    if not update.message: return
    text = (update.message.text or update.message.caption or "").strip() # Ensure strip
    media_file_id, media_type = None, None
    if update.message.photo: media_file_id, media_type = update.message.photo[-1].file_id, "photo"
    elif update.message.video: media_file_id, media_type = update.message.video.file_id, "video"
    elif update.message.animation: media_file_id, media_type = update.message.animation.file_id, "gif"
    # Ensure either text or media exists
    if not text and not media_file_id:
        await send_message_with_retry(context.bot, chat_id, "Broadcast message cannot be empty. Please send text or media.", parse_mode=None)
        return # Keep state awaiting_broadcast_message
    context.user_data['broadcast_content'] = {'text': text, 'media_file_id': media_file_id, 'media_type': media_type}
    context.user_data.pop('state', None) # Clear state after receiving content
    preview_msg = "📢 Confirm Broadcast\n\nPreview:\n\n"
    if media_file_id: preview_msg += f"{media_type.capitalize()} attached\n"
    text_preview = text[:500] + ('...' if len(text) > 500 else '')
    preview_msg += text_preview if text else "(No text)"
    preview_msg += "\n\nSend this message to all users?"
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Send Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_broadcast")]
    ]
    await send_message_with_retry(context.bot, chat_id, preview_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)