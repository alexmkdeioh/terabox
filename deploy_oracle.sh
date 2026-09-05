#!/bin/bash
# ==============================================================================
# TeraStream Pro - Oracle Cloud Always-Free One-Click Setup Script
# Works on Ubuntu 22.04 / 24.04 LTS (x86_64 or ARM64 Ampere)
# ==============================================================================

set -e

echo "=========================================================="
echo "🚀 Starting TeraStream Pro Setup on Oracle Cloud Free Tier"
echo "=========================================================="

# 1. Update and install required system packages
echo "📦 Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl ufw nginx

# 2. Configure Firewall (Oracle Cloud Ubuntu iptables fix)
echo "🛡️ Configuring firewall rules for HTTP, HTTPS & Port 8080..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || true

# Also configure UFW if active
sudo ufw allow 22/tcp || true
sudo ufw allow 80/tcp || true
sudo ufw allow 443/tcp || true
sudo ufw allow 8080/tcp || true

# 3. Create deployment directory
APP_DIR="/opt/terastream"
echo "📂 Setting up app directory at $APP_DIR..."
sudo mkdir -p $APP_DIR
sudo chown -R $USER:$USER $APP_DIR

if [ ! -d "$APP_DIR/.git" ]; then
    echo "📥 Cloning repository..."
    git clone https://github.com/offertricksandpromocode-dotcom/terastream-pro.git $APP_DIR
else
    echo "🔄 Updating existing repository..."
    cd $APP_DIR
    git pull origin main
fi

cd $APP_DIR

# 4. Set up Python virtual environment
echo "🐍 Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Create Systemd Service (24/7/365 Auto-Restart)
echo "⚙️ Creating Systemd 24/7 service (terastream.service)..."
sudo bash -c "cat <<EOF > /etc/systemd/system/terastream.service
[Unit]
Description=TeraStream Pro Native Service
After=network.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn app:app --workers 2 --threads 8 --worker-class gthread --timeout 120 --bind 0.0.0.0:8080
Restart=always
RestartSec=3
Environment=PORT=8080
Environment=SECRET_KEY=terastream_secure_session_key_2026

[Install]
WantedBy=multi-user.target
EOF"

# 6. Configure Nginx Reverse Proxy (Forward Port 80 to Port 8080)
echo "🌐 Configuring Nginx reverse proxy on Port 80..."
sudo bash -c "cat <<EOF > /etc/nginx/sites-available/terastream
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 500M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \"upgrade\";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
EOF"

sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/terastream /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx

# 7. Start and Enable TeraStream Service
echo "🚀 Starting TeraStream Pro service..."
sudo systemctl daemon-reload
sudo systemctl enable terastream
sudo systemctl restart terastream

PUBLIC_IP=$(curl -s https://api.ipify.org || echo "YOUR_SERVER_IP")

echo ""
echo "=========================================================="
echo "🎉 SUCCESS! TeraStream Pro is running on Oracle Cloud!"
echo "👉 Direct Access: http://$PUBLIC_IP"
echo "👉 Port 8080:     http://$PUBLIC_IP:8080"
echo "👉 Health Check:  http://$PUBLIC_IP/ping"
echo "=========================================================="
echo "Useful Commands:"
echo "  Check Status: sudo systemctl status terastream"
echo "  Restart App:  sudo systemctl restart terastream"
echo "  View Logs:    sudo journalctl -u terastream -f"
echo "=========================================================="
