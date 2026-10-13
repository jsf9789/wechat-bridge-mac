#!/usr/bin/env python3
"""
wechat_ax.py — macOS 微信 4.x 辅助功能 (AX) 桥接库
通过 Accessibility API 读消息、切会话；通过 AXValue 直写 + 发送按钮发消息。
不注入、不 hook、不改微信二进制——纯"外挂式"UI 自动化。
"""
import re
import subprocess
import time
import ctypes

from ApplicationServices import (
    AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
    AXUIElementPerformAction, AXUIElementIsAttributeSettable,
    AXUIElementSetAttributeValue, AXUIElementSetMessagingTimeout,
    kAXChildrenAttribute, kAXRoleAttribute, kAXSubroleAttribute,
    kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute,
    kAXPositionAttribute, kAXSizeAttribute,
)
import Quartz
from Quartz import (
    CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap,
    kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp,
    CGEventSourceCreate, kCGEventSourceStateCombinedSessionState,
    CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
    CGEventSetFlags,
)

AX_ERR = {0: "OK", -25200: "AXError APIServerFailure", -25201: "InvalidUIElement",
          -25202: "InvalidUIElementObserver", -25203: "CannotComplete",
          -25204: "AttributeUnsupported", -25205: "ActionUnsupported",
          -25206: "IllegalArgument", -25207: "NotificationUnsupported",
          -25208: "NotImplemented", -25209: "NotificationAlreadyRegistered",
          -25210: "NotificationNotRegistered", -25211: "APIDisabled",
          -25212: "NoValue", -25213: "ParameterizedAttributeUnsupported",
          -25214: "NotEnoughPrecision", -25215: "AttributeDeprecated"}


