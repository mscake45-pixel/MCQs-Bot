import asyncio
import logging
import pickle
import os
import aiosqlite
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from config import BOT_TOKEN, DB_PATH
from database.db import init_db, write_queue, db_pool
from middlewares.throttling import ThrottlingMiddleware
from handlers import user, admin, test_management, quiz
from shared.state import active_lobbies

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger('apscheduler').setLevel(logging.DEBUG)  # <-- added for debugging
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

async def recover_scheduled_jobs(scheduler):
    """Reload scheduled jobs from database on bot restart (bot is global now)"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, chat_id, test_id, run_date, interval, shuffle FROM scheduled_tests"
            ) as cursor:
                jobs = await cursor.fetchall()
        
        recovered_count = 0
        for job_id, chat_id, test_id, run_date, interval, shuffle in jobs:
            try:
                # run_date is stored as UTC ISO string (naive but in UTC)
                run_datetime = datetime.fromisoformat(run_date)
                # Compare with current UTC time
                now_utc = datetime.now(timezone.utc)
                # Make run_datetime timezone-aware (UTC) for comparison
                if run_datetime.replace(tzinfo=timezone.utc) > now_utc:
                    from handlers.quiz import trigger_scheduled_test
                    
                    scheduler.add_job(
                        trigger_scheduled_test,
                        'date',
                        run_date=run_datetime,   # naive, but scheduler timezone is UTC
                        args=[chat_id, test_id, interval, bool(shuffle), job_id],
                        id=f"test_job_{job_id}",
                        replace_existing=True,
                        misfire_grace_time=60    # allow 60 seconds delay
                    )
                    recovered_count += 1
                    logger.info(f"Recovered scheduled job {job_id} for test {test_id}")
                else:
                    await db.execute("DELETE FROM scheduled_tests WHERE id = ?", (job_id,))
                    await db.commit()
                    logger.info(f"Deleted expired schedule {job_id}")
            except Exception as e:
                logger.error(f"Failed to recover job {job_id}: {e}")
        
        logger.info(f"Recovered {recovered_count} scheduled jobs")
    except Exception as e:
        logger.error(f"Failed to recover scheduled jobs: {e}")

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
    
    jobstores = {
        'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
    }
    # Set scheduler timezone to UTC explicitly
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone="UTC")
    scheduler.start()
    
    # Inject bot instances BEFORE recovering scheduled jobs
    test_management.bot = bot
    quiz.bot_instance = bot
    
    # Recover scheduled jobs from database (no bot argument needed)
    await recover_scheduled_jobs(scheduler)
    
    # Start stale lobby cleanup task
    asyncio.create_task(cleanup_stale_lobbies())
    
    @dp.update.middleware()
    async def scheduler_middleware(handler, event, data):
        data["scheduler"] = scheduler
        return await handler(event, data)
    
    dp.message.middleware(ThrottlingMiddleware(limit=1.0))
    
    dp.include_router(admin.router)
    dp.include_router(test_management.router)
    dp.include_router(quiz.router)
    dp.include_router(user.router)
    
    logger.info("Starting Telegram Bot...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        # Pass scheduler as a keyword argument so it's available in all handlers
        await dp.start_polling(bot, scheduler=scheduler)
    finally:
        logger.info("Shutting down...")
        scheduler.shutdown()
        await write_queue.stop()
        await db_pool.close_all()
        save_lobbies()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
