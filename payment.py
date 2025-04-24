import logging
import sqlite3
import time
import os
import shutil
import asyncio
import uuid # For generating unique order IDs
import requests # For making API calls to NOWPayments
from decimal import Decimal, ROUND_UP, ROUND_DOWN # Use Decimal for precision

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode # Keep import for reference
from telegram.ext import ContextTypes # Use ContextTypes
from telegram import helpers # Keep for potential non-escaping uses
import telegram.error as telegram_error
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation # Import InputMedia types
# -------------------------

# Import necessary items from utils and user
from utils import (
    send_message_with_retry, format_currency, ADMIN_ID,
    LANGUAGES, load_all_data, BASKET_TIMEOUT, MIN_DEPOSIT_EUR,
    NOWPAYMENTS_API_KEY, NOWPAYMENTS_API_URL, WEBHOOK_URL,
    get_currency_to_eur_price, format_expiration_time, FEE_ADJUSTMENT,
    add_pending_deposit, remove_pending_deposit, # Import DB helpers for pending deposits
    get_nowpayments_min_amount, # **** Import NEW function ****
    get_db_connection, MEDIA_DIR # Import helper and MEDIA_DIR
)
# Import user module to call functions like clear_expired_basket, validate_discount_code
import user
from collections import Counter, defaultdict # Import Counter and defaultdict

logger = logging.getLogger(__name__)

# --- NOWPayments Deposit Creation ---

