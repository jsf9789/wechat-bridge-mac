#!/usr/bin/env python3
"""把微信窗口切到独立桌面（Space）——零遮挡方案。

用法：
  python move_wechat_to_space.py          # 微信全屏独占桌面（推荐）
  python move_wechat_to_space.py restore  # 退出全屏

原理：macOS 没有程序化创建 Spaces 的公开 API（Mission Control 无 AX 元素、
快捷键状态难控、拖拽边缘在单 Space 下无效）。
但微信窗口支持 **AXFullScreenButton**（标题栏全屏按钮可 AXPress）——
全屏 = 独占 Space，永不被任何窗口遮挡，且 AX/截图/OCR 全链路正常（已验证）。

全屏后的注意：
- 会话区更大更清晰（OCR 质量提升）
- activate() 会切到微信全屏桌面，需要几秒动画
- 退出：再跑一次本脚本（toggle）
"""
import sys
import time
import subprocess

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from wechat_ax import WeChatAX
from ApplicationServices import (
    AXUIElementCopyAttributeValue, AXUIElementPerformAction,
    kAXSubroleAttribute,
)
import Quartz


def is_fullscreen():
    cgwins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    for w in cgwins:
        if w.get("kCGWindowOwnerName") == "微信":
            b = dict(w.get("kCGWindowBounds") or {})
            if b.get("Width") in (1920, 2048) and b.get("Height") in (1080, 1055, 1054) \
                    and b.get("X") == 0 and b.get("Y") == 0:
                return True
    return False


def toggle_fullscreen():
    wx = WeChatAX()
    wx.activate()
    time.sleep(0.6)
    win = wx.get_window()
    if win is None:
        print("未找到微信窗口")
        return False
    AXUIElementPerformAction(win, "AXRaise")
    time.sleep(0.4)
    for b in wx.find_all(win, role="AXButton"):
        err, sub = AXUIElementCopyAttributeValue(b, kAXSubroleAttribute, None)
        if sub == "AXFullScreenButton":
            AXUIElementPerformAction(b, "AXPress")
            time.sleep(3)  # 全屏动画
            return True
    print("未找到全屏按钮")
    return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    toggle_fullscreen()
    fs = is_fullscreen()
    print("微信:", "全屏独立桌面 ✓（零遮挡）" if fs else "窗口模式")
    return 0


if __name__ == "__main__":
    sys.exit(main())
