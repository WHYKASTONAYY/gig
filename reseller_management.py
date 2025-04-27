# --- START OF FILE reseller_management.py ---

import sqlite3
import logging
from decimal import Decimal, ROUND_DOWN # Use Decimal for precision
import math # For pagination calculation

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import telegram.error as telegram_error
# -------------------------

# Import shared elements from utils
from utils import (
    ADMIN_ID, LANGUAGES, get_db_connection, send_message_with_retry,
    PRODUCT_TYPES, format_currency, log_admin_action, load_all_data, # Added load_all_data
    DEFAULT_PRODUCT_EMOJI
)

# Logging setup specific to this module
logger = logging.getLogger(__name__)

# Constants
USERS_PER_PAGE_RESELLER = 10 # Users per page when selecting reseller

# --- Helper Function to Get Reseller Discount ---
def get_reseller_discount(user_id: int, product_type: str) -> Decimal:
    """Fetches the discount percentage for a specific reseller and product type."""
    discount = Decimal('0.0')
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Check if user is reseller first
        c.execute("SELECT is_reseller FROM users WHERE user_id = ?", (user_id,))
        res = c.fetchone()
        if res and res['is_reseller'] == 1:
            # Fetch specific discount
            c.execute("""
                SELECT discount_percentage FROM reseller_discounts
                WHERE reseller_user_id = ? AND product_type = ?
            """, (user_id, product_type))
            discount_res = c.fetchone()
            if discount_res:
                discount = Decimal(str(discount_res['discount_percentage']))
                logger.debug(f"Found reseller discount for user {user_id}, type {product_type}: {discount}%")
    except sqlite3.Error as e:
        logger.error(f"DB error fetching reseller discount for user {user_id}, type {product_type}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching reseller discount: {e}", exc_info=True)
    finally:
        if conn: conn.close()
    return discount


# ==================================
# --- Admin: Manage Reseller Status ---
# ==================================

