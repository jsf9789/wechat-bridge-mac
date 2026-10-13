#!/usr/bin/env python3
"""示例：读取微信收到的文件（PDF/Word/任意文件）。

微信 4.x 收到的文件在缓存目录**明文存放**，无需任何 UI 操作：
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/msg/file/YYYY-MM/

零 UI 路线：find_wechat_file() 找到 → read_pdf() 提取文本。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wechat_ax import WeChatAX


def main():
    if len(sys.argv) < 2:
        print("用法: python file_reader.py <文件名前缀> [--month 2026-10]")
        sys.exit(1)

    prefix = sys.argv[1]
    month = None
    if "--month" in sys.argv:
        month = [sys.argv[sys.argv.index("--month") + 1]]

    # 找文件
    files = WeChatAX.find_wechat_file(prefix, months=month)
    if not files:
        print("没找到文件")
        sys.exit(1)

    for f in files:
        print(f"找到: {f} ({os.path.getsize(f)//1024}KB)")

    # PDF 直接读文本
    target = files[0]
    if target.lower().endswith(".pdf"):
        pages = WeChatAX.read_pdf(target, max_pages=20)
        print(f"\n=== {os.path.basename(target)}（{len(pages)} 页）===")
        for i, page in enumerate(pages):
            print(f"\n--- 第 {i+1} 页 ---")
            print(page[:2000])
    else:
        print(f"\n文件已就位（非 PDF 不做文本提取）: {target}")


if __name__ == "__main__":
    main()
