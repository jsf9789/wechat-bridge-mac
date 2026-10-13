#!/usr/bin/env python3
"""示例：群互动闭环 —— 读群消息 → 理解话题 → 发送融入语境的回复。

用法：python group_reply.py "群名" [回复内容]
不带回复内容时只读不写（安全模式）。
"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wechat_ax import WeChatAX


def main():
    if len(sys.argv) < 2:
        print("用法: python group_reply.py <群名> [回复内容]")
        sys.exit(1)

    chat = sys.argv[1]
    reply = sys.argv[2] if len(sys.argv) > 2 else None

    wx = WeChatAX()
    wx.activate()

    # 1. 切换到目标群
    r = wx.switch_chat(chat)
    if not r or chat not in r:
        print(f"切换到 {chat} 失败")
        sys.exit(1)
    print(f"已进入: {r}")

    # 2. 读最近消息（含发送者）
    msgs = wx.get_group_messages(with_senders=True)
    print(f"\n最近 {len(msgs)} 条消息:")
    for sender, text in msgs[-8:]:
        s = (sender or "—")[:12]
        print(f"  [{s}] {text[:60]}")

    # 3. 生成/使用回复并发送
    if reply is None:
        print("\n（安全模式：只读未发。提供第二个参数发送回复）")
        return

    # 发送前再次确认当前会话（守门）
    ok, info = wx.send_message(reply, confirm_chat=chat)
    print(f"\n发送: {ok} {info}")

    # 4. 验证
    time.sleep(1.2)
    msgs2 = wx.get_messages()
    head = reply[:10]
    found = any(head in m for m in msgs2)
    print("验证:", "✓ 已出现在群里" if found else "✗")


if __name__ == "__main__":
    main()
