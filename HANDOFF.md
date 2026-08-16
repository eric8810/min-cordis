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
├── python\       Python 3.11+ 移植(min_cordis\ 8 模块 + tests\ 37 tests)
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

### Python 版:✅ 核心骨架 + Service/traceable 完成,能力边界明确

- deepseek-v4-pro 审计发现的 7 项严重/高危偏离**已全部修复**([docs/audit-python-parity-2026-08-16.md](audit-python-parity-2026-08-16.md)),每项带回归测试
- **Service 基类 + `@Inject` + traceable/caller/shadow 已落地**([docs/design-python-traceable.md](design-python-traceable.md),含实现记录):`_service.py`(Service/Inject/resolveConfig)、`_traceable.py`(访问期绑定视图)、C1–C5 契约全带测试。顺带修复:intercept 原型链语义、inject-config→intercept 条目、`_label_for` 死链
- 测试 19 → **37**(`-W error::RuntimeWarning` 干净)
- **未移植的层**(README 已声明):accessor/mixin(associate.spec #3/#4)、`internal/get`/`internal/set` 瀑布钩子、logger 服务
- 存量待验证项:C4(id(label) 键复用)、C5(__setattr__ 绕过校验)、R3(registry.delete fire-and-forget,service.spec #4 快照对账依赖它)

## 怎么跑

```powershell
# TS(需要 node >=22、已 corepack enable;仓库 node_modules 已装)
cd C:\Users\eric8810\repos\min-cordis
npx vitest run                        # 62 上游测试
node --import tsx --test "test/*.test.ts"   # 10 回归
npm test                              # 两者都跑

# Python(uv 管理,环境已 sync)
cd C:\Users\eric8810\repos\min-cordis\python
uv run pytest -q                      # 37 tests
```

注意:上游 `Inject` 装饰器是 Stage-3 原生装饰器,`experimentalDecorators` 必须**关闭**(vitest 3 + esbuild,不能用 vitest 4 的 oxc)。

## 关键设计决策(为什么长这样)

1. **`ctx.plugin()` 返回原型 wrapper 而非真 fiber**:直接在真 fiber 上放 `then` 会造成 thenable 递归 OOM(实测踩过)。wrapper + `restart()/update()` 内部重锚 `this.ctx.fiber` = 上游 752dbee 方向,也是对 vendored critical C1 的修复。
2. **审计修复清单**(两版共有):emit 只遏制异步 rejection(同步异常照抛);`internal/status` 逐回调隔离;`update()` 先校验后落盘;`_hooks` null-proto;parallel 如实上报模式。
3. **Python 版有意收紧**:属性读在任何 ctx 都要求 inject(TS root 宽松读是审计发现);`ctx.get()` 是显式逃生口。
4. **Python waterfall 的坑**(修过两次):handler 返回协程要透传,**不能**自行 chained-await——否则 apply 双重执行。

## 下一步(按价值排)

1. ~~Python 补 Service 基类 + @Inject + traceable/caller~~ ✅ 已完成(见上,设计文档含实现记录)
2. accessor/mixin(intercept 合并已随 Service 落地;剩 `ctx.accessor`/`ctx.mixin`,~150 行,解锁 associate.spec #3/#4)
3. 移植上游 spec 剩余等价测试(service.spec #4 快照对账被 R3 阻塞)
4. 清 C4/C5/R3 待验证项(R3 顺带解锁 #3 的快照测试)
5. 可选:Go/Rust 移植(命题已由 Python 验证成立,纯工程活)

## 会话历史摘要(需要细节时查)

- Cordis 学习笔记 + 三波审计:`D:\code\deepseek-harness\docs\research\notes\`
- 审计波次:wave1(方向切分 6 agent)→ wave2(文件穷尽+二轮对抗 7 agent)→ wave3(端到端裁决+fuzz 2 agent)
- 供应链事实:上游 fork `deepseek-harness/cordis` 已删(404);core+loader 快照 commit `56b3d4f7` 在 cordiverse 历史;5 个插件包的快照 `abb0a307` 仅存于本地 vendor
