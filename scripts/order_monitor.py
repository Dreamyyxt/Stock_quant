#!/usr/bin/env python3
"""
订单实时监控 - 每 30 秒检查订单状态，有变化就输出报告
用法：python order_monitor.py
"""

import sys
import os
import time
import json
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.tiger_client import TigerClient
import pytz

US_EASTERN = pytz.timezone('America/New_York')

STATE_FILE = PROJECT_ROOT / 'logs' / 'order_monitor_state.json'
EVENT_LOG = PROJECT_ROOT / 'logs' / 'order_monitor_events.jsonl'

def is_market_hours():
    """检查是否在美股交易时间内"""
    now_us = datetime.now(US_EASTERN)
    if now_us.weekday() >= 5:  # 周末
        return False
    # 交易时间 9:30 - 16:00 ET
    market_open = now_us.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_us <= market_close

def get_orders():
    """获取订单列表"""
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
            'order_id': order_id,
            'symbol': symbol,
            'status': status,
            'filled': filled,
            'quantity': qty,
            'limit_price': price,
            'avg_fill_price': avg_fill,
        })
    
    return sorted(result, key=lambda x: x['symbol'])

def load_state():
    """加载上次状态"""
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

def save_state(state):
    """保存状态"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def log_event(event_type, data):
    """记录事件到日志"""
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    event = {
        'ts': datetime.now().isoformat(),
        'type': event_type,
        **data
    }
    with open(EVENT_LOG, 'a') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')

def compare_orders(old, new):
    """比较订单状态变化"""
    if old is None:
        return {'new': new, 'changed': [], 'filled': [], 'cancelled': []}
    
    old_by_id = {o['order_id']: o for o in old}
    new_by_id = {o['order_id']: o for o in new}
    
    changed = []
    filled = []
    cancelled = []
    
    for order_id, new_o in new_by_id.items():
        old_o = old_by_id.get(order_id)
        if old_o is None:
            changed.append({'old': None, 'new': new_o})
        else:
            if old_o['status'] != new_o['status'] or old_o['filled'] != new_o['filled']:
                changed.append({'old': old_o, 'new': new_o})
                
                # 检查是否新成交
                if 'FILLED' in new_o['status'] and ('FILLED' not in old_o['status'] or new_o['filled'] > old_o['filled']):
                    filled.append(new_o)
                elif 'CANCEL' in new_o['status'] and 'CANCEL' not in old_o['status']:
                    cancelled.append(new_o)
    
    return {'new': new, 'changed': changed, 'filled': filled, 'cancelled': cancelled}

def format_update(changes):
    """格式化更新报告"""
    lines = []
    
    if changes['filled']:
        lines.append("✅ **成交**")
        for o in changes['filled']:
            lines.append(f"  {o['symbol']}: {o['filled']}股 @ ${o['avg_fill_price']:.2f}")
    
    if changes['cancelled']:
        lines.append("❌ **取消**")
        for o in changes['cancelled']:
            lines.append(f"  {o['symbol']}: {o['filled']}/{o['quantity']}股")
    
    other_changes = [c for c in changes['changed'] 
                     if c['new'] not in changes['filled'] and c['new'] not in changes['cancelled']]
    if other_changes:
        lines.append("📋 **状态更新**")
        for c in other_changes:
            new_o = c['new']
            old_status = c['old']['status'] if c['old'] else 'NEW'
            lines.append(f"  {new_o['symbol']}: {old_status} → {new_o['status']} ({new_o['filled']}/{new_o['quantity']}股)")
    
    return '\n'.join(lines) if lines else None

def format_full_report(orders):
    """生成完整报告"""
    filled = [o for o in orders if 'FILLED' in o['status']]
    pending = [o for o in orders if 'FILLED' not in o['status']]
    
    lines = ["📊 **订单汇总**"]
    
    if filled:
        lines.append("\n✅ **已成交**")
        for o in filled:
            lines.append(f"  {o['symbol']}: {o['filled']}股 @ ${o['avg_fill_price']:.2f}")
    
    if pending:
        lines.append("\n⏳ **等待中**")
        for o in pending:
            lines.append(f"  {o['symbol']}: {o['filled']}/{o['quantity']}股 @ ${o['limit_price']:.2f} [{o['status']}]")
    
    return '\n'.join(lines)

def main():
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 启动订单监控 (间隔：{interval}秒)")
    print(f"当前美东时间：{datetime.now(US_EASTERN):%Y-%m-%d %H:%M:%S %Z}")
    
    last_state = load_state()
    
    while True:
        # 检查是否交易时间
        if not is_market_hours():
            now_us = datetime.now(US_EASTERN)
            # 如果是周末或非交易时间，等到下一个交易日
            if now_us.weekday() >= 5:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 周末，停止监控")
            else:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 非交易时间 (ET: {now_us:%H:%M})，停止监控")
            break
        
        try:
            orders = get_orders()
            changes = compare_orders(last_state, orders)
            
            # 有变化就报告
            if changes['changed']:
                report = format_update(changes)
                if report:
                    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {report}")
                    log_event('order_update', changes)
            
            # 保存状态
            save_state(orders)
            last_state = orders
            
        except Exception as e:
            print(f"[ERROR] {datetime.now()}: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(interval)
    
    # 收盘报告
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 🌅 美股收盘，生成最终报告:")
    try:
        orders = get_orders()
        print(format_full_report(orders))
        log_event('market_close', {'orders': orders})
    except Exception as e:
        print(f"[ERROR] 生成收盘报告失败：{e}")

if __name__ == '__main__':
    main()
