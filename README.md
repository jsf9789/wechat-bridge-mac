# WeChat Bridge Mac

**让 AI Agent 操作 macOS 上的微信 4.x**：读消息、切会话、发消息、搜聊天记录、读文章/PDF/图片——纯系统辅助功能（Accessibility）+ 合成事件，**零注入、零 hook、零协议模拟**。

> 对微信来说，你的 Agent 就是一个"用鼠标键盘的人类"。没有封号层面的额外暴露面。

## 能力总览

| 能力 | 方法 / 命令 | 状态 |
|---|---|---|
| 读当前会话消息 | `get_messages()` / `wxctl read 20` | ✅ |
| 读群消息**含发送者昵称** | `get_group_messages(with_senders=True)` | ✅ |
| 切换会话（列表点击+搜索双路径） | `switch_chat(name)` / `wxctl switch` | ✅ |
| **全局搜索**切换会话 | 同上（搜索优先，不受列表重排影响） | ✅ |
| 全局聊天记录搜索 | `search_messages(keyword)` / `wxctl search --messages` | ✅ |
| 发文本消息（带会话守门） | `send_message(text, confirm_chat=...)` | ✅ |
| 发图片（剪贴板方案） | `send_image(path)` | ✅ |
| 读公众号/服务号文章全文 | 点卡片 → 截图 → OCR | ✅ |
| 读 PDF/文件（零 UI，直接读缓存） | `find_wechat_file()` + `read_pdf()` | ✅ |
| 读图片内容 | 点开 → 截图 → OCR | ✅ |
| 任意窗口截图 OCR | `ocr_window()` / `ocr_image()` | ✅ |

## 环境要求

- macOS（Apple Silicon / Intel），**SIP 无需关闭**
- 微信 4.x（已验证 4.1.15），保持登录
- Python 3.10+，依赖：`pyobjc-framework-ApplicationServices`（AX API）、`pyobjc-framework-Quartz`（事件/截图）、`Pillow`（图像处理）、`pymupdf`（PDF）
- **辅助功能权限**：运行 Python 的终端/宿主进程需在 系统设置 → 隐私与安全 → 辅助功能 列表中
- 自动化运行时屏幕不能锁定（锁屏后 AX 通道断开）

```bash
pip install pyobjc-framework-ApplicationServices pyobjc-framework-Quartz Pillow pymupdf
```

## 快速开始

### 作为库

```python
from wechat_ax import WeChatAX

wx = WeChatAX()
wx.activate()                      # 激活微信窗口（必须，AX 通道才完整）

wx.switch_chat("某某群")     # 切到任意会话/群
msgs = wx.get_messages()           # 读最近消息（~40 条可见区）
msgs = wx.get_group_messages(with_senders=True)  # 群消息+发送者

wx.send_message("财报落地前不敢动", confirm_chat="某某群")
#  ^ confirm_chat 守门：当前会话不是它就拒绝发送，绝不误发
```

### 作为 CLI（给 Agent 调用最方便）

```bash
python wxctl.py status                    # 当前会话 + 最近消息
python wxctl.py sessions                  # 列出可见会话
python wxctl.py switch "某某群"             # 切换会话
python wxctl.py read 20                   # 读最近 20 条
python wxctl.py send "文件传输助手" "消息"   # 切换并发送（带守门）
python wxctl.py send-here "消息"           # 发到当前会话
python wxctl.py search "关键词"             # 搜索切换会话
python wxctl.py search "关键词" --messages  # 全局搜聊天记录
```

所有输出 JSON（`ensure_ascii=False`），Agent 可直接解析。

## 架构原理

```
Agent
 │ wechat_ax.py（库） / wxctl.py（CLI）
 ▼
macOS Accessibility API (AXUIElement)      ← 读：窗口树/消息/输入框/按钮
   ├─ AXValue 直写输入框                    ← 写：文本直接进框（绕过键盘模拟）
   ├─ CGEvent 合成鼠标/键盘                 ← 写：真实坐标点击（按钮不理睬 AXPress）
   └─ Vision OCR + screencapture           ← 读：webview 内容/图片/发送者昵称
 ▼
微信 4.x（原生壳 + WeChatAppEx webview）
```

### 为什么可行（微信 4.x 的 10 个关键坑）

1. **窗口必须激活**（`open -a WeChat`）后 `AXWindows` 才有值
2. 虚拟化列表：可见项 `size=0x0` 但 `position` 有效——点击用坐标
3. **`AXIsProcessSettable` 撒谎**（返回 False）但 `AXValue` 直写实际生效——直接写+读回验证
4. 按钮/列表项对 `AXPress` 不响应——必须 CGEvent 真实坐标点击
5. 点击会话项后列表**重排**——切换后必须读回会话名验证
6. 搜索面板分类头不固定（最常使用/群聊/联系人/功能/聊天记录/搜索网络结果）——用"逐候选点击+读回验证"容错
7. "搜索网络结果"下是**网络联想词**（点了是网页搜索）——只点真实分类下的项
8. AX 子元素遍历顺序 ≠ 视觉顺序——按 y 坐标排序
9. WeChatAppEx webview（订阅流/看一看）**免疫合成点击**——只有原生聊天气泡里的卡片可点开
10. 群资料侧面板/确认弹窗会**让 AX 坐标缓存失效**——发送前自动检测清理（已内置防御）

详见 `docs/EXPLORATION_NOTES.md`（完整踩坑实录）。

## 群消息发送者识别（独家方案）

