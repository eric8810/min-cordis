# min-cordis Handoff

> 2026-08-16。给新会话的完整交接:项目是什么、怎么来的、现状、怎么跑、下一步。读完这份即可继续开发,不需要回放原会话。

## 一句话

min-cordis 是从 DeepSeek Harness vendored 的 Cordis 4.0.0-rc.7 中裁剪出的**最小插件框架内核**(TS 版),外加一个验证跨语言可移植性的 Python 移植。零运行时依赖,自带审计背书。

## 目录与位置

```
C:\Users\eric8810\repos\min-cordis     ← 本项目(独立 git 仓库)
├── src\          TS 版:context/reflect/fiber/events/registry/service/utils/logger/index(9 文件)
├── tests\        上游 Cordis 测试套件(11 spec / 62 tests,来自 cordis-workspace HEAD)
├── test\         自写审计回归(10 tests)
├── python\       Python 3.11+ 移植(min_cordis\ 9 模块 + tests\ 85 tests)
└── docs\         两份等价性审计报告(见下)
```

相关但**不属于本仓库**:
- `D:\code\deepseek-harness` — 源头仓库。vendored cordis 在 `vendor\`,三波安全审计报告在 `docs\research\notes\`(cordis-audit-2026-08-15.md、cordis-audit-wave2、wave3),还有学习笔记 cordis-core-model.md
- `C:\Users\eric8810\repos\cordis-workspace` — 上游 cordiverse/cordis 克隆(测试套件和 cherry-pick 的来源,只读对照)

远端:`https://github.com/eric8810/min-cordis`,main 分支,最新 commit `b30c63b`。推送用你本机 gh/git 凭据即可。

## 背景:为什么存在

原会话对 vendored Cordis 做了**三波、15 agent 的安全审计**(发现问题:1 critical / 7 high / ~30 medium),结论是核心引擎(fiber/events/reflect)被 fuzz 验证稳定,而 bug 集中在为 Koishi 聊天机器人场景造的层(loader 树手术、HMR、schemastery、cosmokit)。于是裁剪:

| 保留(核心) | 删除(理由) |
|---|---|
| Context+Proxy、reflect 服务目录、fiber 生命周期、events 五模式、registry、Service、traceable、isolate/intercept/extend、Standard Schema 校验 | loader/include/group(运行期树手术,3 个 fuzz high 的宿主)、hmr(装机态无人用)、schemastery(换任意 Standard Schema 校验器如 zod)、cosmokit(内联 3 个函数)、完整 logger/timer(重写) |

## 现状(截至 b30c63b)

### TS 版:✅ 完成度高

- **语义等价性已被 gpt-5.6-sol 审计确认零意外偏离**([docs/audit-ts-parity-2026-08-16.md](audit-ts-parity-2026-08-16.md))
- 相对 vendored 原版带 7 项修复 + 2 个上游 cherry-pick(be7d36e callable shadow、8abd903 symbols.caller)
- 测试:62 上游 + 10 回归全绿

### Python 版:✅ 与 TS 核心全面对齐(收官)

