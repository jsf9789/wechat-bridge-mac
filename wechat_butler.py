#!/usr/bin/env python3
"""
wechat_butler.py — 微信管家守护进程 v4（多会话 + 群@触发）

角色架构（重要）：
  Mac 微信登录的号 = 管家号（本 Agent/自动化操作的号）
  会话里的联系人（如"张三"）= 主人本人（手机上的号）
  主人从手机发消息 → 管家号收到（显示在会话左侧）
  管家回复 → 主人手机上收到

v4 新增：
  - 同时监控多个会话（--chats 用逗号分隔）
  - 私聊（单聊）：主人发来的消息全回复
  - 群聊：**只有被 @ 管家时才回复**（@所有人 不触发），其他消息只围观
  - 群聊自动学习自己的昵称（第一次被 @ 时记录）

用法：
  python wechat_butler.py                                     # 默认监控"张三"
  python wechat_butler.py --chats "张三,某某群"               # 多会话监控
  python wechat_butler.py --chats "某某群" --group-mode only    # 只监控群
  python wechat_butler.py --dry-run                            # 只检测不发送
  python wechat_butler.py --interval 15                        # 轮询间隔
  python wechat_butler.py --both-sides                         # 调试：自己发的也回
"""
import sys
import os
import re
import time
import json
import argparse
import signal
import urllib.request
import subprocess as sp
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_ax import WeChatAX

# ═══ LLM 通道：优先 GLM-5.3（WorkBuddy 同款，内网 Anthropic Messages API），ollama 兜底 ═══
GLM_BASE = "http://127.0.0.1:8000"
GLM_MODEL = "GLM-5.3-BF16[1m]"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "GLM-5.3（fallback: qwen2.5:7b@ollama）"

# dumall skill 路径与解释器
SKILLS_DIR = os.path.expanduser("~/skills")
DUMALL_PY = "python3"
ORDER_PIPELINE = os.path.join(SKILLS_DIR, "dumall-order", "order_pipeline.py")
CLAIM_COUPONS = os.path.join(SKILLS_DIR, "dumall-order", "claim_coupons.py")
CHECK_PRICES = os.path.join(SKILLS_DIR, "dumall-order", "check_prices.py")
QUERY_ORDERS = os.path.join(SKILLS_DIR, "dumall-order", "query_orders.py")

# 左右判定阈值（OCR x 比例，实测微信 4.x）：
# 对方消息气泡起点 x < 0.18；自己消息气泡 x >= 0.20
LEFT_THRESHOLD = 0.18

BUTLER_PROMPT = """你是微信里的智能管家"小锋管家"，服务对象叫张三（绝不叫"主人"，直接称"张三"或用"你"）。
你对张三从手机发来的消息给出简短、务实、有帮助的回应。

风格要求：
- 像一位干练的私人助理/顾问：口吻自然、简短（一般1-3句话，最多不超过80字）
- 称呼用"张三"或"你"，绝不用"主人""您"
- 有用优先：能给建议就给建议，需要信息就问关键问题
- 不堆客套话，不用"您好""请问有什么可以帮您"这种空话
- 如果张三问的是事实/分析类问题，给出简洁观点或提醒风险
- 如果是待办/提醒类，确认收到并给出时间建议
- 如果只是闲聊，轻松接一句即可
- 直接输出回复内容本身，不要任何前缀、引号或解释
- 你回复后张三会继续发消息，保持对话连贯"""


# ═══════════════ dumall 意图识别与执行 ═══════════════

ORDER_KW = re.compile(r"下单|代下单|下一单|买一?个?单|帮忙买|帮买")
CLAIM_KW = re.compile(r"领券|领优惠|一键领")
# 价格意图：动词类（查价/询价/比价）+ 名词类（价格/多少钱/价位…）
PRICE_KW = re.compile(r"查价|询价|比价")
PRICE_NP = re.compile(r"价格|多少钱|几块|几元|价位|啥价|什么价|便宜|贵不")
ORDERS_KW = re.compile(r"查订单|订单状态|我的订单|发货了吗|发货了没|物流|运单|订单怎么样")
SHIPPING_KW = re.compile(r"在途|没到货|到货|签收")


