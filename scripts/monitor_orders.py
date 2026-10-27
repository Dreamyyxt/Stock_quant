#!/usr/bin/env python3
"""
Order Monitor - 持续监控订单状态并推送通知
用法：python monitor_orders.py [--interval 60] [--channel feishu]
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tiger_client import TigerClient
from core.event_logger import EventLogger

# 美东时区
import pytz
US_EASTERN = pytz.timezone('America/New_York')

def is_market_open():
    """检查美股是否开盘"""
    now_us = datetime.now(US_EASTERN)
    # 美股交易时间：9:30 - 16:00 ET，周一到周五
    if now_us.weekday() >= 5:  # 周末
        return False
    market_open = now_us.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_us <= market_close

def get_market_close_time():
    """返回今日美东收盘时间"""
    now_us = datetime.now(US_EASTERN)
    close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
    return close

def check_orders(verbose=False):
    """检查订单状态，返回变化"""
    tiger = TigerClient()
    tiger.connect()
    orders = tiger.get_orders()
    
    results = {
        'filled': [],
        'pending': [],
        'cancelled': [],
        'rejected': [],
    }
    
    for o in orders:
        symbol = getattr(getattr(o, 'contract', None), 'symbol', '') or getattr(o, 'symbol', '')
        status = str(getattr(o, 'status', ''))
        filled = int(getattr(o, 'filled', 0) or 0)
        qty = int(getattr(o, 'quantity', 0) or 0)
        price = float(getattr(o, 'limit_price', 0) or 0)
        avg_fill = float(getattr(o, 'avg_fill_price', 0) or 0)
        order_id = getattr(o, 'id', 0)
        
        order_info = {
            'symbol': symbol,
            'status': status,
            'filled': filled,
            'quantity': qty,
            'limit_price': price,
            'avg_fill_price': avg_fill,
            'order_id': order_id,
        }
        
        if 'FILLED' in status.upper():
            results['filled'].append(order_info)
        elif 'CANCEL' in status.upper():
            results['cancelled'].append(order_info)
        elif 'REJECT' in status.upper():
            results['rejected'].append(order_info)
        else:
            results['pending'].append(order_info)
    
    return results

def format_report(results):
    """格式化报告"""
    lines = []
    
    if results['filled']:
        lines.append("✅ **已成交**")
        for o in results['filled']:
            lines.append(f"  {o['symbol']}: {o['filled']}股 @ ${o['avg_fill_price']:.2f}")
    
    if results['pending']:
        lines.append("⏳ **等待中**")
        for o in results['pending']:
            lines.append(f"  {o['symbol']}: {o['filled']}/{o['quantity']}股 @ ${o['limit_price']:.2f} ({o['status']})")
    
    if results['cancelled']:
        lines.append("❌ **已取消**")
        for o in results['cancelled']:
            lines.append(f"  {o['symbol']}: {o['filled']}/{o['quantity']}股")
    
    if results['rejected']:
        lines.append("🚫 **已拒单**")
        for o in results['rejected']:
            lines.append(f"  {o['symbol']}: {o['quantity']}股")
    
    return "\n".join(lines) if lines else "无订单更新"

def main():
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 60  # 默认 60 秒
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始监控订单 (间隔：{interval}秒)")
    
    last_state = None
    
    while is_market_open():
        try:
            results = check_orders(verbose)
            report = format_report(results)
            
            # 检测状态变化
            current_state = json.dumps(results, sort_keys=True)
            if current_state != last_state:
                print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 订单状态变化:")
                print(report)
                # 这里可以添加飞书推送逻辑
                last_state = current_state
            elif verbose:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 无变化")
            
        except Exception as e:
            print(f"[ERROR] {datetime.now()}: {e}")
        
        time.sleep(interval)
    
    # 收盘报告
    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 美股已收盘，生成最终报告...")
    results = check_orders(verbose=True)
    print(format_report(results))

if __name__ == '__main__':
    main()
