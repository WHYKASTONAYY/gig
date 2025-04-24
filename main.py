
import logging
import asyncio
import os
import signal
import sqlite3
from functools import wraps
from datetime import timedelta
import threading # Added for Flask
import requests # Added for webhook processing
from decimal import Decimal # Added for webhook processing
import json # Added for webhook logging
import time # Added for main loop sleep

# --- Telegram Imports ---
from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, ApplicationBuilder, Defaults, ContextTypes,
    CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    PicklePersistence, JobQueue
)
from telegram.constants import ParseMode
import telegram.error as telegram_error

# --- Flask Import ---
from flask import Flask, request, Response # Added

# --- Library Imports ---
import pytz
import nest_asyncio # Added for running flask in thread

# --- Local Imports ---
# Ensure utils is imported first to initialize DB and load data
import utils
from utils import (
    TOKEN, ADMIN_ID, init_db, load_all_data, LANGUAGES, THEMES,
    SUPPORT_USERNAME, BASKET_TIMEOUT, clear_all_expired_baskets,
    SECONDARY_ADMIN_IDS, WEBHOOK_URL, # Added WEBHOOK_URL
    get_db_connection, DATABASE_PATH,
    get_pending_deposit, remove_pending_deposit, # Added DB helpers
    FEE_ADJUSTMENT, get_currency_to_eur_price # Added Fee and Price utils
)
from user import (
    start, handle_shop, handle_city_selection, handle_district_selection,
    handle_type_selection, handle_product_selection, handle_add_to_basket,
    handle_view_basket, handle_clear_basket, handle_remove_from_basket,
    handle_profile, handle_language_selection, handle_price_list,
    handle_price_list_city, handle_reviews_menu, handle_leave_review,
    handle_view_reviews, handle_leave_review_message, handle_back_start,
    handle_user_discount_code_message, apply_discount_start, remove_discount,
    handle_leave_review_now, handle_refill, handle_view_history,
    handle_refill_amount_message,
    # REMOVED: handle_withdrawal_request, process_withdrawal_message
)
from admin import (
    handle_admin_menu, handle_sales_analytics_menu, handle_sales_dashboard,
    handle_sales_select_period, handle_sales_run, handle_adm_city,
    handle_adm_dist, handle_adm_type, handle_adm_add, handle_adm_size,
    handle_adm_custom_size, handle_confirm_add_drop, cancel_add,
    handle_adm_manage_cities, handle_adm_add_city, handle_adm_edit_city,
    handle_adm_delete_city, handle_adm_manage_districts,
    handle_adm_manage_districts_city, handle_adm_add_district,
    handle_adm_edit_district, handle_adm_remove_district,
    handle_adm_manage_products, handle_adm_manage_products_city,
    handle_adm_manage_products_dist, handle_adm_manage_products_type,
    handle_adm_delete_prod, handle_adm_manage_types, handle_adm_add_type,
    handle_adm_delete_type, handle_adm_manage_discounts,
    handle_adm_toggle_discount, handle_adm_delete_discount,
    handle_adm_add_discount_start, handle_adm_use_generated_code,
    handle_adm_set_discount_type, handle_adm_set_media,
    handle_confirm_yes, handle_adm_add_city_message,
    handle_adm_add_district_message, handle_adm_edit_district_message,
    handle_adm_edit_city_message, handle_adm_custom_size_message,
    handle_adm_price_message, handle_adm_drop_details_message,
    handle_adm_bot_media_message, handle_adm_add_type_message,
    handle_adm_discount_code_message, handle_adm_discount_value_message,
    handle_adm_broadcast_start, handle_adm_broadcast_message,
    handle_confirm_broadcast, handle_cancel_broadcast,
    handle_adm_manage_reviews, handle_adm_delete_review_confirm # Add missing admin handlers
)
from viewer_admin import (
    handle_viewer_admin_menu,
    handle_viewer_added_products,
    handle_viewer_view_product_media
)
from payment import (
    handle_confirm_pay,
    close_cryptopay_client, # Keep this (though it does nothing now)
    handle_select_refill_crypto,
    process_successful_refill # Import needed for webhook
    # REMOVED: handle_check_cryptobot_payment
)
from stock import handle_view_stock