async def handle_manage_resellers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays a paginated list of users to toggle their reseller status."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])

    users = []
    total_users = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE user_id != ?", (ADMIN_ID,))
        count_res = c.fetchone(); total_users = count_res['count'] if count_res else 0
        c.execute("""
            SELECT user_id, username, is_reseller FROM users
            WHERE user_id != ?
            ORDER BY user_id DESC LIMIT ? OFFSET ?
        """, (ADMIN_ID, USERS_PER_PAGE_RESELLER, offset))
        users = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching users for reseller mgmt: {e}")
        await query.edit_message_text("❌ DB Error fetching users.")
        return
    finally:
        if conn: conn.close()

    msg_parts = ["👤 Manage Reseller Status\n\nSelect user to toggle status:\n"]
    keyboard = []
    item_buttons = []

    if not users and offset == 0: msg_parts.append("\nNo users found.")
    elif not users: msg_parts.append("\nNo more users.")
    else:
        for user in users:
            user_id_target = user['user_id']
            username = user['username'] or f"ID_{user_id_target}"
            status_emoji = "✅ Reseller" if user['is_reseller'] else "❌ Not Reseller"
            button_text = f"{status_emoji} - @{username}"
            msg_parts.append(f"\n • @{username} (ID: {user_id_target}) - Status: {status_emoji}") # Info in message
            item_buttons.append([InlineKeyboardButton(button_text, callback_data=f"reseller_toggle_status|{user_id_target}|{offset}")])
        keyboard.extend(item_buttons)
        # Pagination
        total_pages = math.ceil(max(0, total_users) / USERS_PER_PAGE_RESELLER)
        current_page = (offset // USERS_PER_PAGE_RESELLER) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage_resellers_menu|{max(0, offset - USERS_PER_PAGE_RESELLER)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage_resellers_menu|{offset + USERS_PER_PAGE_RESELLER}"))
        if nav_buttons: keyboard.append(nav_buttons)
        msg_parts.append(f"\nPage {current_page}/{total_pages}")

    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    final_msg = "".join(msg_parts)
    try:
        await query.edit_message_text(final_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing reseller status list: {e}")
            await query.answer("Error updating list.", show_alert=True)
        else: await query.answer()
    except Exception as e:
        logger.error(f"Unexpected error display reseller list: {e}", exc_info=True)
        await query.edit_message_text("❌ Error displaying list.")


async def handle_reseller_toggle_status(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Toggles the is_reseller flag for a user."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_user_id = int(params[0])
    offset = int(params[1])
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT is_reseller FROM users WHERE user_id = ?", (target_user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.answer("User not found.", show_alert=True)
            return await handle_manage_resellers_menu(update, context, [str(offset)])

        current_status = user_data['is_reseller']
        new_status = 0 if current_status == 1 else 1
        c.execute("UPDATE users SET is_reseller = ? WHERE user_id = ?", (new_status, target_user_id))
        conn.commit()

        # Log action
        action_desc = "RESELLER_ENABLED" if new_status == 1 else "RESELLER_DISABLED"
        log_admin_action(admin_id, action_desc, target_user_id=target_user_id, old_value=current_status, new_value=new_status)

        status_text = "enabled" if new_status == 1 else "disabled"
        await query.answer(f"Reseller status {status_text} for user {target_user_id}.")
        await handle_manage_resellers_menu(update, context, [str(offset)]) # Refresh list

    except sqlite3.Error as e:
        logger.error(f"DB error toggling reseller status {target_user_id}: {e}")
        await query.answer("DB Error.", show_alert=True)
    except Exception as e:
        logger.error(f"Error toggling reseller status {target_user_id}: {e}", exc_info=True)
        await query.answer("Error.", show_alert=True)
    finally:
        if conn: conn.close()


# ========================================
# --- Admin: Manage Reseller Discounts ---
# ========================================

async def handle_manage_reseller_discounts_select_reseller(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects which active reseller to manage discounts for."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])

    resellers = []
    total_resellers = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_reseller = 1")
        count_res = c.fetchone(); total_resellers = count_res['count'] if count_res else 0
        c.execute("""
            SELECT user_id, username FROM users
            WHERE is_reseller = 1 ORDER BY user_id DESC LIMIT ? OFFSET ?
        """, (USERS_PER_PAGE_RESELLER, offset))
        resellers = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching active resellers: {e}")
        await query.edit_message_text("❌ DB Error fetching resellers.")
        return
    finally:
        if conn: conn.close()

    msg = "👤 Manage Reseller Discounts\n\nSelect an active reseller to set their discounts:\n"
    keyboard = []
    item_buttons = []

    if not resellers and offset == 0: msg += "\nNo active resellers found."
    elif not resellers: msg += "\nNo more resellers."
    else:
        for r in resellers:
            username = r['username'] or f"ID_{r['user_id']}"
            item_buttons.append([InlineKeyboardButton(f"👤 @{username}", callback_data=f"reseller_manage_specific|{r['user_id']}")])
        keyboard.extend(item_buttons)
        # Pagination
        total_pages = math.ceil(max(0, total_resellers) / USERS_PER_PAGE_RESELLER)
        current_page = (offset // USERS_PER_PAGE_RESELLER) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage_reseller_discounts_select_reseller|{max(0, offset - USERS_PER_PAGE_RESELLER)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage_reseller_discounts_select_reseller|{offset + USERS_PER_PAGE_RESELLER}"))
        if nav_buttons: keyboard.append(nav_buttons)
        msg += f"\nPage {current_page}/{total_pages}"

    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing reseller selection list: {e}")
            await query.answer("Error updating list.", show_alert=True)
        else: await query.answer()
    except Exception as e:
        logger.error(f"Error display reseller selection list: {e}", exc_info=True)
        await query.edit_message_text("❌ Error displaying list.")


async def handle_manage_specific_reseller_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays current discounts for a specific reseller and allows adding/editing."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid user ID.", show_alert=True); return

    target_reseller_id = int(params[0])
    discounts = []
    username = f"ID_{target_reseller_id}"
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (target_reseller_id,))
        user_res = c.fetchone(); username = user_res['username'] if user_res and user_res['username'] else username
        c.execute("""
            SELECT product_type, discount_percentage FROM reseller_discounts
            WHERE reseller_user_id = ? ORDER BY product_type
        """, (target_reseller_id,))
        discounts = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching discounts for reseller {target_reseller_id}: {e}")
        await query.edit_message_text("❌ DB Error fetching discounts.")
        return
    finally:
        if conn: conn.close()

    msg = f"🏷️ Discounts for Reseller @{username} (ID: {target_reseller_id})\n\n"
    keyboard = []

    if not discounts: msg += "No specific discounts set yet."
    else:
        msg += "Current Discounts:\n"
        for discount in discounts:
            p_type = discount['product_type']
            # Ensure PRODUCT_TYPES is loaded if needed here, or rely on global load
            emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
            percentage = Decimal(str(discount['discount_percentage']))
            msg += f" • {emoji} {p_type}: {percentage:.1f}%\n"
            # Add Edit/Delete buttons per item
            keyboard.append([
                 InlineKeyboardButton(f"✏️ Edit {p_type} ({percentage:.1f}%)", callback_data=f"reseller_edit_discount|{target_reseller_id}|{p_type}"),
                 InlineKeyboardButton(f"🗑️ Delete", callback_data=f"reseller_delete_discount_confirm|{target_reseller_id}|{p_type}")
            ])

    keyboard.append([InlineKeyboardButton("➕ Add New Discount Rule", callback_data=f"reseller_add_discount_select_type|{target_reseller_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Reseller List", callback_data="manage_reseller_discounts_select_reseller|0")]) # Back to first page

    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing specific reseller discounts: {e}")
            await query.answer("Error updating view.", show_alert=True)
        else: await query.answer()
    except Exception as e:
        logger.error(f"Error display specific reseller discounts: {e}", exc_info=True)
        await query.edit_message_text("❌ Error displaying discounts.")


async def handle_reseller_add_discount_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects product type for a new reseller discount rule."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid user ID.", show_alert=True); return

    target_reseller_id = int(params[0])
    load_all_data() # Ensure PRODUCT_TYPES is fresh

    if not PRODUCT_TYPES:
        await query.edit_message_text("❌ No product types configured. Please add types via 'Manage Product Types'.")
        return

    keyboard = []
    for type_name, emoji in sorted(PRODUCT_TYPES.items()):
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name}", callback_data=f"reseller_add_discount_enter_percent|{target_reseller_id}|{type_name}")])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")])
    await query.edit_message_text("Select Product Type for new discount rule:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_reseller_add_discount_enter_percent(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin needs to enter the percentage for the new rule."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    context.user_data['state'] = 'awaiting_reseller_discount_percent'
    context.user_data['reseller_mgmt_target_id'] = target_reseller_id
    context.user_data['reseller_mgmt_product_type'] = product_type
    context.user_data['reseller_mgmt_mode'] = 'add' # 'add' or 'edit'

    await query.edit_message_text(
        f"Enter discount percentage for {emoji} {product_type} (e.g., 10 or 15.5):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")]]),
        parse_mode=None
    )
    await query.answer("Enter percentage in chat.")


