#!/data/data/com.termux/files/usr/bin/bash
# ===================================================
# TeraStream Pro - 1-Click Mobile Localhost Runner
# Designed for Termux on Android
# ===================================================

cd "$(dirname "$0")"

echo "=========================================="
echo "  🚀 Starting TeraStream Pro on Mobile    "
echo "=========================================="

# 1. Check & install Python & Git if missing
if ! command -v python &> /dev/null; then
    echo "📦 First-time setup: Installing Python & system tools..."
    pkg update -y
    pkg install -y python git clang libffi openssl
fi

# 2. Check & install pip dependencies if missing
if ! python -c "import flask, requests" &> /dev/null; then
    echo "📦 Installing required Python libraries..."
    pip install --upgrade pip
    pip install -r requirements.txt
fi

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

# 3. Start the server
python app.py
