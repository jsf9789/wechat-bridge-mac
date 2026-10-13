#!/usr/bin/env python3
"""示例：读微信公众号/服务号文章全文。

流程：切到含文章卡片的服务号会话 → 点击文章卡片 →
等待"微信 (窗口)"文章浏览器弹出 → 截图 → Vision OCR 读全文。

文章卡片在 AX 树里是空 title 节点（图片渲染），必须坐标点击。
"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wechat_ax import WeChatAX


def main():
    if len(sys.argv) < 2:
        print("用法: python article_reader.py <服务号/公众号会话名>")
        sys.exit(1)

    chat = sys.argv[1]
    wx = WeChatAX()
    wx.activate()

    r = wx.switch_chat(chat)
    if not r:
        print("切换失败")
        sys.exit(1)
    print(f"已进入: {r}")
    time.sleep(0.8)

    # 找消息列表里的文章卡片（AX 空 title 节点）
    win = wx.get_window()
    lists = wx.find_all(win, role="AXList", title_contains="消息")
    card_pos = None
    for lst in lists:
        kids = wx.children(lst)
        for i, k in enumerate(kids):
            t = wx.title(k)
            if not t.strip() and i > 0:  # 空 title = 卡片/图片消息
                x, y, w, h = wx.get_pos_size(k)
                if x is not None and y is not None and y > 100:
                    card_pos = (x, y)
                    print(f"文章卡片候选 @({x},{y})")
                    break
        if card_pos:
            break

    if not card_pos:
        print("没找到文章卡片")
        sys.exit(1)

    # 点击卡片（点击后可能先弹出服务号会话独立窗口，需再点窗口内的卡片）
    x, y = card_pos
    wx.mouse_click(x + 150, y + 80)
    time.sleep(2.5)

    # 检查弹出的窗口
    from ApplicationServices import AXUIElementCopyAttributeValue
    err, wins = AXUIElementCopyAttributeValue(wx.app, "AXWindows", None)
    titles = [wx.title(w) for w in (wins or [])]
    print("当前窗口:", titles)

    # 文章浏览器窗口名是 "微信 (窗口)"（920x890 左右）
    if "微信 (窗口)" in titles:
        lines = wx.ocr_window(window_title="微信 (窗口)", save_path="/tmp/article.png")
        print(f"\n=== 文章全文（OCR {len(lines)} 行）===")
        for y, txt in lines:
            print(txt)
    elif chat in titles:
        # 弹出的是服务号会话独立窗口——在里面再找卡片点一次
        print("弹出了服务号窗口，二次点击文章卡片...")
        # （在独立窗口里重复查找空节点并点击）
        for w in (wins or []):
            if wx.title(w) == chat:
                l2 = wx.find_all(w, role="AXList", title_contains="消息")
                for lst in l2:
                    for k in wx.children(lst):
                        if not wx.title(k).strip():
                            kx, ky, kw, kh = wx.get_pos_size(k)
                            if kx is not None and ky and ky > 100:
                                wx.mouse_click(kx + 150, ky + 80)
                                time.sleep(3.0)
                                lines = wx.ocr_window(
                                    window_title="微信 (窗口)",
                                    save_path="/tmp/article.png")
                                if lines:
                                    print(f"\n=== 文章全文（OCR {len(lines)} 行）===")
                                    for yy, txt in lines:
                                        print(txt)
                                    return
                                break
    else:
        print("未检测到文章窗口")


if __name__ == "__main__":
    main()