微信 4.x 的 AX 树**不暴露发送者昵称**。本项目用 **AX 内容 + OCR 昵称 + y 坐标配对**：

- AX 消息节点 y 是消息块顶边，OCR 昵称文字 y 在块内 0~40px
- 昵称在 OCR 左轨道（x 比例 < 0.18），引用卡片在 x > 0.5
- 昵称启发式过滤（`_looks_like_nickname`）滤掉头像图形的 OCR 幻觉
- 微信连续发言只显示首个昵称 → 前向填充 + 时间戳边界

## 安全边界（务必遵守）

- **发送守门**：`send_message(confirm_chat=...)` 强制校验当前会话，切不过去拒绝发送
- **测试只用文件传输助手**（发给自己），绝不往真实群聊发测试消息
- 自动化运行时**用户不要同时操作电脑**（抢焦点会导致点击落错位置）
- 敏感操作（转账、加人、退群）不要自动化——本项目未封装这些且不建议
- 微信必须保持登录，屏幕不能锁定

## 项目结构

```
wechat-bridge-mac/
├── wechat_ax.py          # 核心库（WeChatAX 类，~700 行）
├── wxctl.py               # CLI（JSON 输出，Agent 友好）
├── test_final.py          # 端到端验收测试（文件传输助手收发）
├── examples/              # 使用示例
│   ├── group_reply.py     # 群互动闭环（读→理解→回）
│   └── article_reader.py  # 读公众号文章全文
└── docs/
    └── EXPLORATION_NOTES.md  # 深度踩坑实录（微信4.x 逆向笔记）
```

## 局限

- 只能读当前打开会话的**可见消息**（~40 条）；更早的走搜索或数据库方案（需关 SIP，另条路线）
- 图片/语音消息在 AX 树是占位符；图片走"点开→截图→OCR"路线
- 图片缓存 `.dat` 是 V4 加密格式，需密钥——本项目绕过（UI 截图）
- 收到的**文件**是明文缓存（`msg/file/YYYY-MM/`），直接可读
- 发消息期间微信窗口会被抢到前台

## 已验证场景（2026-10-12，微信 4.1.15 / M1 Mac mini / macOS）

- 文件传输助手收发文本、图片 ✓
- 真实群（某某群）读消息+发送者识别 ✓
- 真实群（某某群）结合话题互动回复 ✓
- 盐言故事服务号文章全文 OCR 阅读 ✓
- 《刷公版命令改3码命令.pdf》全文提取 ✓
- 全局聊天记录搜索 ✓

## License

MIT

## 微信管家守护进程（wechat_butler.py）

监控指定会话（默认"张三"），检测到新消息后以管家/顾问口吻自动回复。

```bash
# 前台跑（Ctrl+C 退出）
python wechat_butler.py --chat 张三 --interval 15

# 观察模式（只检测不发送）
python wechat_butler.py --dry-run

# 后台常驻
nohup python -u wechat_butler.py --interval 15 > /tmp/butler.log 2>&1 &
```

架构：
```
轮询(15s) → switch_chat → 读消息快照 → 全量比对检测新消息
  → processed 集合防重复（同一条绝不回两次）
  → self_sent 集合防自嗨（自己的回复不当新消息）
  → 本地 ollama (qwen2.5:1.5b) 生成管家式回复（≤80字，干练不客套）
  → send_message（confirm_chat 守门）
```

依赖：本机 `ollama serve` + `ollama pull qwen2.5:1.5b`（约 1GB，本地跑无 API 费用）。
换模型改脚本顶部 `MODEL` 常量。

坑（实测踩过）：
- **绝不能用快照差异做唯一判据**：回复后列表刷新延迟会导致同一消息被处理多次 → 必须用 processed 集合按消息文本去重
- 启动前已存在的消息只记基线不回复（防止启动瞬间连环回复历史消息）

## v2 角色修正（23:24）

**架构澄清**：Mac 上登录的微信号 = 管家号；会话里的联系人（"张三"）= 主人本人（手机）。

v1 的致命错误：把"任何新消息"都当主人的指示——包括管家自己发的，导致自嗨。
v2 修正：
- **OCR 气泡左右判定**：主人消息在左（x 比例 < 0.18），管家自己在右（≥ 0.20）
- 只回复左侧消息，右侧只做对话语境
- 实测：管家自己发消息 → 完全静默 ✓

左右判定原理：AX 节点 x 都是同一列锚点（无法区分），必须截图 OCR 气泡实际渲染位置。

## 零遮挡方案：微信全屏独立桌面（00:45）

**痛点**：GUI 自动化一晚翻车三次都是窗口遮挡（WorkBuddy 窗口盖住微信导致 OCR 读到错误内容）。

**结论（踩坑实录）**：
- macOS 没有程序化创建 Spaces 的公开 API（Mission Control 无 AX 元素、单 Space 时不显示条、快捷键状态难控、拖边缘建桌面无效）
- 但微信窗口支持 **AXFullScreenButton**（标题栏绿色按钮）AXPress → **全屏 = 独占 Space**

**方案**：`move_wechat_to_space.py`——AXPress 全屏按钮，微信独占一个桌面：
- 永不被任何窗口遮挡（物理隔离）
- AX / 截图 / OCR 全链路正常（已验证，OCR 质量反而更好——全屏窗口更大文字更清晰）
- 屏幕逻辑分辨率实测 1920x1080（此前误以为 2048，是 2x 截图混淆）

注意：全屏后每次 activate 有 2-3 秒桌面切换动画，轮询要接受这个开销。