async def create_nowpayments_payment(user_id: int, target_eur_amount: Decimal, pay_currency_code: str) -> dict:
    """
    Creates a payment invoice using the NOWPayments API. Checks minimum amount.

    Args:
        user_id: The Telegram user ID initiating the deposit.
        target_eur_amount: The amount in EUR the user wants to deposit (as Decimal).
        pay_currency_code: The lowercase crypto currency code (e.g., 'btc', 'ltc').

    Returns:
        A dictionary containing the NOWPayments API response on success,
        or an error dictionary {'error': 'message', ...} on failure.
    """
    if not NOWPAYMENTS_API_KEY:
        logger.error("NOWPayments API key is not configured.")
        return {'error': 'payment_api_misconfigured'}

    logger.info(f"Attempting to create NOWPayments invoice for user {user_id}, {target_eur_amount} EUR via {pay_currency_code}")

    # 1. Get Crypto/EUR Price
    eur_price = get_currency_to_eur_price(pay_currency_code)
    if eur_price is None or eur_price <= 0:
        logger.error(f"Could not get valid EUR price for {pay_currency_code}")
        return {'error': 'rate_fetch_error', 'currency': pay_currency_code.upper()}

    # 2. Calculate Crypto Amount
    # Use high precision division, round *up* slightly to ensure enough value
    crypto_amount_needed = (target_eur_amount / eur_price).quantize(Decimal('1E-8'), rounding=ROUND_UP)
    logger.info(f"Calculated {crypto_amount_needed} {pay_currency_code} needed for {target_eur_amount} EUR (Rate: {eur_price} EUR/{pay_currency_code})")

    # --- NEW: Check Minimum Amount ---
    min_amount_api = get_nowpayments_min_amount(pay_currency_code) # Sync call, uses cache
    if min_amount_api is None:
        logger.error(f"Could not fetch minimum amount for {pay_currency_code} from NOWPayments API.")
        return {'error': 'min_amount_fetch_error', 'currency': pay_currency_code.upper()}

    if crypto_amount_needed < min_amount_api:
        logger.warning(f"Calculated amount {crypto_amount_needed} {pay_currency_code} is less than NOWPayments minimum {min_amount_api} {pay_currency_code}.")
        return {
            'error': 'amount_too_low_api',
            'currency': pay_currency_code.upper(),
            'min_amount': f"{min_amount_api:f}".rstrip('0').rstrip('.'), # Format decimal nicely
            'crypto_amount': f"{crypto_amount_needed:f}".rstrip('0').rstrip('.') # Also send calculated amount
        }
    # --- END NEW Check ---

    # 3. Prepare API Request Data
    order_id = f"USER{user_id}_DEPOSIT_{int(time.time())}_{uuid.uuid4().hex[:6]}" # Unique order ID
    ipn_callback_url = f"{WEBHOOK_URL}/webhook" # Ensure this matches your Render URL + /webhook

    payload = {
        "price_amount": float(crypto_amount_needed), # API expects float for price_amount (crypto amount)
        "price_currency": pay_currency_code, # Price amount is now in crypto, price_currency should match pay_currency
        "pay_currency": pay_currency_code, # The currency the user will pay with
        "ipn_callback_url": ipn_callback_url,
        "order_id": order_id,
        "order_description": f"Balance top-up for user {user_id} (~{target_eur_amount:.2f} EUR)",
        "is_fixed_rate": False, # Use floating rate for flexibility
    }

    headers = {
        'x-api-key': NOWPAYMENTS_API_KEY,
        'Content-Type': 'application/json'
    }
    payment_url = f"{NOWPAYMENTS_API_URL}/v1/payment"

    # 4. Make API Call (Using synchronous requests in a thread)
    try:
        def make_request():
            try:
                response = requests.post(payment_url, headers=headers, json=payload, timeout=20) # Added timeout
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                return response.json()
            except requests.exceptions.Timeout:
                 logger.error(f"NOWPayments API request timed out for order {order_id}.")
                 return {'error': 'api_timeout', 'internal': True}
            except requests.exceptions.RequestException as e:
                 logger.error(f"NOWPayments API request error for order {order_id}: {e}", exc_info=True)
                 status_code = e.response.status_code if e.response is not None else None
                 error_content = e.response.text if e.response is not None else "No response content"
                 if status_code == 401: return {'error': 'api_key_invalid'}
                 # Check specific error based on previous log
                 if status_code == 400 and "AMOUNT_MINIMAL_ERROR" in error_content:
                     logger.warning(f"NOWPayments rejected payment for {order_id} due to amount being too low (API check).")
                     # Extract min amount from message if possible (fallback)
                     min_amount_fallback = "N/A"
                     try:
                         msg_data = json.loads(error_content)
                         min_amount_fallback = msg_data.get("message", "").split(" ")[-1] # Attempt to parse
                     except: pass
                     return {'error': 'amount_too_low_api', 'currency': pay_currency_code.upper(), 'min_amount': min_amount_fallback}

                 return {'error': 'api_request_failed', 'details': str(e), 'status': status_code, 'content': error_content[:200]}
            except Exception as e:
                 logger.error(f"Unexpected error during NOWPayments API call for order {order_id}: {e}", exc_info=True)
                 return {'error': 'api_unexpected_error', 'details': str(e)}

        payment_data = await asyncio.to_thread(make_request)

        if 'error' in payment_data:
             if payment_data['error'] == 'api_key_invalid': logger.critical("NOWPayments API Key seems invalid!")
             elif payment_data.get('internal'): logger.error("Internal error during API request (e.g., timeout).")
             else: logger.error(f"NOWPayments API returned error: {payment_data}")
             return payment_data

        # 5. Validate Response
        if not all(k in payment_data for k in ['payment_id', 'pay_address', 'pay_amount', 'pay_currency']):
             logger.error(f"Invalid response from NOWPayments API for order {order_id}: Missing keys. Response: {payment_data}")
             return {'error': 'invalid_api_response'}

        # 6. Store Pending Deposit Info
        add_success = await asyncio.to_thread(
            add_pending_deposit,
            payment_data['payment_id'],
            user_id,
            payment_data['pay_currency'],
            float(target_eur_amount)
        )

        if not add_success:
             logger.error(f"Failed to add pending deposit to DB for payment_id {payment_data['payment_id']} (user {user_id}).")
             return {'error': 'pending_db_error'}

        logger.info(f"Successfully created NOWPayments invoice {payment_data['payment_id']} for user {user_id}.")
        return payment_data

    except Exception as e:
        logger.error(f"Unexpected error in create_nowpayments_payment for user {user_id}: {e}", exc_info=True)
        return {'error': 'internal_server_error', 'details': str(e)}


