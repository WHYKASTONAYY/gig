# --- START OF FILE viewer_admin.py ---

import sqlite3
import os
import logging
import asyncio
import shutil
from datetime import datetime, timedelta
from collections import defaultdict
import math # For pagination calculation

# --- Telegram Imports ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAnimation
)
from telegram.constants import ParseMode # Keep import for reference
from telegram.ext import ContextTypes
from telegram import helpers # Keep for potential other uses, but not escaping
import telegram.error as telegram_error
# -------------------------

# Import shared elements from utils
from utils import (
    ADMIN_ID, LANGUAGES, format_currency, send_message_with_retry,
    SECONDARY_ADMIN_IDS, fetch_reviews,
    get_db_connection, MEDIA_DIR # Import helper and MEDIA_DIR
)
# Import the shared stock handler from stock.py
try:
    from stock import handle_view_stock # <-- IMPORT shared stock handler
except ImportError:
     # Create a logger instance before using it in the dummy handler
    logger_dummy_stock = logging.getLogger(__name__ + "_dummy_stock")
    logger_dummy_stock.error("Could not import handle_view_stock from stock.py. Stock viewing will not work.")
    # Define a dummy handler
    async def handle_view_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query
        msg = "Stock viewing handler not found (stock.py missing or error).\nPlease contact the primary admin."
        if query: await query.edit_message_text(msg, parse_mode=None) # Use None
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None) # Use None

# Logging setup specific to this module
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constants ---
PRODUCTS_PER_PAGE_LOG = 5 # Number of products to show per page in the log
REVIEWS_PER_PAGE_VIEWER = 5 # Number of reviews to show per page for viewer admin


