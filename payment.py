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

# Import necessary items from utils
import utils # Import the whole module to access its functions
from utils import (
    NOWPAYMENTS_API_KEY, WEBHOOK_URL, FEE_ADJUSTMENT, # NOWPayments config
    send_message_with_retry, format_currency, ADMIN_ID,
    LANGUAGES, load_all_data, BASKET_TIMEOUT,
    get_db_connection, MEDIA_DIR, clear_expired_basket,
    add_pending_deposit, # NEW DB function
    get_currency_to_eur_price, # Price fetching utility
    format_expiration_time, # Time formatting utility
    validate_discount_code # Import the moved function
    # REMOVED: Withdrawal functions
)
# REMOVED: import user (No longer needed)
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

# --- Constants ---
MIN_DEPOSIT_EUR = Decimal('5.0') # Example: 5 EUR minimum deposit

# --- Helper function (Placeholder) ---
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
        logger.error("Webhook URL is not configured for NOWPayments.")
        # Allow creation but webhook won't work - WARN heavily
        logger.warning("NOWPayments deposit created, but WEBHOOK_URL is missing. Confirmation will NOT be automatic.")
        # return {"status": "error", "message": "Webhook URL not configured."} # Option: block creation

    # 1. Fetch Crypto Price in EUR
    price_eur = get_currency_to_eur_price(pay_currency_code) # Uses function from utils
    if price_eur is None or price_eur <= 0:
        logger.error(f"Failed to get EUR price for {pay_currency_code}")
        # Use default language 'en' for error messages in backend logic
        error_msg = LANGUAGES['en'].get('deposit_fetch_price_error', "Failed to fetch price for {currency}.").format(currency=pay_currency_code.upper())
        return {"status": "error", "message": error_msg}

    # 2. Calculate Crypto Amount (ensure minimum is met)
    required_eur = max(target_eur_amount, MIN_DEPOSIT_EUR) # Ensure minimum is met
    # Calculate crypto amount needed for the required EUR value
    # Increase precision for calculation before rounding at the end
    crypto_amount_precise = (required_eur / price_eur).quantize(Decimal('0.0000000001'), rounding=ROUND_UP) # Higher precision for crypto

    # 3. Create NOWPayments Payment
    url = "https://api.nowpayments.io/v1/payment"
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    order_id = f"DEPOSIT_{user_id}_{int(time.time())}" # Unique order ID
    ipn_url = f"{WEBHOOK_URL}/webhook" if WEBHOOK_URL else None # Use webhook URL if configured

    payload = {
        # NOWPayments needs the price in the *destination* currency (crypto)
        "price_amount": float(crypto_amount_precise),
        "price_currency": pay_currency_code, # The currency of price_amount
        "pay_currency": pay_currency_code,   # The currency the user will pay in
        "order_id": order_id,
        "order_description": f"Balance top-up for user {user_id} (~{required_eur:.2f} EUR)", # Optional
        # "is_fixed_rate": True, # Consider using fixed rate for ~15 mins
        # "is_fee_paid_by_user": False # Let NOWPayments handle their fee structure unless specified
    }
    # Only add IPN callback URL if it's configured
    if ipn_url:
        payload["ipn_callback_url"] = ipn_url
    else:
        logger.warning(f"No WEBHOOK_URL configured, ipn_callback_url not set for payment {order_id}. Manual check needed.")


    try:
        logger.info(f"Sending NOWPayments create_payment request: {json.dumps(payload, indent=2)}")
        response = requests.post(url, json=payload, headers=headers, timeout=20) # Increased timeout
        response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        logger.info(f"NOWPayments create_payment response: {json.dumps(data, indent=2)}")

        # Validate essential fields in response more robustly
        required_keys = ['payment_id', 'pay_address', 'pay_amount', 'pay_currency', 'created_at', 'expiration_estimate_date']
        if not all(k in data for k in required_keys):
             logger.error(f"Invalid or incomplete response structure from NOWPayments: {data}")
             return {"status": "error", "message": "Invalid response from payment gateway."}

        # Add pending deposit info to DB (Synchronous DB call from async context needs thread)
        # Ensure add_pending_deposit uses get_db_connection
        await asyncio.to_thread(
            add_pending_deposit,
            data['payment_id'],
            user_id,
            data['pay_currency'], # Use currency returned by NOWPayments
            required_eur # Store the EUR amount used for calculation
        )

        # Add required_eur back to the returned data for display purposes
        data['required_eur_amount'] = float(required_eur)
        data['status'] = 'success'
        return data

    except requests.exceptions.Timeout:
        logger.error(f"Timeout creating NOWPayments payment for user {user_id}.")
        # Use default language 'en' for error messages
        timeout_msg = LANGUAGES['en'].get('payment_timeout_error', "Payment gateway request timed out.")
        return {"status": "error", "message": timeout_msg}
    except requests.exceptions.HTTPError as http_err:
        error_code = http_err.response.status_code
        error_body = http_err.response.text
        logger.error(f"NOWPayments API HTTP error: {error_code} - {error_body}")
        # Try to parse error from NOWPayments if possible
        try:
            err_data = http_err.response.json()
            api_message = err_data.get('message', error_body)
        except json.JSONDecodeError:
            api_message = error_body if len(error_body)<100 else f"HTTP Status {error_code}"

        error_msg_template = LANGUAGES['en'].get('deposit_api_error', "API error ({error_code}). Contact support.")
        return {"status": "error", "message": f"API Error: {api_message[:100]}"} # Keep API message concise
    except requests.exceptions.RequestException as e:
        logger.error(f"NOWPayments API connection failed: {e}")
        conn_err_msg = LANGUAGES['en'].get('payment_connection_error', "Could not connect to payment gateway.")
        return {"status": "error", "message": conn_err_msg}
    except Exception as e:
        logger.error(f"Unexpected error creating NOWPayments payment: {e}", exc_info=True)
        unexp_err_msg = LANGUAGES['en'].get('error_unexpected', "An unexpected error occurred.")
        return {"status": "error", "message": unexp_err_msg}

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
        # Use default language 'en' for error messages
        error_msg = LANGUAGES['en'].get('error_unexpected', "An unexpected error occurred.")
        await query.edit_message_text(f"❌ Error: Refill amount context lost ({error_msg}). Please start the top up again.", parse_mode=None)
        context.user_data.pop('state', None) # Reset state
        return

    refill_eur_amount_decimal = Decimal(str(refill_eur_amount))

    # Get translated texts
    generating_payment_msg = lang_data.get("generating_payment", "⏳ Generating payment details...")
    payment_generation_failed_template = lang_data.get("payment_generation_failed", "❌ Failed to generate payment details. Please try again or contact support. Reason: {reason}")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")

    try:
        # Acknowledge quickly, edit message
        await query.edit_message_text(generating_payment_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        # Ignore "message is not modified" error, log others
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Couldn't edit message in handle_select_refill_crypto: {e}")
        # Still answer the callback to remove the loading indicator on the button
        await query.answer("Generating...")

    try:
        # Call the function to create the NOWPayments deposit
        payment_result = await create_nowpayments_payment(user_id, refill_eur_amount_decimal, selected_currency)

        if payment_result is None or payment_result.get("status") == "error":
            reason = payment_result.get("message", "Unknown error") if payment_result else "API connection failed"
            payment_failed_msg = payment_generation_failed_template.format(reason=reason)
            logger.error(f"NOWPayments payment generation failed for user {user_id}, currency {selected_currency}: {reason}")
            kb = [[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]]
            # Attempt to edit the message again with the error
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
        # Attempt to edit the message with the unexpected error
        try:
            await query.edit_message_text(error_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
        except telegram_error.BadRequest as edit_err:
             if "message is not modified" not in str(edit_err).lower():
                  logger.error(f"Failed to edit message with unexpected error: {edit_err}")


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

    if not all([pay_address, pay_amount_str, pay_currency, payment_id, expires_iso]): # Added expires_iso check
        logger.error(f"Missing data for displaying NOWPayments invoice: {payment_data}")
        await query.answer("Error displaying payment details.", show_alert=True)
        return

    # Use the fixed format_expiration_time function
    expires_in_display = utils.format_expiration_time(expires_iso)

    # Get translated texts
    invoice_title = lang_data.get("nowpayments_invoice_title", "Deposit Invoice")
    pay_amount_label = lang_data.get("nowpayments_pay_amount_label", "Amount to pay")
    send_to_label = lang_data.get("nowpayments_send_to_label", "Send the exact amount to this address:")
    address_label_template = lang_data.get("nowpayments_address_label", "{currency} Address")
    copy_hint = lang_data.get("nowpayments_copy_hint", "(Click to copy)")
    expires_label = lang_data.get("nowpayments_expires_label", "Expires in")
    network_warning_template = lang_data.get("nowpayments_network_warning", "⚠️ Ensure you send {currency} using the correct network.")
    status_note = lang_data.get("status_note", "Payment status will be updated automatically...")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    target_eur_label = lang_data.get("target_top_up_amount", "Target top-up amount")
    minimum_deposit_warning_template = lang_data.get("minimum_deposit_warning", "1. ⚠️ Minimum deposit: {min_amount} {currency}. Amounts below this may be lost.") # Added minimum
    unique_address_note = lang_data.get("unique_address_note", "3. This address is unique to this payment attempt and expires.") # Added unique address note

    address_label = address_label_template.format(currency=pay_currency)
    network_warning = network_warning_template.format(currency=pay_currency)

    # --- Calculate Minimum Deposit (Example - adjust based on NOWPayments requirements if available) ---
    # This is a guess; NOWPayments might enforce minimums server-side.
    min_deposit_crypto = Decimal('0.0001') # Example: Set a reasonable small crypto amount as a guess
    # You might need to fetch actual minimums via API if they provide it.
    minimum_deposit_warning = minimum_deposit_warning_template.format(min_amount=min_deposit_crypto, currency=pay_currency)
    # --- End Minimum Deposit ---


    # Construct message using plain text and backticks for copyable parts
    msg_parts = [
        f"💰 {invoice_title} (ID: {payment_id})\n",
        f"{target_eur_label}: {target_eur_display} EUR\n",
        f"{pay_amount_label}: `{pay_amount_str}` {pay_currency}\n", # Amount in backticks
        f"{send_to_label}\n",
        f"{address_label} {copy_hint}:\n`{pay_address}`\n", # Address in backticks
        f"⏳ {expires_label}: {expires_in_display}\n",
        f"{network_warning}\n",
        "--- Important Notes ---",
        minimum_deposit_warning,
        f"2. Send *exactly* `{pay_amount_str}` {pay_currency}.", # Use backticks for amount
        unique_address_note,
        "---------------------\n",
        f"{status_note}\n"
    ]
    msg = "\n".join(msg_parts)

    keyboard = [
        [InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]
    ]

    try:
        await query.edit_message_text(
            msg, reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=None, # Use None to ensure backticks render correctly
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
        else: await query.answer() # Ignore if not modified

# --- Payment Confirmation Check (REMOVED/Placeholder) ---
async def handle_check_cryptobot_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Placeholder - Automatic payment checking relies on webhook in this version."""
    query = update.callback_query
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    webhook_check_note = lang_data.get("status_note", "Payment status will be updated automatically once confirmed on the blockchain (may take time).")
    manual_check_note = lang_data.get("manual_check_note", "If your deposit isn't confirmed automatically after some time, please contact support with your transaction details.")
    await query.answer(f"{webhook_check_note}\n{manual_check_note}", show_alert=True, cache_time=5) # Show alert longer
    logger.info(f"User {query.from_user.id} clicked check payment button (webhook based).")


# --- process_successful_refill (Triggered by Webhook via main.py) ---
async def process_successful_refill(user_id: int, credited_eur_amount: Decimal, payment_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates and notifies user after a deposit is confirmed (usually via webhook)."""
    lang = context.user_data.get("lang", "en") # Get user's language from context if available
    # Fallback to 'en' if context is missing or doesn't have lang
    if not lang or lang not in LANGUAGES: lang = 'en'
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not isinstance(credited_eur_amount, Decimal) or credited_eur_amount <= 0:
        logger.error(f"Invalid credited_eur_amount in process_successful_refill: {credited_eur_amount}")
        return False

    conn = None
    db_update_successful = False
    new_balance_dec = Decimal('0.0') # Initialize

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE") # Use exclusive to prevent race conditions on balance

        logger.info(f"WEBHOOK TRIGGERED: Updating balance user {user_id} by {credited_eur_amount:.2f} EUR (PaymentID: {payment_id})")

        # Fetch current balance robustly
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        current_balance_res = c.fetchone()

        if current_balance_res is None:
            # This case should ideally not happen if user exists, but handle defensively
            logger.error(f"User {user_id} not found during webhook DB update (PaymentID: {payment_id}). Cannot update balance.")
            conn.rollback()
            return False

        current_balance_dec = Decimal(str(current_balance_res['balance']))
        new_balance_dec = current_balance_dec + credited_eur_amount

        # Update balance
        update_result = c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (float(new_balance_dec), user_id))

        if update_result.rowcount == 0:
            # Should not happen if SELECT worked, but check anyway
            logger.error(f"Failed to update balance for user {user_id} (PaymentID: {payment_id}) - rowcount 0.")
            conn.rollback()
            return False

        conn.commit()
        db_update_successful = True
        logger.info(f"Success webhook DB update user {user_id}. Added: {credited_eur_amount:.2f} EUR. New Balance: {new_balance_dec:.2f} EUR.")

    except sqlite3.Error as e:
        logger.error(f"DB error during webhook process_refill user {user_id} (PaymentID: {payment_id}): {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False # Indicate failure
    except Exception as e:
        logger.error(f"Unexpected error during webhook process_refill user {user_id} (PaymentID: {payment_id}): {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False # Indicate failure
    finally:
        if conn: conn.close()

    # --- Send Notification AFTER DB update ---
    if db_update_successful:
        try:
            amount_str = format_currency(float(credited_eur_amount))
            new_balance_str = format_currency(float(new_balance_dec))
            success_msg_template = lang_data.get("deposit_confirmed_ipn", "✅ Deposit Confirmed! Amount credited: {amount_usd} EUR. New balance: {new_balance} EUR")
            success_msg = success_msg_template.format(amount_usd=amount_str, new_balance=new_balance_str)

            # Use the bot instance from context if available
            if hasattr(context, 'bot') and context.bot:
                await send_message_with_retry(context.bot, user_id, success_msg, parse_mode=None)
                logger.info(f"Sent deposit confirmation to user {user_id}.")
            else:
                logger.warning(f"Could not send deposit confirmation to user {user_id}: Bot instance missing from context.")

        except Exception as notify_err:
            logger.error(f"Failed to send deposit confirmation notification to user {user_id}: {notify_err}", exc_info=True)
            # Don't return False here, the DB update was successful

    return db_update_successful


# --- process_successful_cryptobot_purchase (Keep as is - Unused) ---
async def process_successful_cryptobot_purchase(user_id, payment_details, context: ContextTypes.DEFAULT_TYPE):
    logger.warning(f"process_successful_cryptobot_purchase called unexpectedly for user {user_id}")
    return False

# --- process_purchase_with_balance (Keep as is) ---
async def process_purchase_with_balance(user_id: int, amount_to_deduct: Decimal, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates when paying with internal balance."""
    chat_id = context._chat_id or context._user_id or user_id # Get chat_id if available
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not basket_snapshot:
        logger.error(f"Empty basket_snapshot provided for user {user_id} during balance purchase.")
        return False
    if not isinstance(amount_to_deduct, Decimal) or amount_to_deduct < Decimal('0.0'):
        logger.error(f"Invalid amount_to_deduct provided for balance purchase: {amount_to_deduct}")
        return False

    conn = None
    sold_out_items_info = [] # Store info about sold out items
    final_purchase_details = defaultdict(list) # {product_id: [details_dict, ...]}
    db_transaction_ok = False
    processed_product_ids = [] # Keep track of IDs successfully processed
    purchase_records_to_insert = [] # List of tuples for executemany

    # Get translated error messages
    balance_err_msg = lang_data.get("balance_changed_error", "❌ Transaction failed: Your balance changed or is insufficient.")
    sold_out_err_msg = lang_data.get("order_failed_all_sold_out_balance", "❌ Order Failed: All selected items became unavailable.")
    process_err_msg = lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Please contact support if your balance was deducted.")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE") # Lock for balance and inventory check/update

        # 1. Verify Balance Again (within transaction)
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance_result = c.fetchone()
        # Use Decimal for comparison
        if not balance_result or Decimal(str(balance_result['balance'])) < amount_to_deduct:
            conn.rollback()
            logger.warning(f"Balance check failed for user {user_id} inside transaction. Required: {amount_to_deduct}, Has: {balance_result['balance'] if balance_result else 'N/A'}")
            await send_message_with_retry(context.bot, chat_id, balance_err_msg, parse_mode=None)
            return False

        # 2. Deduct Balance
        # Use float for SQLite REAL column update
        deduction_result = c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (float(amount_to_deduct), user_id))
        if deduction_result.rowcount == 0:
            # Should not happen if SELECT worked, but safety check
            conn.rollback()
            logger.error(f"Failed to deduct balance for user {user_id} (rowcount 0).")
            await send_message_with_retry(context.bot, chat_id, process_err_msg, parse_mode=None)
            return False

        # 3. Process Inventory (Check availability and update)
        product_ids_in_basket = list(set(item['product_id'] for item in basket_snapshot))
        if not product_ids_in_basket:
            conn.rollback() # Rollback balance deduction if basket was empty
            logger.error(f"Basket snapshot was empty for user {user_id} after balance deduction.")
            return False

        # Fetch current product details WITHIN the transaction
        placeholders = ','.join('?' * len(product_ids_in_basket))
        # Use column names
        c.execute(f"""
            SELECT id, name, product_type, size, price, city, district, available, reserved, original_text
            FROM products WHERE id IN ({placeholders})
        """, product_ids_in_basket)
        product_db_details = {row['id']: dict(row) for row in c.fetchall()}

        now_iso = datetime.now().isoformat()

        for item in basket_snapshot:
            pid = item['product_id']
            details = product_db_details.get(pid)

            if not details:
                # Product record disappeared between basket view and now
                sold_out_items_info.append(f"ID {pid} (Not Found)")
                logger.warning(f"Product ID {pid} not found in DB during balance purchase for user {user_id}.")
                continue

            # a. Decrement 'reserved' count (was incremented on add to basket)
            reserve_update_result = c.execute("UPDATE products SET reserved = MAX(0, reserved - 1) WHERE id = ?", (pid,))
            if reserve_update_result.rowcount == 0:
                # This is odd, means the product was likely deleted? Treat as sold out.
                sold_out_items_info.append(f"{details.get('name','?')} {details.get('size','?')}")
                logger.warning(f"Failed to decrement reservation for product {pid} (user {user_id}).")
                continue # Skip to next item

            # b. Check 'available' and decrement if possible
            available_update_result = c.execute("UPDATE products SET available = available - 1 WHERE id = ? AND available > 0", (pid,))
            if available_update_result.rowcount == 0:
                # Item became unavailable between reservation release and availability check
                sold_out_items_info.append(f"{details.get('name','?')} {details.get('size','?')}")
                # IMPORTANT: Rollback the reservation decrement if availability failed
                c.execute("UPDATE products SET reserved = reserved + 1 WHERE id = ?", (pid,))
                logger.warning(f"Product {pid} became unavailable during balance purchase for user {user_id}. Reservation restored.")
                continue # Skip to next item

            # If both updates successful, record the purchase
            purchase_records_to_insert.append((
                user_id, pid, details['name'], details['product_type'], details['size'],
                float(details['price']), details['city'], details['district'], now_iso
            ))
            processed_product_ids.append(pid)
            # Store details needed for sending message/media later
            final_purchase_details[pid].append({
                'name': details['name'], 'size': details['size'], 'text': details.get('original_text')
            })

        # 4. Check if *any* items were successfully processed
        if not purchase_records_to_insert:
            conn.rollback() # Rollback balance deduction if nothing was bought
            logger.warning(f"All items were sold out during balance purchase for user {user_id}.")
            await send_message_with_retry(context.bot, chat_id, sold_out_err_msg, parse_mode=None)
            return False

        # 5. Insert Purchase Records
        c.executemany("""
            INSERT INTO purchases
            (user_id, product_id, product_name, product_type, product_size, price_paid, city, district, purchase_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, purchase_records_to_insert)

        # 6. Update User's Total Purchases
        c.execute("UPDATE users SET total_purchases = total_purchases + ? WHERE user_id = ?", (len(purchase_records_to_insert), user_id))

        # 7. Increment Discount Code Usage (if applicable)
        if discount_code_used:
            c.execute("UPDATE discount_codes SET uses_count = uses_count + 1 WHERE code = ?", (discount_code_used,))

        # 8. Clear User's Basket String in DB
        c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,))

        # --- If all DB operations successful ---
        conn.commit()
        db_transaction_ok = True
        logger.info(f"Successfully processed balance purchase for user {user_id}. Deducted: {amount_to_deduct} EUR. Items purchased: {len(purchase_records_to_insert)}.")

    except sqlite3.Error as e:
        logger.error(f"DB error during balance purchase for user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        db_transaction_ok = False
    except Exception as e:
        logger.error(f"Unexpected error during balance purchase processing for user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        db_transaction_ok = False
    finally:
        if conn: conn.close()

    # --- Post-Transaction Processing (Sending messages, deleting products) ---
    if db_transaction_ok:
        # Clear basket and discount from context data
        context.user_data['basket'] = []
        context.user_data.pop('applied_discount', None)

        # Fetch media info for purchased items
        media_info = defaultdict(list)
        if processed_product_ids:
            conn_media = None
            try:
                conn_media = get_db_connection()
                c_media = conn_media.cursor()
                placeholders = ','.join('?' * len(processed_product_ids))
                # Use column names
                c_media.execute(f"""
                    SELECT product_id, media_type, telegram_file_id, file_path
                    FROM product_media WHERE product_id IN ({placeholders})
                """, processed_product_ids)
                for row in c_media.fetchall():
                    media_info[row['product_id']].append(dict(row)) # Store as dicts
            except sqlite3.Error as e:
                logger.error(f"DB error fetching media info post-purchase: {e}", exc_info=True)
            finally:
                if conn_media: conn_media.close()

        # Send confirmation messages and purchased item details
        success_title = lang_data.get("purchase_success", "🎉 Purchase Complete!")
        await send_message_with_retry(context.bot, chat_id, success_title, parse_mode=None)

        # Loop through *successfully processed* products
        for pid in processed_product_ids:
            item_list = final_purchase_details.get(pid)
            if not item_list: continue # Should not happen, but safety check

            # Send details for each instance purchased (usually just one)
            for item_instance in item_list:
                item_name, item_size = item_instance['name'], item_instance['size']
                item_text = item_instance['text'] or "(No details provided)" # Plain text
                item_header = f"--- Item: {item_name} {item_size} ---" # Plain text

                media_items_for_pid = media_info.get(pid, [])
                media_sent = False
                if media_items_for_pid:
                    # Attempt to send the first media item found
                    m_item = media_items_for_pid[0]
                    file_id, m_type, f_path = m_item.get('telegram_file_id'), m_item.get('media_type'), m_item.get('file_path')
                    caption = item_header # Attach header as caption to media

                    try:
                        if file_id: # Prefer sending by file_id if available
                            if m_type == 'photo': await context.bot.send_photo(chat_id, photo=file_id, caption=caption, parse_mode=None)
                            elif m_type == 'video': await context.bot.send_video(chat_id, video=file_id, caption=caption, parse_mode=None)
                            elif m_type == 'gif': await context.bot.send_animation(chat_id, animation=file_id, caption=caption, parse_mode=None)
                            media_sent = True
                        elif f_path and await asyncio.to_thread(os.path.exists, f_path): # Fallback to path
                            # Use asyncio.to_thread for blocking file I/O
                             async with await asyncio.to_thread(open, f_path, 'rb') as file_content:
                                 if m_type == 'photo': await context.bot.send_photo(chat_id, photo=file_content, caption=caption, parse_mode=None)
                                 elif m_type == 'video': await context.bot.send_video(chat_id, video=file_content, caption=caption, parse_mode=None)
                                 elif m_type == 'gif': await context.bot.send_animation(chat_id, animation=file_content, caption=caption, parse_mode=None)
                                 media_sent = True
                        else:
                             logger.warning(f"Media path invalid or inaccessible for purchased product {pid}: {f_path}")
                    except Exception as e:
                        logger.error(f"Error sending media for purchased product {pid} to user {user_id}: {e}", exc_info=True)
                        media_sent = False # Ensure text is sent if media fails

                # Send text separately if no media was sent
                if not media_sent:
                    await send_message_with_retry(context.bot, chat_id, item_header + "\n" + item_text, parse_mode=None)
                else:
                    # Send text only (without header) if media+caption was sent
                    await send_message_with_retry(context.bot, chat_id, item_text, parse_mode=None)


            # --- Delete Product Record and Media AFTER sending details ---
            conn_del = None
            try:
                conn_del = get_db_connection()
                c_del = conn_del.cursor()
                c_del.execute("BEGIN")
                # Delete media records first (due to foreign key cascade ON DELETE CASCADE)
                c_del.execute("DELETE FROM product_media WHERE product_id = ?", (pid,))
                # Then delete the product record itself
                delete_product_result = c_del.execute("DELETE FROM products WHERE id = ?", (pid,))
                conn_del.commit()

                if delete_product_result.rowcount > 0:
                    logger.info(f"Deleted purchased product record ID {pid} from database.")
                    # Schedule media directory deletion (fire and forget)
                    media_dir_to_delete = os.path.join(MEDIA_DIR, str(pid))
                    if await asyncio.to_thread(os.path.exists, media_dir_to_delete):
                       asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_delete, ignore_errors=True))
                       logger.info(f"Scheduled deletion of media directory: {media_dir_to_delete}")
                else:
                    logger.warning(f"Attempted to delete product {pid} after purchase, but it was not found (maybe already deleted?).")

            except sqlite3.Error as e:
                logger.error(f"DB error deleting product {pid} post-purchase: {e}", exc_info=True)
                if conn_del and conn_del.in_transaction: conn_del.rollback()
            except Exception as e:
                logger.error(f"Unexpected error deleting product {pid} or its media post-purchase: {e}", exc_info=True)
                if conn_del and conn_del.in_transaction: conn_del.rollback()
            finally:
                if conn_del: conn_del.close()
        # --- End Deletion ---

        # --- Final Summary Message ---
        final_msg_parts = ["Purchase details sent above."]
        if sold_out_items_info:
             # Format the sold out items note (plain text)
             sold_out_note_template = lang_data.get("sold_out_note", "⚠️ Note: The following items became unavailable during checkout: {items}.")
             sold_out_items_str = ", ".join(sold_out_items_info)
             final_msg_parts.append(sold_out_note_template.format(items=sold_out_items_str))

        # Add "Leave Review" button
        leave_review_button_text = lang_data.get("leave_review_button", "Leave a Review")
        keyboard = [[InlineKeyboardButton(f"✍️ {leave_review_button_text}", callback_data="leave_review_now")]]
        await send_message_with_retry(context.bot, chat_id, "\n\n".join(final_msg_parts), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        # --- End Final Summary ---

        return True # Indicate overall success
    else:
        # Transaction failed in DB phase
        if not sold_out_items_info: # Only send generic error if no specific items were identified as sold out
            await send_message_with_retry(context.bot, chat_id, process_err_msg, parse_mode=None)
        return False


# --- handle_confirm_pay (Decides payment method) ---
async def handle_confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the 'Pay Now' button press from the basket."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    clear_expired_basket(context, user_id) # Sync call
    basket = context.user_data.get("basket", [])
    applied_discount_info = context.user_data.get('applied_discount')
    conn = None

    if not basket:
        await query.answer("Your basket is empty!", show_alert=True)
        return await handle_view_basket(update, context) # Use user module's handler

    original_total = Decimal('0.0') # Use Decimal
    final_total = Decimal('0.0')    # Use Decimal
    basket_snapshot = []            # Create snapshot for processing
    discount_code_used = None

    try:
        product_ids_in_basket = list(set(item['product_id'] for item in basket))
        if not product_ids_in_basket:
            await query.answer("Basket seems empty after validation.", show_alert=True)
            context.user_data['basket'] = [] # Ensure context is cleared
            context.user_data.pop('applied_discount', None)
            return await handle_view_basket(update, context) # Use user module's handler

        conn = get_db_connection()
        c = conn.cursor()
        # Fetch current prices for items in basket WITHIN a transaction for consistency? No, view is enough.
        placeholders = ','.join('?' * len(product_ids_in_basket))
        # Use column names
        c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", product_ids_in_basket)
        current_prices = {row['id']: Decimal(str(row['price'])) for row in c.fetchall()} # Use Decimal

        # Build snapshot and calculate original total
        for item in basket:
             pid = item['product_id']
             if pid in current_prices:
                 current_price = current_prices[pid]
                 original_total += current_price
                 # Create a copy for the snapshot, storing the price *at the time of confirmation*
                 item_snap = item.copy()
                 item_snap['price_at_checkout'] = current_price # Store as Decimal
                 basket_snapshot.append(item_snap)
             else:
                 logger.warning(f"Product ID {pid} from user {user_id}'s basket context not found in DB during payment confirmation. Item skipped.")
                 # Optionally notify user that an item was removed implicitly?

        # If snapshot is empty after checking DB, clear context and inform user
        if not basket_snapshot:
             context.user_data['basket'] = []
             context.user_data.pop('applied_discount', None)
             logger.warning(f"All items in user {user_id}'s basket were unavailable during payment confirmation.")
             kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
             await query.edit_message_text("❌ All items in your basket became unavailable. Please shop again.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
             return

        final_total = original_total # Start with original total

        # Re-validate discount code if applied
        if applied_discount_info:
            # Use the function moved to utils
            code_valid, validation_message, discount_details = utils.validate_discount_code(applied_discount_info['code'], float(original_total))
            if code_valid and discount_details:
                final_total = Decimal(str(discount_details['final_total'])) # Update final total using Decimal
                discount_code_used = applied_discount_info.get('code')
                # Update context with potentially recalculated amount
                context.user_data['applied_discount']['final_total'] = float(final_total)
                context.user_data['applied_discount']['amount'] = discount_details['discount_amount']
            else:
                # Discount became invalid, reset final total and remove from context
                final_total = original_total
                discount_code_used = None
                context.user_data.pop('applied_discount', None)
                await query.answer(f"Discount '{applied_discount_info['code']}' removed: {validation_message}", show_alert=True)
                # Refresh basket view to show updated total without discount
                return await handle_view_basket(update, context) # Use user module's handler

        # Ensure final total is not negative (shouldn't happen with Decimal logic)
        if final_total < Decimal('0.0'):
            logger.error(f"Calculated negative final_total {final_total} for user {user_id}. Aborting.")
            await query.answer("Calculation error (negative total).", show_alert=True)
            return

        # Fetch user's current balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance_result = c.fetchone()
        user_balance = Decimal(str(balance_result['balance'])) if balance_result else Decimal('0.0') # Use Decimal

    except sqlite3.Error as e:
         logger.error(f"DB error during payment confirmation prep for user {user_id}: {e}", exc_info=True)
         kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
         await query.edit_message_text("❌ Error calculating final total. Please try again.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
         return
    except Exception as e:
         logger.error(f"Unexpected error preparing payment confirmation for user {user_id}: {e}", exc_info=True)
         kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
         await query.edit_message_text("❌ An unexpected error occurred. Please try again.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
         return
    finally:
         if conn: conn.close()

    # --- Payment Decision ---
    logger.info(f"Payment Confirmation for User {user_id}. Final Total: {final_total:.2f}, Balance: {user_balance:.2f}")

    if user_balance >= final_total:
        # Sufficient balance - process directly
        logger.info(f"Sufficient balance found for user {user_id}. Processing purchase.")
        try:
            await query.edit_message_text("⏳ Processing payment using your balance...", reply_markup=None, parse_mode=None)
        except telegram_error.BadRequest: pass # Ignore if message not modified

        # Call the balance processing function (now handles DB and messaging)
        success = await process_purchase_with_balance(user_id, final_total, basket_snapshot, discount_code_used, context)

        if not success:
            # process_purchase_with_balance should have sent an error message
            # Refresh basket view to show current state after failure
            logger.warning(f"process_purchase_with_balance returned failure for user {user_id}.")
            await handle_view_basket(update, context) # Use user module's handler

    else:
        # Insufficient balance - prompt to top up
        logger.info(f"Insufficient balance for user {user_id}.")
        needed_str = format_currency(final_total)
        balance_str = format_currency(user_balance)
        insufficient_msg = lang_data.get("insufficient_balance", "⚠️ Insufficient Balance!")
        top_up_needed_note = lang_data.get("top_up_needed_note", "Top up needed to complete purchase.") # Example new string needed
        top_up_btn_text = lang_data.get("top_up_button", "Top Up Balance")
        back_btn_text = lang_data.get("back_basket_button", "Back to Basket")

        full_msg = ( # Plain text construction
            f"{insufficient_msg}\n\n"
            f"Required: {needed_str} EUR\n"
            f"Your Balance: {balance_str} EUR\n\n"
            f"{top_up_needed_note}"
        )
        keyboard = [
            [InlineKeyboardButton(f"💸 {top_up_btn_text}", callback_data="refill")],
            [InlineKeyboardButton(f"⬅️ {back_btn_text}", callback_data="view_basket")]
        ]
        await query.edit_message_text(full_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None) # Use None
