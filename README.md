# 残特奥赛事保障

三个城市联合承办、二十多个大项、近四千名运动员在**报到、医学分级、训练、比赛、住宿、交通**之间流转时的保障调度服务。

保障中心只登记**赛事资格与功能支持所需的最少信息**（功能需求码，不登记诊断），把**场馆无障碍能力、酒店房型、车辆位置、器材及志愿者资质**作为**有期限的资源**统一安排；医学分级变化、赛程延误、设备故障只重排受影响行程。

## 核心约束如何落地

| 需求 | 实现 |
| --- | --- |
| 只登记最少信息、不公开诊断 | 功能需求码白名单（`support/catalog.py`）；`诊断/病历/diagnosis` 等字段在消息入口直接拒绝并拒绝落库 |
| 资源是有期限的 | 车辆/器材 `active_until`、志愿者 `credential_until`，过期不参与排程 |
| 能力匹配 | 功能需求 → 车辆装置（升降/轮椅固定…）、客房属性（无障碍淋浴…）、志愿者技能（手语/引导/转移…）、场馆能力码 |
| 不把同一辆无障碍车承诺给两队 | 重叠时间窗互斥（同队可在容量内拼车）；`查冲突→写承诺` 在同一把锁内完成，并发下也无双占 |
| 分级变化/赛程延误/设备故障只重排受影响行程 | `classification_change` / `schedule_delay` / `equipment_failure` 只定位关联的**未来**行程段，先释放该段承诺再重排，其他承诺一律不动 |
| 紧急医疗可优先占用普通通勤资源 | 仅允许抢占 `commuter`，绝不抢占无障碍专车；**必须给理由**；抢占后释放原承诺、重排原段、通知原安排负责人（领队或赛区调度员） |
| 重复消息与服务重启 | `message_id` 幂等表持久化到 SQLite（含类型与规范化载荷的 sha256 摘要）；**完全相同**的消息任意次投递（含重启后、并发）只生效一次并返回首结果（`deduplicated`）；同一编号被**不同命令**复用（类型不同或规范化载荷不同）以 **409 冲突**拒绝、绝不执行，双方摘要落 `command_conflicts` 表供交接（`/internal/conflicts`） |
| 跨城交接不断档 | 交接携带功能需求到目的城市；目的城市把接入段支持资源**排齐后**交接才能成立；`/internal/continuity` 检测到点未接入的断档 |
| 公众赛程不泄露敏感需求 | `/public/schedule` 只输出时间/地点/项目/延时后的新时间，不含人员、需求、车辆、理由 |
| 内部可回看每次改派及实际影响 | `/internal/changes` 记录触发类型、理由、动作人、释放/新签/空档/抢占明细与关联通知 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 稳定服务身份 |
| POST | `/commands` | 唯一命令入口，消息体 `{"message_id","type","payload"}`；编号复用且类型/规范化载荷不一致时返回 `409` 冲突响应 |
| GET | `/public/schedule[?city=]` | 公众赛程（脱敏） |
| GET | `/internal/changes` | 改派审计流 |
| GET | `/internal/notifications` | 通知记录（含紧急占用通知） |
| GET | `/internal/gaps[?city=]` | 未解决保障空档 |
| GET | `/internal/handoffs[?status=]` | 跨城交接状态 |
| GET | `/internal/segments/<id>` | 行程段明细与资源承诺 |
| GET | `/internal/utilization[?city=]` | 车辆承诺时间线（核对无双占） |
| GET | `/internal/continuity` | 跨城断档检测 |
| GET | `/internal/conflicts[?…]` | 消息编号复用冲突（首次与冲突双方摘要，交接追踪） |

命令类型：`register_team/person/venue/hotel/vehicle/equipment/volunteer/event/segment`、`cover_segment`、`cover_team`、`update_needs`、`classification_change`、`schedule_delay`、`equipment_failure`、`vehicle_status`、`emergency_transport`、`open_handoff`、`accept_handoff`。

## 运行

```bash
python3 service.py --check                    # 基础配置检查
python3 service.py --db data/para.db \
  --seed fixtures/seed_messages.json --port 8000
```

种子本身就是一批带固定 `message_id` 的命令消息，重复导入自动去重。

## 测试

```bash
npm test          # python3 -m unittest discover，共 61 个用例
python3 -m compileall -q .  # 编译检查全部 Python 模块
```

覆盖：诊断字段拒绝与公众脱敏、能力/资质/期限匹配、同车双承诺禁止与容量拼车、三类增量重排只动受影响段、紧急占用理由+通知+原段重排、并发与重启幂等（相同消息回放首结果、编号复用 409 冲突且双方摘要落库、冲突不产生部分分配）、跨城交接成立条件与断档检测、HTTP 契约。

## 代码结构

```
service.py               HTTP 适配与启动/种子导入
support/catalog.py       功能需求码白名单、诊断字段黑名单、需求→资源能力映射
support/store.py         SQLite 表结构与持久化（消息幂等摘要、编号复用冲突、承诺窗口、空档、审计、通知、交接）
support/engine.py        覆盖排程、互斥、增量重排、紧急占用、跨城交接
support/messages.py      命令消息分发与幂等契约（相同消息回放 / 编号复用冲突拒绝）
support/views.py         公众脱敏视图与内部回看视图
fixtures/domain.json     领域词表
fixtures/seed_messages.json  三地联调种子（命令消息）
tests/                   61 个 unittest 用例
```

时间均为 `YYYY-MM-DDTHH:MM` 本地排程时间；重排只处理 `now` 之后的未来段（测试通过固定时钟注入）。
