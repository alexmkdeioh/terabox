import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_0JyzHhFLNGs1@ep-gentle-bonus-ae1k78jw-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
)

from app import app
