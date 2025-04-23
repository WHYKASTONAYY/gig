# --- START OF FILE payment.py ---

import logging
import sqlite3
import time
import os
import shutil
import asyncio
import requests # Added
from decimal import Decimal, ROUND_UP, InvalidOperation # Use Decimal for precision
from datetime import datetime, timedelta
# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram import helpers
import telegram.error as telegram_error
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation
# -------------------------

# Import necessary items from utils and user
from utils import (
    NOWPAYMENTS_API_KEY, WEBHOOK_URL, # NOWPayments config
    send_message_with_retry, format_currency, ADMIN_ID,
    LANGUAGES, load_all_data, BASKET_TIMEOUT,
    get_db_connection, MEDIA_DIR, clear_expired_basket,
    add_pending_deposit, # NEW DB function
    get_currency_to_eur_price, # Price fetching utility
    format_expiration_time, # Time formatting utility
    # REMOVED: Withdrawal functions (get_jwt_token, initiate_payout, is_valid_ltc_address)
)
# Import user module (still needed for basket/discount validation)
import user
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

# --- Constants ---
MIN_DEPOSIT_EUR = Decimal('5.0') # Example: 5 EUR minimum deposit

# --- Helper function (No longer needed for CryptoBot, but keep structure) ---
async def close_cryptopay_client():
    logger.info("close_cryptopay_client called (no client in NOWPayments version).")
    pass

# --- Create NOWPayments Deposit ---
async def create_nowpayments_payment(user_id: int, target_eur_amount: Decimal, pay_currency_code: str) -> dict | None:
    """
    Creates a deposit payment request via NOWPayments API.
    Returns: Dictionary with payment data or None on failure.
    """
    pay_currency_code = pay_currency_code.lower()
    logger.info(f"Attempting to create NOWPayments deposit for user {user_id}, currency {pay_currency_code}, target EUR {target_eur_amount}")

    if not NOWPAYMENTS_API_KEY:
        logger.error("NOWPayments API Key is not configured.")
        return {"status": "error", "message": "Payment gateway not configured."}
    if not WEBHOOK_URL:
        logger.error("Webhook URL is not configured.")
        # Allow creation but webhook won't work
        # return {"status": "error", "message": "Webhook URL not configured."}

    # 1. Fetch Crypto Price in EUR
    price_eur = get_currency_to_eur_price(pay_currency_code) # Uses function from utils
    if price_eur is None or price_eur <= 0:
        logger.error(f"Failed to get EUR price for {pay_currency_code}")
        error_msg = LANGUAGES['en'].get('deposit_fetch_price_error', "Failed to fetch price for {currency}.").format(currency=pay_currency_code.upper())
        return {"status": "error", "message": error_msg}

    # 2. Calculate Crypto Amount (ensure minimum is met)
    required_eur = max(target_eur_amount, MIN_DEPOSIT_EUR) # Ensure minimum is met
    # Calculate crypto amount needed for the required EUR value
    crypto_amount_precise = (required_eur / price_eur).quantize(Decimal('0.00000001'), rounding=ROUND_UP) # High precision

    # 3. Create NOWPayments Payment
    url = "https://api.nowpayments.io/v1/payment"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    order_id = f"DEPOSIT_{user_id}_{int(time.time())}" # Unique order ID
    ipn_url = f"{WEBHOOK_URL}/webhook" # Ensure this matches your Flask route

    payload = {
        # For deposit, calculate required crypto amount and set that as price_amount in crypto
        "price_amount": float(crypto_amount_precise),
        "price_currency": pay_currency_code,
        "pay_currency": pay_currency_code,
        "ipn_callback_url": ipn_url,
        "order_id": order_id,
        "order_description": f"Balance top-up for user {user_id} (~{target_eur_amount:.2f} EUR)", # Optional
        # "fixed_rate": True, # Consider if needed
    }

    try:
        logger.info(f"Sending NOWPayments create_payment request: {payload}")
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.info(f"NOWPayments create_payment response: {data}")

        # Validate essential fields in response
        if not all(k in data for k in ['payment_id', 'pay_address', 'pay_amount', 'pay_currency', 'created_at']):
             logger.error(f"Invalid response structure from NOWPayments: {data}")
             return {"status": "error", "message": "Invalid response from payment gateway."}

        # Store pending deposit info in DB (Synchronous DB call from async context needs thread)
        # Run synchronous DB function in thread
        await asyncio.to_thread(
            add_pending_deposit,
            data['payment_id'],
            user_id,
            data['pay_currency'], # Use currency returned by NOWPayments
            required_eur # Store the EUR amount used for calculation
        )

        # Add required_eur to the returned data for display purposes
        data['required_eur_amount'] = float(required_eur)
        data['status'] = 'success'
        return data

    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating NOWPayments payment for user {user_id}.")
        return {"status": "error", "message": "Payment gateway request timed out."}
    except requests.exceptions.RequestException as e:
        logger.error(f"NOWPayments API request failed: {e}")
        error_code = e.response.status_code if e.response is not None else 'N/A'
        error_msg_template = LANGUAGES['en'].get('deposit_api_error', "API error ({error_code}). Contact support.")
        return {"status": "error", "message": error_msg_template.format(error_code=error_code)}
    except Exception as e:
        logger.error(f"Unexpected error creating NOWPayments payment: {e}", exc_info=True)
        return {"status": "error", "message": "An unexpected error occurred."}


