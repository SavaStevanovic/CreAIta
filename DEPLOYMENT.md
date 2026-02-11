# Production Deployment Guide

Complete guide for deploying CreAIta to a production server with Nginx, SSL, and systemd.

## Prerequisites

- Ubuntu 22.04+ or Debian 11+ server
- Domain name pointed to your server's IP
- Root or sudo access
- FFmpeg, streamlink, and yt-dlp installed

## 1. Server Setup

### Update system
```bash
sudo apt update && sudo apt upgrade -y
```

### Install dependencies
```bash
sudo apt install -y nginx python3.11 python3.11-venv git ffmpeg curl
```

### Install Poetry
```bash
curl -sSL https://install.python-poetry.org | python3 -
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### Install streamlink and yt-dlp
```bash
sudo apt install -y streamlink
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

## 2. Create Deployment User

```bash
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG www-data deploy
sudo mkdir -p /var/log/creaita
sudo chown deploy:www-data /var/log/creaita
```

## 3. Deploy Application

### Clone and setup
```bash
sudo su - deploy
cd ~
git clone https://github.com/yourusername/CreAIta.git creaita
cd creaita

# Install dependencies
poetry install --without dev

# Create required directories
mkdir -p streams
chmod 755 streams

# Test the application
poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000
# Press Ctrl+C after verifying it starts
```

### Install Gunicorn for production
```bash
poetry add gunicorn
```

## 4. Setup Systemd Service

```bash
# Exit from deploy user
exit

# Copy service file
sudo cp /home/deploy/creaita/systemd/creaita.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable and start service
sudo systemctl enable creaita
sudo systemctl start creaita

# Check status
sudo systemctl status creaita

# View logs
sudo journalctl -u creaita -f
```

## 5. Configure Nginx

### Copy nginx configuration
```bash
sudo cp /home/deploy/creaita/nginx/creaita.conf /etc/nginx/sites-available/

# Edit the file and replace your-domain.com with your actual domain
sudo nano /etc/nginx/sites-available/creaita.conf
```

**Important**: Update these lines:
```nginx
server_name your-domain.com www.your-domain.com;
ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;
ssl_trusted_certificate /etc/letsencrypt/live/your-domain.com/chain.pem;
```

### Create symlink (don't enable yet - need SSL first)
```bash
sudo ln -s /etc/nginx/sites-available/creaita.conf /etc/nginx/sites-enabled/
```

## 6. Setup SSL with Let's Encrypt

### Install Certbot
```bash
sudo apt install -y certbot python3-certbot-nginx
```

### Get SSL certificate
```bash
# Temporarily create a simple nginx config for certbot
sudo tee /etc/nginx/sites-available/creaita-temp.conf > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name your-domain.com www.your-domain.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
}
EOF

# Disable main config temporarily
sudo rm /etc/nginx/sites-enabled/creaita.conf
sudo ln -s /etc/nginx/sites-available/creaita-temp.conf /etc/nginx/sites-enabled/

# Test and reload nginx
sudo nginx -t
sudo systemctl reload nginx

# Get certificate (replace with your domain and email)
sudo certbot certonly --webroot -w /var/www/html \
    -d your-domain.com -d www.your-domain.com \
    --email your-email@example.com --agree-tos --no-eff-email

# Enable main config
sudo rm /etc/nginx/sites-enabled/creaita-temp.conf
sudo ln -s /etc/nginx/sites-available/creaita.conf /etc/nginx/sites-enabled/

# Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

### Setup auto-renewal
```bash
# Test renewal
sudo certbot renew --dry-run

# Certbot automatically sets up a systemd timer for renewal
# Check it with:
sudo systemctl status certbot.timer
```

## 7. Firewall Configuration

```bash
# Allow SSH (if not already allowed)
sudo ufw allow OpenSSH

# Allow HTTP and HTTPS
sudo ufw allow 'Nginx Full'

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status
```

## 8. Optimization & Monitoring

### Nginx tuning
```bash
sudo nano /etc/nginx/nginx.conf
```

Add/update these settings:
```nginx
worker_processes auto;
worker_rlimit_nofile 65535;

events {
    worker_connections 4096;
    use epoll;
    multi_accept on;
}

http {
    # Buffer sizes
    client_body_buffer_size 128k;
    client_max_body_size 10m;
    client_header_buffer_size 1k;
    large_client_header_buffers 4 8k;

    # Timeouts
    client_body_timeout 12;
    client_header_timeout 12;
    keepalive_timeout 65;
    send_timeout 10;

    # File descriptors
    open_file_cache max=10000 inactive=20s;
    open_file_cache_valid 30s;
    open_file_cache_min_uses 2;
    open_file_cache_errors on;
}
```

### Monitor logs
```bash
# Application logs
sudo journalctl -u creaita -f

# Nginx access logs
sudo tail -f /var/log/nginx/creaita_access.log

# Nginx error logs
sudo tail -f /var/log/nginx/creaita_error.log