def detect_dumall_intent(msg):
    """识别 dumall 意图。返回 None 或 dict(action=..., payload=...)"""
    if CLAIM_KW.search(msg):
        return {"action": "claim", "payload": msg}
    # 价格意图：动词类直接触发
    if PRICE_KW.search(msg):
        m = re.sub(PRICE_KW, "", msg).strip(" ，。?？")
        return {"action": "price", "payload": m or "小度商城"}
    # 名词类（价格/多少钱…）+ 商城语境（小度/内购/商城/dumall）→ 也触发
    if PRICE_NP.search(msg) and re.search(r"小度|内购|商城|dumall|活动页|今天的", msg, re.I):
        return {"action": "price", "payload": "小度商城"}
    if ORDERS_KW.search(msg) or (SHIPPING_KW.search(msg) and "订单" in msg):
        action = {"action": "orders", "payload": msg}
        # 指定订单号
        m = re.search(r"\b(\d{10,})\b", msg)
        if m:
            action["order_id"] = m.group(1)
        # 在途
        if re.search(r"在途|没到货|还没到", msg):
            action["shipping_only"] = True
        return action
    if ORDER_KW.search(msg):
        has_phone = re.search(r"1[3-9]\d{9}", msg)
        if has_phone:
            return {"action": "order", "payload": msg}
        # 有下单关键词但缺手机号/地址 → 不执行，让 LLM 自然追问
        return None
    return None


def run_dumall_skill(action, payload=None, **kwargs):
    """调 dumall skill 脚本，返回 JSON dict（失败也返回带 error 的 dict）"""
    try:
        if action == "order":
            r = sp.run([DUMALL_PY, ORDER_PIPELINE, "--text", payload or ""],
                       capture_output=True, text=True, timeout=300)
        elif action == "claim":
            args = [DUMALL_PY, CLAIM_COUPONS]
            m = re.search(r"/activity/[\w/-]+", payload or "")
            if m:
                args += ["--activity", m.group()]
            r = sp.run(args, capture_output=True, text=True, timeout=300)
        elif action == "price":
            # check_prices.py 按活动页查全部商品实付价（--activity）
            args = [DUMALL_PY, CHECK_PRICES]
            m = re.search(r"/activity/[\w/-]+", payload or "")
            if m:
                args += ["--activity", m.group()]
            r = sp.run(args, capture_output=True, text=True, timeout=180)
            # 合并 monitor_list.json 的内购价（闺蜜机/学习机/智能屏等不在活动页的商品）
            try:
                ml_path = os.path.join(SKILLS_DIR, "dumall-order", "monitor_list.json")
                with open(ml_path, encoding="utf-8") as f:
                    ml = json.load(f)
                base = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
                base_products = base.get("products") or []
                # 内购清单按商品名去重后追加（标记为内购价）
                existing_names = " ".join(
                    str(p.get("name", "")) for p in base_products)
                merged = []
                for item in ml:
                    nm = str(item.get("name") or "")
                    if not nm or nm in existing_names:
                        continue
                    emp = item.get("employee")
                    ac = item.get("afterCoupon")
                    entry = {
                        "name": nm,
                        "originalPrice": f"¥{emp}" if emp else None,
                        "actualPayment": f"¥{ac}" if ac else (f"¥{emp}" if emp else "?"),
                        "couponUsed": item.get("couponName") or "无",
                        "gift": item.get("gift") or "无",
                        "internal": True,
                    }
                    merged.append(entry)
                if merged:
                    base["products"] = base_products + merged
                    if isinstance(base.get("productCount"), int):
                        base["productCount"] += len(merged)
                    r = sp.CompletedProcess(args, 0,
                                            stdout=json.dumps(base, ensure_ascii=False),
                                            stderr="")
            except Exception as e:
                print(f"[butler] monitor_list merge failed: {e}")
        elif action == "orders":
            # query_orders.py 查订单状态（v1.1）
            args = [DUMALL_PY, QUERY_ORDERS]
            if kwargs.get("order_id"):
                args += ["--order", kwargs["order_id"]]
            elif kwargs.get("shipping_only"):
                args += ["--shipping"]
            r = sp.run(args, capture_output=True, text=True, timeout=180)
        else:
            return {"success": False, "error": f"未知 action: {action}"}
        out = r.stdout.strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            start = out.rfind("{\n")
            if start == -1:
                start = out.rfind("{")
            if start >= 0:
                try:
                    return json.loads(out[start:])
                except json.JSONDecodeError:
                    pass
            return {"success": False, "error": f"脚本输出解析失败: {out[:200]}"}
    except sp.TimeoutExpired:
        return {"success": False, "error": "skill 执行超时"}
    except Exception as e:
        return {"success": False, "error": f"skill 调用异常: {e}"}


