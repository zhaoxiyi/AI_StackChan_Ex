# 下一阶段功能设计 (2026-10)

> 阶段目标: ①联网信息助手 (免 approve, 后台异步); ②DeepSeek Harness 调度接口 (预留);
> ③实时翻译模式 (渐进修正 + 落盘 + 面板)。
> 本文为架构设计, 分步实现; 所有"需口头允许"的联动均已标注。

---

## 0. 总览与落地顺序

| 目标 | 依赖 | 建议顺序 | 授权要求 |
|------|------|---------|---------|
| ③ 实时翻译模式 | 纯 server (STT/LLM/落盘/面板) | **先做** (最独立, 面板收益直观) | 无 |
| ① 联网信息助手 | server + 搜索源 + LLM 提炼 | 次之 | 无 (白名单内, 免 approve) |
| ② Harness 调度 | Harness 接口 (同机) | 接口预留 → 下轮实现 | **口头允许** |

三者共享三个复用点: **动态背景块 (Prompt 槽位)**、**落盘目录规范**、**AdminEvent/SSE 面板通道**。

---

## 1. 联网信息助手 (免 approve, 后台异步)

### 1.1 目标
- 用户问"今天天气/最新新闻/帮我查…"时, **不阻塞当前对话**, 后台异步完成:
  搜索 → LLM 提炼 → 落盘 → **适时播报**;
- **无需授权** (本地白名单功能);
- **Prompt 中只保留一个"后台任务"描述槽位直至播报完成/放弃; 播报后槽位移除,
  只留播报内容作为正常对话历史**。

### 1.2 数据流

```
用户语句完整提交 (_process_utterance)
  ① needs_web 判定 (复用 IntentRouter._needs_web / 词表可扩)
  ② 注册 WebQueryTask(query), 背景块追加 [后台任务]:"搜索中: '<query>'"
  ③ asyncio.create_task(web_query_worker):
       search_provider.search(query) → top-k 摘要
       → LLM 提炼 (走现有引擎池, max_tokens≈256)
       → 落盘 cache/web_queries/<ts>_<slug>.json (status=done)
  ④ 本轮回复不等待: LLM 正常回 "我去查一下…" (无结果注入)
  ⑤ 结果回流 (两档):
       - 快 (<inline_wait_s): 本轮合成前融入回复
       - 慢: 下一轮交互前, 若存在"已完成未播报"任务 →
         背景块 [后台任务] 注入 "已完成: '<query>' 摘要: …" + 主动播报指令
  ⑥ 播报完成: 槽位移除; 播报内容留在正常历史
  ⑦ 放弃: 超时 (timeout_s) → 标记 EXPIRED, 槽位移除 (落盘保留供回顾)
```

### 1.3 Prompt 槽位 (核心设计)

背景块 (现有分层结构 [3]) 增加最后一个小节 (**变得最快, 放最靠后**):

```
[后台任务 (供参考; 播报后自动移除, 勿复述本节)]
- 搜索中: '今天北京天气' (已 12s)
- 已完成待播报: '比特币价格' | 摘要: …
```

约束: **只有"未播报"的任务出现在此**; 播报过的内容以普通 assistant 轮保留 —
即"只保留播报历史作为正常 Prompt 历史"。

### 1.4 组件与接口

```python
# orchestration/web_query.py
class WebQueryTask:          # id/query/status(RUNNING|DONE|FAILED|EXPIRED)/created/result_path/summary
class WebQueryManager:       # 每会话持有; register()/done_pending()/mark_broadcast()/expire_stale()
class SearchProvider(Protocol):   # async search(query) -> list[Snippet]
#     实现: SearXNG(推荐, 自建/内网) | Brave API(需 key) | DDG HTML(无 key 兜底)
```

### 1.5 落盘格式

`server/cache/web_queries/<YYYYmmdd-HHMMSS>_<slug>.json`
```json
{ "ts": 0, "session": "…", "query": "…", "status": "done",
  "sources": [{"title": "", "url": "", "snippet": ""}],
  "summary": "…", "broadcast": true, "broadcast_ts": 0 }
```

### 1.6 参数 (config.yaml 新增 `web_query` 段)

| 参数 | 默认 | 说明 |
|------|------|------|
| `enabled` | true | 总开关 |
| `provider` | "searxng" | 搜索源 |
| `endpoint` | "http://localhost:8080/search" | SearXNG 地址 |
| `max_results` | 5 | 取前 N 条 |
| `inline_wait_s` | 2.5 | 本轮内联等待上限 |
| `timeout_s` | 120 | 后台任务放弃时限 |
| `summary_max_tokens` | 256 | LLM 提炼输出上限 |

---

## 2. DeepSeek Harness 调度 (评估 + 接口预留)

