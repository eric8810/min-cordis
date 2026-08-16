# min-cordis ↔ 上游 cordis 差距审计(2026-08-16)

> 审计者:gpt-5.6-sol(独立 agent,只读)。范围:两侧 9 文件核心面(`src/` vs `packages/core/src/`),对照上游 cordis-workspace HEAD `8cc9e33` 与快照 `56b3d4f`。
> 基线勘误:HANDOFF 旧记 `56f3d4f7` 有误,实际快照 commit 为 `56b3d4f`。

## 一句话结论

**保留面上未发现新的高危/中危未记录运行时分歧。** 快照以来触及 core 的 9 个上游 commit 全部三态归位;唯一保留代码漂移是 `29581f6` 的 dispatch 分配优化(低危,纯性能)。主要残余风险不是分歧,而是 **min-cordis 已含 reentrant 生命周期改写但缺对抗性测试**。

## 1. 漂移表(56b3d4f..8cc9e33,packages/core)

| 上游 commit | 变更 | min-cordis 状态 | 说明 |
|---|---|---|---|
| `eb5604d` | 完整 logger 的 WARN/INFO 级别序修正 | **有意跳过** | logger 已被有意替换(阈值序 `info:1/warn:2` 独立正确,src/logger.ts:6) |
| `fd96b0a` | logger exporter ID 捕获 + buffer 裁剪优化 | **有意跳过** | exporters 已裁;errors 环形缓冲对应(src/logger.ts:51,76) |
| `29581f6` | dispatch 免 `.bind()` 分配 + 私有 `_resolve()` | **缺失(行为等价)** | 唯一保留代码漂移;per-listener bind 每次分发多分配(src/events.ts:170),监听可见语义不变 |
| `be7d36e` | callable 服务的影子上下文/继承描述符 | **已有**(cherry-pick) | utils.ts:199/262/278 |
| `752dbee` | wrapped-fiber 生命周期重锚 | **已有**(cherry-pick) | fiber.ts:741/764 + registry.ts:323 原型 wrapper |
| `8abd903` | `symbols.caller` 直呼方追踪 + logger 命名 | **已有**(保留面);logger 命名部分随裁剪有意缺席 | utils.ts:215/221 |
| `f46ae95` | 版本号 + dispatch 弃用标注(注释级) | 无运行时变化 | — |
| `fab126f` / `8cc9e33` | core README | **有意跳过** | 文档 |

### 非 main 分支的重要发现

`origin/feat/reentrant-fiber-lifecycle`(`6e576cc`、`46d2ae5`,**未并入 main**)——min-cordis 已包含该分支生命周期工作的实质性改写(发布前置子所有权、pending 子清理、effect setup 屏障、可加入的在途销毁、stale-epoch 检查、惰性 config 解析),但**未移植其对抗性测试**(见 §3-C)。

## 2. 分歧发现(全部低危)

1. **dispatch 仍按 listener 分配 bound callback**(上游 `_resolve()` + `Reflect.apply`):纯性能,高频事件总线才可感。src/events.ts:170,177,188。
2. **无人监听时仍发 `internal/dispatch`**(上游先查 hook 再发):一次空调度,纯性能。src/events.ts:173。
3. **保留的公开 `dispatch()` 无上游弃用标注**:类型层提示,无运行时差异。

文档化偏离(异步 emit 遏制、null-proto hook map、status 逐回调遏制、拒绝更新不毒化、mixin 缺源拒写、logger 替换)均按既例不算发现。

## 3. 覆盖缺口

**A. 未测但正确**:五种模式的 `internal/dispatch` 载荷与 mode;`dispatch()` 显式 `this` 绑定;`internal/config` 瀑布(变换/否决);async `internal/update` 返回传播;嵌套直呼服务的 caller 追踪;TS 侧 logger 环上限与阈值路由。

**B. 未测且有意分歧(需钉住防"修回"上游 bug)**:`parallel` 如实上报 `"parallel"`(上游 HEAD 报 `"emit"`);`internal/status`/`internal/plugin` 观察者**异步** rejection 遏制(fiber.ts:591/119,现仅测同步抛);mixin 缺源 setter 返回 false。

**C. reentrant 分支高价值未钉行为(12 项)**:同步 setup 期间的销毁并入 async 回滚;重复销毁返回/并入同一清理任务;单个清理抛错不阻断后续且严格 LIFO;`UNLOADING` 期拒绝 effect 注册;父销毁在 `internal/plugin` 发布期间排空 PENDING 子 effect;loading 父并入子在途清理;stale 执行失败不毒化新代;依赖 A→B→A 不复活 stale;重复依赖通知不伪重载;校验失败不被依赖可用静默清除;非法 pending config 可经后续合法 update 恢复。集中在 fiber.ts:402/265/630/666。

## 4. 裁决与 Top 3 行动

**强语义对等**(保留面),多项已知场景刻意优于上游的失败遏制。行动按价值:

1. **移植 reentrant 分支的对抗性生命周期测试**(远超其他)——对象恰是 min-cordis 里最复杂、现有 62+10 只部分触及的代码。
2. **补内部扩展点测试**:`internal/dispatch` 全模式、async `internal/update`、`internal/config` 变换/否决、async 观察者遏制——保护有意偏离不被"简化"回上游 bug。
3. (可选)**采纳 `29581f6` 原始回调 dispatch 路径**,同时保留 null-proto hook map、异步遏制、parallel 如实上报、逐回调遏制。

原文全量报告见本会话;仓库另有 docs/audit-ts-parity-2026-08-16.md(TS 侧等价性)与 docs/audit-python-parity-2026-08-16.md(Python 侧)。
