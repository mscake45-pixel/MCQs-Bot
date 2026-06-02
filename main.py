import asyncio
import logging
import pickle
import os
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher
from config import BOT_TOKEN, DB_PATH
from database.db import init_db, write_queue, db_pool
from middlewares.throttling import ThrottlingMiddleware
from handlers import user, admin, test_management, quiz
from shared.state import active_lobbies

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

LOBBY_BACKUP_FILE = "lobbies_backup.pkl"
STALE_LOBBY_TIMEOUT = 3600  # 1 hour

def load_lobbies():
    """Load lobbies from backup and clean stale entries"""
    if os.path.exists(LOBBY_BACKUP_FILE):
        try:
            with open(LOBBY_BACKUP_FILE, 'rb') as f:
                saved_lobbies = pickle.load(f)
                
            current_time = datetime.now().timestamp()
            cleaned_count = 0
            
            for chat_id, lobby_data in list(saved_lobbies.items()):
                lobby_age = current_time - lobby_data.get('created_at', current_time)
                if lobby_age > STALE_LOBBY_TIMEOUT:
                    del saved_lobbies[chat_id]
                    cleaned_count += 1
                else:
                    lobby_data["lock"] = asyncio.Lock()
                    lobby_data["starting_lock"] = asyncio.Lock()
                    active_lobbies[chat_id] = lobby_data
            
            if cleaned_count > 0:
                logger.info(f"Cleaned {cleaned_count} stale lobbies from backup")
            
            os.remove(LOBBY_BACKUP_FILE)
            logger.info(f"Recovered {len(active_lobbies)} active lobbies from backup.")
        except Exception as e:
            logger.error(f"Failed to load lobbies backup: {e}")

def save_lobbies():
    """Save active lobbies with timestamp for cleanup"""
    if active_lobbies:
        try:
            safe_lobbies = {}
            for chat_id, lobby in active_lobbies.items():
                safe_lobby = lobby.copy()
                safe_lobby.pop("lock", None)
                safe_lobby.pop("starting_lock", None)
                safe_lobby.pop("timeout_task", None)
                safe_lobby.pop("task", None)
                safe_lobby["created_at"] = lobby.get('created_at', datetime.now().timestamp())
                safe_lobbies[chat_id] = safe_lobby
                
            with open(LOBBY_BACKUP_FILE, 'wb') as f:
                pickle.dump(safe_lobbies, f)
            logger.info(f"Saved {len(active_lobbies)} active lobbies to backup.")
        except Exception as e:
            logger.error(f"Failed to save lobbies backup: {e}")

async def cleanup_stale_lobbies():
    """Periodic cleanup of stale lobbies (only those in 'waiting' state)"""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        try:
            current_time = datetime.now().timestamp()
            stale_chats = []
            
            for chat_id, lobby in active_lobbies.items():
                created_at = lobby.get('created_at', current_time)
                # Only clean up lobbies that are still waiting and have exceeded timeout
                if lobby.get('status') == 'waiting' and (current_time - created_at) > STALE_LOBBY_TIMEOUT:
                    stale_chats.append(chat_id)
            
            for chat_id in stale_chats:
                logger.info(f"Removing stale lobby for chat {chat_id}")
                # Cancel timeout task if exists
                lobby = active_lobbies.get(chat_id)
                if lobby and "timeout_task" in lobby:
                    lobby["timeout_task"].cancel()
                del active_lobbies[chat_id]
        except Exception as e:
            logger.error(f"Stale lobby cleanup error: {e}")

async def main():
    logger.info("Initializing database...")
    await init_db()
    
    # Start write queue for database operations
    await write_queue.start()
    
    # Load lobbies with timestamp-based cleanup
    load_lobbies()
    
    # Inject bot instances
    test_management.bot = bot
    quiz.bot_instance = bot
    
    # Start stale lobby cleanup task
    asyncio.create_task(cleanup_stale_lobbies())
    
    dp.message.middleware(ThrottlingMiddleware(limit=1.0))
    
    dp.include_router(admin.router)
    dp.include_router(test_management.router)
    dp.include_router(quiz.router)
    dp.include_router(user.router)
    
    logger.info("Starting Telegram Bot...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down...")
        await write_queue.stop()
        await db_pool.close_all()
        save_lobbies()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