# --- Callback Handler for Crypto Selection during Refill ---
async def handle_select_refill_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting crypto for refill and triggers NOWPayments."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_refill_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    # NOWPayments uses lowercase tickers, ensure consistency
    selected_currency = params[0].lower()
    logger.info(f"User {user_id} selected {selected_currency} for refill.")

    refill_eur_amount = context.user_data.get('refill_eur_amount')
    if not refill_eur_amount or refill_eur_amount <= 0:
        logger.error(f"Refill amount context lost before asset selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Refill amount context lost. Please start the top up again.", parse_mode=None)
        context.user_data.pop('state', None) # Reset state
        return

    refill_eur_amount_decimal = Decimal(str(refill_eur_amount))

    # Get translated texts
    generating_payment_msg = lang_data.get("generating_payment", "⏳ Generating payment details...")
    payment_generation_failed_template = lang_data.get("payment_generation_failed", "❌ Failed to generate payment details. Please try again or contact support. Reason: {reason}")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")

    try:
        await query.edit_message_text(generating_payment_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Couldn't edit message: {e}")
        await query.answer("Generating...")

    try:
        # Call the function to create the NOWPayments deposit
        payment_result = await create_nowpayments_deposit(user_id, refill_eur_amount_decimal, selected_currency)

        if payment_result is None or payment_result.get("status") == "error":
            reason = payment_result.get("message", "Unknown error") if payment_result else "API connection failed"
            payment_failed_msg = payment_generation_failed_template.format(reason=reason)
            logger.error(f"NOWPayments payment generation failed for user {user_id}, currency {selected_currency}: {reason}")
            kb = [[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]]
            await query.edit_message_text(payment_failed_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
            context.user_data.pop('state', None)
            context.user_data.pop('refill_eur_amount', None)
            return

        # Clear state AFTER successfully getting the payment details
        context.user_data.pop('state', None)
        context.user_data.pop('refill_eur_amount', None)

        # Call the function to display the instructions
        await display_nowpayments_invoice(update, context, payment_result)

    except Exception as e:
        logger.error(f"Error during refill crypto selection (NOWPayments) for user {user_id}: {e}", exc_info=True)
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None)
        error_msg = lang_data.get("error_preparing_payment", "❌ An error occurred while preparing the payment request.")
        kb = [[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]]
        await query.edit_message_text(error_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)


# --- Display NOWPayments Invoice ---
async def display_nowpayments_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_data: dict):
    """Displays the unique NOWPayments invoice details."""
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    # Extract data safely
    pay_address = payment_data.get('pay_address')
    pay_amount_str = payment_data.get('pay_amount') # Amount in crypto as string
    pay_currency = payment_data.get('pay_currency', '').upper()
    expires_iso = payment_data.get('expiration_estimate_date') # ISO UTC string
    payment_id = payment_data.get('payment_id')
    target_eur_display = format_currency(payment_data.get('required_eur_amount', 0.0))
    network_info = payment_data.get('network', pay_currency) # Check if NOWPayments provides network

    if not all([pay_address, pay_amount_str, pay_currency, payment_id]):
        logger.error(f"Missing data for displaying NOWPayments invoice: {payment_data}")
        await query.answer("Error displaying payment details.", show_alert=True)
        return

    expires_in_display = format_expiration_time(expires_iso) # Use helper

    # Get translated texts
    invoice_title = lang_data.get("nowpayments_invoice_title", "Deposit Invoice")
    pay_amount_label = lang_data.get("nowpayments_pay_amount_label", "Amount to pay")
    send_to_label = lang_data.get("nowpayments_send_to_label", "Send the exact amount to this address:")
    address_label_template = lang_data.get("nowpayments_address_label", "{currency} Address")
    copy_hint = lang_data.get("nowpayments_copy_hint", "(Click to copy)")
    expires_label = lang_data.get("nowpayments_expires_label", "Expires in")
    network_warning_template = lang_data.get("nowpayments_network_warning", "⚠️ Ensure you send {currency} using the correct network.")
    # fee_note = lang_data.get("nowpayments_fee_note", "Note: A small fee adjustment may apply.") # Consider if needed
    status_note = lang_data.get("status_note", "Payment status will be updated automatically...")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    target_eur_label = lang_data.get("target_top_up_amount", "Target top-up amount")

    address_label = address_label_template.format(currency=pay_currency)
    network_warning = network_warning_template.format(currency=pay_currency)

    msg_parts = [
        f"💰 {invoice_title} (ID: {payment_id})\n",
        f"{target_eur_label}: {target_eur_display} EUR\n",
        f"{pay_amount_label}: `{pay_amount_str}` {pay_currency}\n", # Use backticks for crypto amount
        f"{send_to_label}\n",
        f"{address_label} {copy_hint}\n`{pay_address}`\n", # Use backticks for address
        f"⏳ {expires_label}: {expires_in_display}\n",
        f"{network_warning}\n",
        # f"{fee_note}\n", # Optional fee note
        f"{status_note}\n"
    ]
    msg = "\n".join(msg_parts)

    keyboard = [
        [InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]
    ]

    try:
        await query.edit_message_text(
            msg, reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None, # Send as plain text to ensure backticks work
            disable_web_page_preview=True
        )
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing NOWPayments invoice message: {e}")
             # Fallback send if edit fails
             await send_message_with_retry(
                 context.bot, chat_id, msg,
                 reply_markup=InlineKeyboardMarkup(keyboard),
                 parse_mode=None, disable_web_page_preview=True
             )
        else: await query.answer()


