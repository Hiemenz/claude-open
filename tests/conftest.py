import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# bot.py reads these at import time, so they must exist before the first import.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_ID", "111")
os.environ.setdefault("ALLOWED_CHANNEL_ID", "222")
