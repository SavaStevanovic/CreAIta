#!/bin/bash
# Health check script for CreAIta
# Can be used with monitoring tools or cron

HEALTH_ENDPOINT="http://127.0.0.1:8000/api/streams"
TIMEOUT=10

# Check if service is running
if ! systemctl is-active --quiet creaita; then
    echo "ERROR: CreAIta service is not running"
    exit 1
fi

# Check if endpoint responds
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time $TIMEOUT $HEALTH_ENDPOINT)

if [ "$HTTP_CODE" = "200" ]; then
    echo "OK: CreAIta is healthy (HTTP $HTTP_CODE)"
    exit 0
else
    echo "ERROR: CreAIta is unhealthy (HTTP $HTTP_CODE)"
    exit 1
fi
