#!/usr/bin/env python3
"""
proactive_agent.py — 主动型助理守护进程
========================================
定时巡检多维信息 → GLM-5.3 判断是否值得打扰 → 值得就推微信给张三。

巡检项（每个周期全跑，输出汇总给 LLM 决策）：
  1. dumall 订单状态变化（新发货/新签收/待支付超时提醒）
  2. Mac 健康：CDP Chrome 9222 / 微信管家进程 / 磁盘 / 内存 / VPN
  3. dumall 优惠券临期（有到期日的券 3 天内到期提醒）

防打扰机制：
  - 每个巡检项有独立冷却时间（默认 4h），同事项不重复推
  - LLM 只做措辞和"值不值得推"的二次把关，事实由代码判断
  - 推送目标会话：张三（可 --chat 改）

用法：
  python3 proactive_agent.py                    # 前台跑（默认 15 分钟一轮）
  python3 proactive_agent.py --interval 30     # 30 分钟一轮
  python3 proactive_agent.py --once            # 跑一轮就退出（调试）
  python3 proactive_agent.py --chat 某某群      # 推到别的会话
"""
import argparse
import json
import os
import re
import signal
import subprocess as sp
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wechat_ax import WeChatAX

# ═════════ 配置 ═════════
GLM_BASE = "http://127.0.0.1:8000"
GLM_MODEL = "GLM-5.3-BF16[1m]"
SKILLS_DIR = os.path.expanduser("~/skills")
DUMALL_PY = "python3"
CDP_URL = "http://127.0.0.1:9222"
STATE_FILE = "/tmp/proactive_agent_state.json"
COOLDOWN_DEFAULT = 4 * 3600          # 同事项推送冷却 4h
COOLDOWNS = {                        # 按事项覆写
    "order_change": 30 * 60,         # 订单变化 30 分钟
    "health_alert": 2 * 3600,        # 健康告警 2h
    "coupon_expiry": 12 * 3600,      # 券临期 12h
    "daily_digest": 20 * 3600,       # 日常汇总 20h（≈每天一次）
}

PROMPT = """你是张三的主动型助理，负责把巡检结果转化为该不该打扰他的决策。

规则：
- 只在【真正重要】时推送：订单状态变化、服务挂了、券要过期、每日汇总
- 状态没变化就不推（state_unchanged 项直接忽略）
- 日常汇总（daily_digest）每次都要推
- 推送文案要干练：一句话说清楚事实+建议，不超过 3 行，开头用 emoji 标类型
- 决策输出严格 JSON：{"push": true/false, "messages": ["...", ...]}
- messages 为空数组 = 不推
- 多条独立事项就多元素，别合成一坨"""


# ═════════ 工具 ═════════
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"last_push": {}, "last_orders": []}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def in_cooldown(st, key):
    return time.time() - st["last_push"].get(key, 0) < COOLDOWNS.get(
        key.split(":")[0] if ":" in key else key, COOLDOWN_DEFAULT)


def mark_pushed(st, key):
    st["last_push"][key] = time.time()


# ═════════ 巡检项 ═════════
# dumall orderStatus 状态码 → 中文（实测 2026-10-12 逆向）
ORDER_STATUS = {
    "10": "已提交待支付", "20": "已支付", "30": "订单确认中", "40": "已确认",
    "50": "下发库房", "60": "分拣中", "61": "打包完成", "62": "库房发出",
    "70": "已发货（交接承运商）", "80": "配送中", "90": "已完成/签收",
}


def status_text(code):
    return ORDER_STATUS.get(str(code), f"状态{code}")


def check_orders(st):
    """dumall 订单状态变化（全量订单 diff，含发货/签收/运单号）"""
    script = (
        "import sys; sys.path.insert(0, '" + SKILLS_DIR + "/dumall-order')\n"
        "import json, time\n"
        "from order_pipeline import CDP, api_fetch\n"
        "cdp = CDP()\n"
        "try:\n"
        "    # 全量订单（不按状态过滤），每个订单取详情里的 operLogs 最新一条 + 运单\n"
        "    j = api_fetch(cdp, 'GET', '/api/orders/v1?timestamp=' + str(int(time.time()*1000)) + "
        "'&startIndex=0&count=15&channelId=0&platformId=0')\n"
        "    orders = (((j or {}).get('data') or {}).get('order') or {}).get('result') or []\n"
        "    out = []\n"
        "    for o in orders[:8]:\n"
        "        oid = o.get('orderId')\n"
        "        dj = api_fetch(cdp, 'GET', '/api/orders/' + str(oid) + '/v1?timestamp=' + "
        "str(int(time.time()*1000)) + '&channelId=0&platformId=0')\n"
        "        det = ((dj or {}).get('data') or {}).get('order') or {}\n"
        "        logs = det.get('operLogs') or []\n"
        "        latest_log = logs[0].get('content', '') if logs else ''\n"
        "        express = det.get('express') or {}\n"
        "        out.append({'id': oid, 'status': o.get('orderStatus'),\n"
        "                    'name': o.get('consigneeName'),\n"
        "                    'express': express.get('carrierName', '') + ' ' + express.get('mailno', ''),\n"
        "                    'log': latest_log[:60]})\n"
        "    print(json.dumps(out, ensure_ascii=False))\n"
        "finally:\n"
        "    cdp.close()\n"
    )
    try:
        r = sp.run([DUMALL_PY, "-c", script], capture_output=True, text=True, timeout=120)
        orders = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return [f"orders_check_failed: {e}"], False

    sig = {o["id"]: o["status"] for o in orders}
    prev = {o["id"]: o["status"] for o in st.get("last_orders", [])}
    changed = []
    for o in orders:
        oid = o["id"]
        cur, old = str(o["status"]), prev.get(oid)
        if oid not in prev:
            continue  # 基线轮不报存量订单（避免重启轰炸）
        if old != cur:
            line = f"dumall 订单 {oid}（收件人{o.get('name','?')}）: {status_text(old)} → {status_text(cur)}"
            if str(cur) == "70" and o.get("express", "").strip():
                line += f"，{o['express']}"
            if o.get("log"):
                line += f"｜{o['log']}"
            changed.append(line)
    st["last_orders"] = orders
    if not changed:
        first_round = not prev
        return [f"state_unchanged: {len(orders)} 个订单无变化"
                + ("（首轮基线）" if first_round else "")], False
    return changed, True