# --- Logging Setup ---
# Configure logging (consider moving basicConfig to utils if used across modules)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING) # Silence PTB's http library logs
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Silence Flask logs unless WARNING
logger = logging.getLogger(__name__)

# Allow nested asyncio loops for running Flask in a thread with PTB's loop
nest_asyncio.apply()

# --- Flask App for Webhook ---
flask_app = Flask(__name__)
# Global variable to hold the Telegram Application instance for the webhook handler
telegram_app: Application | None = None

@flask_app.route('/webhook', methods=['POST'])
def nowpayments_webhook():
    """Handles incoming webhook notifications from NOWPayments."""
    global telegram_app # Use the global application instance
    if not request.is_json:
        logger.warning("Webhook received non-JSON request.")
        return Response("Request must be JSON", status=415)

    data = request.json
    # Use json.dumps for potentially large/nested data logging
    logger.info(f"Received NOWPayments IPN: {json.dumps(data, indent=2)}")

    if not telegram_app:
        logger.error("Telegram application not initialized in webhook handler.")
        return Response("Internal Server Error: Bot not ready", status=500)

    payment_id = data.get('payment_id')
    payment_status = data.get('payment_status')
    paid_amount_str = data.get('actually_paid') # Amount paid in crypto (as string or number)
    pay_currency = data.get('pay_currency') # Crypto currency code

    if not payment_id:
        logger.warning("Webhook received without payment_id.")
        return Response("Missing payment_id", status=400)

    # Process based on status (adjust statuses based on NOWPayments docs if needed)
    if payment_status == 'finished':
        if paid_amount_str is None or not pay_currency:
             logger.error(f"Webhook 'finished' status missing paid amount or currency for {payment_id}.")
             return Response("Missing amount or currency for finished payment", status=400)

        try:
            paid_amount_dec = Decimal(str(paid_amount_str))
        except (InvalidOperation, ValueError):
             logger.error(f"Invalid paid amount format '{paid_amount_str}' for payment {payment_id}.")
             return Response("Invalid amount format", status=400)

        # Fetch pending deposit info (runs sync DB query)
        pending_info = get_pending_deposit(str(payment_id))

        if pending_info:
            user_id = pending_info['user_id']
            # Convert paid crypto amount to EUR
            price_eur = get_currency_to_eur_price(pay_currency) # Uses function from utils

            if price_eur and price_eur > 0:
                eur_equiv_dec = (paid_amount_dec * price_eur)
                # Apply fee adjustment (example: 1.5% fee)
                credited_eur_amount = eur_equiv_dec * (Decimal('1.0') - FEE_ADJUSTMENT)
                credited_eur_amount = credited_eur_amount.quantize(Decimal("0.01")) # Round to 2 decimal places

                # Ensure credited amount is positive after fees
                if credited_eur_amount <= Decimal('0.0'):
                     logger.warning(f"Credited EUR amount for payment {payment_id} is zero or negative after fee adjustment. Original EUR equivalent: {eur_equiv_dec:.4f}. Not crediting balance.")
                     # Remove pending record even if not crediting, as payment finished
                     remove_pending_deposit(str(payment_id))
                     return Response(status=200) # Acknowledge webhook, but take no crediting action

                logger.info(f"Processing successful deposit {payment_id} for user {user_id}. Paid: {paid_amount_dec} {pay_currency}, EUR equivalent: {eur_equiv_dec:.4f}, Credited (after fees): {credited_eur_amount:.2f}")

                # Run the balance update and notification in the bot's event loop
                async def process_in_loop():
                    # Create a minimal context for process_successful_refill
                    # We don't have the full chat/user context here, only user_id
                    bot_context = ContextTypes.DEFAULT_TYPE(application=telegram_app, user_id=user_id)
                    # Manually set bot instance and potentially fetch user_data if needed for language
                    bot_context._bot = telegram_app.bot
                    # bot_context.user_data = await telegram_app.persistence.get_user_data() # Example if needed

                    success = await process_successful_refill(user_id, credited_eur_amount, str(payment_id), bot_context)
                    if success:
                        # Remove pending record ONLY after successful processing
                        remove_pending_deposit(str(payment_id))
                    else:
                        logger.error(f"Failed to process successful refill in DB for payment {payment_id}, user {user_id}. Pending record NOT removed.")
                        # Consider sending an alert to admin if DB update fails

                try:
                    # Get the running event loop from the main thread where PTB runs
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                         asyncio.run_coroutine_threadsafe(process_in_loop(), loop)
                    else:
                         logger.error("Cannot schedule webhook processing: Event loop is not running.")
                except RuntimeError as e:
                    logger.error(f"Error obtaining event loop for webhook processing: {e}")
                except Exception as e:
                    logger.error(f"Error scheduling webhook processing task: {e}")

            else:
                logger.error(f"Could not get EUR price for {pay_currency} to process payment {payment_id}. Deposit pending manual review.")
                # Optionally notify admin
        else:
            logger.warning(f"Received 'finished' webhook for unknown or already processed payment_id: {payment_id}")

    elif payment_status in ['failed', 'refunded', 'expired']:
        logger.warning(f"Received non-successful payment status '{payment_status}' for payment_id: {payment_id}. Removing pending record.")
        # Clean up pending record (runs sync DB query)
        remove_pending_deposit(str(payment_id))
        # Optionally notify user or admin about the failure/expiry

    else:
         logger.info(f"Received webhook with status '{payment_status}' for payment_id: {payment_id}. No action taken.")

    return Response(status=200) # Always return 200 OK to NOWPayments

