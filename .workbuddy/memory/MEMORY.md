# 皇帝后宫模拟器（suhu）开发记录

## UI优化记录
1. game.html tabs改为固定底部导航栏（bottom-nav），wechat风格
2. style.css新增.bottom-nav/.nav-item样式
3. 移动端修复：textarea宽度（flex:1但显式加width:100%）、attribute-changes加overflow-x:auto、modal-content加max-height:80vh/overflow-y:auto
4. toast bottom改为80px并更新fadein/fadeout动画keyframe
5. showConfirmDialog dialog加max-height:85vh/overflow-y:auto
6. Render超时建议timeout从60s改为25s

## 剧情对话着色功能（2026-03-25）
- 新增流式输出对话文字着色：检测 `""` 包裹的人物对话，实时渲染为紫色斜体
- 实现方式：
  - style.css: 新增 `.dialogue-text { color: #7B3F9B; font-style: italic; }`
  - game.html: 新增 `inDialogue` 全局状态变量跟踪引号状态
  - game.html: 新增 `processDialogueText()` 函数处理引号检测和HTML生成
  - game.html: 流式输出 chunk 处理改用 `streamingText.innerHTML = processDialogueText(fullStory)`
- 引号检测支持：中文左引号 `"` (8220)、中文右引号 `"` (8221)、ASCII `"` (34)
- 每次executeAction开始时重置 `inDialogue = false`
- 显示效果：对话内容为紫色斜体，引号保留显示，对话前后换行
- 注意：不处理单引号 `''` 和书名号 `《》`

## Render超时修复（2026-03-26）
- 问题：Render免费版30秒超时导致大模型调用被切断
- 解决方案：任务队列 + 前端轮询
- 后端实现：
  - 新增 `tasks` 字典存储任务状态（pending/running/done/error）
  - 新增 `run_execute_action_task()` 后台线程函数执行大模型调用
  - 修改 `/api/execute_action` 立即返回任务ID，不等待大模型
  - 新增 `/api/get_task_result/<task_id>` 轮询接口获取结果
  - API timeout 从60秒改为120秒
- 前端实现：
  - `executeAction()` 改为先获取任务ID，再轮询结果
  - 新增 `pollTaskResult()` 轮询函数，每秒查询一次
  - 最长等待10分钟
- 效果：后端请求1秒内返回，避开30秒限制；大模型可运行任意时长