def check_health(st):
    """Mac 基础设施健康"""
    facts, alert = [], False
    # CDP Chrome
    try:
        ver = urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=5).read()
        facts.append(f"cdp_chrome: ok ({json.loads(ver).get('Browser', '?')})")
    except Exception:
        facts.append("cdp_chrome: DOWN (9222 无响应)")
        alert = True
    # 微信管家
    r = sp.run(["pgrep", "-f", "wechat_butler"], capture_output=True)
    if r.returncode == 0:
        facts.append("wechat_butler: ok")
    else:
        facts.append("wechat_butler: DOWN")
        alert = True
    # 磁盘
    df = sp.run(["df", "-h", "/"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)%", df)
    if m:
        pct = int(m.group(1))
        if pct > 90:
            facts.append(f"disk: {pct}% 满（超阈值）")
            alert = True
        else:
            facts.append(f"disk: {pct}%")
    # 内存（Apple Silicon 页大小 16KB；可用 = free + inactive）
    vm = sp.run(["vm_stat"], capture_output=True, text=True).stdout
    psm = re.search(r"page size of (\d+) bytes", vm)
    page = int(psm.group(1)) if psm else 16384
    fm = re.search(r"Pages free:\s+(\d+)", vm)
    im = re.search(r"Pages inactive:\s+(\d+)", vm)
    if fm and im:
        avail_gb = (int(fm.group(1)) + int(im.group(1))) * page / 1e9
        if avail_gb < 1.5:
            facts.append(f"memory: 可用仅 {avail_gb:.1f}GB")
            alert = True
        else:
            facts.append(f"memory: 可用 {avail_gb:.1f}GB")
    return facts, alert


def check_coupons(st):
    """dumall 有效券（含到期时间）"""
    script = (
        "import sys; sys.path.insert(0, '" + SKILLS_DIR + "/dumall-order')\n"
        "import json, time\n"
        "from order_pipeline import CDP, api_fetch\n"
        "cdp = CDP()\n"
        "try:\n"
        "    j = api_fetch(cdp, 'GET', '/api/coupons/v1?timestamp=' + str(int(time.time()*1000)) + "
        "'&status=1&startIndex=0&count=50&channelId=0&platformId=0')\n"
        "    d = ((j or {}).get('data') or {})\n"
        "    coupons = d.get('coupons') or d.get('result') or []\n"
        "    print(json.dumps([{'name': c.get('couponName') or c.get('name'), "
        "'expire': c.get('endTime') or c.get('expireTime')} for c in coupons], ensure_ascii=False))\n"
        "finally:\n"
        "    cdp.close()\n"
    )
    try:
        r = sp.run([DUMALL_PY, "-c", script], capture_output=True, text=True, timeout=60)
        coupons = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return [f"coupons_check_failed: {e}"], False

    expiring = []
    now = time.time()
    for c in coupons:
        exp = c.get("expire")
        if not exp:
            continue
        if isinstance(exp, str):
            try:
                exp = time.mktime(time.strptime(exp[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                try:
                    exp = time.mktime(time.strptime(exp[:10], "%Y-%m-%d"))
                except Exception:
                    continue
        elif isinstance(exp, (int, float)) and exp > 1e12:
            exp = exp / 1000
        left_days = (exp - now) / 86400
        if 0 < left_days <= 3:
            expiring.append(f"券「{c.get('name', '?')}」还剩 {left_days:.1f} 天到期")
    if not expiring:
        return [f"state_unchanged: {len(coupons)} 张有效券，无 3 天内到期"], False
    return expiring, True


def make_daily_digest(facts):
    """日常汇总（每个巡检周期如果冷却到了就生成）"""
    return [f"daily_digest: 巡检完成。{'；'.join(facts[:8])}"]


# ═════════ LLM 决策 ═════════
def llm_decide(findings):
    """findings: list[str]。返回要推送的 messages 列表（空=不推）"""
    try:
        payload = {
            "model": GLM_MODEL,
            "max_tokens": 3000,  # GLM-5.3 thinking 消耗大，给足余量防截断
            "system": PROMPT,
            "messages": [{"role": "user",
                          "content": "巡检结果：\n" + "\n".join(findings)}],
        }
        req = urllib.request.Request(
            f"{GLM_BASE}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "anthropic-version": "2023-06-01",
                     "x-api-key": "none"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        text = "".join(c.get("text", "") for c in d.get("content", [])
                       if c.get("type") == "text").strip()
        # 解析 JSON（容忍 markdown 代码块包裹）
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return []
        j = json.loads(m.group())
        return j.get("messages") or [] if j.get("push") else []
    except Exception as e:
        print(f"[agent] LLM 决策失败: {e}")
        # LLM 挂了：健康告警类直接推（不经过 LLM 也能保底）
        return [f for f in findings if "DOWN" in f or "失败" in f]


# ═════════ 主循环 ═════════
class Agent:
    def __init__(self, chat, interval, once=False):
        self.chat = chat
        self.interval = interval
        self.once = once
        self.wx = None
        self.running = True

    def stop(self, *a):
        self.running = False

    def push_wechat(self, msgs):
        """推送微信。先切到目标会话再发（管家进程可能停在其他会话）"""
        if not msgs:
            return
        for attempt in range(3):
            try:
                if self.wx is None:
                    self.wx = WeChatAX()
                self.wx.activate()
                # 先切到目标会话（管家进程轮询可能切走了）
                r = self.wx.switch_chat(self.chat)
                if not r or self.chat not in r:
                    print(f"[agent] 切会话失败（第{attempt+1}次）: 当前 {r}")
                    time.sleep(3)
                    continue
                text = "\n".join(msgs)
                ok, info = self.wx.send_message(text, confirm_chat=self.chat)
                print(f"[agent] 微信推送: {ok} {info}")
                if ok:
                    return True
                time.sleep(3)
            except Exception as e:
                print(f"[agent] 微信推送异常: {e}")
                self.wx = None  # 下轮重连
        return False

    def cycle(self):
        st = load_state()
        print(f"\n[agent] === 巡检开始 {time.strftime('%H:%M:%S')} ===")
        all_findings = []
        push_keys = []  # (key, findings) 本轮有实绩的事项

        # 1) 订单
        try:
            f, changed = check_orders(st)
            all_findings += f
            if changed:
                push_keys.append(("order_change", f))
        except Exception as e:
            print(f"[agent] 订单巡检异常: {e}")

        # 2) 健康
        try:
            f, alert = check_health(st)
            all_findings += f
            if alert:
                push_keys.append(("health_alert", f))
        except Exception as e:
            print(f"[agent] 健康巡检异常: {e}")

        # 3) 券临期
        try:
            f, expiring = check_coupons(st)
            all_findings += f
            if expiring:
                push_keys.append(("coupon_expiry", f))
        except Exception as e:
            print(f"[agent] 券巡检异常: {e}")

        # 4) 日常汇总（独立冷却）
        if not in_cooldown(st, "daily_digest"):
            push_keys.append(("daily_digest", make_daily_digest(all_findings)))

        # 过滤冷却中的事项
        to_decide = []
        for key, findings in push_keys:
            dedup_key = key.split(":")[0]
            if in_cooldown(st, dedup_key):
                print(f"[agent] [{key}] 冷却中，跳过")
                continue
            to_decide += findings

        if not to_decide:
            print("[agent] 无待推送事项")
            save_state(st)
            return

        print(f"[agent] 待决策事项: {len(to_decide)}")
        msgs = llm_decide(to_decide)
        if msgs:
            ok = self.push_wechat(msgs)
            if ok:
                for key, _ in push_keys:
                    mark_pushed(st, key.split(":")[0])
        else:
            print("[agent] LLM 判定不推送")
        save_state(st)

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        print(f"[agent] 主动型助理启动")
        print(f"[agent] 推送会话: {self.chat} | 周期: {self.interval}s | LLM: GLM-5.3")
        print(f"[agent] 巡检: dumall订单 / Mac健康 / 券临期 / 日常汇总")
        while self.running:
            try:
                self.cycle()
            except Exception as e:
                print(f"[agent] 周期异常: {e}")
            if self.once:
                break
            time.sleep(self.interval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chat", default="张三", help="推送目标会话")
    p.add_argument("--interval", type=int, default=900, help="巡检周期秒数（默认900）")
    p.add_argument("--once", action="store_true", help="跑一轮退出（调试）")
    args = p.parse_args()
    Agent(args.chat, args.interval, args.once).run()


if __name__ == "__main__":
    main()