# --- Viewer Admin Menu ---
async def handle_viewer_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the limited admin dashboard for secondary admins."""
    user = update.effective_user
    query = update.callback_query

    if not user:
        logger.warning("handle_viewer_admin_menu triggered without effective_user.")
        if query: await query.answer("Error: Could not identify user.", show_alert=True)
        return

    user_id = user.id
    chat_id = update.effective_chat.id

    # --- Authorization Check ---
    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)

    if not is_primary_admin and not is_secondary_admin:
        logger.warning(f"Non-admin user {user_id} attempted to access viewer admin menu.")
        if query: await query.answer("Access denied.", show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, "Access denied.", parse_mode=None)
        return

    # --- Prepare Message Content ---
    total_users, active_products = 0, 0
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column names
        c.execute("SELECT COUNT(*) as count FROM users")
        res_users = c.fetchone(); total_users = res_users['count'] if res_users else 0
        c.execute("SELECT COUNT(*) as count FROM products WHERE available > reserved")
        res_products = c.fetchone(); active_products = res_products['count'] if res_products else 0
    except sqlite3.Error as e:
        logger.error(f"DB error fetching viewer admin dashboard data: {e}", exc_info=True)
        pass # Continue without stats on error
    finally:
        if conn: conn.close() # Close connection if opened

    msg = (
       f"🔧 Admin Dashboard (Viewer)\n\n"
       f"👥 Total Users: {total_users}\n"
       f"📦 Active Products: {active_products}\n\n"
       "Select a report or log to view:"
    )

    # --- Keyboard Definition ---
    keyboard = [
        [InlineKeyboardButton("📦 View Bot Stock", callback_data="view_stock")],
        [InlineKeyboardButton("📜 View Added Products Log", callback_data="viewer_added_products|0")],
        [InlineKeyboardButton("🚫 View Reviews", callback_data="adm_manage_reviews|0")], # Reuse admin handler
        [InlineKeyboardButton("🏠 User Home Menu", callback_data="back_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Send or Edit Message ---
    if query:
        try:
            await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode=None)
        except telegram_error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error editing viewer admin menu message: {e}")
                await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
            else: await query.answer()
        except Exception as e:
            logger.error(f"Unexpected error editing viewer admin menu: {e}", exc_info=True)
            await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
    else: # Called by command or other non-callback scenario
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)


# --- Added Products Log Handler ---
async def handle_viewer_added_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays a paginated log of products added to the database for viewer admin."""
    query = update.callback_query
    user_id = query.from_user.id

    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)
    if not is_primary_admin and not is_secondary_admin:
        return await query.answer("Access Denied.", show_alert=True)

    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])

    products = []
    total_products = 0
    conn = None

    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()

        # Use column names
        c.execute("SELECT COUNT(*) as count FROM products")
        count_res = c.fetchone(); total_products = count_res['count'] if count_res else 0

        c.execute("""
            SELECT p.id, p.city, p.district, p.product_type, p.size, p.price,
                   p.original_text, p.added_date,
                   (SELECT COUNT(*) FROM product_media pm WHERE pm.product_id = p.id) as media_count
            FROM products p ORDER BY p.id DESC LIMIT ? OFFSET ?
        """, (PRODUCTS_PER_PAGE_LOG, offset))
        products = c.fetchall()

    except sqlite3.Error as e:
        logger.error(f"DB error fetching viewer added product log: {e}", exc_info=True)
        await query.edit_message_text("❌ Error fetching product log from database.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    msg_parts = ["📜 Added Products Log\n"]
    keyboard = []
    item_buttons = []

    if not products:
        if offset == 0: msg_parts.append("\nNo products have been added yet.")
        else: msg_parts.append("\nNo more products to display.")
    else:
        for product in products: # product is now a Row object
            try:
                # Access by column name
                prod_id = product['id']
                city_name, dist_name = product['city'], product['district']
                type_name, size_name = product['product_type'], product['size']
                price_str = format_currency(product['price'])
                media_indicator = "📸" if product['media_count'] > 0 else "🚫"
                added_date_str = "Unknown Date"
                if product['added_date']:
                    try: added_date_str = datetime.fromisoformat(product['added_date']).strftime("%Y-%m-%d %H:%M")
                    except (ValueError, TypeError): pass
                original_text_preview = (product['original_text'] or "")[:150] + ("..." if len(product['original_text'] or "") > 150 else "")
                text_display = original_text_preview if original_text_preview else "No text provided"
                item_msg = (
                    f"\nID {prod_id} | {added_date_str}\n"
                    f"📍 {city_name} / {dist_name}\n"
                    f"📦 {type_name} {size_name} ({price_str} €)\n"
                    f"📝 Text: {text_display}\n"
                    f"{media_indicator} Media Attached: {'Yes' if product['media_count'] > 0 else 'No'}\n"
                    f"---\n"
                )
                msg_parts.append(item_msg)
                # Buttons
                button_text = f"🖼️ View Media & Text #{prod_id}" if product['media_count'] > 0 else f"📄 View Full Text #{prod_id}"
                item_buttons.append([InlineKeyboardButton(button_text, callback_data=f"viewer_view_product_media|{prod_id}|{offset}")])
            except Exception as e:
                 logger.error(f"Error formatting viewer product log item ID {product.get('id', 'N/A')}: {e}")
                 msg_parts.append(f"\nID {product.get('id', 'N/A')} | (Error displaying item)\n---\n")
        keyboard.extend(item_buttons)
        # Pagination
        total_pages = math.ceil(total_products / PRODUCTS_PER_PAGE_LOG)
        current_page = (offset // PRODUCTS_PER_PAGE_LOG) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"viewer_added_products|{max(0, offset - PRODUCTS_PER_PAGE_LOG)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"viewer_added_products|{offset + PRODUCTS_PER_PAGE_LOG}"))
        if nav_buttons: keyboard.append(nav_buttons)
        msg_parts.append(f"\nPage {current_page}/{total_pages}")

    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="viewer_admin_menu")])
    final_msg = "".join(msg_parts)
    try:
        if len(final_msg) > 4090: final_msg = final_msg[:4090] + "\n\n✂️ ... Message truncated."
        await query.edit_message_text(final_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" in str(e).lower(): await query.answer()
        else: logger.error(f"Failed to edit viewer_added_products msg: {e}"); await query.answer("Error displaying product log.", show_alert=True)
    except Exception as e:
        logger.error(f"Unexpected error in handle_viewer_added_products: {e}", exc_info=True)
        await query.edit_message_text("❌ An unexpected error occurred.", parse_mode=None)


# --- View Product Media/Text Handler ---
async def handle_viewer_view_product_media(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Fetches and sends the media and original text for a specific product ID for viewer admin."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)
    if not is_primary_admin and not is_secondary_admin:
        return await query.answer("Access Denied.", show_alert=True)

    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Missing/invalid product ID/offset.", show_alert=True)
        return

    product_id = int(params[0])
    original_offset = int(params[1])
    back_button_callback = f"viewer_added_products|{original_offset}"

    media_items = []
    original_text = ""
    product_name = f"Product ID {product_id}"
    conn = None

    try:
        conn = get_db_connection() # Use helper
        # row_factory set in helper
        c = conn.cursor()
        # Use column names
        c.execute("SELECT name, original_text FROM products WHERE id = ?", (product_id,))
        prod_info = c.fetchone()
        if prod_info:
             original_text = prod_info['original_text'] or ""
             product_name = prod_info['name'] or product_name
        else:
            await query.answer("Product not found.", show_alert=True)
            try: await query.edit_message_text("Product not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Log", callback_data=back_button_callback)]]), parse_mode=None)
            except telegram_error.BadRequest: pass
            return
        # Use column names
        c.execute("SELECT media_type, telegram_file_id, file_path FROM product_media WHERE product_id = ?", (product_id,))
        media_items = c.fetchall()

    except sqlite3.Error as e:
        logger.error(f"DB error fetching media/text for product {product_id}: {e}", exc_info=True)
        await query.answer("Error fetching product details.", show_alert=True)
        try: await query.edit_message_text("Error fetching product details.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Log", callback_data=back_button_callback)]]), parse_mode=None)
        except telegram_error.BadRequest: pass
        return
    finally:
        if conn: conn.close()

    await query.answer("Fetching details...")
    try: await query.edit_message_text(f"⏳ Fetching details for product ID {product_id}...", parse_mode=None)
    except telegram_error.BadRequest: pass

    media_sent_count = 0
    media_group = []
    caption_sent_separately = False
    # Use MEDIA_DIR from utils
    first_media_caption = f"Details for {product_name} (ID: {product_id})\n\n{original_text if original_text else 'No text provided'}"
    if len(first_media_caption) > 1020: first_media_caption = first_media_caption[:1020] + "..."

    opened_files = []
    try:
        for i, item in enumerate(media_items): # item is now a Row object
            # Access by column name
            media_type = item['media_type']
            file_id = item['telegram_file_id']
            # file_path already includes MEDIA_DIR from when it was saved
            file_path = item['file_path']
            caption_to_use = first_media_caption if i == 0 else None
            input_media = None
            file_handle = None
            try:
                if file_id:
                    if media_type == 'photo': input_media = InputMediaPhoto(media=file_id, caption=caption_to_use, parse_mode=None)
                    elif media_type == 'video': input_media = InputMediaVideo(media=file_id, caption=caption_to_use, parse_mode=None)
                    elif media_type == 'gif': input_media = InputMediaAnimation(media=file_id, caption=caption_to_use, parse_mode=None)
                    else: logger.warning(f"Unknown media type '{media_type}' with file_id P{product_id}"); continue
                elif file_path and await asyncio.to_thread(os.path.exists, file_path): # Check existence before open
                    logger.info(f"Opening media file {file_path} P{product_id}")
                    # Use asyncio.to_thread for blocking file I/O
                    file_handle = await asyncio.to_thread(open, file_path, 'rb')
                    opened_files.append(file_handle) # Keep track to close later
                    if media_type == 'photo': input_media = InputMediaPhoto(media=file_handle, caption=caption_to_use, parse_mode=None)
                    elif media_type == 'video': input_media = InputMediaVideo(media=file_handle, caption=caption_to_use, parse_mode=None)
                    elif media_type == 'gif': input_media = InputMediaAnimation(media=file_handle, caption=caption_to_use, parse_mode=None)
                    else:
                        logger.warning(f"Unsupported media type '{media_type}' from path {file_path}")
                        # Ensure file handle is closed if we skip
                        await asyncio.to_thread(file_handle.close)
                        opened_files.remove(file_handle)
                        continue # Skip adding to media_group
                else: logger.warning(f"Media item invalid P{product_id}: No file_id and path '{file_path}' missing or inaccessible."); continue

                media_group.append(input_media)
                media_sent_count += 1

            except Exception as e:
                logger.error(f"Error preparing media item {i+1} P{product_id}: {e}", exc_info=True)
                # If preparing the first item fails, the caption needs to be sent separately
                if i == 0: caption_sent_separately = True
                # Clean up file handle if opened during failed preparation
                if file_handle and file_handle in opened_files:
                    await asyncio.to_thread(file_handle.close)
                    opened_files.remove(file_handle)

        # Send media group
        if media_group:
            try:
                await context.bot.send_media_group(chat_id, media=media_group)
                logger.info(f"Sent media group with {len(media_group)} items for product {product_id} to chat {chat_id}.")
            except Exception as e:
                 logger.error(f"Failed send media group P{product_id}: {e}")
                 # If sending fails, ensure caption is sent separately if it was attached
                 if media_group and media_group[0].caption:
                      caption_sent_separately = True

    finally:
        # Close ALL originally opened file handles in the finally block
        for f in opened_files:
            try:
                if not f.closed:
                    await asyncio.to_thread(f.close)
                    logger.debug(f"Closed file handle: {getattr(f, 'name', 'unknown')}")
            except Exception as close_e:
                logger.warning(f"Error closing file handle '{getattr(f, 'name', 'unknown')}' during cleanup: {close_e}")

    # Send the text caption separately if it wasn't sent with media or if sending failed
    if media_sent_count == 0 or caption_sent_separately:
         text_to_send = f"Details for {product_name} (ID: {product_id})\n\n{original_text if original_text else 'No text provided'}" # Plain text
         if media_sent_count == 0 and not original_text:
              text_to_send = f"No media or text found for product ID {product_id}" # Plain text

         await send_message_with_retry(
             context.bot,
             chat_id,
             text_to_send,
             parse_mode=None # Use None
         )

    # Send a final message indicating completion, with a back button
    await send_message_with_retry(
        context.bot,
        chat_id,
        f"End of details for product ID {product_id}.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Log", callback_data=back_button_callback)]]),
        parse_mode=None # Use None
    )

# --- END OF FILE viewer_admin.py ---