# Application logs
sudo tail -f /var/log/creaita/access.log
sudo tail -f /var/log/creaita/error.log
```

### Setup log rotation
```bash
sudo tee /etc/logrotate.d/creaita > /dev/null <<'EOF'
/var/log/creaita/*.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 deploy www-data
    sharedscripts
    postrotate
        systemctl reload creaita > /dev/null
    endscript
}
EOF
```

## 9. Database Cleanup

Setup periodic cleanup of old user sessions:

```bash
# Create cleanup script
sudo tee /home/deploy/creaita/scripts/cleanup.sh > /dev/null <<'EOF'
#!/bin/bash
cd /home/deploy/creaita
source .venv/bin/activate
python3 -c "from app.database import cleanup_old_sessions; cleanup_old_sessions(30)"
EOF

sudo chmod +x /home/deploy/creaita/scripts/cleanup.sh
sudo chown deploy:deploy /home/deploy/creaita/scripts/cleanup.sh

# Add to crontab (runs daily at 3 AM)
sudo -u deploy crontab -l 2>/dev/null | { cat; echo "0 3 * * * /home/deploy/creaita/scripts/cleanup.sh"; } | sudo -u deploy crontab -
```

## 10. Updates & Maintenance

### Update application
```bash
sudo su - deploy
cd ~/creaita

# Pull latest changes
git pull

# Update dependencies
poetry install --without dev

# Restart service
exit
sudo systemctl restart creaita
```

### Restart services
```bash
# Restart application
sudo systemctl restart creaita

# Reload nginx
sudo systemctl reload nginx

# View status
sudo systemctl status creaita
sudo systemctl status nginx
```

### Troubleshooting

#### Application won't start
```bash
# Check logs
sudo journalctl -u creaita -n 100 --no-pager

# Check if port is in use
sudo ss -tlnp | grep 8000

# Test manually
sudo su - deploy
cd ~/creaita
poetry run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

#### Nginx errors
```bash
# Test configuration
sudo nginx -t

# Check error logs
sudo tail -50 /var/log/nginx/error.log

# Check if upstream is running
curl http://127.0.0.1:8000
```

#### Permission issues
```bash
# Fix ownership
sudo chown -R deploy:www-data /home/deploy/creaita
sudo chmod -R 755 /home/deploy/creaita
sudo chmod -R 775 /home/deploy/creaita/streams

# Fix log directories
sudo chown -R deploy:www-data /var/log/creaita
sudo chmod 755 /var/log/creaita
```

## 11. Security Best Practices

### 1. Keep system updated
```bash
sudo apt update && sudo apt upgrade -y
```

### 2. Configure fail2ban
```bash
sudo apt install -y fail2ban

sudo tee /etc/fail2ban/jail.local > /dev/null <<'EOF'
[nginx-limit-req]
enabled = true
port = http,https
logpath = /var/log/nginx/*error.log
maxretry = 5
findtime = 600
bantime = 3600
EOF

sudo systemctl restart fail2ban
```

### 3. Enable automatic security updates
```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

### 4. Disable root SSH login
```bash
sudo nano /etc/ssh/sshd_config
# Set: PermitRootLogin no
sudo systemctl restart sshd
```

## 12. Monitoring & Alerts

### Setup basic monitoring with monit
```bash
sudo apt install -y monit

sudo tee /etc/monit/conf.d/creaita > /dev/null <<'EOF'
check process creaita with pidfile /run/creaita.pid
    start program = "/bin/systemctl start creaita"
    stop program = "/bin/systemctl stop creaita"
    if failed host 127.0.0.1 port 8000 protocol http
        request "/api/streams"
        with timeout 30 seconds
        for 2 cycles
    then restart
    if 3 restarts within 5 cycles then alert

check process nginx with pidfile /var/run/nginx.pid
    start program = "/bin/systemctl start nginx"
    stop program = "/bin/systemctl stop nginx"
    if failed host 127.0.0.1 port 443 protocol https
        with timeout 15 seconds
        for 2 cycles
    then restart
EOF

sudo systemctl enable monit
sudo systemctl start monit
```

## Performance Testing

After deployment, test your setup:

```bash
# Test HTTPS
curl -I https://your-domain.com

# Test SSL configuration
ssllabs.com/ssltest/analyze.html?d=your-domain.com

# Load testing (install apache2-utils)
sudo apt install -y apache2-utils
ab -n 1000 -c 10 https://your-domain.com/
```

## Backup Strategy

```bash
# Create backup script
sudo tee /home/deploy/backup.sh > /dev/null <<'EOF'
#!/bin/bash
BACKUP_DIR="/home/deploy/backups"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Backup database
cp /home/deploy/creaita/streams/creaita.db "$BACKUP_DIR/creaita_$DATE.db"

# Keep only last 7 days
find $BACKUP_DIR -name "creaita_*.db" -mtime +7 -delete
EOF

sudo chmod +x /home/deploy/backup.sh

# Schedule daily backups at 2 AM
(sudo -u deploy crontab -l 2>/dev/null; echo "0 2 * * * /home/deploy/backup.sh") | sudo -u deploy crontab -
```

Your CreAIta application is now running in production! 🚀

Access it at: `https://your-domain.com`