# --- Callback Data Parsing Decorator ---
# Maps callback data prefixes to handler functions
CALLBACK_HANDLERS = {
    # User Handlers
    "start": start, "back_start": handle_back_start, "shop": handle_shop,
    "city": handle_city_selection, "dist": handle_district_selection,
    "type": handle_type_selection, "product": handle_product_selection,
    "add": handle_add_to_basket, "view_basket": handle_view_basket,
    "clear_basket": handle_clear_basket, "remove": handle_remove_from_basket,
    "profile": handle_profile, "language": handle_language_selection,
    "price_list": handle_price_list, "price_list_city": handle_price_list_city,
    "reviews": handle_reviews_menu, "leave_review": handle_leave_review,
    "view_reviews": handle_view_reviews, "leave_review_now": handle_leave_review_now,
    "refill": handle_refill,
    "view_history": handle_view_history,
    "apply_discount_start": apply_discount_start, "remove_discount": remove_discount,
    # Payment Handlers
    "confirm_pay": handle_confirm_pay,
    "select_refill_crypto": handle_select_refill_crypto,
    # Primary Admin Handlers
    "admin_menu": handle_admin_menu,
    "sales_analytics_menu": handle_sales_analytics_menu,
    "sales_dashboard": handle_sales_dashboard,
    "sales_select_period": handle_sales_select_period,
    "sales_run": handle_sales_run,
    "adm_city": handle_adm_city, "adm_dist": handle_adm_dist,
    "adm_type": handle_adm_type, "adm_add": handle_adm_add,
    "adm_size": handle_adm_size, "adm_custom_size": handle_adm_custom_size,
    "confirm_add_drop": handle_confirm_add_drop, "cancel_add": cancel_add,
    "adm_manage_products": handle_adm_manage_products,
    "adm_manage_products_city": handle_adm_manage_products_city,
    "adm_manage_products_dist": handle_adm_manage_products_dist,
    "adm_manage_products_type": handle_adm_manage_products_type,
    "adm_delete_prod": handle_adm_delete_prod,
    "adm_manage_discounts": handle_adm_manage_discounts,
    "adm_toggle_discount": handle_adm_toggle_discount,
    "adm_delete_discount": handle_adm_delete_discount,
    "adm_add_discount_start": handle_adm_add_discount_start,
    "adm_use_generated_code": handle_adm_use_generated_code,
    "adm_set_discount_type": handle_adm_set_discount_type,
    "adm_manage_districts": handle_adm_manage_districts,
    "adm_manage_districts_city": handle_adm_manage_districts_city,
    "adm_add_district": handle_adm_add_district,
    "adm_edit_district": handle_adm_edit_district,
    "adm_remove_district": handle_adm_remove_district,
    "adm_manage_cities": handle_adm_manage_cities,
    "adm_add_city": handle_adm_add_city,
    "adm_edit_city": handle_adm_edit_city,
    "adm_delete_city": handle_adm_delete_city,
    "adm_manage_types": handle_adm_manage_types,
    "adm_add_type": handle_adm_add_type,
    "adm_delete_type": handle_adm_delete_type,
    "adm_manage_reviews": handle_adm_manage_reviews,
    "adm_delete_review_confirm": handle_adm_delete_review_confirm,
    "adm_broadcast_start": handle_adm_broadcast_start,
    "confirm_broadcast": handle_confirm_broadcast,
    "cancel_broadcast": handle_cancel_broadcast,
    "adm_set_media": handle_adm_set_media,
    "confirm_yes": handle_confirm_yes, # Generic confirmation
    # Stock Handler (Shared)
    "view_stock": handle_view_stock,
    # Viewer Admin Handlers
    "viewer_admin_menu": handle_viewer_admin_menu,
    "viewer_added_products": handle_viewer_added_products,
    "viewer_view_product_media": handle_viewer_view_product_media,
}

