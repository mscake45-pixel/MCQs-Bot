import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Create logger for config module
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    raise ValueError("CRITICAL: BOT_TOKEN is missing or invalid in .env file.")

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
TIMEZONE = os.getenv("TIMEZONE", "UTC")
DB_PATH = os.path.join("database", "quiz_database.db")

def validate_config():
    """Validate all configuration before starting"""
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required")
    
    if DB_PATH and not os.access(os.path.dirname(DB_PATH) or ".", os.W_OK):
        raise PermissionError(f"Cannot write to database directory: {DB_PATH}")
    
    logger.info("Configuration validated successfully")

# Run validation
validate_config()