import sys
import os

# Insert current directory into path
sys.path.insert(0, os.path.dirname(__file__))

# Lifetime Cloud Database fallback if not set in cPanel environment
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_0JyzHhFLNGs1@ep-gentle-bonus-ae1k78jw-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
)

from app import app as application