### 2.1 可行性
- Harness 与 vLLM **同机** → server 经本机 HTTP 直连, 无跨网成本;
- 适合"重任务外包"(长分析/工具链编排); 延迟 = 提交 + 轮询, 属异步任务型;
- 风险: 长任务生命周期管理、失败重试、接口协议未定 (需 Harness 侧提供)。

### 2.2 预留接口 (本轮仅接口 + Null 实现)

```python
# orchestration/harness.py
class HarnessStatus: ...        # RUNNING|DONE|FAILED + output
class HarnessClient(Protocol):
    async def submit(self, task: str, ctx: dict) -> str: ...   # -> task_id
    async def poll(self, task_id: str) -> HarnessStatus: ...

class NullHarnessClient:        # 未配置时 no-op (接口占位)
```

- 配置: `routing.harness = {enabled: false, endpoint: "", api_key: "", poll_interval_s: 2}`;
- **授权: 口头允许** — 复用 `AuthorizationManager` (新增 intent="harness"),
  授权话术示例: "这个任务需要交给 Harness 处理, 可以吗?";
- 结果回流复用 §1 的 `[后台任务]` 槽位与落盘机制 (cache/harness/)。

---

## 3. 实时翻译模式

### 3.1 目标与行为
- 进入/退出: 触摸长按或语音命令 ("翻译模式" / "退出翻译");
- 模式内:
  - **不打断** (barge-in 判定旁路);
  - **不播报** (特殊沟通除外: 退出确认 / "刚才说什么"回读);
  - **持续翻译**当前收听内容, 允许粗糙断句, **渐进修正**。

### 3.2 渐进式翻译流水

```
STT (粗断句, silence_break 800ms; 或最长 force_s 强制断段)
  → TranslationUnit(seg_id, src)         # 原文(可后续微调)
  → LLM 翻译调用 (每新段触发):
      Prompt: [system 翻译引导] + [最近 N 对已译 (原文/译文)] + [本段原文]
      指令: "翻译本段; 并结合上下文修正前 1-2 段译文 (如更准确)。
             输出 JSON: {revised:[{seg, tgt}...]}"
  → 更新 TranslationLog (版本追加, 不覆盖) → 落盘 + 面板事件
```

### 3.3 Prompt 策略 (对齐现有分层/prefix cache 结构)

```
[system]  恒定翻译引导: 角色/输出语言/风格/术语表 (永不变化)
[历史]    最近 N=8 对 (src/tgt) — 窗口滑动
[当前]    本段原文 + "修正前 1-2 段" 指令 (每段变化, 放最后)
```

### 3.4 落盘与面板

- 落盘: `server/cache/translations/<session>_<YYYYmmdd>.jsonl`, 每行:
  ```json
  {"ts": 0, "seg": 12, "src": "…", "tgt": "…", "rev": 2, "final": false}
  ```
- **面板**: 复用 `AdminEvent` (新增 `type="translation"`, 字段: seg/src/tgt/rev)
  → SSE `/admin` → panel (已有"翻译按钮"占位, 前端加双栏"原文/译文"区域)。

### 3.5 总结归纳 (未来联动预留)

- 模式退出/定时 → 读取当日 jsonl → LLM 总结 (纪要/关键词/待办) → 落盘 `summary.md`;
- **联动接口预留**: 总结文本可作为"任务"提交给 Agent / MCP / Harness —
  **与 §2 同一授权机制 (口头允许)**。

### 3.6 组件与参数

```python
# orchestration/translation.py
class TranslationMode:   # 开关/旁路 barge-in/驱动翻译流水
class TranslationLog:    # 版本化日志 + jsonl 落盘
class Translator:        # LLM 调用封装 (引擎池复用)
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `segment_window` | 8 | 历史翻译对窗口 |
| `revise_depth` | 2 | 可修正的前段数 |
| `segment_force_s` | 6 | 强制断段时长 |
| `min_segment_chars` | 4 | 过短段不翻译 |

---

## 4. 与现有架构的衔接 (复用清单)

| 复用点 | 现有实现 | 用于 |
|--------|---------|------|
| 动态背景块 (Prompt 槽位) | `build_messages` [3] 段 | [后台任务] 注入 |
| 授权管理 | `AuthorizationManager` | Harness / 云联动口头允许 |
| 面板通道 | `AdminEvent` + SSE `/admin` + panel | 翻译实时展示 |
| 引擎池 | `LLMEngine.feed` | 搜索提炼 / 翻译 / 总结 |
| 落盘规范 | 新增 `server/cache/` (web_queries/ translations/ harness/) | 中间结果与回顾 |

## 5. 落地步骤建议

1. **实时翻译模式** (独立): translation.py + 面板事件 + 落盘 → 实机验证;
2. **联网信息助手**: SearchProvider (先 DDG 兜底/SearXNG) + web_query.py + [后台任务] 槽位 → 实机验证;
3. **Harness**: NullHarnessClient 接口先行 → 待 Harness 接口确定后接实现 (口头允许)。