# --- Callback Handler for Crypto Selection during Refill ---
async def handle_select_refill_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting the crypto asset for refill, creates NOWPayments invoice."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en") # Get language
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_refill_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    selected_asset_code = params[0].lower()
    logger.info(f"User {user_id} selected {selected_asset_code} for refill.")

    refill_eur_amount_float = context.user_data.get('refill_eur_amount')
    if not refill_eur_amount_float or refill_eur_amount_float <= 0:
        logger.error(f"Refill amount context lost before asset selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Refill amount context lost. Please start the top up again.", parse_mode=None)
        context.user_data.pop('state', None)
        return

    refill_eur_amount_decimal = Decimal(str(refill_eur_amount_float))

    # Get translated texts
    preparing_invoice_msg = lang_data.get("preparing_invoice", "⏳ Preparing your payment invoice...")
    error_preparing_payment_msg = lang_data.get("error_preparing_payment", "❌ An error occurred while preparing the payment. Please try again later.")
    failed_invoice_creation_msg = lang_data.get("failed_invoice_creation", "❌ Failed to create payment invoice. Please try again later or contact support.")
    error_nowpayments_api_msg = lang_data.get("error_nowpayments_api", "❌ Payment API Error: Could not create payment. Please try again later or contact support.")
    error_invalid_response_msg = lang_data.get("error_invalid_nowpayments_response", "❌ Payment API Error: Invalid response received. Please contact support.")
    error_api_key_msg = lang_data.get("error_nowpayments_api_key", "❌ Payment API Error: Invalid API key. Please contact support.")
    error_pending_db_msg = lang_data.get("payment_pending_db_error", "❌ Database Error: Could not record pending payment. Please contact support.")
    # Specific message for amount too low based on API check
    error_amount_too_low_api_msg = lang_data.get("payment_amount_too_low_api", "❌ Payment Amount Too Low: The equivalent of {target_eur_amount} EUR in {currency} ({crypto_amount}) is below the minimum required by the payment provider ({min_amount} {currency}). Please try a higher EUR amount.")
    error_min_amount_fetch_msg = lang_data.get("error_min_amount_fetch", "❌ Error: Could not retrieve minimum payment amount for {currency}. Please try again later or select a different currency.")
    error_getting_rate_msg = lang_data.get("error_getting_rate", "❌ Error: Could not get exchange rate for {asset}. Please try another currency or contact support.")

    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]])

    try:
        await query.edit_message_text(preparing_invoice_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Couldn't edit message in handle_select_refill_crypto: {e}")
        await query.answer("Preparing...")

    # --- Call NOWPayments API ---
    payment_result = await create_nowpayments_payment(user_id, refill_eur_amount_decimal, selected_asset_code)

    # --- Handle Result ---
    if 'error' in payment_result:
        error_code = payment_result['error']
        logger.error(f"Failed to create NOWPayments invoice for user {user_id}: {error_code} - Details: {payment_result}")

        # Map error codes to user-friendly messages
        error_message_to_user = failed_invoice_creation_msg # Default
        if error_code == 'rate_fetch_error': error_message_to_user = error_getting_rate_msg.format(asset=selected_asset_code.upper())
        elif error_code == 'api_key_invalid': error_message_to_user = error_api_key_msg
        elif error_code == 'invalid_api_response': error_message_to_user = error_invalid_response_msg
        elif error_code == 'pending_db_error': error_message_to_user = error_pending_db_msg
        # **** NEW ERROR HANDLING ****
        elif error_code == 'amount_too_low_api':
            error_message_to_user = error_amount_too_low_api_msg.format(
                target_eur_amount=format_currency(refill_eur_amount_decimal),
                currency=payment_result.get('currency', selected_asset_code.upper()),
                crypto_amount=payment_result.get('crypto_amount', 'N/A'),
                min_amount=payment_result.get('min_amount', 'N/A')
            )
        elif error_code == 'min_amount_fetch_error':
            error_message_to_user = error_min_amount_fetch_msg.format(currency=payment_result.get('currency', selected_asset_code.upper()))
        # **** END NEW ERROR HANDLING ****
        elif error_code in ['api_timeout', 'api_request_failed', 'api_unexpected_error', 'internal_server_error', 'payout_error_detected']:
            error_message_to_user = error_nowpayments_api_msg # Generic API error for user

        try: await query.edit_message_text(error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        except Exception as edit_e: logger.error(f"Failed to edit message with invoice creation error: {edit_e}"); await send_message_with_retry(context.bot, chat_id, error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None) # Reset state on error
    else:
        # Success - Display the invoice details
        logger.info(f"NOWPayments invoice created successfully for user {user_id}. Payment ID: {payment_result.get('payment_id')}")
        context.user_data.pop('refill_eur_amount', None) # Clear intermediate value
        context.user_data.pop('state', None) # Reset state
        await display_nowpayments_invoice(update, context, payment_result)


# --- Display NOWPayments Invoice ---
# (display_nowpayments_invoice function remains unchanged from previous correct version)
async def display_nowpayments_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_data: dict):
    """Displays the NOWPayments invoice details to the user by EDITING the message."""
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en") # Get language
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    try:
        # Extract required data safely
        pay_address = payment_data.get('pay_address')
        pay_amount_str = payment_data.get('pay_amount') # Amount in crypto (string from API)
        pay_currency = payment_data.get('pay_currency', 'N/A').upper() # Display uppercase
        payment_id = payment_data.get('payment_id', 'N/A')
        expiration_date_str = payment_data.get('expiration_estimate_date')
        # Try to get target EUR from original context if available, otherwise from payment data (might be crypto amount there)
        target_eur_context = context.user_data.get('refill_eur_amount') # This might have been popped already
        target_eur_display = "N/A"
        if target_eur_context:
            target_eur_display = format_currency(Decimal(str(target_eur_context)))
        else:
            # Fallback: Check if 'price_amount' and 'price_currency' indicate EUR in response
            price_curr = payment_data.get('price_currency')
            price_amt = payment_data.get('price_amount')
            if price_curr and price_curr.lower() == 'eur' and price_amt:
                try: target_eur_display = format_currency(Decimal(str(price_amt)))
                except: pass # Keep N/A on error
            # If still N/A, maybe try fetching from DB (less ideal here)

        if not pay_address or not pay_amount_str:
            logger.error(f"Missing critical data in NOWPayments response for display: {payment_data}")
            raise ValueError("Missing payment address or amount")

        pay_amount_decimal = Decimal(pay_amount_str)
        # Format crypto amount precisely, removing trailing zeros after normalization
        pay_amount_display = '{:f}'.format(pay_amount_decimal.normalize())

        # Format expiration time
        expiration_display = format_expiration_time(expiration_date_str)

        # Get translated texts
        invoice_title_refill = lang_data.get("invoice_title_refill", "Top-Up Invoice Created")
        please_pay_label = lang_data.get("please_pay_label", "Please pay")
        target_value_label = lang_data.get("target_value_label", "Target Value")
        payment_address_label = lang_data.get("payment_address_label", "Payment Address")
        amount_label = lang_data.get("amount_label", "Amount")
        expires_at_label = lang_data.get("expires_at_label", "Expires At")
        send_warning_template = lang_data.get("send_warning_template", "⚠️ Send only {asset}. Ensure you send the exact amount.")
        confirmation_note = lang_data.get("confirmation_note", "✅ Confirmation is automatic. Please wait a few minutes after sending.")
        back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")

        msg_parts = [
            f"*{invoice_title_refill}*", # Use Markdown for title
            f"\n{amount_label}: `{pay_amount_display}` {pay_currency}", # Use backticks for amount
            f"({target_value_label}: ~{target_eur_display} EUR)\n",
            f"{payment_address_label}:\n`{pay_address}`\n", # Use backticks for address
            send_warning_template.format(asset=pay_currency),
            f"{expires_at_label}: {expiration_display}\n",
            f"{confirmation_note}"
        ]

        # Escape markdown characters in the parts that need it
        escaped_msg = helpers.escape_markdown("\n".join(msg_parts), version=2)

        keyboard = [[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]]

        await query.edit_message_text(
            escaped_msg, # Send the escaped message
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2, # Tell Telegram it's MarkdownV2
            disable_web_page_preview=True
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Error formatting or displaying NOWPayments invoice: {e}. Data: {payment_data}", exc_info=True)
        error_display_msg = lang_data.get("error_preparing_payment", "❌ An error occurred while preparing the payment details. Please try again later.")
        back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {lang_data.get('back_profile_button', 'Back to Profile')}", callback_data="profile")]])
        try: await query.edit_message_text(error_display_msg, reply_markup=back_button_markup, parse_mode=None)
        except Exception: pass # Ignore edit error here
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing NOWPayments invoice message: {e}. Message: {escaped_msg}")
             # If editing fails, maybe send a new message? Or just log it.
             # For simplicity, just log the error for now.
        else: await query.answer() # Ignore "not modified"
    except Exception as e:
         logger.error(f"Unexpected error in display_nowpayments_invoice: {e}", exc_info=True)
         # Handle unexpected errors similarly
         error_display_msg = lang_data.get("error_preparing_payment", "❌ An unexpected error occurred while preparing the payment details.")
         back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {lang_data.get('back_profile_button', 'Back to Profile')}", callback_data="profile")]])
         try: await query.edit_message_text(error_display_msg, reply_markup=back_button_markup, parse_mode=None)
         except Exception: pass


# --- Process Successful Refill (Called by Webhook Handler) ---
# (process_successful_refill function remains unchanged from previous correct version)
async def process_successful_refill(user_id: int, amount_to_add_eur: Decimal, payment_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handles DB updates after a NOWPayments deposit is confirmed via webhook.
    Returns True if balance update was successful, False otherwise.
    """
    # context might be a dummy context created in the webhook, containing only context.bot
    bot = context.bot
    # We don't have the original user's context, so fetch language from DB or default
    user_lang = 'en' # Default
    conn_lang = None
    try:
        conn_lang = get_db_connection()
        c_lang = conn_lang.cursor()
        c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        lang_res = c_lang.fetchone()
        if lang_res and lang_res['language'] in LANGUAGES:
            user_lang = lang_res['language']
    except sqlite3.Error as e:
        logger.error(f"DB error fetching language for user {user_id} during refill confirmation: {e}")
    finally:
        if conn_lang: conn_lang.close()

    lang_data = LANGUAGES.get(user_lang, LANGUAGES['en'])

    if not isinstance(amount_to_add_eur, Decimal) or amount_to_add_eur <= Decimal('0.0'):
        logger.error(f"Invalid amount_to_add_eur in process_successful_refill: {amount_to_add_eur}")
        return False

    conn = None
    db_update_successful = False
    # Convert Decimal to float JUST for DB storage
    amount_float = float(amount_to_add_eur)
    new_balance = Decimal('0.0') # Initialize as Decimal

    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        logger.info(f"Attempting balance update for user {user_id} by {amount_float:.2f} EUR (Payment ID: {payment_id})")

        # --- Check if payment_id was already processed (idempotency) ---
        # This requires adding a column to track processed payments if needed.
        # For now, we rely on remove_pending_deposit happening after this returns True.
        # If this function fails, remove_pending_deposit won't be called,
        # allowing the webhook to potentially retry (if NOWPayments retries).

        update_result = c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount_float, user_id))
        if update_result.rowcount == 0:
            logger.error(f"User {user_id} not found during refill DB update (Payment ID: {payment_id}). Rowcount: {update_result.rowcount}")
            conn.rollback()
            return False # User doesn't exist?

        # Fetch new balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance_result = c.fetchone()
        if new_balance_result: new_balance = Decimal(str(new_balance_result['balance'])) # Convert DB float back to Decimal
        else: logger.error(f"Could not fetch new balance for {user_id} after update."); conn.rollback(); return False

        conn.commit()
        db_update_successful = True
        logger.info(f"Successfully processed refill DB update for user {user_id}. Added: {amount_to_add_eur:.2f} EUR. New Balance: {new_balance:.2f} EUR.")

        # --- Send confirmation message to user ---
        top_up_success_title = lang_data.get("top_up_success_title", "✅ Top Up Successful!")
        amount_added_label = lang_data.get("amount_added_label", "Amount Added")
        new_balance_label = lang_data.get("new_balance_label", "Your new balance")
        back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")

        amount_str = format_currency(amount_to_add_eur) # Use Decimal
        new_balance_str = format_currency(new_balance) # Use Decimal

        success_msg = (f"{top_up_success_title}\n\n{amount_added_label}: {amount_str} EUR\n"
                       f"{new_balance_label}: {new_balance_str} EUR")
        keyboard = [[InlineKeyboardButton(f"👤 {back_to_profile_button}", callback_data="profile")]]

        # Send message using the bot instance from the passed context
        await send_message_with_retry(bot, user_id, success_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

        return True # Indicate DB update success

    except sqlite3.Error as e:
        logger.error(f"DB error during process_successful_refill user {user_id} (Payment ID: {payment_id}): {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False # Indicate DB update failure
    except Exception as e:
         logger.error(f"Unexpected error during process_successful_refill user {user_id} (Payment ID: {payment_id}): {e}", exc_info=True)
         if conn and conn.in_transaction: conn.rollback()
         return False # Indicate failure
    finally:
        if conn: conn.close()


# --- Process Purchase with Balance (Largely unchanged, ensure Decimal usage) ---
# (process_purchase_with_balance function remains unchanged)
async def process_purchase_with_balance(user_id: int, amount_to_deduct: Decimal, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates when paying with internal balance."""
    chat_id = context._chat_id or context._user_id or user_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} balance purchase."); return False
    if not isinstance(amount_to_deduct, Decimal) or amount_to_deduct < Decimal('0.0'): logger.error(f"Invalid amount_to_deduct {amount_to_deduct}."); return False

    conn = None
    sold_out_during_process = []
    final_pickup_details = defaultdict(list)
    db_update_successful = False
    processed_product_ids = []
    purchases_to_insert = []
    # Convert Decimal to float for DB interactions
    amount_float_to_deduct = float(amount_to_deduct)

    balance_changed_error = lang_data.get("balance_changed_error", "❌ Transaction failed: Balance changed.")
    order_failed_all_sold_out_balance = lang_data.get("order_failed_all_sold_out_balance", "❌ Order Failed: All items sold out.")
    error_processing_purchase_contact_support = lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support.")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE")
        # 1. Verify balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        current_balance_result = c.fetchone()
        # Compare Decimal with float from DB
        if not current_balance_result or Decimal(str(current_balance_result['balance'])) < amount_to_deduct:
             logger.warning(f"Insufficient balance user {user_id}. Needed: {amount_to_deduct:.2f}")
             conn.rollback()
             await send_message_with_retry(context.bot, chat_id, balance_changed_error, parse_mode=None)
             return False
        # 2. Deduct balance
        update_res = c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount_float_to_deduct, user_id))
        if update_res.rowcount == 0: logger.error(f"Failed to deduct balance user {user_id}."); conn.rollback(); return False
        # 3. Process items
        product_ids_in_snapshot = list(set(item['product_id'] for item in basket_snapshot))
        if not product_ids_in_snapshot: logger.warning(f"Empty snapshot IDs user {user_id}."); conn.rollback(); return False
        placeholders = ','.join('?' * len(product_ids_in_snapshot))
        c.execute(f"SELECT id, name, product_type, size, price, city, district, available, reserved, original_text FROM products WHERE id IN ({placeholders})", product_ids_in_snapshot)
        product_db_details = {row['id']: dict(row) for row in c.fetchall()}
        purchase_time_iso = datetime.now(timezone.utc).isoformat()
        for item_snapshot in basket_snapshot:
            product_id = item_snapshot['product_id']
            details = product_db_details.get(product_id)
            if not details: sold_out_during_process.append(f"Item ID {product_id} (unavailable)"); continue
            res_update = c.execute("UPDATE products SET reserved = MAX(0, reserved - 1) WHERE id = ?", (product_id,))
            if res_update.rowcount == 0: logger.warning(f"Failed reserve decr. P{product_id} user {user_id}."); sold_out_during_process.append(f"{details.get('name', '?')} {details.get('size', '?')}"); continue
            avail_update = c.execute("UPDATE products SET available = available - 1 WHERE id = ? AND available > 0", (product_id,))
            if avail_update.rowcount == 0: logger.error(f"Failed available decr. P{product_id} user {user_id}. Race?"); sold_out_during_process.append(f"{details.get('name', '?')} {details.get('size', '?')}"); c.execute("UPDATE products SET reserved = reserved + 1 WHERE id = ?", (product_id,)); continue # Rollback reservation
            # Ensure Decimal prices from DB are converted to float for insert if needed
            item_price_float = float(Decimal(str(details['price'])))
            purchases_to_insert.append((user_id, product_id, details['name'], details['product_type'], details['size'], item_price_float, details['city'], details['district'], purchase_time_iso))
            processed_product_ids.append(product_id)
            final_pickup_details[product_id].append({'name': details['name'], 'size': details['size'], 'text': details.get('original_text')})
        if not purchases_to_insert:
            logger.warning(f"No items processed user {user_id}. Rolling back balance deduction.")
            conn.rollback()
            await send_message_with_retry(context.bot, chat_id, order_failed_all_sold_out_balance, parse_mode=None)
            return False
        # 4. Record Purchases & Update User Stats
        c.executemany("INSERT INTO purchases (user_id, product_id, product_name, product_type, product_size, price_paid, city, district, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", purchases_to_insert)
        c.execute("UPDATE users SET total_purchases = total_purchases + ? WHERE user_id = ?", (len(purchases_to_insert), user_id))
        if discount_code_used: c.execute("UPDATE discount_codes SET uses_count = uses_count + 1 WHERE code = ?", (discount_code_used,))
        c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,))
        conn.commit()
        db_update_successful = True
        logger.info(f"Processed balance purchase user {user_id}. Deducted: {amount_to_deduct:.2f} EUR.")
    except sqlite3.Error as e:
        logger.error(f"DB error during balance purchase user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error during balance purchase user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

    # --- Post-Transaction Cleanup & Message Sending ---
    if db_update_successful:
        media_details = defaultdict(list)
        if processed_product_ids:
            # Fetch Media Details
            conn_media = None
            try:
                conn_media = get_db_connection()
                c_media = conn_media.cursor()
                media_placeholders = ','.join('?' * len(processed_product_ids))
                c_media.execute(f"SELECT product_id, media_type, telegram_file_id, file_path FROM product_media WHERE product_id IN ({media_placeholders})", processed_product_ids)
                for row in c_media.fetchall(): media_details[row['product_id']].append(dict(row))
            except sqlite3.Error as e: logger.error(f"DB error fetching media: {e}")
            finally:
                if conn_media: conn_media.close()

            # Send Confirmation and Details
            success_title = lang_data.get("purchase_success", "🎉 Purchase Complete! Pickup details below:")
            await send_message_with_retry(context.bot, chat_id, success_title, parse_mode=None)

            for prod_id in processed_product_ids:
                item_details = final_pickup_details.get(prod_id)
                if not item_details: continue
                item_name, item_size = item_details[0]['name'], item_details[0]['size']
                item_text = item_details[0]['text'] or "(No specific pickup details provided)"
                item_header = f"--- Item: {item_name} {item_size} ---"
                sent_media = False

                # Send Media if available
                if prod_id in media_details:
                    media_list = media_details[prod_id]
                    if media_list:
                        # Simple: Send only the first media item
                        media_item = media_list[0]
                        file_id, media_type, file_path = media_item.get('telegram_file_id'), media_item.get('media_type'), media_item.get('file_path')
                        caption = item_header
                        try:
                            if file_id and media_type == 'photo': await context.bot.send_photo(chat_id, photo=file_id, caption=caption, parse_mode=None); sent_media = True
                            elif file_id and media_type == 'video': await context.bot.send_video(chat_id, video=file_id, caption=caption, parse_mode=None); sent_media = True
                            elif file_id and media_type == 'gif': await context.bot.send_animation(chat_id, animation=file_id, caption=caption, parse_mode=None); sent_media = True
                            elif file_path and await asyncio.to_thread(os.path.exists, file_path):
                                async with await asyncio.to_thread(open, file_path, 'rb') as f:
                                    if media_type == 'photo': await context.bot.send_photo(chat_id, photo=f, caption=caption, parse_mode=None); sent_media = True
                                    elif media_type == 'video': await context.bot.send_video(chat_id, video=f, caption=caption, parse_mode=None); sent_media = True
                                    elif media_type == 'gif': await context.bot.send_animation(chat_id, animation=f, caption=caption, parse_mode=None); sent_media = True
                            else: logger.warning(f"Media path invalid/missing for P{prod_id}: {file_path}")
                        except Exception as e: logger.error(f"Error sending media P{prod_id} user {user_id}: {e}", exc_info=True)

                # Always send Text Details separately
                await send_message_with_retry(context.bot, chat_id, item_text, parse_mode=None)

                # Delete Product Record and Media Directory
                conn_del = None
                try:
                    conn_del = get_db_connection()
                    c_del = conn_del.cursor()
                    c_del.execute("DELETE FROM product_media WHERE product_id = ?", (prod_id,))
                    delete_result = c_del.execute("DELETE FROM products WHERE id = ?", (prod_id,))
                    conn_del.commit()
                    if delete_result.rowcount > 0:
                        logger.info(f"Successfully deleted purchased product record ID {prod_id}.")
                        media_dir_to_delete = os.path.join(MEDIA_DIR, str(prod_id))
                        if os.path.exists(media_dir_to_delete):
                            asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_delete, ignore_errors=True))
                            logger.info(f"Scheduled deletion of media dir: {media_dir_to_delete}")
                    else: logger.warning(f"Product record ID {prod_id} not found for deletion.")
                except sqlite3.Error as e: logger.error(f"DB error deleting product ID {prod_id}: {e}", exc_info=True); conn_del.rollback() if conn_del and conn_del.in_transaction else None
                except Exception as e: logger.error(f"Unexpected error deleting product ID {prod_id}: {e}", exc_info=True)
                finally:
                    if conn_del: conn_del.close()

        # Final Message
        final_message_parts = ["Purchase details sent above."]
        if sold_out_during_process:
             sold_out_items_str = ", ".join(item for item in sold_out_during_process)
             sold_out_note = lang_data.get("sold_out_note", "⚠️ Note: The following items became unavailable: {items}. You were not charged for these.")
             final_message_parts.append(sold_out_note.format(items=sold_out_items_str))
        leave_review_button = lang_data.get("leave_review_button", "Leave a Review")
        keyboard = [[InlineKeyboardButton(f"✍️ {leave_review_button}", callback_data="leave_review_now")]]
        await send_message_with_retry(context.bot, chat_id, "\n\n".join(final_message_parts), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

        # Clear user context
        context.user_data['basket'] = []
        context.user_data.pop('applied_discount', None)
        return True
    else: # Purchase failed
        if not sold_out_during_process: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support, parse_mode=None)
        return False