def format_order_reply(result):
    """把 order_pipeline JSON 组织成给主人的微信回复文本"""
    if not result.get("success"):
        errs = []
        for r in result.get("results", []):
            if not r.get("success"):
                errs.append(r.get("error", "未知错误"))
        if not errs and result.get("error"):
            errs = [result["error"]]
        if not errs:
            errs = ["未知错误"]
        return "❌ 下单失败：" + "；".join(errs[:3])

    lines = []
    for r in result.get("results", []):
        if not r.get("success"):
            lines.append(f"❌ {r.get('name')}: {r.get('error')}")
            continue
        lines.append("✅ 下单成功！")
        lines.append(f"📦 订单号：{r.get('orderId')}")
        prods = "、".join(p.get("name", "?") for p in r.get("products", []))
        lines.append(f"🛒 商品：{prods}")
        lines.append(f"💰 原价 {r.get('originalAmount')} → 实付 {r.get('amount')}")
        if r.get("couponUsed") and r.get("couponUsed") != "无":
            lines.append(f"🎟️ 用券：{r.get('couponUsed')}")
        lines.append(f"👤 收件：{r.get('recipient')} {r.get('phone')}")
        lines.append(f"📍 {r.get('address')}")
        lines.append("💳 支付宝扫码支付，二维码截图马上发")
    return "\n".join(lines)


def format_claim_reply(result):
    if result.get("error") and not result.get("accounts"):
        return f"❌ 领券失败：{result['error']}"
    s = result.get("summary", {})
    parts = []
    for acc in result.get("accounts", []):
        parts.append(f"{acc.get('name')}: 新领{acc.get('claimed', 0)}张 "
                     f"已有{acc.get('alreadyHad', 0)} 抢完{acc.get('soldOut', 0)}")
        if acc.get("error"):
            parts[-1] += f" ⚠️{acc['error']}"
    head = f"🎟️ 领券完成（{result.get('totalBatches', 0)} 个批次）"
    return head + "\n" + "\n".join(parts) + \
        f"\n合计：新领 {s.get('totalClaimed', 0)} 张，已领过 {s.get('totalAlreadyHad', 0)}，抢完 {s.get('totalSoldOut', 0)}"


def format_orders_reply(result, order_id=None):
    """query_orders 输出 → 微信回复文本"""
    if isinstance(result, dict) and result.get("error"):
        return f"❌ 查订单失败：{result['error']}"
    orders = result.get("orders") or []
    if not orders:
        return "📭 没查到订单（可能还没有下单记录）"
    lines = []
    for o in orders[:10]:
        line = f"📦 {o.get('id')}｜{o.get('statusText')}"
        if o.get("recipient"):
            line += f"｜{o['recipient']}"
        if o.get("amount"):
            line += f"｜{o['amount']}"
        if o.get("orderTime"):
            line += f"｜{o['orderTime']}"
        lines.append(line)
        exp = o.get("express") or {}
        if exp and exp.get("waybill"):
            lines.append(f"   🚚 {exp.get('carrier', '')} {exp['waybill']}")
        if o.get("latestLog"):
            lines.append(f"   📍 {str(o['latestLog'])[:60]}")
        # 指定订单查询 → 给完整轨迹
        if order_id and o.get("operLogs"):
            for lg in o["operLogs"][:10]:
                lines.append(f"   [{lg.get('time', '')}] {lg.get('content', '')[:56]}")
    head = f"📋 订单（共 {result.get('total', len(orders))} 个，显示 {len(orders)} 个）：" if not order_id else "📋 订单详情："
    return head + "\n" + "\n".join(lines)


