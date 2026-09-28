#!/data/data/com.termux/files/usr/bin/bash
# ===================================================
# TeraStream Pro - 1-Click Mobile Localhost Runner
# Designed for Termux on Android
# ===================================================

cd "$(dirname "$0")"

echo "=========================================="
echo "  🚀 Starting TeraStream Pro on Mobile    "
echo "=========================================="

# 1. Update pkg & install build tools needed for Termux
echo "📦 Ensuring system tools & Python are installed..."
pkg update -y
pkg install -y python git clang libffi openssl postgresql

# 2. Upgrade pip and wheel
pip install --upgrade pip setuptools wheel

# 3. Install core packages without breaking on C-extensions
echo "📦 Installing Python packages..."
pip install flask requests pycryptodome yt-dlp beautifulsoup4 telethon gunicorn

# 4. Optional postgres driver (ignore failure if Termux uses SQLite)
pip install psycopg2-binary || true

echo ""
echo "=========================================="
echo "  ✅ Server Starting!"
echo "  🌐 Localhost URL: http://localhost:8080"
echo "  🛡️ Admin Panel:   http://localhost:8080/admin"
echo "=========================================="
echo ""

# Automatically open browser on phone after 2 seconds
if command -v termux-open-url &> /dev/null; then
    (sleep 2 && termux-open-url http://localhost:8080) &
elif command -v am &> /dev/null; then
    (sleep 2 && am start -a android.intent.action.VIEW -d "http://localhost:8080" > /dev/null 2>&1) &
fi

# 5. Start the server
python app.py
