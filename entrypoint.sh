#!/bin/bash

# 如果没有在环境变量里设置 CRON_INTERVAL，默认 3600 秒（1小时）运行一次
INTERVAL=${CRON_INTERVAL:-3600}

echo "Starting Edookit summary service. Checking every $INTERVAL seconds."

while true; do
    echo "======================================"
    echo "[$(date)] Running gather_updates.py..."
    
    # 执行脚本
    python3 /app/gather_updates.py /data/cookies.json
    
    echo "[$(date)] Run finished. Sleeping for $INTERVAL seconds..."
    sleep $INTERVAL
done
