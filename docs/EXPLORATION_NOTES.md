# 微信 4.x (macOS) 自动化踩坑实录

> 2026-10-12 深度探索笔记。环境：微信 4.1.15 / M1 Mac mini / macOS / SIP 开启。
> 全程使用辅助功能（AX）+ CGEvent + Vision OCR，未注入未 hook 未改二进制。

## 一、AX 通道的坑

### 1. 窗口必须激活
微信主窗口**非激活状态**下 `AXWindows` 返回空（0 个窗口），AX 通道静默断开。
每次操作前先 `open -a WeChat` 激活。判断特征：`AXFocusedWindow` 报 `-25212 (NoValue)`。

### 2. `AXIsProcessSettable` 是骗子
对输入框查询 `AXValue` 是否可写返回 **False**，但直接 `AXUIElementSetAttributeValue` 写入**实际生效**。
永远不要信 API 声明——直接写 + 读回验证。

### 3. `AXPress` 基本没用
微信 4.x 的按钮（发送、列表项、搜索结果）对 `AXPress` 大多不响应。
例外：搜索聊天记录窗口的关闭按钮（AXCloseButton）在 AXRaise 后 AXPress 有效。
通用方案：CGEvent 合成真实坐标点击。

### 4. 虚拟化列表
会话列表、消息列表的可见项 `size` 是 `0x0`，但 `position` 有效。
点击策略：x 偏移 +100~150，y 偏移 +16（会话项）/+60~80（消息卡）。

### 5. AX 遍历顺序 ≠ 视觉顺序
`AXChildren` 返回顺序与屏幕上下位置无关。解析列表必须按 y 坐标重排。

### 6. 坐标缓存失效
窗口移动/侧面板弹出后，AX 报的元素坐标可能是**旧布局的**。
操作前重新 `get_pos_size`；发现点击无效时先检查有没有弹窗/面板。

## 二、微信 4.x 界面行为

### 7. 群资料侧面板 & 确认弹窗
- 误点"聊天信息"打开侧面板 → 消息区被压缩 → 所有按钮坐标失效
- 误点危险项（清空聊天记录/退出群聊）弹确认框 → 挡住所有点击
- **防御**（已内置 send_message）：发送前检测"取消"按钮（Esc 关）和"退出群聊"（点"聊天信息"收起）

### 8. 搜索面板结构
搜索框（Cmd+F 聚焦）接受 `AXValue` 直写（合成键盘输入无效）。
结果面板是**独立无标题 AXWindow**，分类头不固定：
`最常使用 / 群聊 / 联系人 / 功能 / 聊天记录 / 搜索网络结果 / 更多 / 网络查找微信号：`

**"搜索网络结果"下全是网络联想词**（点击是网页搜索），真实入口在后面分类下。
容错策略：收集所有同名候选 → 逐个点击 → `get_current_chat()` 读回验证 → 失败换下一个。

### 9. 会话点击后列表重排
点击会话项触发列表重排（新消息置顶），后续坐标全部漂移。
每次操作前重新定位。

### 10. WeChatAppEx webview 免疫合成事件
公众号订阅流、"看一看"推荐流是 WeChatAppEx 渲染：
- hover 有反应（转发/收藏按钮会出现）
- **点击无反应**（CGEvent 和 AXPress 都不行）
- 只有**原生消息气泡**里的文章卡片可以点开

## 三、富媒体阅读

### 11. 文章浏览器
点开文章 → 独立窗口 `"微信 (窗口)"`（920x890），AX 树几乎为空（webview）。
只能：`screencapture -l <windowID>` 截图 → Vision OCR。
OCR 大图（>1500px 宽）会乱码，先缩到一半。

### 12. 文件缓存是明文
收到的文件在 `msg/file/YYYY-MM/<原名>` 直接可读（PDF 用 pymupdf 提取）。
路径模板：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/msg/file/`

### 13. 图片缓存是密文
`msg/attach/<hash>/YYYY-MM/Img/*.dat`，V4 加密（文件头 `0708563208070004`），需密钥。
绕过：点开图片 → "图片和视频"窗口 → 截图 → OCR。

### 14. 剪贴板发图
CGEvent 合成的 Cmd+V **不触发**粘贴板读取！必须：
```bash
osascript -e 'tell application "System Events" to keystroke "v" using command down'
```
流程：osascript 复制图片到剪贴板 → 点击输入框聚焦 → osascript Cmd+V → Enter。

## 四、群发送者识别（OCR 配对方案）

AX 树不暴露发送者昵称。方案：**AX 内容 + OCR 昵称 + y 坐标配对**。

关键参数（迭代 10 版）：
- 截图区域：窗口右侧 70%（从 30% 处开始），含昵称轨道
- 昵称行：OCR x 比例 < 0.18（左轨道），引用卡片 x > 0.5
- y 配对：昵称文字 y 在消息块节点 y 的 **-5 ~ +40px 内**（AX 节点 y 是块顶边，略小于文字 y）
- 昵称启发式（`_looks_like_nickname`）：头像图形会被 OCR 幻觉成乱码文字，用"连续常用字符占比 ≥ 50%"过滤
- **前向填充**：微信连续发言只显示首个昵称，后续消息继承上一 sender；时间戳是边界

## 五、坐标系备忘

- `screencapture -R` 是**逻辑坐标**；全屏截图是 Retina 2x 物理像素
- OCR 的 boundingBox 是图像归一化坐标（0-1）
- AX 的 position 是屏幕逻辑坐标
- 换算：屏幕 x = 图像 x / 截图宽 × 截图逻辑宽 + 截图起点 x

## 六、能力矩阵总结

| 操作 | 路线 | 可靠性 |
|---|---|---|
| 读消息文本 | AX | ★★★★★ |
| 切会话 | 搜索面板（优先）/ 列表点击 | ★★★★☆ |
| 发文本 | AXValue 直写 + 坐标点发送 | ★★★★☆（有防御后） |
| 发图片 | 剪贴板 + osascript | ★★★★☆ |
| 读文章 | 截图 + OCR | ★★★☆☆（依赖 OCR 质量） |
| 读 PDF | 缓存直读 | ★★★★★ |
| 读图片 | 点开 + 截图 + OCR | ★★★☆☆ |
| 群发送者 | OCR y 配对 | ★★★☆☆（~80%） |
| 订阅流/webview | ❌ 不可行 | 免疫合成事件 |
