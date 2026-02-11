#!/bin/bash
# Local deployment script for CreAIta development
# Run this on your local machine for testing

set -e

echo "🚀 Deploying CreAIta (Local Mode)..."

# Navigate to project directory
cd "$(dirname "$0")/.."

# Pull latest changes (if in git repo)
if [ -d ".git" ]; then
    echo "📥 Pulling latest changes..."
    git pull || echo "⚠️  Git pull failed or no remote configured"
fi

# Update dependencies
echo "📦 Installing dependencies..."
poetry install

# Run database migrations (if any)
echo "🗄️  Setting up database..."
poetry run python3 -c "from app.database import init_db; init_db()"

# Kill any existing uvicorn processes
echo "🔄 Stopping existing processes..."
pkill -f "uvicorn app.main:app" || echo "No existing processes found"

# Wait for processes to stop
sleep 2

# Start the application in background
echo "🚀 Starting CreAIta..."
nohup poetry run uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/creaita.log 2>&1 &
PID=$!

# Wait for service to start
echo "⏳ Waiting for service to start..."
sleep 3

# Check if process is still running
if ps -p $PID > /dev/null; then
    echo "✅ CreAIta is running! (PID: $PID)"
    echo "📊 Log file: /tmp/creaita.log"
    echo "🌐 Visit: http://localhost:8000"
    echo ""
    echo "To view logs: tail -f /tmp/creaita.log"
    echo "To stop: pkill -f 'uvicorn app.main:app'"
else
    echo "❌ CreAIta failed to start!"
    echo "📋 Last 20 lines of log:"
    tail -20 /tmp/creaita.log
    exit 1
fi

echo "✨ Deployment complete!"