async def handle_reseller_edit_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin wants to edit an existing discount percentage."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    # Fetch current value for display if needed, but prompt is for new value
    # current_discount = get_reseller_discount(target_reseller_id, product_type) # Could add this

    context.user_data['state'] = 'awaiting_reseller_discount_percent'
    context.user_data['reseller_mgmt_target_id'] = target_reseller_id
    context.user_data['reseller_mgmt_product_type'] = product_type
    context.user_data['reseller_mgmt_mode'] = 'edit' # Set mode to edit

    await query.edit_message_text(
        f"Enter *new* discount percentage for {emoji} {product_type} (e.g., 10 or 15.5):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")]]),
        parse_mode=None
    )
    await query.answer("Enter new percentage in chat.")

async def handle_reseller_percent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount percentage via message."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if admin_id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_reseller_discount_percent': return
    if not update.message or not update.message.text: return

    percent_text = update.message.text.strip()
    target_user_id = context.user_data.get('reseller_mgmt_target_id')
    product_type = context.user_data.get('reseller_mgmt_product_type')
    mode = context.user_data.get('reseller_mgmt_mode', 'add') # Default to add

    if target_user_id is None or not product_type:
        logger.error("State awaiting_reseller_discount_percent missing context data.")
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.")
        context.user_data.pop('state', None)
        # Try to send back to reseller list if possible, otherwise admin menu
        fallback_cb = "manage_reseller_discounts_select_reseller|0"
        await send_message_with_retry(context.bot, chat_id, "Returning...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=fallback_cb)]]))
        return

    back_callback = f"reseller_manage_specific|{target_user_id}"

    try:
        percentage = Decimal(percent_text)
        if not (Decimal('0.0') <= percentage <= Decimal('100.0')):
            raise ValueError("Percentage must be between 0 and 100.")

        conn = None
        old_value = None # For logging edits
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("BEGIN") # Start transaction

            if mode == 'edit':
                # Fetch old value before updating
                c.execute("SELECT discount_percentage FROM reseller_discounts WHERE reseller_user_id = ? AND product_type = ?", (target_user_id, product_type))
                old_res = c.fetchone()
                old_value = old_res['discount_percentage'] if old_res else None

            if mode == 'add':
                sql = "INSERT INTO reseller_discounts (reseller_user_id, product_type, discount_percentage) VALUES (?, ?, ?)"
                params_sql = (target_user_id, product_type, float(percentage))
                action_desc = "RESELLER_DISCOUNT_ADD"
            else: # mode == 'edit'
                sql = "UPDATE reseller_discounts SET discount_percentage = ? WHERE reseller_user_id = ? AND product_type = ?"
                params_sql = (float(percentage), target_user_id, product_type)
                action_desc = "RESELLER_DISCOUNT_EDIT"

            result = c.execute(sql, params_sql)

            # Ensure the edit actually changed a row if editing
            if mode == 'edit' and result.rowcount == 0:
                 logger.warning(f"Attempted to edit non-existent discount rule for user {target_user_id}, type {product_type}")
                 conn.rollback() # Rollback the failed update attempt
                 await send_message_with_retry(context.bot, chat_id, "❌ Error: Rule not found to edit.")
            else:
                conn.commit() # Commit successful insert or update

                # Log action
                log_admin_action(
                    admin_id=admin_id,
                    action=action_desc,
                    target_user_id=target_user_id,
                    reason=f"Type: {product_type}",
                    old_value=old_value, # Will be None for adds
                    new_value=float(percentage)
                )

                action_verb = "added" if mode == 'add' else "updated"
                await send_message_with_retry(context.bot, chat_id, f"✅ Discount rule {action_verb} for {product_type}: {percentage:.1f}%",
                                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back_callback)]]))

            # Clear state ONLY after successful operation or known error handling
            context.user_data.pop('state', None); context.user_data.pop('reseller_mgmt_target_id', None)
            context.user_data.pop('reseller_mgmt_product_type', None); context.user_data.pop('reseller_mgmt_mode', None)


        except sqlite3.IntegrityError: # Handles adding duplicate PK
            await send_message_with_retry(context.bot, chat_id, f"❌ Error: A discount rule for {product_type} already exists for this user. Use 'Edit' instead.")
            context.user_data.pop('state', None) # Clear state even on error
        except sqlite3.Error as e:
            logger.error(f"DB error {mode} reseller discount: {e}", exc_info=True)
            if conn and conn.in_transaction: conn.rollback()
            await send_message_with_retry(context.bot, chat_id, "❌ DB Error saving discount rule.")
            context.user_data.pop('state', None) # Clear state even on error
        finally:
            if conn: conn.close()

    except ValueError:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid percentage. Enter a number between 0 and 100 (e.g., 10 or 15.5).")
        # Keep state awaiting valid input


async def handle_reseller_delete_discount_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Discount' button press, shows confirmation."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    context.user_data["confirm_action"] = f"confirm_delete_reseller_discount|{target_reseller_id}|{product_type}"
    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Delete the discount rule for {emoji} {product_type} for user ID {target_reseller_id}?\n\n"
           f"🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Rule", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- END OF FILE reseller_management.py ---