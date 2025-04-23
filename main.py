# --- START OF FILE main.py ---

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
    handle_refill_amount_message, validate_discount_code,
    # Add withdrawal handlers if they exist in user.py (assuming removed for now)
    # handle_withdrawal_request, process_withdrawal_message
)
from admin import (
    handle_admin_menu, handle_sales_analytics_menu, handle_sales_dashboard,
    handle_sales_select_period, handle_sales_run, handle_adm_city, handle_adm_dist,
    handle_adm_type, handle_adm_add, handle_adm_size, handle_adm_custom_size,
    handle_confirm_add_drop, cancel_add, handle_adm_manage_cities, handle_adm_add_city,
    handle_adm_edit_city, handle_adm_delete_city, handle_adm_manage_districts,
    handle_adm_manage_districts_city, handle_adm_add_district, handle_adm_edit_district,
    handle_adm_remove_district, handle_adm_manage_products, handle_adm_manage_products_city,
    handle_adm_manage_products_dist, handle_adm_manage_products_type, handle_adm_delete_prod,
    handle_adm_manage_types, handle_adm_add_type, handle_adm_delete_type,
    handle_adm_manage_discounts, handle_adm_toggle_discount, handle_adm_delete_discount,
    handle_adm_add_discount_start, handle_adm_use_generated_code, handle_adm_set_discount_type,
    handle_adm_set_media,
    handle_adm_broadcast_start, handle_cancel_broadcast,
    handle_confirm_broadcast, handle_adm_broadcast_message,
    handle_confirm_yes,
    handle_adm_add_city_message,
    handle_adm_add_district_message, handle_adm_edit_district_message,
    handle_adm_edit_city_message, handle_adm_custom_size_message, handle_adm_price_message,
    handle_adm_drop_details_message, handle_adm_bot_media_message, handle_adm_add_type_message,
    process_discount_code_input, handle_adm_discount_code_message, handle_adm_discount_value_message,
    handle_adm_manage_reviews, handle_adm_delete_review_confirm
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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Silence Flask logs unless WARNING
logger = logging.getLogger(__name__)

# Allow nested asyncio loops for running Flask in a thread
nest_asyncio.apply()

# --- Flask App for Webhook ---
flask_app = Flask(__name__)
# Global variable to hold the Telegram Application instance for the webhook handler
telegram_app: Application | None = None

@flask_app.route('/webhook', methods=['POST'])
def nowpayments_webhook():
    """Handles incoming webhook notifications from NOWPayments."""
    global telegram_app # Use the global application instance
    data = request.json
    logger.info(f"Received NOWPayments IPN: {json.dumps(data, indent=2)}")

    if not telegram_app:
        logger.error("Telegram application not initialized in webhook handler.")
        return Response(status=500) # Internal Server Error

    payment_id = data.get('payment_id')
    payment_status = data.get('payment_status')
    paid_amount = data.get('actually_paid') # Amount paid in crypto
    pay_currency = data.get('pay_currency')

    if not payment_id:
        logger.warning("Webhook received without payment_id.")
        return Response(status=400) # Bad Request

    # Process based on status (adjust statuses based on NOWPayments docs)
    if payment_status == 'finished':
        if paid_amount is None or pay_currency is None:
             logger.error(f"Webhook 'finished' status missing paid amount or currency for {payment_id}.")
             return Response(status=400)

        pending_info = get_pending_deposit(str(payment_id))
        if pending_info:
            user_id = pending_info['user_id']
            # Convert paid crypto amount to EUR
            price_eur = get_currency_to_eur_price(pay_currency)
            if price_eur and price_eur > 0:
                paid_amount_dec = Decimal(str(paid_amount))
                eur_equiv_dec = (paid_amount_dec * price_eur)
                # Apply fee adjustment (optional)
                credited_eur_amount = eur_equiv_dec * (Decimal('1.0') - FEE_ADJUSTMENT)
                credited_eur_amount = credited_eur_amount.quantize(Decimal("0.01")) # Round to 2 decimal places

                logger.info(f"Processing successful deposit {payment_id} for user {user_id}. Paid: {paid_amount} {pay_currency}, EUR equivalent: {eur_equiv_dec:.4f}, Credited (after fees): {credited_eur_amount:.2f}")

                # Run the balance update and notification in the bot's event loop
                async def process_in_loop():
                    # Create a dummy context (or fetch user_data if needed for language)
                    # Note: We can't easily get user_data here without user interaction.
                    # We'll pass the bot instance directly.
                    dummy_context = ContextTypes.DEFAULT_TYPE(application=telegram_app, chat_id=user_id, user_id=user_id)
                    dummy_context._bot = telegram_app.bot # Manually set bot instance

                    success = await process_successful_refill(user_id, credited_eur_amount, str(payment_id), dummy_context)
                    if success:
                        remove_pending_deposit(str(payment_id)) # Remove after successful processing
                    else:
                        logger.error(f"Failed to process successful refill in DB for payment {payment_id}, user {user_id}")
                        # Consider sending an alert to admin if DB update fails

                try:
                    # Ensure the bot's event loop is running before scheduling
                    loop = asyncio.get_running_loop()
                    asyncio.run_coroutine_threadsafe(process_in_loop(), loop)
                except RuntimeError:
                    logger.error("No running event loop found to schedule webhook processing.")
                except Exception as e:
                    logger.error(f"Error scheduling webhook processing task: {e}")

            else:
                logger.error(f"Could not get EUR price for {pay_currency} to process payment {payment_id}. Deposit pending manual review.")
                # Optionally notify admin
        else:
            logger.warning(f"Received 'finished' webhook for unknown or already processed payment_id: {payment_id}")

    elif payment_status in ['failed', 'refunded', 'expired']:
        logger.warning(f"Received non-successful payment status '{payment_status}' for payment_id: {payment_id}. Removing pending record.")
        remove_pending_deposit(str(payment_id)) # Clean up pending record
        # Optionally notify user or admin about the failure

    else:
         logger.info(f"Received webhook with status '{payment_status}' for payment_id: {payment_id}. No action taken.")

    return Response(status=200) # Always return 200 to NOWPayments

# --- Callback Data Parsing Decorator (Keep as is) ---
def callback_query_router(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query and query.data:
            parts = query.data.split('|')
            command = parts[0]; params = parts[1:]
            # --- KNOWN_HANDLERS (Remove check_crypto_payment) ---
            KNOWN_HANDLERS = {
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
                "withdraw": user.handle_withdrawal_request, # Add withdrawal handler if needed
                # Payment Handlers
                "confirm_pay": handle_confirm_pay,
                # REMOVED: "check_crypto_payment": ...,
                "select_refill_crypto": handle_select_refill_crypto,
                # Primary Admin Handlers
                "admin_menu": handle_admin_menu,
                # ... (keep all other admin handlers) ...
                "adm_delete_review_confirm": handle_adm_delete_review_confirm,
                # Stock Handler
                "view_stock": handle_view_stock,
                # Viewer Admin Handlers
                "viewer_admin_menu": handle_viewer_admin_menu,
                "viewer_added_products": handle_viewer_added_products,
                "viewer_view_product_media": handle_viewer_view_product_media
            }
            target_func = KNOWN_HANDLERS.get(command)
            if target_func and asyncio.iscoroutinefunction(target_func):
                await target_func(update, context, params)
            else:
                logger.warning(f"No handler for callback command: {command}")
                try: await query.answer("Unknown action.", show_alert=True)
                except Exception as e: logger.error(f"Error answering unknown CBQ {command}: {e}")
        # ... (keep rest of decorator) ...
        elif query:
            logger.warning("Callback query handler received update without data.")
            try: await query.answer()
            except Exception as e: logger.error(f"Error answering CBQ without data: {e}")
        else:
            logger.warning("Callback query handler received update without query object.")
    return wrapper

@callback_query_router
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass # Decorator handles routing

# --- Central Message Handler (for states) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user: return
    user_id = update.effective_user.id
    state = context.user_data.get('state')
    logger.debug(f"Message received from user {user_id}, state: {state}")

    STATE_HANDLERS = {
        'awaiting_review': handle_leave_review_message,
        'awaiting_user_discount_code': handle_user_discount_code_message,
        'awaiting_refill_amount': handle_refill_amount_message,
        # 'awaiting_withdrawal_details': user.process_withdrawal_message, # Add if withdrawal exists
        # --- Admin States ---
        # ... (keep all admin states) ...
        'awaiting_discount_value': handle_adm_discount_value_message,
    }

    handler_func = STATE_HANDLERS.get(state)
    if handler_func:
        await handler_func(update, context)
    else:
        logger.debug(f"Ignoring message from user {user_id} in state: {state or 'None'}")

# --- Error Handler (Keep as is) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (implementation unchanged) ...
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if chat_id:
        error_message = "An internal error occurred. Please try again later or contact support."
        if isinstance(context.error, telegram_error.BadRequest):
            logger.warning(f"Telegram API BadRequest: {context.error}")
            error_message = "An error occurred communicating with Telegram. Please try again."
        elif isinstance(context.error, telegram_error.NetworkError):
            logger.warning(f"Telegram API NetworkError: {context.error}")
            error_message = "A network error occurred. Please check your connection and try again."
        elif isinstance(context.error, sqlite3.Error):
            logger.error(f"Database error during update handling: {context.error}", exc_info=True)
        else:
             logger.exception("An unexpected error occurred during update handling.")
             error_message = "An unexpected error occurred. Please contact support."
        try:
            await context.bot.send_message(chat_id=chat_id, text=error_message, parse_mode=None)
        except Exception as e:
            logger.error(f"Failed to send error message to user {chat_id}: {e}")

# --- Bot Setup Functions (Keep post_init, post_shutdown, clear_expired_baskets_job as is) ---
async def post_init(application: Application) -> None:
    # ... (implementation unchanged) ...
    logger.info("Running post_init setup...")
    logger.info("Setting bot commands...")
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot / Main menu"),
        BotCommand("admin", "Access admin panel (Admin only)"),
    ])
    if BASKET_TIMEOUT > 0:
        job_queue = application.job_queue
        if job_queue:
            logger.info(f"Setting up background job for expired baskets (interval: 60s)...")
            job_queue.run_repeating(clear_expired_baskets_job, interval=timedelta(seconds=60), first=timedelta(seconds=10), name="clear_baskets")
            logger.info("Background job setup complete.")
        else: logger.warning("Job Queue is not available.")
    else: logger.warning("BASKET_TIMEOUT is not positive. Skipping background job setup.")
    logger.info("Post_init finished.")

