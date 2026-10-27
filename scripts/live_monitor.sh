#!/bin/bash
# 订单实时监控脚本 - 每 30 秒检查一次，有变化就记录
# 用法：./live_monitor.sh

set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$PROJECT/logs"
DATE=$(date +"%Y%m%d")
STATE_FILE="$LOGDIR/order_state_$DATE.json"
EVENT_LOG="$LOGDIR/order_monitor_$DATE.log"

mkdir -p "$LOGDIR"

echo "[$(date)] 开始监控订单..." | tee -a "$EVENT_LOG"

# 初始化状态文件
if [[ ! -f "$STATE_FILE" ]]; then
    echo '{}' > "$STATE_FILE"
fi

while true; do
    # 检查是否美股交易时间
    HOUR_ET=$(TZ="America/New_York" date +%H)
    MIN_ET=$(TZ="America/New_York" date +%M)
    DOW_ET=$(TZ="America/New_York" date +%u)  # 1=Monday, 7=Sunday
    
    # 美股交易时间：9:30-16:00 ET, 周一到周五
    if [[ "$DOW_ET" -ge 6 ]] || [[ "$HOUR_ET" -lt 9 ]] || [[ "$HOUR_ET" -gt 16 ]]; then
        echo "[$(date)] 美股已收盘 (ET: ${HOUR_ET}:${MIN_ET})" | tee -a "$EVENT_LOG"
        break
    fi
    
    # 获取订单状态
    cd "$PROJECT"
    python3 -c "
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from core.tiger_client import TigerClient

tiger = TigerClient()
tiger.connect()
orders = tiger.get_orders()

result = []
for o in orders:
    symbol = getattr(getattr(o, 'contract', None), 'symbol', '') or getattr(o, 'symbol', '')
    status = str(getattr(o, 'status', ''))
    filled = int(getattr(o, 'filled', 0) or 0)
    qty = int(getattr(o, 'quantity', 0) or 0)
    price = float(getattr(o, 'limit_price', 0) or 0)
    avg_fill = float(getattr(o, 'avg_fill_price', 0) or 0)
    order_id = getattr(o, 'id', 0)
    result.append({
        'symbol': symbol, 'status': status, 'filled': filled,
        'quantity': qty, 'limit_price': price, 'avg_fill_price': avg_fill,
        'order_id': order_id
    })
print(json.dumps(result, sort_keys=True))
" 2>/dev/null > "$LOGDIR/current_orders.json" || true
    
    # 比较状态变化
    if [[ -f "$STATE_FILE" ]] && [[ -f "$LOGDIR/current_orders.json" ]]; then
        if ! diff -q "$STATE_FILE" "$LOGDIR/current_orders.json" > /dev/null 2>&1; then
            echo "[$(date)] 📢 订单状态变化!" | tee -a "$EVENT_LOG"
            python3 -c "
import json
with open('$LOGDIR/current_orders.json') as f:
    orders = json.load(f)
for o in orders:
    status = o['status']
    if 'FILLED' in status:
        print(f\"✅ {o['symbol']}: {o['filled']}股 @ \${o['avg_fill_price']:.2f}\")
    elif 'HELD' in status or 'SUBMITTED' in status:
        print(f\"⏳ {o['symbol']}: {o['filled']}/{o['quantity']}股 @ \${o['limit_price']:.2f}\")
    elif 'CANCEL' in status:
        print(f\"❌ {o['symbol']}: 已取消\")
    elif 'REJECT' in status:
        print(f\"🚫 {o['symbol']}: 已拒单\")
" | tee -a "$EVENT_LOG"
            cp "$LOGDIR/current_orders.json" "$STATE_FILE"
        fi
    else
        cp "$LOGDIR/current_orders.json" "$STATE_FILE" 2>/dev/null || true
    fi
    
    sleep 30
done

echo "[$(date)] 监控结束" | tee -a "$EVENT_LOG"

# 生成收盘报告
echo "" | tee -a "$EVENT_LOG"
echo "[$(date)] 📊 收盘报告:" | tee -a "$EVENT_LOG"
python3 -c "
import json
from pathlib import Path
from core.tiger_client import TigerClient

tiger = TigerClient()
tiger.connect()
orders = tiger.get_orders()

filled = []
pending = []
for o in orders:
    symbol = getattr(getattr(o, 'contract', None), 'symbol', '') or getattr(o, 'symbol', '')
    status = str(getattr(o, 'status', ''))
    filled_qty = int(getattr(o, 'filled', 0) or 0)
    qty = int(getattr(o, 'quantity', 0) or 0)
    avg_fill = float(getattr(o, 'avg_fill_price', 0) or 0)
    
    if 'FILLED' in status:
        filled.append(f'{symbol}: {filled_qty}股 @ \${avg_fill:.2f}')
    else:
        pending.append(f'{symbol}: {filled_qty}/{qty}股 ({status})')

print('✅ 已成交:')
for f in filled:
    print(f'  {f}')
print()
print('⏳ 等待中:')
for p in pending:
    print(f'  {p}')
" | tee -a "$EVENT_LOG"