class WeChatAX:
    def __init__(self, pid=None):
        self.pid = pid or self._find_pid()
        self.app = AXUIElementCreateApplication(self.pid)
        # AX 调用超时 3 秒
        AXUIElementSetMessagingTimeout(self.app, 3.0)
        self.src = CGEventSourceCreate(kCGEventSourceStateCombinedSessionState)

    @staticmethod
    def _find_pid():
        try:
            out = subprocess.check_output(["pgrep", "-x", "WeChat"], text=True)
            return int(out.strip().split("\n")[0])
        except Exception:
            raise RuntimeError("WeChat 未运行")

    # ---------- 基础 ----------
    @staticmethod
    def _ax(attr, el):
        err, val = AXUIElementCopyAttributeValue(el, attr, None)
        if err == 0 and val is not None:
            return val
        return None

    @staticmethod
    def _s(x):
        try:
            return str(x)
        except Exception:
            return repr(x)

    def _repr_point(self, val):
        m = re.search(r"x:([\-\d.]+) y:([\-\d.]+)", repr(val))
        return (float(m.group(1)), float(m.group(2))) if m else (None, None)

    def _repr_size(self, val):
        m = re.search(r"w=([\-\d.]+) h=([\-\d.]+)", repr(val))
        if not m:
            m = re.search(r"width=([\-\d.]+).{0,3}height=([\-\d.]+)", repr(val))
        return (float(m.group(1)), float(m.group(2))) if m else (None, None)

    def get_pos_size(self, el):
        pos = self._ax(kAXPositionAttribute, el)
        size = self._ax(kAXSizeAttribute, el)
        x, y = self._repr_point(pos) if pos is not None else (None, None)
        w, h = self._repr_size(size) if size is not None else (None, None)
        return x, y, w, h

    def activate(self):
        subprocess.run(["open", "-a", "WeChat"])
        time.sleep(1.2)

    def get_window(self):
        wins = self._ax("AXWindows", self.app)
        return list(wins)[0] if wins else None

    def children(self, el, limit=80):
        ch = self._ax(kAXChildrenAttribute, el)
        return list(ch)[:limit] if ch else []

    def role(self, el):
        r = self._ax(kAXRoleAttribute, el)
        return self._s(r) if r else ""

    def title(self, el):
        t = self._ax(kAXTitleAttribute, el)
        return self._s(t) if t else ""

    def value(self, el):
        v = self._ax(kAXValueAttribute, el)
        return self._s(v) if v is not None else ""

    def desc(self, el):
        d = self._ax(kAXDescriptionAttribute, el)
        return self._s(d) if d else ""

    def find_all(self, el, role=None, title_contains=None, desc_contains=None,
                 out=None, limit=80):
        """深度遍历找元素（限制广度防卡顿）"""
        if out is None: out = []
        r = self.role(el)
        t = self.title(el)
        d = self.desc(el)
        ok = True
        if role and r != role: ok = False
        if title_contains and title_contains not in t: ok = False
        if desc_contains and desc_contains not in d: ok = False
        if ok: out.append(el)
        for c in self.children(el, limit):
            self.find_all(c, role, title_contains, desc_contains, out, limit)
        return out

    def walk(self, el, depth=0, max_depth=12, limit=60, out=None):
        """返回 (depth, role, title, desc, value, x, y, w, h) 列表"""
        if out is None: out = []
        if depth > max_depth: return out
        x, y, w, h = self.get_pos_size(el)
        out.append((depth, self.role(el), self.title(el), self.desc(el),
                    self.value(el), x, y, w, h))
        for c in self.children(el, limit):
            self.walk(c, depth + 1, max_depth, limit, out)
        return out

    # ---------- 鼠标/键盘 ----------
    def mouse_click(self, x, y, delay=0.12):
        pt = Quartz.CGPoint(x, y)
        ev = CGEventCreateMouseEvent(self.src, kCGEventMouseMoved, pt, 0)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(delay)
        ev = CGEventCreateMouseEvent(self.src, kCGEventLeftMouseDown, pt, 0)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.05)
        ev = CGEventCreateMouseEvent(self.src, kCGEventLeftMouseUp, pt, 0)
        CGEventPost(kCGHIDEventTap, ev)

    def right_click(self, x, y, delay=0.12):
        """合成右键点击（弹出上下文菜单）"""
        pt = Quartz.CGPoint(x, y)
        ev = CGEventCreateMouseEvent(self.src, kCGEventMouseMoved, pt, 0)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(delay)
        ev = CGEventCreateMouseEvent(self.src, Quartz.kCGEventRightMouseDown, pt, 1)
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.06)
        ev = CGEventCreateMouseEvent(self.src, Quartz.kCGEventRightMouseUp, pt, 1)
        CGEventPost(kCGHIDEventTap, ev)

    def _close_floating_menu(self):
        """按 Esc 关闭浮层菜单/弹窗"""
        self.press_key(0x35)  # Escape
        time.sleep(0.4)

    def type_text(self, text):
        """通过 HID 事件输入任意 unicode 文本"""
        for i in range(0, len(text), 20):
            chunk = text[i:i+20]
            down = CGEventCreateKeyboardEvent(self.src, 0, True)
            up = CGEventCreateKeyboardEvent(self.src, 0, False)
            CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
            CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)

    def press_key(self, keycode, flags=0):
        down = CGEventCreateKeyboardEvent(self.src, keycode, True)
        up = CGEventCreateKeyboardEvent(self.src, keycode, False)
        if flags:
            CGEventSetFlags(down, flags)
            CGEventSetFlags(up, flags)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)

    # ---------- 微信高层操作 ----------
    def get_current_chat(self):
        """当前打开的会话名（输入框 title）"""
        win = self.get_window()
        if not win: return None
        tas = self.find_all(win, role="AXTextArea")
        # 会话输入框的 title 是会话名；搜索框 title 是"搜索"
        for ta in tas:
            t = self.title(ta)
            if t and t != "搜索":
                return t
        return None

    def get_messages(self, limit=60):
        """读当前会话的消息列表（AXList title=消息）"""
        win = self.get_window()
        if not win: return []
        lists = self.find_all(win, role="AXList", title_contains="消息")
        if not lists: return []
        msgs = []
        for lst in lists:
            for item in self.children(lst, limit):
                t = self.title(item)
                if t.strip():
                    msgs.append(t)
        return msgs

    def switch_chat(self, name, exact=False, retries=3):
        """切换到指定会话。优先搜索框定位（不受列表重排影响），失败退回列表点击。"""
        # 已经在目标会话就直接返回
        cur = self.get_current_chat()
        if cur and name in cur:
            return cur

        for attempt in range(retries):
            # 方案A：搜索框
            r = self._switch_via_search(name)
            if r and name in r:
                return r

            # 方案B：列表点击
            r = self._switch_via_list(name, exact)
            if r and name in r:
                return r
            time.sleep(0.8)
        return None

    # 真实分类头（"搜索网络结果"下的都是网络联想词，不能点）
    SEARCH_CATEGORIES = ("群聊", "联系人", "功能", "聊天记录", "最近在搜")

    def _get_search_panel(self):
        """获取搜索面板窗口（无标题 AXWindow）"""
        err, wins = AXUIElementCopyAttributeValue(self.app, "AXWindows", None)
        for w in (wins or []):
            if self.title(w) == "":
                return w
        return None

    def _open_search(self, keyword):
        """Cmd+F + AXValue 写入搜索词，返回 (panel, search_ta) 或 (None, None)"""
        self.activate()
        self.press_key(3, (1 << 20) | (1 << 8))  # Cmd+F
        time.sleep(0.7)
        err, wins = AXUIElementCopyAttributeValue(self.app, "AXWindows", None)
        main_win = None
        for w in (wins or []):
            if self.title(w) == "微信":
                main_win = w
        if main_win is None:
            return None, None
        tas = self.find_all(main_win, role="AXTextArea", title_contains="搜索")
        if not tas:
            return None, None
        st = tas[0]
        AXUIElementSetAttributeValue(st, "AXValue", keyword)
        time.sleep(1.8)
        return self._get_search_panel(), st

    def _search_panel_items(self, panel):
        """解析搜索面板: 返回 [(category, text, el), ...]，跳过网络联想区"""
        items = []
        cur_cat = "搜索网络结果"
        for s in self.find_all(panel, role="AXStaticText"):
            t = self.title(s).strip()
            if not t:
                continue
            if t in self.SEARCH_CATEGORIES:
                cur_cat = t
                continue
            if cur_cat == "搜索网络结果" and t == "搜索网络结果":
                continue
            items.append((cur_cat, t, s))
        return items

    def _switch_via_search(self, name, max_attempts=4):
        """搜索面板切换会话——不受列表重排/滚动影响，最可靠路径。
        面板分类头不固定（最常使用/群聊/联系人/功能/搜索网络结果……），
        所以用"逐个候选点击 + 读回验证"的容错策略。"""
        panel, st = self._open_search(name)
        if panel is None:
            return None

        def collect(panel):
            cands = []
            for s in self.find_all(panel, role="AXStaticText"):
                t = self.title(s).strip()
                if t == name:
                    x, y, w, h = self.get_pos_size(s)
                    if x is not None:
                        cands.append((y, x))
            cands.sort()
            return cands

        cands = collect(panel)
        for y, x in cands[:max_attempts]:
            self.mouse_click(x + 60, y + 12)
            time.sleep(1.6)
            cur = self.get_current_chat()
            if cur and name in cur:
                return cur
            # 没切过去：重新打开搜索再来
            panel, st = self._open_search(name)
            if panel is None:
                return None
            cands = collect(panel)
        return None

    def search_messages(self, keyword):
        """全局搜索聊天记录。返回匹配到该关键词的会话名列表。
        点开"聊天记录"结果会打开独立的搜索聊天记录窗口（带消息预览和进入聊天按钮）。
        """
        panel, st = self._open_search(keyword)
        if panel is None:
            return []
        # AX 遍历顺序不保证空间顺序，先按 y 排序
        entries = []
        for s in self.find_all(panel, role="AXStaticText"):
            t = self.title(s).strip()
            if not t:
                continue
            x, y, w, h = self.get_pos_size(s)
            entries.append((y or 0, t))
        entries.sort()

        chats = []
        in_chatlog = False
        for y, t in entries:
            if t == "聊天记录":
                in_chatlog = True
                continue
            if in_chatlog:
                if t in self.SEARCH_CATEGORIES or t == "搜索网络结果":
                    break
                if t in ("更多", "网络查找微信号："):
                    break
                chats.append(t)
        self.close_search_panel()
        return chats

    def close_search_panel(self):
        """关闭搜索面板（Esc 或点空白处）"""
        panel = self._get_search_panel()
        if panel is not None:
            self.press_key(53)  # Esc
            time.sleep(0.5)

    # ---------- 群消息增强 ----------
    def get_group_messages(self, with_senders=False):
        """读群消息。AX 只给内容不给发送者（微信4.x 限制）；
        with_senders=True 时用 OCR 补全发送者昵称，返回 [(sender, text), ...]。
        OCR 路线稍慢（约2秒），仅群聊需要。"""
        win = self.get_window()
        if not win:
            return []
        # AX 消息列表（快）
        ax_msgs = []
        lists = self.find_all(win, role="AXList", title_contains="消息")
        for lst in lists:
            for k in self.children(lst):
                t = self.title(k)
                if t.strip():
                    ax_msgs.append((k, t))

        if not with_senders:
            return [(None, t) for _, t in ax_msgs]

        # OCR 补发送者：截消息区（窗口右 62%），昵称行在内容行上方且缩进
        import subprocess as sp
        import Quartz
        cgwins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        bounds = None
        for w in cgwins:
            if w.get("kCGWindowOwnerName") == "微信" and w.get("kCGWindowName") == "微信":
                bounds = dict(w.get("kCGWindowBounds") or {})
                break
        if not bounds:
            return [(None, t) for _, t in ax_msgs]
        x0 = int(bounds["X"] + bounds["Width"] * 0.30)
        y0 = int(bounds["Y"] + 50)
        cap_w = int(bounds["Width"] * 0.70)
        cap_h = int(bounds["Height"] - 100)
        import tempfile, os
        tmp = os.path.join(tempfile.mkdtemp(), "grp.png")
        sp.run(["screencapture", "-x", f"-R{x0},{y0},{cap_w},{cap_h}", tmp])
        ocr = self.ocr_image(tmp, return_boxes=True)

        # y 坐标严格配对：OCR 每行带 (y比例, 文本)。
        # 昵称行在消息首行上方，y 差 < 0.06（比例）。
        # AX 消息节点也有 y 坐标（屏幕系），换算成 OCR 比例后配对。
        import re
        TIME_PAT = re.compile(r"^(昨天|今天|星期|上午|下午|\d{1,2}:\d{2}|\d{4})")
        PLACEHOLDER = ("图片", "[图片]", "[动画表情]", "[视频]", "[文件]", "[语音]")

        ax_positions = []  # (screen_y, ax_text)
        for k, t in ax_msgs:
            kx, ky, kw, kh = self.get_pos_size(k)
            if ky is not None:
                ax_positions.append((ky, t.strip()))
        ax_positions.sort()

        # OCR 行 → 屏幕坐标。昵称的 x 比例 < 0.15（消息区左边缘），
        # 引用卡片/内嵌预览 x > 0.5 —— 用 x 过滤掉
        ocr_rows = []  # (screen_y, text, is_left_rail)
        for xr, yr, txt in ocr:
            t = txt.strip()
            if not t or (TIME_PAT.match(t) and len(t) <= 14):
                continue
            screen_y = y0 + yr * cap_h
            ocr_rows.append((screen_y, t, xr < 0.18))

        # 对每条 AX 消息，找它上方最近的"昵称行"（非消息内容、非占位）
        def is_content_line(t):
            for _, axt in ax_positions:
                if t == axt.split("\n")[0] or (len(t) >= 6 and t in axt):
                    return True
            return False

        result = []
        for ay, axt in ax_positions:
            sender = None
            # 昵称紧贴气泡上方（约 20-40px），窗口收紧到 45px
            # 且昵称行通常较短、无句末标点
            best = None
            for oy, ot, is_rail in ocr_rows:
                if not is_rail:  # 只认消息左轨道上的行（昵称列）
                    continue
                if not (-5 <= oy - ay <= 40):  # 昵称文字在消息块顶部内（节点y略小于文字y）
                    continue
                if is_content_line(ot):
                    continue
                if not self._looks_like_nickname(ot):
                    continue
                if best is None or ay - oy < ay - best[0]:
                    best = (oy, ot)
            if best:
                sender = best[1]
            result.append((sender, axt))

        # 前向填充：微信连续发言只在首条显示昵称，
        # 时间戳/系统消息是边界（不继承）
        filled = []
        last = None
        for sender, axt in result:
            is_boundary = bool(TIME_PAT.match(axt.strip())) or axt.strip() in PLACEHOLDER
            if sender:
                last = sender
                filled.append((sender, axt))
            elif is_boundary or last is None:
                filled.append((None, axt))
                if is_boundary:
                    last = None
            else:
                filled.append((last, axt))
        return filled

    def _unused_marker(self):
        return None

    @staticmethod
    def _looks_like_nickname(t):
        """昵称启发式：≤30字，非占位/提及，含足够常用字（滤 OCR 乱码）"""
        PLACEHOLDER = ("图片", "[图片]", "[动画表情]", "[视频]", "[文件]", "[语音]")
        if not t or len(t) > 30 or t.startswith(PLACEHOLDER) or t.startswith("@"):
            return False
        import re
        # 常用汉字+英文+数字占比高；纯生僻/符号组合是乱码
        good = re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}|\d{2,}", t)
        if not good:
            return False
        covered = sum(len(g) for g in good)
        return covered >= max(2, len(t) * 0.5)

    # ---------- 富媒体 ----------
    def ocr_window(self, save_path=None, window_title=None):
        """截图指定窗口并 OCR。window_title=None 时截微信主窗口。
        返回 [(y比例, 文本), ...] 按从上到下排序。"""
        import subprocess as sp
        import Quartz
        target = None
        cgwins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        for w in cgwins:
            if w.get("kCGWindowOwnerName") != "微信":
                continue
            name = w.get("kCGWindowName", "")
            if window_title and name != window_title:
                continue
            if not window_title and name != "微信":
                continue
            b = dict(w.get("kCGWindowBounds") or {})
            if b.get("Width", 0) > 200:
                target = w
                break
        if target is None:
            return []
        if save_path is None:
            import tempfile, os
            save_path = os.path.join(tempfile.mkdtemp(), "ocr_win.png")
        sp.run(["screencapture", "-x", "-l", str(target.get("kCGWindowNumber")), save_path])
        return self.ocr_image(save_path)

    @staticmethod
    def ocr_image(img_path, lang="zh-Hans", return_boxes=False):
        """Vision OCR 一张图片。默认返回 [(y比例, 文本), ...]；
        return_boxes=True 时返回 [(x比例, y比例, 文本), ...]"""
        try:
            from Vision import VNRecognizeTextRequest, VNImageRequestHandler
            from Foundation import NSURL
        except ImportError:
            return []
        from PIL import Image
        # 大图先缩小一半防乱码
        img = Image.open(img_path)
        if img.width > 1500:
            img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
            img.save(img_path)
        url = NSURL.fileURLWithPath_(img_path)
        handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        req = VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLanguages_([lang, "en-US"])
        handler.performRequests_error_([req], None)
        lines = []
        for r in req.results():
            txt = r.topCandidates_(1)[0].string() if r.topCandidates_(1) else ""
            if not txt.strip():
                continue
            try:
                box = r.boundingBox()
                y_rel = 1 - box.origin.y - box.size.height
                if return_boxes:
                    lines.append((box.origin.x, y_rel, txt))
                else:
                    lines.append((y_rel, txt))
            except Exception:
                if return_boxes:
                    lines.append((0, 0, txt))
                else:
                    lines.append((0, txt))
        lines.sort()
        return lines

    def transcribe_voice(self, msg_index=-1, timeout=25):
        """把当前会话里的语音消息转成文字（微信自带「转文字」功能）。

        原理：右键语音气泡 → 上下文菜单点「转文字」→ 等待转换
        → OCR 读取气泡下方出现的文字。微信 4.x 语音文件是 V4 加密
        不落盘明文，UI 路线是唯一可靠通道。

        msg_index: 第几个语音气泡（-1 最新）
        返回: 转写文本，失败返回 None
        """
        win = self.get_window()
        if not win:
            return None

        # 1) 找全部语音气泡（AX title 是 "[语音]"）
        lists = self.find_all(win, role="AXList", title_contains="消息")
        voices = []
        for lst in lists:
            for k in self.children(lst):
                t = self.title(k).strip()
                if t in ("[语音]", "语音"):
                    x, y, w, h = self.get_pos_size(k)
                    if x is not None:
                        voices.append((x, y, w, h))
        if not voices:
            return None
        x, y, w, h = voices[msg_index]

        # 记录转换前该气泡的 y（转换后文字出现在气泡正下方）
        bubble_y = y + (h or 0)

        # 2) 右键语音气泡中心
        cx = x + 100
        cy = y + max((h or 40) / 2, 20)
        self.right_click(cx, cy)
        time.sleep(1.2)

        # 3) 找「转文字」菜单项并点击
        from ApplicationServices import AXUIElementCopyAttributeValue
        err, wins = AXUIElementCopyAttributeValue(self.app, "AXWindows", None)
        clicked = False
        for w in (wins or []):
            t = self.title(w)
            # 上下文菜单的窗口标题可能是会话名/空，找菜单项特征
            for item in self.find_all(w, role="AXMenuItem", title_contains="转文字"):
                ix, iy, iw, ih = self.get_pos_size(item)
                if ix is None:
                    continue
                self.mouse_click(ix + iw / 2, iy + max(ih / 2, 8))
                clicked = True
                break
            if clicked:
                break
        if not clicked:
            self._close_floating_menu()
            print("[voice] 没找到「转文字」菜单项")
            return None

        # 4) 等待转换完成（气泡下方出现文字，从 AX 树找新增文本气泡）
        time.sleep(3.0)
        text = None
        for attempt in range(6):
            win2 = self.get_window()
            new_texts = []
            for lst in self.find_all(win2, role="AXList", title_contains="消息"):
                for k in self.children(lst):
                    t2 = self.title(k).strip()
                    kx, ky, kw, kh = self.get_pos_size(k)
                    if (t2 and t2 not in ("[语音]", "语音", "[图片]", "[文件]")
                            and ky is not None and bubble_y - 15 <= ky <= bubble_y + 130
                            and len(t2) > 1):
                        new_texts.append((ky, t2))
            if new_texts:
                new_texts.sort()
                text = new_texts[0][1]
                break
            time.sleep(2.5)

        # 5) 转换的文字气泡会一直显示，右键可「隐藏转文字」，不强制清理
        return text

    def open_image_viewer(self, msg_index=-1):
        """点击当前会话里的图片消息，打开"图片和视频"查看器。
        msg_index: 第几个图片消息（-1 最新的）。返回查看器窗口标题或 None。"""
        win = self.get_window()
        if not win:
            return None
        lists = self.find_all(win, role="AXList", title_contains="消息")
        pics = []
        for lst in lists:
            for k in self.children(lst):
                t = self.title(k).strip()
                if t in ("图片", "[图片]"):
                    x, y, w, h = self.get_pos_size(k)
                    if x is not None:
                        pics.append((x, y))
        if not pics:
            return None
        x, y = pics[msg_index]
        # 消息气泡缩略图在锚点右侧
        self.mouse_click(x + 150, y + 60)
        time.sleep(2.2)
        from ApplicationServices import AXUIElementCopyAttributeValue
        err, wins = AXUIElementCopyAttributeValue(self.app, "AXWindows", None)
        for w in (wins or []):
            t = self.title(w)
            if t == "图片和视频":
                return t
        return None

    def send_image(self, image_path):
        """通过剪贴板发送图片。CGEvent 的 Cmd+V 不触发粘贴，必须 osascript。"""
        import subprocess as sp
        sp.run(["osascript", "-e",
                f'set the clipboard to (read (POSIX file "{image_path}") as «class PNGf»)'],
               check=True)
        self.activate()
        win = self.get_window()
        if not win:
            return False, "no window"
        tas = self.find_all(win, role="AXTextArea")
        target = None
        for ta in tas:
            t = self.title(ta)
            if t and t != "搜索":
                target = ta
                break
        if target is None:
            return False, "找不到输入框"
        x, y, w, h = self.get_pos_size(target)
        if x is not None:
            self.mouse_click(x + 200, y + 30)
        time.sleep(0.5)
        sp.run(["osascript", "-e",
                'tell application "System Events" to keystroke "v" using command down'],
               check=True)
        time.sleep(1.2)
        if "\uFFFC" not in self.value(target):
            return False, "图片未进入输入框"
        self.press_key(36)
        time.sleep(1.5)
        return True, "图片已发送"

    @staticmethod
    def find_wechat_file(filename_prefix, months=None):
        """在微信文件缓存里找接收的文件（明文，可直接读）。
        months: 如 ["2026-10"]，None 时扫全部月份目录。"""
        import os, glob
        base = os.path.expanduser(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
        results = []
        for wxid in os.listdir(base):
            fdir = os.path.join(base, wxid, "msg", "file")
            if not os.path.isdir(fdir):
                continue
            for m in (months or os.listdir(fdir)):
                for p in glob.glob(os.path.join(fdir, m, filename_prefix + "*")):
                    results.append(p)
        return results

    @staticmethod
    def read_pdf(pdf_path, max_pages=10):
        """读 PDF 文本（pymupdf）。返回 [页文本, ...]"""
        import pymupdf
        doc = pymupdf.open(pdf_path)
        return [doc[i].get_text() for i in range(min(max_pages, doc.page_count))]

    def _switch_via_list(self, name, exact=False):
        """会话列表直接点击。点击后立刻验证，防止点错。"""
        win = self.get_window()
        if not win: return None
        items = self.find_all(win, role="AXStaticText", title_contains=name)
        for item in items:
            t = self.title(item)
            first_line = t.split("\n")[0].strip()
            if (exact and first_line == name) or (not exact and name in first_line):
                x, y, w, h = self.get_pos_size(item)
                if x is None or y is None:
                    continue
                cy = y + 16 if (h or 0) < 5 else y + h/2
                cx = x + 100 if (w or 0) < 5 else x + w/2
                self.mouse_click(cx, cy)
                time.sleep(1.2)
                return self.get_current_chat()
        return None

    def send_message(self, text, confirm_chat=None):
        """向当前会话发送文本。confirm_chat: 期望会话名，不符则拒绝发送。
        自动防御：发送前检测弹窗（取消/确定）和侧面板（退出群聊）并按 Esc 清理。"""
        win = self.get_window()
        if not win:
            return False, "no window"

        # 防御0：窗口出屏检查——按钮坐标在屏幕外时点击全部无效（假成功）
        # 窗口右界超屏则复位窗口到 (200,30) 1200x934
        try:
            import Quartz
            main_w = Quartz.CGDisplayPixelsWide(Quartz.CGMainDisplayID())
            bounds = None
            cgwins = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
            for w in cgwins:
                if (w.get("kCGWindowOwnerName") == "微信"
                        and w.get("kCGWindowName") == "微信"):
                    bounds = dict(w.get("kCGWindowBounds") or {})
                    break
            if bounds and bounds["X"] + bounds["Width"] > main_w + 10:
                import subprocess as sp
                sp.run(["osascript", "-e",
                        'tell application "System Events" to tell process "WeChat" '
                        'to set position of window 1 to {200, 30}'], check=False)
                sp.run(["osascript", "-e",
                        'tell application "System Events" to tell process "WeChat" '
                        'to set size of window 1 to {1200, 934}'], check=False)
                time.sleep(1.0)
                win = self.get_window()
        except Exception:
            pass

        # 防御1：确认弹窗（取消/确定按钮）——点击会被拦截
        cancels = self.find_all(win, role="AXButton", title_contains="取消")
        if cancels:
            self.press_key(53)  # Esc 关弹窗
            time.sleep(0.6)
            win = self.get_window()
        # 防御2：群资料侧面板（含"退出群聊"）——布局被压缩，按钮坐标失效
        for b in self.find_all(win, role="AXButton"):
            if "退出" in self.title(b) or "清空聊天记录" in self.title(b):
                # 面板开着：点"聊天信息"按钮收起
                info_btns = self.find_all(win, role="AXButton", title_contains="聊天信息")
                if info_btns:
                    ix, iy, iw, ih = self.get_pos_size(info_btns[0])
                    if ix is not None:
                        self.mouse_click(ix + 25, iy + 12)
                        time.sleep(1.0)
                win = self.get_window()
                break
        tas = self.find_all(win, role="AXTextArea")
        target = None
        for ta in tas:
            t = self.title(ta)
            if t and t != "搜索":
                target = ta
                break
        if target is None:
            return False, "找不到输入框"
        chat = self.title(target)
        if confirm_chat and confirm_chat not in chat:
            return False, f"当前会话是 {chat!r}，期望 {confirm_chat!r}，拒绝发送"

        # AXValue 直写（微信 4.x 的 IsAttributeSettable 返回假但直写实际生效，
        # 所以直接写，读回验证，失败才走 CGEvent 兜底）
        ok_input = False
        AXUIElementSetAttributeValue(target, "AXValue", text)
        time.sleep(0.5)
        cur = self.value(target)
        if text in cur:
            ok_input = True
        else:
            # 清空残留后改走 CGEvent
            AXUIElementSetAttributeValue(target, "AXValue", "")
            time.sleep(0.2)
            x, y, w, h = self.get_pos_size(target)
            if x is not None and y is not None:
                self.mouse_click(x + (w or 200)/2, y + (h or 40)/2)
                time.sleep(0.3)
            self.type_text(text)
            time.sleep(0.6)
            cur = self.value(target)
            if text in cur:
                ok_input = True

        if not ok_input:
            return False, f"输入失败，输入框内容: {cur[:60]!r}"

        # 发送：微信 4.x 的按钮对 AXPress 不响应，必须真实坐标点击
        btns = self.find_all(win, role="AXButton", title_contains="发送")
        for b in btns:
            if self.title(b) == "发送":
                bx, by, bw, bh = self.get_pos_size(b)
                if bx is not None:
                    self.mouse_click(bx + 30, by + 10)
                    time.sleep(1.2)
                    # 真实验证（防假成功）：输入框必须已清空
                    cur2 = self.value(target)
                    if text[:10] in cur2:
                        # 没发出去，回车键重试一次
                        self.press_key(36)
                        time.sleep(1.0)
                        cur3 = self.value(target)
                        if text[:10] in cur3:
                            return False, "发送失败（内容滞留输入框）"
                    return True, f"已发送到 {chat}"
                return False, "发送按钮无坐标"
        return False, "找不到发送按钮"


if __name__ == "__main__":
    wx = WeChatAX()
    wx.activate()
    win = wx.get_window()
    print("当前会话:", wx.get_current_chat())
    print("消息数:", len(wx.get_messages()))