# --- Payment Confirmation Check (REMOVED/Placeholder) ---
async def handle_check_cryptobot_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Placeholder - Automatic payment checking relies on webhook in this version."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    webhook_check_note = lang_data.get("status_note", "Payment status will be updated automatically once confirmed on the blockchain (may take time).")
    manual_check_note = lang_data.get("manual_check_note", "If your deposit isn't confirmed automatically after some time, please contact support with your transaction details.")
    await query.answer(f"{webhook_check_note}\n{manual_check_note}", show_alert=True)
    logger.info(f"User {query.from_user.id} clicked check payment button (webhook based).")


# --- process_successful_refill (Triggered by Webhook via main.py) ---
async def process_successful_refill(user_id: int, credited_eur_amount: Decimal, payment_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates after a deposit is confirmed (usually via webhook)."""
    # No direct chat_id available here, bot instance is used if needed
    lang = context.user_data.get("lang", "en") # Get language for potential message later
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not isinstance(credited_eur_amount, Decimal) or credited_eur_amount <= 0:
        logger.error(f"Invalid credited_eur_amount in process_successful_refill: {credited_eur_amount}")
        return False # Should not happen if called from webhook correctly

    conn = None
    db_update_successful = False
    amount_float = float(credited_eur_amount) # Convert Decimal for SQLite storage
    new_balance = Decimal('0.0') # Initialize as Decimal

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        logger.info(f"WEBHOOK TRIGGERED: Attempting balance update for user {user_id} by {credited_eur_amount:.2f} EUR (NOWPayments ID: {payment_id})")

        # Get current balance as Decimal
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        current_balance_res = c.fetchone()
        current_balance_dec = Decimal(str(current_balance_res['balance'])) if current_balance_res else Decimal('0.0')

        # Calculate new balance using Decimal
        new_balance_dec = current_balance_dec + credited_eur_amount

        # Update database with the new balance (as float)
        update_result = c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (float(new_balance_dec), user_id))
        if update_result.rowcount == 0:
            logger.error(f"User {user_id} not found during webhook refill DB update (Payment ID: {payment_id}). Rowcount: {update_result.rowcount}")
            conn.rollback()
            # Optionally notify admin here if user disappears after payment starts
            return False

        conn.commit()
        db_update_successful = True
        logger.info(f"Successfully processed webhook refill DB update for user {user_id}. Added: {credited_eur_amount:.2f} EUR. New Balance: {new_balance_dec:.2f} EUR.")

        # --- Notify User ---
        # Notification is now handled in the webhook handler in main.py,
        # as it has access to the bot instance from the application context.
        # --- End Notify User ---

        return True # DB update successful
    except sqlite3.Error as e:
        logger.error(f"DB error during webhook process_successful_refill user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False
    except Exception as e:
         logger.error(f"Unexpected error during webhook process_successful_refill user {user_id}: {e}", exc_info=True)
         if conn and conn.in_transaction: conn.rollback()
         return False
    finally:
        if conn: conn.close()


# --- process_successful_cryptobot_purchase (Keep as is - Unused) ---
async def process_successful_cryptobot_purchase(user_id, payment_details, context: ContextTypes.DEFAULT_TYPE):
    logger.warning(f"process_successful_cryptobot_purchase called unexpectedly for user {user_id}")
    return False

# --- process_purchase_with_balance (Keep as is) ---
async def process_purchase_with_balance(user_id: int, amount_to_deduct: Decimal, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates when paying with internal balance."""
    # ... (Implementation remains exactly the same as it's independent of deposit method) ...
    chat_id = context._chat_id or context._user_id or user_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} balance purchase."); return False
    if not isinstance(amount_to_deduct, Decimal) or amount_to_deduct < 0: logger.error(f"Invalid amount_to_deduct {amount_to_deduct}."); return False

    conn = None; sold_out = []; final_details = defaultdict(list); db_ok = False; processed_ids = []; purchases = []
    amount_float = float(amount_to_deduct)
    balance_err = lang_data.get("balance_changed_error", "❌ Transaction failed: Balance changed.")
    sold_out_err = lang_data.get("order_failed_all_sold_out_balance", "❌ Order Failed: All items sold out.")
    process_err = lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support.")

    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("BEGIN EXCLUSIVE")
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        bal_res = c.fetchone()
        # Use Decimal for comparison
        if not bal_res or Decimal(str(bal_res['balance'])) < amount_to_deduct:
             conn.rollback(); await send_message_with_retry(context.bot, chat_id, balance_err, parse_mode=None); return False
        if c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount_float, user_id)).rowcount == 0:
            conn.rollback(); return False # Should not happen if select worked
        prod_ids = list(set(item['product_id'] for item in basket_snapshot))
        if not prod_ids: conn.rollback(); return False
        placeholders = ','.join('?' * len(prod_ids))
        c.execute(f"SELECT id, name, product_type, size, price, city, district, available, reserved, original_text FROM products WHERE id IN ({placeholders})", prod_ids)
        prod_db = {row['id']: dict(row) for row in c.fetchall()}
        now_iso = datetime.now().isoformat()
        for item in basket_snapshot:
            pid = item['product_id']; details = prod_db.get(pid)
            if not details: sold_out.append(f"ID {pid}"); continue
            if c.execute("UPDATE products SET reserved = MAX(0, reserved - 1) WHERE id = ?", (pid,)).rowcount == 0:
                 sold_out.append(f"{details.get('name','?')} {details.get('size','?')}"); continue
            if c.execute("UPDATE products SET available = available - 1 WHERE id = ? AND available > 0", (pid,)).rowcount == 0:
                 sold_out.append(f"{details.get('name','?')} {details.get('size','?')}"); c.execute("UPDATE products SET reserved = reserved + 1 WHERE id = ?", (pid,)); continue
            purchases.append((user_id, pid, details['name'], details['product_type'], details['size'], float(details['price']), details['city'], details['district'], now_iso))
            processed_ids.append(pid)
            final_details[pid].append({'name': details['name'], 'size': details['size'], 'text': details.get('original_text')})
        if not purchases:
            conn.rollback(); await send_message_with_retry(context.bot, chat_id, sold_out_err, parse_mode=None); return False
        c.executemany("INSERT INTO purchases (user_id, product_id, product_name, product_type, product_size, price_paid, city, district, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", purchases)
        c.execute("UPDATE users SET total_purchases = total_purchases + ? WHERE user_id = ?", (len(purchases), user_id))
        if discount_code_used: c.execute("UPDATE discount_codes SET uses_count = uses_count + 1 WHERE code = ?", (discount_code_used,))
        c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,)); conn.commit(); db_ok = True
        logger.info(f"Processed balance purchase user {user_id}. Deducted: {amount_to_deduct} EUR.")
    except sqlite3.Error as e: logger.error(f"DB error balance purchase {user_id}: {e}"); conn.rollback() if conn else None; db_ok = False
    except Exception as e: logger.error(f"Unexpected error balance purchase {user_id}: {e}"); conn.rollback() if conn else None; db_ok = False
    finally: conn.close() if conn else None

    if db_ok: # ... (Keep the post-transaction logic exactly as before) ...
        media_info = defaultdict(list)
        if processed_ids:
            conn_media = None
            try:
                conn_media = get_db_connection(); c_media = conn_media.cursor()
                placeholders = ','.join('?' * len(processed_ids))
                c_media.execute(f"SELECT product_id, media_type, telegram_file_id, file_path FROM product_media WHERE product_id IN ({placeholders})", processed_ids)
                for row in c_media.fetchall(): media_info[row['product_id']].append(dict(row))
            except sqlite3.Error as e: logger.error(f"DB error fetching media: {e}")
            finally: conn_media.close() if conn_media else None
        success_title = lang_data.get("purchase_success", "🎉 Purchase Complete!")
        await send_message_with_retry(context.bot, chat_id, success_title, parse_mode=None)
        for pid in processed_ids:
            item_info = final_details.get(pid)
            if not item_info: continue
            item_name, item_size = item_info[0]['name'], item_info[0]['size']
            item_text = item_info[0]['text'] or "(No details)"
            item_header = f"--- Item: {item_name} {item_size} ---"
            if pid in media_info:
                m_list = media_info[pid]
                if m_list:
                    m_item = m_list[0]; file_id, m_type, f_path = m_item.get('telegram_file_id'), m_item.get('media_type'), m_item.get('file_path')
                    caption = item_header
                    try:
                        if file_id and m_type == 'photo': await context.bot.send_photo(chat_id, photo=file_id, caption=caption, parse_mode=None)
                        elif file_id and m_type == 'video': await context.bot.send_video(chat_id, video=file_id, caption=caption, parse_mode=None)
                        elif file_id and m_type == 'gif': await context.bot.send_animation(chat_id, animation=file_id, caption=caption, parse_mode=None)
                        elif f_path and await asyncio.to_thread(os.path.exists, f_path):
                             async with await asyncio.to_thread(open, f_path, 'rb') as f:
                                 if m_type == 'photo': await context.bot.send_photo(chat_id, photo=f, caption=caption, parse_mode=None)
                                 elif m_type == 'video': await context.bot.send_video(chat_id, video=f, caption=caption, parse_mode=None)
                                 elif m_type == 'gif': await context.bot.send_animation(chat_id, animation=f, caption=caption, parse_mode=None)
                        else: logger.warning(f"Media path invalid P{pid}: {f_path}")
                    except Exception as e: logger.error(f"Error sending media P{pid} user {user_id}: {e}")
            await send_message_with_retry(context.bot, chat_id, item_text, parse_mode=None)
            conn_del = None
            try:
                conn_del = get_db_connection(); c_del = conn_del.cursor()
                c_del.execute("DELETE FROM product_media WHERE product_id = ?", (pid,))
                if c_del.execute("DELETE FROM products WHERE id = ?", (pid,)).rowcount > 0:
                    logger.info(f"Deleted purchased product record ID {pid}.")
                    media_dir_del = os.path.join(MEDIA_DIR, str(pid))
                    if os.path.exists(media_dir_del): asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_del, ignore_errors=True)); logger.info(f"Scheduled deletion: {media_dir_del}")
                conn_del.commit()
            except sqlite3.Error as e: logger.error(f"DB error deleting product {pid}: {e}"); conn_del.rollback() if conn_del else None
            except Exception as e: logger.error(f"Error deleting product {pid}: {e}")
            finally: conn_del.close() if conn_del else None
        final_msg_parts = ["Purchase details sent above."]
        if sold_out: final_msg_parts.append(lang_data.get("sold_out_note", "⚠️ Note: Items unavailable: {items}.").format(items=", ".join(sold_out)))
        kb = [[InlineKeyboardButton(f"✍️ {lang_data.get('leave_review_button', 'Leave Review')}", callback_data="leave_review_now")]]
        await send_message_with_retry(context.bot, chat_id, "\n\n".join(final_msg_parts), reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
        context.user_data['basket'] = []; context.user_data.pop('applied_discount', None)
        return True
    else:
        if not sold_out: await send_message_with_retry(context.bot, chat_id, process_err, parse_mode=None)
        return False

# --- handle_confirm_pay (Keep as is - decides payment method) ---
async def handle_confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the 'Pay Now' button press from the basket."""
    # ... (Implementation remains exactly the same as it decides between balance/refill) ...
    query = update.callback_query; user_id = query.from_user.id; chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en"); lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    clear_expired_basket(context, user_id); basket = context.user_data.get("basket", [])
    applied_discount = context.user_data.get('applied_discount'); conn = None
    if not basket: await query.answer("Basket empty!", show_alert=True); return await user.handle_view_basket(update, context)
    original_total, final_total = Decimal('0.0'), Decimal('0.0'); snapshot = []; discount_code = None
    try:
        pids = list(set(item['product_id'] for item in basket))
        if not pids: await query.answer("Basket empty after validation.", show_alert=True); return await user.handle_view_basket(update, context)
        conn = get_db_connection(); c = conn.cursor(); placeholders = ','.join('?' * len(pids))
        c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", pids)
        prices = {r['id']: Decimal(str(r['price'])) for r in c.fetchall()}
        for item in basket:
             pid = item['product_id']
             if pid in prices:
                 original_total += prices[pid]
                 item_snap = item.copy(); item_snap['price_at_checkout'] = prices[pid]; snapshot.append(item_snap)
             else: logger.warning(f"P{pid} missing during payment confirm {user_id}.")
        if not snapshot:
             context.user_data['basket'] = []; context.user_data.pop('applied_discount', None); logger.warning(f"All items unavailable {user_id}.")
             kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]; await query.edit_message_text("❌ All items unavailable.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return
        final_total = original_total
        if applied_discount:
            valid, _, details = user.validate_discount_code(applied_discount['code'], float(original_total)) # validate needs float
            if valid and details:
                final_total = Decimal(str(details['final_total'])); discount_code = applied_discount.get('code')
                context.user_data['applied_discount']['final_total'] = float(final_total); context.user_data['applied_discount']['amount'] = details['discount_amount']
            else: final_total = original_total; discount_code = None; context.user_data.pop('applied_discount', None); await query.answer("Discount invalid.", show_alert=True)
        if final_total < Decimal('0.00'): await query.answer("Negative amount.", show_alert=True); return
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        bal_res = c.fetchone(); user_balance = Decimal(str(bal_res['balance'])) if bal_res else Decimal('0.0')
    except sqlite3.Error as e:
         logger.error(f"DB error payment confirm {user_id}: {e}"); kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]; await query.edit_message_text("❌ Error calculating.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return
    except Exception as e:
         logger.error(f"Unexpected error prep payment {user_id}: {e}"); kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]; await query.edit_message_text("❌ Unexpected error.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return
    finally: conn.close() if conn else None

    logger.info(f"Payment confirm {user_id}. Total: {final_total:.2f}, Balance: {user_balance:.2f}")
    if user_balance >= final_total:
        logger.info(f"Sufficient balance {user_id}. Processing."); await query.edit_message_text("⏳ Processing payment...", reply_markup=None, parse_mode=None)
        success = await process_purchase_with_balance(user_id, final_total, snapshot, discount_code, context)
        if success: await query.edit_message_text("✅ Purchase successful! Details sent.", reply_markup=None, parse_mode=None)
        else: await user.handle_view_basket(update, context) # Refresh basket on failure
    else:
        logger.info(f"Insufficient balance {user_id}.")
        needed_str, balance_str = format_currency(final_total), format_currency(user_balance)
        insufficient_msg = lang_data.get("insufficient_balance", "⚠️ Insufficient Balance! Top up needed.")
        top_up_btn = lang_data.get("top_up_button", "Top Up Balance"); back_btn = lang_data.get("back_basket_button", "Back to Basket")
        full_msg = (f"{insufficient_msg}\n\nRequired: {needed_str} EUR\nBalance: {balance_str} EUR")
        kb = [[InlineKeyboardButton(f"💸 {top_up_btn}", callback_data="refill")], [InlineKeyboardButton(f"⬅️ {back_btn}", callback_data="view_basket")]]
        await query.edit_message_text(full_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)

# --- END OF FILE payment.py ---