def format_price_reply(result):
    """check_prices 输出 {products: [{name, originalPrice, actualPayment, couponUsed, gift}]}"""
    if isinstance(result, dict) and result.get("error"):
        return f"❌ 查价失败：{result['error']}"
    items = result.get("products") if isinstance(result, dict) else result
    if not isinstance(items, list):
        return f"💰 查价结果：\n{json.dumps(result, ensure_ascii=False)[:600]}"

    def fmt(p, maxlen=30):
        name = str(p.get("name") or "?")[:maxlen]
        pay = p.get("actualPayment") or p.get("finalPrice") or "?"
        orig = p.get("originalPrice")
        coupon = p.get("couponUsed") or ""
        line = f"· {name}：实付 {pay}"
        if orig and orig != pay:
            line += f"（原 {orig}）"
        if coupon and coupon != "无":
            line += f" [{coupon[:16]}]"
        gift = p.get("gift")
        if gift and gift != "无":
            line += f" 赠:{str(gift)[:12]}"
        return line

    # 分组：活动页实时价 vs 员工内购清单价
    live = [p for p in items if not p.get("internal")]
    internal = [p for p in items if p.get("internal")]
    lines = []
    if live:
        lines.append(f"📢 活动页实时价（{len(live)} 个）：")
        lines += [fmt(p, 24) for p in live[:12]]
    if internal:
        lines.append(f"🎁 员工内购券后价（{len(internal)} 个）：")
        lines += [fmt(p, 30) for p in internal[:40]]
    if not lines:
        return "💰 没发现商品"
    return "\n".join(lines)