def callback_query_router(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query and query.data:
            # Ensure query is answered to remove loading state
            try: await query.answer()
            except Exception as e: logger.debug(f"Minor error answering CBQ {query.data}: {e}")

            parts = query.data.split('|', 1) # Split only once
            command = parts[0]
            params_str = parts[1] if len(parts) > 1 else ""
            params = params_str.split('|') if params_str else []

            target_func = CALLBACK_HANDLERS.get(command)
            if target_func and asyncio.iscoroutinefunction(target_func):
                try:
                    await target_func(update, context, params)
                except Exception as e:
                     logger.error(f"Error executing callback handler '{command}': {e}", exc_info=True)
                     # Optionally notify user of error
                     try: await query.edit_message_text("An error occurred processing your request.")
                     except Exception: pass # Ignore if edit fails
            else:
                logger.warning(f"No async handler found for callback command: {command}")
                try: await query.answer("Unknown action.", show_alert=True)
                except Exception as e: logger.error(f"Error answering unknown CBQ {command}: {e}")
        elif query:
            logger.warning("Callback query handler received update without data.")
            try: await query.answer()
            except Exception as e: logger.error(f"Error answering CBQ without data: {e}")
        else:
            # This case should ideally not happen with CallbackQueryHandler
            logger.warning("Callback query handler received update without query object.")
    return wrapper

@callback_query_router
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function body is now effectively empty as the decorator handles everything.
    # It's kept for clarity as the entry point for the handler.
    pass

# --- Central Message Handler (for states) ---
# Maps state names to handler functions
STATE_HANDLERS = {
    # User States
    'awaiting_review': handle_leave_review_message,
    'awaiting_user_discount_code': handle_user_discount_code_message,
    'awaiting_refill_amount': handle_refill_amount_message,
    # REMOVED: 'awaiting_withdrawal_details': process_withdrawal_message,
    # Admin States
    'awaiting_new_city_name': handle_adm_add_city_message,
    'awaiting_edit_city_name': handle_adm_edit_city_message,
    'awaiting_new_district_name': handle_adm_add_district_message,
    'awaiting_edit_district_name': handle_adm_edit_district_message,
    'awaiting_new_type_name': handle_adm_add_type_message,
    'awaiting_custom_size': handle_adm_custom_size_message,
    'awaiting_price': handle_adm_price_message,
    'awaiting_drop_details': handle_adm_drop_details_message,
    'awaiting_bot_media': handle_adm_bot_media_message,
    'awaiting_discount_code': handle_adm_discount_code_message,
    'awaiting_discount_value': handle_adm_discount_value_message,
    'awaiting_broadcast_message': handle_adm_broadcast_message,
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text/media messages based on user state."""
    if not update.message or not update.effective_user: return # Ignore updates without messages or users

    user_id = update.effective_user.id
    state = context.user_data.get('state')
    logger.debug(f"Message received from user {user_id}, state: {state}")

    handler_func = STATE_HANDLERS.get(state)
    if handler_func and asyncio.iscoroutinefunction(handler_func):
        try:
            await handler_func(update, context)
        except Exception as e:
            logger.error(f"Error executing state handler '{state}' for user {user_id}: {e}", exc_info=True)
            # Optionally clear state on error?
            # context.user_data.pop('state', None)
            try: # Notify user of error
                await update.message.reply_text("An error occurred processing your input. Please try again or /start.")
            except Exception: pass
    else:
        # Ignore messages if user is not in a specific state needing input
        logger.debug(f"Ignoring message from user {user_id} in state: {state or 'None'}")
        # You might want to add a default reply here if users send unexpected messages
        # e.g., await update.message.reply_text("I didn't understand that. Use /start to see options.")

# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors and sends a generic error message to the user."""
    # Log the error before handling it
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    # Extract chat_id if possible
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id

    # Prepare error message (consider translation if needed)
    error_message = "An internal error occurred. Please try again later or contact support."
    if SUPPORT_USERNAME:
        error_message += f" (@{SUPPORT_USERNAME})"

    # Handle specific error types if necessary (optional)
    if isinstance(context.error, telegram_error.BadRequest):
        # e.g., Message not modified, chat not found, etc.
        # Might not always need to notify the user for these.
        logger.warning(f"BadRequest error: {context.error}")
        # Avoid sending message for "message not modified"
        if "message is not modified" in str(context.error).lower():
            return
    elif isinstance(context.error, telegram_error.Unauthorized):
        logger.warning(f"Unauthorized error (bot blocked?): {context.error}")
        # Can't send a message if blocked
        return
    elif isinstance(context.error, telegram_error.NetworkError):
        logger.warning(f"Network error: {context.error}")
        # Maybe just log network errors unless they persist

    # Send user-friendly error message if possible
    if chat_id:
        try:
            # Use the reliable send function
            await send_message_with_retry(context.bot, chat_id=chat_id, text=error_message, parse_mode=None)
        except Exception as e:
            logger.error(f"Failed even to send error message to user {chat_id}: {e}")

# --- Bot Setup Functions ---
async def post_init(application: Application) -> None:
    """Actions to perform after the Application is built and initialized."""
    logger.info("Running post_init setup...")

    # Set bot commands
    logger.info("Setting bot commands...")
    commands = [
        BotCommand("start", "Start the bot / Main menu"),
    ]
    # Add admin command only if ADMIN_ID is set
    if ADMIN_ID is not None:
        commands.append(BotCommand("admin", "Access admin panel (Admin only)"))

    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set successfully.")
    except Exception as e:
        logger.error(f"Failed to set bot commands: {e}")

    # Set up background job for clearing expired baskets
    if BASKET_TIMEOUT > 0:
        job_queue = application.job_queue
        if job_queue:
            # Check if job already exists
            current_jobs = job_queue.get_jobs_by_name("clear_baskets")
            if not current_jobs:
                logger.info(f"Setting up background job 'clear_baskets' (interval: 60s)...")
                job_queue.run_repeating(
                    clear_expired_baskets_job,
                    interval=timedelta(seconds=60),
                    first=timedelta(seconds=10), # Start after 10 seconds
                    name="clear_baskets"
                )
                logger.info("Background job 'clear_baskets' scheduled.")
            else:
                 logger.info("Background job 'clear_baskets' already exists.")
        else:
            logger.warning("Job Queue is not available. Expired baskets will not be cleared automatically.")
    else:
        logger.warning("BASKET_TIMEOUT is not positive. Skipping background job setup for expired baskets.")

    logger.info("Post_init finished.")

async def post_shutdown(application: Application) -> None:
    """Actions to perform during graceful shutdown."""
    logger.info("Running post_shutdown cleanup...")
    # Placeholder for closing any external clients (like CryptoPay if it were used)
    await close_cryptopay_client()
    logger.info("Post_shutdown finished.")

async def clear_expired_baskets_job(context: ContextTypes.DEFAULT_TYPE):
    """Job function to clear expired baskets for all users."""
    logger.debug("Running background job: clear_expired_baskets_job")
    try:
         # Run the synchronous DB operation in a thread to avoid blocking asyncio loop
         await asyncio.to_thread(clear_all_expired_baskets)
         logger.info("Background job: Cleared expired baskets successfully.")
    except Exception as e:
          logger.error(f"Error in background job clear_expired_baskets_job: {e}", exc_info=True)

# --- Main Function (Webhook setup) ---
def main() -> None:
    """Configures and runs the bot with webhook."""
    global telegram_app # Make application instance accessible to webhook

    logger.info("Starting bot application...")

    # --- Essential Configuration Checks ---
    if not TOKEN:
        logger.critical("CRITICAL: Telegram Bot TOKEN is not set in environment variables.")
        raise SystemExit("Missing TOKEN")
    if not WEBHOOK_URL:
        logger.critical("CRITICAL: WEBHOOK_URL is not set. Cannot run in webhook mode.")
        raise SystemExit("Missing WEBHOOK_URL for webhook mode.")
    if not NOWPAYMENTS_API_KEY:
        logger.warning("NOWPAYMENTS_API_KEY is not set. Deposit functionality will fail.")
    if ADMIN_ID is None:
         logger.warning("ADMIN_ID is not set. Admin functionality will be limited.")
    # --- End Checks ---

    # Default settings for handlers
    defaults = Defaults(parse_mode=None, block=False) # Default to no parse mode

    # Build the PTB application
    # No persistence needed for webhook mode usually, state managed in memory/context
    application = (
        ApplicationBuilder().token(TOKEN).defaults(defaults)
        .post_init(post_init).post_shutdown(post_shutdown).build()
    )
    telegram_app = application # Store globally for Flask webhook

    # --- Add Handlers ---
    # Commands
    application.add_handler(CommandHandler("start", start))
    if ADMIN_ID is not None: # Only add admin command if ADMIN_ID is set
        application.add_handler(CommandHandler("admin", handle_admin_menu))

    # Callback Queries (Routed by decorator)
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Message Handler (for states)
    application.add_handler(MessageHandler(
        # Handle text, photos, videos, GIFs (animations), documents (for potential GIFs)
        # Exclude commands unless explicitly needed for a state
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        handle_message
    ))

    # Error Handler (must be last)
    application.add_error_handler(error_handler)
    # --- End Handlers ---

    # --- Webhook and Flask Setup ---
    # Get the asyncio event loop
    loop = asyncio.get_event_loop()

    async def setup_telegram_webhook():
        logger.info("Initializing Telegram Application and setting webhook...")
        try:
            await application.initialize() # Initialize bot internals

            # Construct the full webhook URL for Telegram
            # Note: Render might provide the base URL via WEBHOOK_URL, ensure it ends with '/' if needed
            # Or construct it fully if WEBHOOK_URL is just the domain.
            # Example: Assuming WEBHOOK_URL is 'https://yourapp.onrender.com'
            base_url = WEBHOOK_URL.rstrip('/')
            telegram_hook_path = f"/telegram/{TOKEN}" # Path PTB listens on internally
            full_webhook_url = f"{base_url}{telegram_hook_path}"

            webhook_info = await application.bot.get_webhook_info()

            if webhook_info.url != full_webhook_url:
                logger.info(f"Setting webhook URL to: {full_webhook_url}")
                if not await application.bot.set_webhook(url=full_webhook_url, allowed_updates=Update.ALL_TYPES):
                    logger.error("Failed to set webhook.")
                    return False # Indicate failure
                else:
                    logger.info("Webhook set successfully.")
                    # Verify again
                    webhook_info = await application.bot.get_webhook_info()
                    if webhook_info.url == full_webhook_url:
                         logger.info("Webhook verified successfully.")
                    else:
                         logger.error(f"Webhook verification failed. Expected {full_webhook_url}, got {webhook_info.url}")
                         return False
            else:
                logger.info(f"Webhook already set to: {webhook_info.url}")

            await application.start() # Start polling internally for webhook updates
            logger.info("Telegram Application started and listening for webhook updates.")
            return True # Indicate success

        except Exception as e:
            logger.critical(f"Failed during Telegram webhook setup: {e}", exc_info=True)
            return False # Indicate failure

    # Run the async setup in the event loop
    setup_success = loop.run_until_complete(setup_telegram_webhook())
    if not setup_success:
         logger.critical("Exiting due to failed Telegram setup.")
         raise SystemExit("Telegram setup failed")


    # Define the Telegram webhook endpoint for Flask
    # The path MUST match where PTB expects updates internally
    @flask_app.route(f'/telegram/{TOKEN}', methods=['POST'])
    def telegram_webhook_handler():
        """Handles incoming updates from Telegram forwarded by the webserver."""
        if not telegram_app:
             logger.error("Flask received update, but Telegram app not ready.")
             return Response(status=500)
        try:
            update_data = request.get_json(force=True)
            update = Update.de_json(update_data, telegram_app.bot)
            # Schedule the update processing in PTB's event loop
            asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), loop)
            return Response(status=200) # OK back to Telegram
        except Exception as e:
            logger.error(f"Error processing incoming Telegram update in Flask: {e}", exc_info=True)
            return Response(status=500) # Internal server error

    # Start Flask server in a separate thread
    # Use Render's PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host='0.0.0.0', port=port),
        daemon=True # Allows main thread to exit even if Flask is running
    )
    flask_thread.start()
    logger.info(f"Flask webhook server started on port {port}. Listening for Telegram at /telegram/{TOKEN} and NOWPayments at /webhook.")

    # Keep the main thread alive (PTB runs in the asyncio loop managed by Application)
    try:
        # loop.run_forever() # Alternative: run the main loop forever
        while True:
             time.sleep(3600) # Sleep for a long time, main work is in asyncio loop / Flask thread
             logger.debug("Main thread still alive...") # Optional debug message
    except (KeyboardInterrupt, SystemExit) as e:
        logger.info(f"Shutdown signal ({type(e).__name__}) received. Stopping application...")
        # Signal PTB to stop gracefully
        asyncio.run_coroutine_threadsafe(application.stop(), loop)
        # Wait for the loop to finish processing stop tasks
        # loop.run_until_complete(loop.shutdown_asyncgens()) # Optional cleanup
        # loop.close() # Close the loop
        logger.info("Application stop signal sent.")
        # Flask thread is daemon, should exit automatically

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped manually (KeyboardInterrupt).")
    except SystemExit as e:
        logger.critical(f"Bot stopped due to SystemExit: {e}")
    except Exception as e:
        logger.critical(f"Critical error in main execution: {e}", exc_info=True)