# --- Confirm Pay Handler (Checks Balance, initiates balance payment or refill prompt) ---
# (handle_confirm_pay function remains unchanged from previous correct version)
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

    if not basket:
        await query.answer("Your basket is empty!", show_alert=True)
        return await user.handle_view_basket(update, context)

    # --- Calculate Final Total (using Decimals) ---
    conn = None
    original_total = Decimal('0.0')
    final_total = Decimal('0.0')
    valid_basket_items_snapshot = []
    discount_code_to_use = None

    try:
        product_ids_in_basket = list(set(item['product_id'] for item in basket))
        if not product_ids_in_basket:
             await query.answer("Basket empty after validation.", show_alert=True)
             return await user.handle_view_basket(update, context)

        conn = get_db_connection()
        c = conn.cursor()
        placeholders = ','.join('?' for _ in product_ids_in_basket)
        c.execute(f"SELECT id, price FROM products WHERE id IN ({placeholders})", product_ids_in_basket)
        prices_dict = {row['id']: Decimal(str(row['price'])) for row in c.fetchall()}

        for item in basket:
             prod_id = item['product_id']
             if prod_id in prices_dict:
                 original_total += prices_dict[prod_id]
                 item_snapshot = item.copy()
                 item_snapshot['price_at_checkout'] = prices_dict[prod_id] # Store Decimal price
                 valid_basket_items_snapshot.append(item_snapshot)
             else: logger.warning(f"Product {prod_id} missing during payment confirm user {user_id}.")

        if not valid_basket_items_snapshot:
             context.user_data['basket'] = []
             context.user_data.pop('applied_discount', None)
             logger.warning(f"All items unavailable user {user_id} payment confirm.")
             keyboard_back = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
             try: await query.edit_message_text("❌ Error: All items unavailable.", reply_markup=InlineKeyboardMarkup(keyboard_back), parse_mode=None)
             except telegram_error.BadRequest: await send_message_with_retry(context.bot, chat_id, "❌ Error: All items unavailable.", reply_markup=InlineKeyboardMarkup(keyboard_back), parse_mode=None)
             return

        final_total = original_total
        if applied_discount_info:
            # Sync call, pass float original total
            code_valid, _, discount_details = user.validate_discount_code(applied_discount_info['code'], float(original_total))
            if code_valid and discount_details:
                final_total = Decimal(str(discount_details['final_total'])) # Convert back to Decimal
                discount_code_to_use = applied_discount_info.get('code')
                # Store final total as float in context for consistency if needed elsewhere
                context.user_data['applied_discount']['final_total'] = float(final_total)
                context.user_data['applied_discount']['amount'] = discount_details['discount_amount']
            else:
                final_total = original_total
                discount_code_to_use = None
                context.user_data.pop('applied_discount', None)
                await query.answer("Applied discount became invalid.", show_alert=True)

        if final_total < Decimal('0.0'): await query.answer("Cannot process negative amount.", show_alert=True); return

        # 3. Fetch User Balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        balance_result = c.fetchone()
        user_balance = Decimal(str(balance_result['balance'])) if balance_result else Decimal('0.0')

    except sqlite3.Error as e:
         logger.error(f"DB error during payment confirm user {user_id}: {e}", exc_info=True)
         kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
         await query.edit_message_text("❌ Error calculating total/balance.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None); return
    except Exception as e:
         logger.error(f"Unexpected error prep payment confirm user {user_id}: {e}", exc_info=True)
         kb = [[InlineKeyboardButton("⬅️ Back", callback_data="view_basket")]]
         try: await query.edit_message_text("❌ Unexpected error.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
         except telegram_error.BadRequest: await send_message_with_retry(context.bot, chat_id,"❌ Unexpected error.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)
         return
    finally:
         if conn: conn.close()

    # 4. Compare Balance and Proceed
    logger.info(f"Payment confirm user {user_id}. Final Total: {final_total:.2f}, Balance: {user_balance:.2f}")

    if user_balance >= final_total:
        # Pay with balance
        logger.info(f"Sufficient balance user {user_id}. Processing with balance.")
        try:
            if query.message: await query.edit_message_text("⏳ Processing payment with balance...", reply_markup=None, parse_mode=None)
            else: await send_message_with_retry(context.bot, chat_id, "⏳ Processing payment with balance...", parse_mode=None)
        except telegram_error.BadRequest: await query.answer("Processing...")

        success = await process_purchase_with_balance(user_id, final_total, valid_basket_items_snapshot, discount_code_to_use, context)

        if success:
            try:
                 if query.message: await query.edit_message_text("✅ Purchase successful! Details sent.", reply_markup=None, parse_mode=None)
            except telegram_error.BadRequest: pass # Ignore edit error after success
        else:
            await user.handle_view_basket(update, context) # Refresh basket view on failure

    else:
        # Insufficient balance - Prompt to Refill
        logger.info(f"Insufficient balance user {user_id}.")
        needed_amount_str = format_currency(final_total)
        balance_str = format_currency(user_balance)
        insufficient_msg = lang_data.get("insufficient_balance", "⚠️ Insufficient Balance! Top up needed.")
        top_up_button_text = lang_data.get("top_up_button", "Top Up Balance")
        back_basket_button_text = lang_data.get("back_basket_button", "Back to Basket")
        full_msg = (f"{insufficient_msg}\n\nRequired: {needed_amount_str} EUR\nYour Balance: {balance_str} EUR")
        keyboard = [
            [InlineKeyboardButton(f"💸 {top_up_button_text}", callback_data="refill")],
            [InlineKeyboardButton(f"⬅️ {back_basket_button_text}", callback_data="view_basket")]
        ]
        try: await query.edit_message_text(full_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest: await send_message_with_retry(context.bot, chat_id, full_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
