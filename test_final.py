#!/usr/bin/env python3
"""最终验收测试：从任意状态 → 切换会话 → 读消息 → 发送 → 验证"""
import sys, time
sys.path.insert(0, ".")
from wechat_ax import WeChatAX

wx = WeChatAX()
wx.activate()

r = wx.switch_chat("文件传输助手")
print("1. 切换:", repr(r))
assert r and "文件传输助手" in r, "切换失败"

msgs = wx.get_messages()
print(f"2. 读取消息: {len(msgs)} 条")

msg = "🎉 WorkBuddy 微信桥接验收通过 " + time.strftime("%H:%M:%S")
ok, info = wx.send_message(msg, confirm_chat="文件传输助手")
print("3. 发送:", ok, info)
assert ok, "发送失败"

time.sleep(1.0)
msgs2 = wx.get_messages()
found = any("验收通过" in m for m in msgs2)
print("4. 验证:", "成功" if found else "失败")
assert found, "验证失败"
print("\n=== 全部通过 ===")
