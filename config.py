import os

# Render provides PORT environment variable
PORT = int(os.environ.get('PORT', 8080))

# Other configs...
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]

# Shortener - Check both env and database
SHORTENER_API_URL = os.getenv("SHORTENER_API_URL", "")
SHORTENER_API_KEY = os.getenv("SHORTENER_API_KEY", "")

# Database
DATABASE_PATH = "bot_database.db"

# Verification Settings
VERIFICATION_MIN_TIME = 35  # seconds
SESSION_DURATION = 6 * 60 * 60  # 6 hours in seconds

# Time format
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"