- deepseek-v4-pro 审计发现的 7 项严重/高危偏离**已全部修复**([docs/audit-python-parity-2026-08-16.md](audit-python-parity-2026-08-16.md)),每项带回归测试
- **Service 基类 + `@Inject` + traceable/caller/shadow 已落地**([docs/design-python-traceable.md](design-python-traceable.md),含实现记录):`_service.py`(Service/Inject/resolveConfig)、`_traceable.py`(访问期绑定视图)、C1–C5 契约全带测试
- **accessor/mixin 已落地**:`ctx.accessor`/`ctx.mixin`(字符串源忠实;对象源复刻上游解析怪癖,见 README「Parity notes」),associate.spec #3 移植、#4 经探针证实内层为死代码只移植外层
- **logger 服务已落地**(`_logger.py`):`ctx.logger('name')` 级别过滤 + errors 环形缓冲;错误遏制仍走注入的 `on_error` sink(双面设计:logger 面向用户,sink 面向诊断)
- **`internal/get`/`internal/set` 瀑布钩子已落地**:插件 ctx 的服务读与属性写经事件总线分发,监听可拦截或 `next()` 透传;root 读不走 get 瀑布(TS 同)
- **审计遗留项清零**:C4(store 改按 label 对象为键)、C5(`__setattr__` 路由 reflect.set 校验)已修;R3(delete fire-and-forget)经 test_compare_snapshot 验证与 TS 一致
- 顺带修复:intercept 原型链语义、inject-config→intercept 条目、`_label_for` 死链、**祖先链 inject 可见性**(TS fiber 链 walk 的对应物:own ∪ 祖先 `_inject_requested`)
- 测试 19 → **85**(经两轮独立 agent 交叉审查修复:设计文档第九节;`-W error::RuntimeWarning` 干净);上游语义面:shadow 4/4、invoke 2/2、associate 4/5(#4 死代码除外)、service 5/5、decorator 1/1、fiber 8/8、events 7/7、dispose 14/14(错误路径钉 Python 容制语义)、isolate 3/3、plugin 11/11、reflect 部分边界

## 怎么跑

```powershell
# TS(需要 node >=22、已 corepack enable;仓库 node_modules 已装)
cd C:\Users\eric8810\repos\min-cordis
npx vitest run                        # 96 tests(62 上游 + 27 reentrant 生命周期 + 7 内部钩子)
node --import tsx --test "test/*.test.ts"   # 10 回归
npm test                              # 两者都跑

# Python(uv 管理,环境已 sync)
cd C:\Users\eric8810\repos\min-cordis\python
uv run pytest -q                      # 85 tests
```

注意:上游 `Inject` 装饰器是 Stage-3 原生装饰器,`experimentalDecorators` 必须**关闭**(vitest 3 + esbuild,不能用 vitest 4 的 oxc)。

## 关键设计决策(为什么长这样)

1. **`ctx.plugin()` 返回原型 wrapper 而非真 fiber**:直接在真 fiber 上放 `then` 会造成 thenable 递归 OOM(实测踩过)。wrapper + `restart()/update()` 内部重锚 `this.ctx.fiber` = 上游 752dbee 方向,也是对 vendored critical C1 的修复。
2. **审计修复清单**(两版共有):emit 只遏制异步 rejection(同步异常照抛);`internal/status` 逐回调隔离;`update()` 先校验后落盘;`_hooks` null-proto;parallel 如实上报模式。
3. **Python 版有意收紧**:属性读在任何 ctx 都要求 inject(TS root 宽松读是审计发现);`ctx.get()` 是显式逃生口。
4. **Python waterfall 的坑**(修过两次):handler 返回协程要透传,**不能**自行 chained-await——否则 apply 双重执行。

## 下一步(按价值排)

核心移植**全部完结**;上游差距审计(2026-08-16,gpt-5.6-sol)的 Top 2 行动也已落实:**reentrant 对抗性生命周期测试**(27 个,带动 effect 机制升级为分支的 EffectRecord 处置契约:执行/清理双通道、全量 LIFO、恰好一次处置任务、stale 代际防毒化)+ **内部扩展点钉子**(7 个:internal/dispatch/config/update/status)。TS 96+10、Python 85 双模式全绿。剩余:

1. (可选,低价值)采纳 `29581f6` 免 bind dispatch
2. Go/Rust 移植(命题已由 Python 验证成立,纯工程活)
3. Python 侧同步 EffectRecord 处置契约(全量 LIFO/AggregateError/处置任务同一性)——TS 已升级,Python `_fiber.py` 还是旧链,两版语义面在此处不一致

## 会话历史摘要(需要细节时查)

- Cordis 学习笔记 + 三波审计:`D:\code\deepseek-harness\docs\research\notes\`
- 审计波次:wave1(方向切分 6 agent)→ wave2(文件穷尽+二轮对抗 7 agent)→ wave3(端到端裁决+fuzz 2 agent)
- 供应链事实:上游 fork `deepseek-harness/cordis` 已删(404);core+loader 快照 commit `56b3d4f` 在 cordiverse 历史(旧记 56f3d4f7 有误);5 个插件包的快照 `abb0a307` 仅存于本地 vendor;cordis-workspace 另有未并入 main 的 `origin/feat/reentrant-fiber-lifecycle`(6e576cc、46d2ae5),min-cordis 含其改写但缺其测试
