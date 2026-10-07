import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# slackbot.py reads these at import time.
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")
os.environ.setdefault("SLACK_ALLOWED_USER_ID", "U0123456789")
os.environ.setdefault("SLACK_ALLOWED_CHANNEL_ID", "C0123456789")
