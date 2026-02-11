#!/bin/bash
# Quick deployment script for CreAIta
# Run this on your server after initial setup

set -e

echo "🚀 Deploying CreAIta..."

# Pull latest changes
echo "📥 Pulling latest changes..."
git pull

# Update dependencies
echo "📦 Installing dependencies..."
poetry install --without dev

# Run database migrations (if any)
echo "🗄️  Setting up database..."
poetry run python3 -c "from app.database import init_db; init_db()"

# Restart service
echo "🔄 Restarting service..."
sudo systemctl restart creaita

# Wait for service to start
sleep 3

# Check if service is running
if systemctl is-active --quiet creaita; then
    echo "✅ CreAIta is running!"
    echo "📊 Status:"
    sudo systemctl status creaita --no-pager -l
else
    echo "❌ CreAIta failed to start!"
    echo "📋 Logs:"
    sudo journalctl -u creaita -n 50 --no-pager
    exit 1
fi

# Reload nginx
echo "🔄 Reloading Nginx..."
sudo nginx -t && sudo systemctl reload nginx

echo "✨ Deployment complete!"
echo "🌐 Visit: https://$(hostname -f)"
