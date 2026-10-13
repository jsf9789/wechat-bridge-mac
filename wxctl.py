#!/usr/bin/env python3
"""
wxctl — 微信 4.x (macOS) 命令行控制工具
让 WorkBuddy 通过命令行读写微信：切会话 / 读消息 / 发消息。

用法:
  wxctl status                     当前会话 + 最近消息
  wxctl current                    当前会话名
  wxctl switch <会话名>             切换会话
  wxctl read [N]                   读当前会话最近 N 条消息 (默认20)
  wxctl send <会话名> <消息>        切换并发送（带会话名守门）
  wxctl send-here <消息>            只发给当前会话（不切换）
  wxctl sessions                   列出可见的会话列表
  wxctl search <关键词> [--messages]  搜索切换会话 / 搜聊天记录
"""
import sys
import os
import time
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_ax import WeChatAX


def out_json(ok, **kw):
    kw["ok"] = ok
    print(json.dumps(kw, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("current")
    sub.add_parser("sessions")
    sp = sub.add_parser("switch")
    sp.add_argument("chat")
    rp = sub.add_parser("read")
    rp.add_argument("n", nargs="?", type=int, default=20)
    sp2 = sub.add_parser("send")
    sp2.add_argument("chat")
    sp2.add_argument("message")
    sp3 = sub.add_parser("send-here")
    sp3.add_argument("message")
    sp4 = sub.add_parser("search")
    sp4.add_argument("keyword")
    sp4.add_argument("--messages", action="store_true", help="搜聊天记录而非切换会话")

    args = p.parse_args()
    wx = WeChatAX()
    wx.activate()

    if args.cmd == "status":
        cur = wx.get_current_chat()
        msgs = wx.get_messages()
        out_json(True, chat=cur, recent=[m[-200:] for m in msgs[-10:]])

    elif args.cmd == "current":
        out_json(True, chat=wx.get_current_chat())

    elif args.cmd == "sessions":
        win = wx.get_window()
        items = wx.find_all(win, role="AXStaticText")
        names = []
        for it in items:
            t = wx.title(it)
            x, y, w, h = wx.get_pos_size(it)
            # 会话列表项在 x=512 一列，title 是多行的
            if x and abs(x - 512) < 10 and "\n" in t:
                names.append(t.split("\n")[0].strip())
        out_json(True, sessions=names)

    elif args.cmd == "switch":
        r = wx.switch_chat(args.chat)
        out_json(r is not None and args.chat in (r or ""), chat=r,
                 error=None if r else f"切换到 {args.chat} 失败")

    elif args.cmd == "read":
        msgs = wx.get_messages()
        out_json(True, messages=[m[-500:] for m in msgs[-args.n:]])

    elif args.cmd == "send":
        r = wx.switch_chat(args.chat)
        if r is None or args.chat not in (r or ""):
            out_json(False, error=f"切换到 {args.chat} 失败")
            return
        ok, info = wx.send_message(args.message, confirm_chat=args.chat)
        out_json(ok, info=info)

    elif args.cmd == "send-here":
        ok, info = wx.send_message(args.message)
        out_json(ok, info=info)

    elif args.cmd == "search":
        if args.messages:
            chats = wx.search_messages(args.keyword)
            out_json(True, chats_with_keyword=chats)
        else:
            r = wx.switch_chat(args.keyword)
            out_json(r is not None and args.keyword in (r or ""),
                     chat=r, error=None if r else f"搜索切换到 {args.keyword} 失败")


if __name__ == "__main__":
    main()