def llm_reply(history_msgs, new_msg):
    """管家回复：优先 GLM-5.3（内网 Anthropic Messages API），失败回退本地 ollama"""
    context = ""
    for m in history_msgs[-6:]:
        context += m.replace("\n", " ")[:60] + "\n"
    user_content = (f"【最近对话语境】\n{context}\n【张三的新消息】\n{new_msg}\n\n"
                    f"请以管家身份简短回复（称呼张三，不叫主人，不超过80字，直接给回复内容）：")

    # ---- 主通道：GLM-5.3 ----
    try:
        payload = {
            "model": GLM_MODEL,
            "max_tokens": 2500,  # GLM-5.3 thinking 消耗大，给足余量防截断
            "system": BUTLER_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        }
        req = urllib.request.Request(
            f"{GLM_BASE}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": "none",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        # content 分块列表，取 text 块拼接（跳过 thinking 块）
        text = "".join(c.get("text", "") for c in d.get("content", [])
                       if c.get("type") == "text").strip()
        if text:
            return text
    except Exception as e:
        print(f"[butler] GLM-5.3 调用失败，回退 ollama: {e}")

    # ---- 兜底：本地 ollama ----
    payload = {
        "model": "qwen2.5:7b",
        "stream": False,
        "messages": [
            {"role": "system", "content": BUTLER_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "options": {"temperature": 0.7, "num_predict": 150},
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"].strip()
    except Exception:
        return None


TIME_PAT = re.compile(r"^(昨天|今天|\d{1,2}:\d{2}|\d{4}[-/年])")


def is_noise(m):
    """时间戳/系统占位等非实质消息。注意：[语音] 不算噪音（v4.1 起语音会被转写）"""
    m = m.strip()
    if not m:
        return True
    if TIME_PAT.match(m) and len(m) <= 20:
        return True
    if m in ("图片", "[图片]", "[动画表情]", "[视频]", "[文件]"):
        return True
    return False


class Butler:
    def __init__(self, chat, dry_run=False, interval=20, both_sides=False):
        self.chat = chat
        self.dry_run = dry_run
        self.interval = interval
        self.both_sides = both_sides
        self.wx = WeChatAX()
        self.running = True
        self.msg_count = 0
        self.processed = set()   # 已处理的消息文本，绝不重复回复
        self.self_sent = set()   # 自己发过的回复，永不回复（防自嗨硬保险）
        self.baseline_done = False
        self.my_names = set()    # 管家号在群里的昵称（被@时学习）

    def stop(self, *a):
        print("\n[butler] 收到停止信号，退出")
        self.running = False

    # ---------- 左右判定 ----------
    def get_incoming(self):
        """切到监控会话，返回 (主人发来的实质消息列表, 语境消息列表)
        用 OCR 判定气泡左右：左侧=主人(对方)，右侧=管家(自己)。"""
        # 先激活微信并置顶（防被其他窗口遮挡导致截图内容错误）
        self.wx.activate()
        from ApplicationServices import AXUIElementPerformAction
        win = self.wx.get_window()
        if win is not None:
            try:
                AXUIElementPerformAction(win, "AXRaise")
            except Exception:
                pass

        # 切换前先 Esc 清掉上一轮可能残留的搜索面板/弹窗
        self.wx.press_key(53)
        time.sleep(0.4)

        r = self.wx.switch_chat(self.chat)
        if not r or self.chat not in r:
            return None
        # 切换后再 Esc 一次：switch_chat 的搜索路径可能留下面板
        self.wx.press_key(53)
        time.sleep(0.3)

        # AX 拿消息文本 + 坐标（快速、可靠）
        win = self.wx.get_window()
        ax_msgs = []  # (y, x, w, text)
        for lst in self.wx.find_all(win, role="AXList", title_contains="消息"):
            for k in self.wx.children(lst):
                t = self.wx.title(k).strip()
                if t and not is_noise(t):
                    kx, ky, kw, kh = self.wx.get_pos_size(k)
                    if ky is not None:
                        ax_msgs.append((ky, kx or 0, kw or 0, t))
        ax_msgs.sort()

        # OCR 拿气泡左右（截图消息区右侧 72%）
        import Quartz
        cgwins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        bounds = None
        for w in cgwins:
            if w.get("kCGWindowOwnerName") == "微信" and w.get("kCGWindowName") == "微信":
                bounds = dict(w.get("kCGWindowBounds") or {})
                break
        if not bounds:
            return None

        x0 = int(bounds["X"] + bounds["Width"] * 0.28)
        y0 = int(bounds["Y"]) + 60
        cap_w = int(bounds["Width"] * 0.72)
        cap_h = int(bounds["Height"]) - 140
        tmp = os.path.join(tempfile.mkdtemp(), "sides.png")
        sp.run(["screencapture", "-x", f"-R{x0},{y0},{cap_w},{cap_h}", tmp])
        ocr_rows = self.wx.ocr_image(tmp, return_boxes=True)

        # 消息文本 → 左右归属：OCR 行与 AX 消息按 y 配对（气泡首行）
        incoming, context = [], []
        for ay, ax_x, ax_w, axt in ax_msgs:
            first_line = axt.split("\n")[0][:20]
            side = None
            best = None
            # 引用消息块高（正文+引用块+padding 可到 100px+），配对窗口放大
            pair_win = 120 if "\n引用" in axt else 55
            for xr, yr, txt in ocr_rows:
                sy = y0 + yr * cap_h
                # OCR 行 y 在消息块 y 附近，且文本能对上消息首行
                if -5 <= sy - ay <= pair_win and (first_line[:8] in txt or txt[:8] in first_line):
                    if best is None or abs(sy - ay) < abs(best[0] - ay):
                        best = (sy, xr)
            if best is not None:
                side = "left" if best[1] < LEFT_THRESHOLD else "right"
            if side is None:
                # OCR 配不上文字的消息（语音/图片/引用块渲染特殊）：
                # AX x 坐标兜底——气泡锚点 x 在会话区左 1/4 内 → 左侧消息
                if ax_x and ax_x < x0 + cap_w * 0.25:
                    side = "left"
                elif ax_x and ax_x > x0 + cap_w * 0.4:
                    side = "right"
            if side == "left" or (self.both_sides and side == "right"):
                incoming.append(axt)
            else:
                context.append(axt)  # 自己的消息或无法判定 → 只做语境
        return incoming, context

    # ---------- 群聊 @ 检测 ----------
    def is_mention_to_me(self, text):
        """群消息里是否 @ 了管家（@所有人 不算）。
        微信 4.x 的 @ 提及形如 "@昵称\\u2005内容"（昵称后带专用空格 \\u2005）；
        也兼容普通空格和无分隔的情况。"""
        parts = re.split("\u2005|\u200a", text)
        mentions = []
        for seg in parts:
            m = re.match(r"@([^@\s，。！？]{1,32})", seg.strip())
            if m:
                mentions.append(m.group(1))
        if not mentions:
            mentions = re.findall(r"@([^@\s，。！？]{1,32})", text)
        for m in mentions:
            if m in ("所有人", "all", "All", "ALL"):
                continue
            for name in self.my_names:
                if name in m or m in name:
                    return True, m
            # 未知昵称且不是张三本人 → 视为 @ 管家（首次被@时学习昵称）
            if "张三" not in m and "张三" not in m:
                return True, m
        return False, None

    def is_group_chat(self, chat_title):
        """判断会话是否群聊：群聊标题带 (N) 成员数，或有群聊特征元素"""
        if re.search(r"[（(]\d+[）)]", chat_title or ""):
            return True
        win = self.wx.get_window()
        if win is None:
            return False
        try:
            for s in self.wx.find_all(win, role="AXStaticText"):
                v = self.wx.value(s)
                if v and ("群公告" in v or "显示群成员昵称" in v):
                    return True
        except Exception:
            pass
        return False

    def run_once_for_chat(self, chat):
        """处理单个会话的轮询"""
        result = self.get_incoming()
        if result is None:
            print(f"[butler] [{chat}] 本轮获取失败（切换失败），下轮重试")
            return False
        incoming, context = result

        # 群聊兜底：被 @ 的消息即使在 context 里（OCR 左右配对偶发错位），
        # 也强制视为主人的消息——被@的必然是别人发的
        cur_title = self.wx.get_current_chat() or ""
        if self.is_group_chat(cur_title):
            mentioned_in_ctx = [m for m in context if self.is_mention_to_me(m)[0]]
            if mentioned_in_ctx and not incoming:
                incoming = [mentioned_in_ctx[-1]]
                context = [m for m in context if m != incoming[0]]

        if not incoming:
            return False

        latest = incoming[-1].strip()

        # 硬保险：自己发过的内容绝不回复（防左右判定偶发失效导致自嗨）
        for sent in self.self_sent:
            if latest == sent or (len(latest) > 12 and latest[:12] in sent):
                return False

        is_group = self.is_group_chat(self.wx.get_current_chat() or "")

        # 群聊：只回被 @ 的（@所有人 不触发）
        if is_group and not self.both_sides:
            mentioned, nick = self.is_mention_to_me(latest)
            if not mentioned:
                self.processed.add(latest)
                return False
            if nick and nick not in self.my_names:
                self.my_names.add(nick)
                print(f"[butler] 学到自己的群昵称: {nick}")

        # 基线（每会话独立）
        key = (chat, "baseline")
        if key not in self._baseline:
            self._baseline.add(key)
            self.processed.update(m.strip() for m in incoming)
            print(f"[butler] [{chat}] 基线建立：{len(incoming)} 条消息")
            return False

        if latest in self.processed:
            return False
        self.processed.add(latest)
        self.msg_count += 1
        print(f"\n[butler] [{chat}] 新消息 #{self.msg_count}: {latest[:60]}")

        # ═══ 语音消息分支：微信自带转文字（v4.1） ═══
        if latest in ("[语音]", "语音"):
            try:
                text = self.wx.transcribe_voice()
            except Exception as e:
                print(f"[butler] 语音转写异常: {e}")
                text = None
            if text:
                print(f"[butler] 语音转写: {text[:80]}")
                latest = text.strip()
                if not latest:
                    return True
            else:
                if not self.dry_run:
                    self.wx.send_message("这条语音没转出来（识别失败或没找到语音气泡），"
                                         "麻烦张三再发一次或打字～", confirm_chat=self.chat)
                return True

        # ═══ dumall skill 分支：下单/领券/查价/查订单 ═══
        intent = detect_dumall_intent(latest)
        if intent:
            print(f"[butler] dumall 意图: {intent['action']}")
            if self.dry_run:
                print(f"[butler] (dry-run) 将执行: {intent['action']} payload={intent.get('payload','')[:60]}")
                return True
            act_names = {"order": "下单", "claim": "领券", "price": "查价", "orders": "查订单"}
            self.wx.send_message(f"⏳ 正在{act_names.get(intent['action'], '处理')}，稍等…",
                                 confirm_chat=self.chat)
            result = run_dumall_skill(intent["action"], intent.get("payload"),
                                      order_id=intent.get("order_id"),
                                      shipping_only=intent.get("shipping_only"))
            print(f"[butler] dumall 结果: {json.dumps(result, ensure_ascii=False)[:300]}")

            if intent["action"] == "order":
                reply = format_order_reply(result)
            elif intent["action"] == "claim":
                reply = format_claim_reply(result)
            elif intent["action"] == "orders":
                reply = format_orders_reply(result, intent.get("order_id"))
            else:
                reply = format_price_reply(result)

            ok, info = self.wx.send_message(reply, confirm_chat=self.chat)
            print(f"[butler] 发送: {ok} {info}")

            if intent["action"] == "order" and result.get("success"):
                for r in result.get("results", []):
                    qr = (r or {}).get("qrCodePath")
                    if qr and os.path.exists(qr):
                        time.sleep(1)
                        ok2, info2 = self.wx.send_image(qr, confirm_chat=self.chat)
                        print(f"[butler] 支付码截图 {qr}: {ok2} {info2}")
            return True

        # ═══ 常规 LLM 分支 ═══
        reply = llm_reply(incoming[:-1] + context[-3:], latest)
        if reply:
            reply = reply.strip().strip('"').strip("'")
            print(f"[butler] 管家回复: {reply[:70]}")
            if self.dry_run:
                print("[butler] (dry-run 模式，不发送)")
            else:
                ok, info = self.wx.send_message(reply, confirm_chat=self.chat)
                print(f"[butler] 发送: {ok} {info}")
                if ok:
                    self.self_sent.add(reply)
        else:
            print("[butler] LLM 生成失败，跳过本轮")
        return True

    def run_once(self):
        """多会话轮询"""
        for chat in self.chats:
            self.chat = chat
            try:
                self.run_once_for_chat(chat)
            except Exception as e:
                print(f"[butler] [{chat}] 异常: {e}")

    # ---------- 事件驱动：零干扰信号 ----------
    WX_DB_GLOB = os.path.expanduser(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
        "xwechat_files/*/db_storage")

    def db_fingerprint(self):
        """微信数据库文件的 mtime 指纹。收发消息 → session.db/message*.db
        mtime 立刻变化（实测 <0.5s）。纯文件 stat，零 UI 干扰。"""
        try:
            import glob as _glob
            fps = []
            for account_dir in _glob.glob(self.WX_DB_GLOB):
                for rel in ("session/session.db", "message/message_0.db"):
                    p = os.path.join(account_dir, rel)
                    try:
                        fps.append((p, os.stat(p).st_mtime_ns))
                    except OSError:
                        pass
            return fps
        except Exception:
            return []

    def session_unread(self):
        """【零干扰】后台读会话列表的未读状态（不 activate、不切桌面、不点击）。
        返回 {会话名: 未读数}。微信 4.x 会话行的 title 文本里带 [N条] 未读标记，
        且后台时 AX 树仍可读（实测验证）。"""
        out = {}
        try:
            win = self.wx.get_window()
            if win is None:
                return out
            for lst in self.wx.find_all(win, role="AXList", title_contains="会话"):
                for k in self.wx.children(lst):
                    t = self.wx.title(k)
                    if not t.strip():
                        continue
                    name = t.split("\n")[0].strip()
                    m = re.search(r"\[(\d+)条?\]", t)
                    if m:
                        out[name] = int(m.group(1))
        except Exception:
            pass
        return out

    def wait_for_target_signal(self, timeout=600):
        """【精准事件驱动】两层信号，都不打扰：
        第一层（粗）：stat 微信库 mtime——微信有任何动静（含后台同步）
        第二层（准）：mtime 变了 → 后台 AX 读会话列表未读数——
                     **只有监控会话出现新未读**才返回目标会话列表；否则继续等。
        全程零 UI 干扰：微信可留在后台/别的桌面，不动鼠标不切屏。"""
        baseline = self.db_fingerprint()
        checks = max(1, int(timeout / 2))
        for _ in range(checks):
            if not self.running:
                return []
            time.sleep(2)
            if self.db_fingerprint() != baseline:
                baseline = self.db_fingerprint()
                # mtime 变了，但可能只是别的群/后台同步 → 查未读数精确定位
                unread = self.session_unread()
                targets = [c for c in self.chats if unread.get(c, 0) > 0]
                if targets:
                    return targets
                # 无目标未读：继续静默等（不激活微信！）
        return []

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        print(f"[butler] 微信管家 v6 启动（精准事件驱动）")
        print(f"[butler] 角色模型: Mac号=管家 | 监控: {', '.join(self.chats)}")
        print(f"[butler] 待机: stat 库 mtime（2s/次）→ 有动静再后台读未读数（零UI）")
        print(f"[butler] 只有监控会话有新未读才激活微信进去处理，其他动静一律无视")
        print(f"[butler] dumall skills: 下单/领券/查价 | 模式: {'观察(dry-run)' if self.dry_run else '自动回复'}")
        print(f"[butler] 模型: {MODEL} | Ctrl+C 退出\n")

        # 首轮建基线（每会话），之后进入事件等待
        while self.running:
            try:
                self.run_once()
            except Exception as e:
                print(f"[butler] 异常: {e}")
            if len(self._baseline) >= len(self.chats):
                break
            time.sleep(2)

        # 精准事件驱动主循环
        while self.running:
            try:
                targets = self.wait_for_target_signal(timeout=600)
                if not self.running:
                    break
                if targets:
                    print(f"[butler] [{time.strftime('%H:%M:%S')}] 监控会话有新未读: {targets}，进去处理")
                    for chat in targets:
                        self.chat = chat
                        try:
                            self.run_once_for_chat(chat)
                        except Exception as e:
                            print(f"[butler] [{chat}] 异常: {e}")
                    # 处理完 3 秒写库缓冲，避免把自己的回复当新信号
                    time.sleep(3)
                # 超时无目标信号：静默继续（不再做全量巡检——未读数检查本身就是保活）
            except Exception as e:
                print(f"[butler] 异常: {e}")
                time.sleep(10)

        print(f"[butler] 共处理 {self.msg_count} 条消息，退出")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chat", default=None, help="监控的会话名（兼容旧参数）")
    p.add_argument("--chats", default=None, help="会话列表，逗号分隔（私聊+群聊混搭）")
    p.add_argument("--interval", type=int, default=20, help="轮询秒数")
    p.add_argument("--dry-run", action="store_true", help="只检测不发送")
    p.add_argument("--both-sides", action="store_true", help="连自己发的消息也回复（调试用）")
    p.add_argument("--names", default=None, help="管家在群里的昵称（逗号分隔，预学习）")
    args = p.parse_args()

    if args.chats:
        chats = [c.strip() for c in args.chats.split(",") if c.strip()]
    elif args.chat:
        chats = [args.chat]
    else:
        chats = ["张三"]

    butler = Butler(chats[0], args.dry_run, args.interval, args.both_sides)
    butler.chats = chats
    butler._baseline = set()
    if args.names:
        for n in args.names.split(","):
            n = n.strip()
            if n:
                butler.my_names.add(n)
                print(f"[butler] 预学习昵称: {n}")
    butler.run()


if __name__ == "__main__":
    main()