async def post_shutdown(application: Application) -> None:
    # ... (implementation unchanged) ...
    logger.info("Running post_shutdown cleanup...")
    await close_cryptopay_client() # Still call the placeholder
    logger.info("Post_shutdown finished.")

async def clear_expired_baskets_job(context: ContextTypes.DEFAULT_TYPE):
    # ... (implementation unchanged) ...
    logger.debug("Running background job: clear_expired_baskets_job")
    try:
         await asyncio.to_thread(clear_all_expired_baskets)
         logger.info("Background job: Cleared expired baskets.")
    except Exception as e:
          logger.error(f"Error in background job clear_expired_baskets_job: {e}", exc_info=True)


# --- Main Function (Modified for Webhook) ---
def main() -> None:
    """Configures and runs the bot with webhook."""
    global telegram_app # Make application instance accessible to webhook

    logger.info("Starting bot...")
    # Config validation happens in utils.py now
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL not set. Cannot run in webhook mode.")
        raise SystemExit("WEBHOOK_URL is required for this setup.")

    defaults = Defaults(parse_mode=None, block=False)
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .defaults(defaults)
        .post_init(post_init) # Keep post_init for commands/jobs
        .post_shutdown(post_shutdown)
        .build()
    )
    telegram_app = application # Store instance globally for webhook

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", handle_admin_menu))
    # Add other command handlers if any (e.g., owner commands)

    # Callback Query Handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    # Message Handler
    application.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        handle_message
    ))
    # Error Handler
    application.add_error_handler(error_handler)

    # --- Webhook and Flask Setup ---
    loop = asyncio.get_event_loop()

    async def setup_telegram():
        logger.info("Initializing Telegram Application...")
        await application.initialize()
        webhook_info = await application.bot.get_webhook_info()
        target_webhook = f"{WEBHOOK_URL}/telegram/{TOKEN}" # Using a unique path per bot
        if webhook_info.url != target_webhook:
            logger.info(f"Setting webhook to {target_webhook}")
            await application.bot.set_webhook(url=target_webhook, allowed_updates=Update.ALL_TYPES)
        else:
            logger.info(f"Webhook already set to {webhook_info.url}")
        await application.start()
        logger.info("Telegram Application started.")

    # Run Telegram setup in the main event loop
    loop.run_until_complete(setup_telegram())

    # Define Flask webhook endpoint for Telegram
    @flask_app.route(f'/telegram/{TOKEN}', methods=['POST'])
    def telegram_webhook_handler():
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, application.bot)
        # Process update in the bot's event loop
        asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
        return Response(status=200)

    # Run Flask app in a separate thread
    port = int(os.environ.get("PORT", 8080)) # Render typically uses PORT env var
    flask_thread = threading.Thread(target=lambda: flask_app.run(host='0.0.0.0', port=port), daemon=True)
    flask_thread.start()
    logger.info(f"Flask webhook server started on port {port}.")

    # Keep the main thread running (important for signal handling and jobs)
    try:
        # If using application.run_polling() originally, replace with this loop
        # If using run_webhook() originally, this structure might be similar
        while True:
            time.sleep(1) # Keep main thread alive
            # You might add checks here for thread health if needed
    except (KeyboardInterrupt, SystemExit) as e:
        logger.info(f"Shutdown signal received: {e}. Stopping application...")
        asyncio.run_coroutine_threadsafe(application.stop(), loop)
        # Event loop is stopped externally or by application.stop() completing
        logger.info("Application stopped.")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except SystemExit as e:
         logger.critical(f"SystemExit called: {e}")
    except Exception as e:
         logger.critical(f"Critical error in main execution: {e}", exc_info=True)

# --- END OF FILE main.py ---