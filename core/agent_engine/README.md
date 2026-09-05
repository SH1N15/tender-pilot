# core/agent_engine — P-D1 LangGraph 主编排图

## 概览

本模块把 LangGraph 编排从"真代码 0 调用"（旧 orchestrator DSL 演示）重构为真实可运行的
主编排骨架。拓扑：

```
招标解读(ReAct) → [资格专家 ‖ 技术专家 ‖ 商务专家](并行) → 规则门(确定性)
→ 风险汇总(确定性) → 决策包生成 → HITL决策门(interrupt) → 终态
```

## 文件

| 文件 | 职责 |
| --- | --- |
| `iron_rules.py` | 五条铁律常量（任务书第 3 节）：ReAct 白名单 / 结构化节点禁 ReAct / ReAct 四件套 / 禁纯 LLM 定级 / 超时门型 / 改判必带理由 |
| `state.py` | `GraphState`（TypedDict + reducer）：expert_results/rule_results 合并、决策包四要素、override_reason 写回 |
| `react_node.py` | 解读节点：复用 `agent_framework/agent.py` think_and_act 真循环；四件套（max_iterations / grant_tools 白名单 / 证据门占位 passthrough / RunMetrics 记账）；检索工具显式传 collection_name |
| `experts.py` | 三专家窄职责节点：Pydantic 输入输出 schema，LLM 前置（输入不足→skipped）与后置确定性校验器，有界重试后降级标注"校验未过" |
| `rule_gate.py` | 规则门确定性节点（经 `services/check/graph_adapter.py` 只读包装调 22 项现有检查）+ 风险汇总 |
| `decision.py` | 定级映射（`map_rules_to_level`，确定性可单测）+ 决策包四要素构建 + LLM 解释文字 + 超时策略（`resolve_pending_gate`） |
| `metrics.py` | RunMetrics 每节点 LLM 调用数/token/耗时；CountingLLM 透明计数代理（调用数+真实 token：优先聚合网关 `_token_usage` 累计列表的增量，退化到 core/tracing 的 llm span `token_usage`） |
| `checkpoint.py` | `PGCheckpointSaver`：自研 JSONB 异步 saver（graph_checkpoints / graph_checkpoint_writes 表，Alembic revision `c5d9e1f7a2b4`） |
| `orchestrator.py` | `BidGraphOrchestrator`：build/run_until_interrupt/resume/snapshot/cost_report/apply_gate_timeout |
| `gate_keeper.py` | 旧文件闸门（保留不动；资格预审自研状态机不被触碰） |

## 资格预审吸收点（预留）

现有资格预审自研状态机（`services/qualification/**`）与图并存保回归。图中 `qualification_expert`
节点是其吸收点：后续把状态机的比对逻辑接入 expert 节点输出 schema（findings[].status）即可，
接口已类型化，无需改图拓扑。

## 短期会话摘要（Memurai）

本期不做（任务书第 5 节）；长期记忆仅接 `agent_framework/memory.py` 既有能力。

# services 层与 API 契约（/api/graph）

新增 `services/graph_runtime/runner.py`（RunManager：运行注册表+PG checkpointer+超时巡检+改判日志）
与 `services/routers/graph.py`。鉴权接现有 RBAC（管理员直通；需 `project.create`/`project.update` 权限）。

## Endpoints

### POST /api/graph/runs
```json
请求: {"project_id": "uuid"}
200: {"success": true, "run_id": "grun_xxxxxxxxxxxx", "status": "running"}
```
异步启动全图运行（读项目 tender/bid 文本，口径同 /api/check）。错误：401 未登录 / 403 权限不足。

### GET /api/graph/runs
```json
200: {"success": true, "runs": [{"run_id","project_id","status","created_at","final_level"}]}
```

### GET /api/graph/runs/{run_id}
```json
200: {"success":true,"run_id","project_id","status","error",
      "snapshot":{"node_status":{...},"pending_gate":"hitl_decision_gate|null",
                  "decision_package":{"level","rationale","evidence","risks"},
                  "human_decision":{...},"override_reason":"...","final_level":"..."},
      "override_reason":"...","decision_package":{...}}
404: {"detail": "run 不存在"}
```
status 取值：running / pending_decision / finalized / failed。

### POST /api/graph/runs/{run_id}/decision
```json
请求: {"action": "approve"}                       // 批准建议
请求: {"action": "override", "level": "BID", "reason": "必须给理由"}  // 改判（铁律5）
200: {"success": true, "snapshot": {...终态...}}
409: run 不在决策门 / override 缺理由
404: run 不存在
422: action 非法
```

### GET /api/graph/runs/{run_id}/cost
```json
200: {"success":true,"cost":{"run_id","nodes":{"rule_gate":{"llm_calls":22,"tokens":18432,"duration_ms":123.4},...},
      "total_llm_calls":28,"total_tokens":120431,"total_duration_ms":1041.3}}
```
token 计量：CountingLLM 逐调用聚合网关返回的 usage（`_token_usage` 增量，兜底 tracer llm span），
节点级与总量均入 cost_report（fake/测试网关无 usage 时 tokens=0）。

### POST /api/graph/timeouts/sweep
```json
200: {"success":true,"outcomes":[{"run_id","action":"approve(auto)|wait_human|wait|not_pending"}]}
```
铁律4：BID、CAUTION、NO_BID 均必须人工确认；超时只产生等待/告警状态，不自动放行。阈值 `GRAPH_DECISION_TIMEOUT_SECONDS`（秒）。

## 复跑

```bash
# 单测
venv/Scripts/python.exe -m pytest tests/test_pd1_graph_skeleton.py -q
# 端到端 demo（三进程，真实标书+真实LLM）
venv/Scripts/python.exe scripts/pd1_demo.py start  --run-id pd1_demo_approve
venv/Scripts/python.exe scripts/pd1_demo.py resume --run-id pd1_demo_approve --action approve
venv/Scripts/python.exe scripts/pd1_demo.py start  --run-id pd1_demo_override
venv/Scripts/python.exe scripts/pd1_demo.py resume --run-id pd1_demo_override --action override --level BID --reason "演示改判"
venv/Scripts/python.exe scripts/pd1_demo.py query  --run-id pd1_demo_override
```
