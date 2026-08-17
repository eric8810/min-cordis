# min-cordis Rust 移植设计(v4,范式路线)

> 2026-08-17 定稿。**路线变更**:用户裁决按**范式移植**而非一比一接口/结构移植——实现 Cordis 的核心范式,Rust 惯用表达,不锚定 TS 96 spec,验收 = 范式契约自证。历史:v1→v3 的一比一路线与双模型两轮审阅存档于 [review-rust-design-2026-08-17-sol.md](review-rust-design-2026-08-17-sol.md)、[review-rust-design-2026-08-17-deepseek.md](review-rust-design-2026-08-17-deepseek.md)、[review-rust-design-2026-08-17-round2-sol.md](review-rust-design-2026-08-17-round2-sol.md)、[review-rust-design-2026-08-17-round2-deepseek.md](review-rust-design-2026-08-17-round2-deepseek.md);v3 的语义考据(契约行号锚点)仍是本设计的参考资产。
> **生态背书**:三份调研([research-rust-async-2026-08-17.md](research-rust-async-2026-08-17.md)、[research-rust-ecosystem-2026-08-17.md](research-rust-ecosystem-2026-08-17.md)、[research-hot-reload-2026-08-17.md](research-hot-reload-2026-08-17.md))——async/tokio 官方文档 + Bevy/tower/shaku/Tauri 先例 + 热更新案例(生命周期级先例 = Erlang/OSGi;代码级 dylib/subsecond 为正交互补)。

## 〇、v3 → v4 变更摘要

| v3(一比一路线)的税 | v4 处置 | 依据 |
|---|---|---|
| DynamicValue 分层 + internal 九事件 typed 化 | **删除**。事件系统泛型化:`EventBus::on::<E>` 按事件类型注册,载荷即 E;框架内部擦除用 `Arc<dyn Any + Send + Sync>`,serde_json::Value 只出现在序列化边界(如 agent 的 LLM 消息) | 生态:泛型/枚举优先,Any 仅内部擦除,Value 仅边界(bevy#1431、bevy cheatbook) |
| waterfall 同步/异步双态(Ready/Future) | **删除双态**。全异步 `BoxFuture`;veto = 不调 next 的语义保留 | 范式路线无同步 get/set 包袱;tower 先例的 call 即返回 Future |
| emit poll-once 前缀推进 | **删除**。async 监听器直接 spawn,无 JS 前缀语义负担 | JS 微任务语义无 Rust 对应物,不值得保真 |
| RuntimeKey = apply 闭包 Arc 身份 | **改为显式 `PluginId`**(注册返回,由 Runtime 管理) | 生态:Bevy `Plugin::name + is_unique` 显式身份;闭包指针身份是 JS 妥协 |
| shared future(恰好多观察者) | **改 `tokio::sync::watch`**:终态 `watch<State>`(Pending/Ready(Arc<T>)/Failed(Arc<E>)),天然多订阅、晚订阅、`Arc<E>` 错误 identity | tokio watch = MPMC last-value 官方语义;shared 要求 Output: Clone 且无晚订阅 |
| AtomicBool 协作停止 | **改 `CancellationToken`**(tokio-util) | tokio 官方 graceful-shutdown 推荐;有 cancelled().await 唤醒语义 |
| epoch AtomicU64/锁内 token | 保留**锁内复合 token**(transition mutex 内读写) | 调研#2:std Mutex 短临界区官方推荐;token 不可用序号(破坏等值合并) |
| `yield_now` 作 stale 检查点 | **降级为纯公平性 hint**,无任何正确性/顺序语义;stale 检查只靠锁内 token 比较 | tokio 文档明确不保证轮询顺序 |
| Handle 构造捕获 | **首选注入 `Handle`**;自动获取用 `try_current()` 返回明确错误,不 panic | tokio Handle 文档 |
| 错误需 Clone | **不 Clone**:`CordisError` enum + thiserror(不入公共 API);共享走 `Arc<CordisError>`;聚合 = `Vec<CordisError>` 变体 | API guidelines C-GOOD-ERR;anyhow 非 Clone 共识 |
| 测试锚 TS 96 spec | **范式契约测试自证**(§5) | 用户裁决 |

## 一、范式定义(实现目标,五支柱)

1. **插件 = 装配单元**:一次 `apply`,可提供 0..n 服务、注册 0..n 监听、交回清理(effect)。
2. **fiber = 生命周期容器**:状态机(Pending/Loading/Active/Failed/Disposed/Unloading)+ 依赖门控(声明的依赖全部就绪才启动)+ 级联卸载 + 恰好一次清理。
3. **服务 = 类型键注册表 + 作用域**:`TypeId` 主键 + isolate 作用域隔离(同类型跨作用域可并存;同接口多实例用显式 key,shaku Keyed 模式)。
4. **事件总线 = 五分发语义**:emit(触发即忘)/ parallel(并发全等)/ serial(顺序至 bail)/ bail(同步短路)/ waterfall(中间件续延,可 veto)。
5. **依赖驱动重载**:provider 卸载 → 声明依赖它的 fiber 被驱逐并自动重载;重载经反向依赖索引,卸载顺序正确。

**不属范式(不做)**:Proxy 属性语法、traceable/caller-shadow、JS 对象/callback 引用身份、运行时字符串事件、internal/* 扩展点面(TS 等价面)。

## 二、核心 API 草案

```rust
// ── 类型与错误 ──────────────────────────────────────────────
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;  // = futures::future::BoxFuture
#[derive(Debug, thiserror::Error)]
pub enum CordisError {
    #[error("service {0:?} not found in scope")] ServiceNotFound(String),
    #[error("plugin failed: {0}")] PluginFailed(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("multiple errors: {errors:?}")] Aggregate { errors: Vec<CordisError> },  // 不压平
    #[error("fiber disposed")] InactiveEffect,
    #[error("config validation failed: {issues:?}")] Validation { issues: Vec<String> },
    #[error("dependency cycle or unsatisfied: {0:?}")] InjectUnsatisfied(Vec<String>),
}

// ── 事件:类型化载荷 + 回调注册表 ────────────────────────────
pub trait Event: Send + Sync + 'static { const NAME: &'static str; }

pub struct EventBus { /* 内部: HashMap<TypeId, Vec<Hook>>;owned 闭包 Arc<dyn Fn> */ }
impl EventBus {
    pub fn on<E: Event>(&self, ctx: &Ctx, f: impl Fn(&Ctx, &E) -> ListenerResult + Send + Sync + 'static) -> Disposer;
    pub fn emit<E: Event>(&self, ctx: &Ctx, e: &E);                    // 同步调用;async 变体 spawn+遏制
    pub async fn parallel<E: Event>(&self, ctx: &Ctx, e: &E) -> Result<(), CordisError>;  // 聚合全部错误
    pub async fn serial<E: Event>(&self, ctx: &Ctx, e: &E) -> Option<Value<E>>;
    pub fn bail<E: Event>(&self, ctx: &Ctx, e: &E) -> Option<Value<E>>;
    pub async fn waterfall<E: Event>(&self, ctx: &Ctx, e: &E, next: Next<'_>) -> BoxFuture<'_, Result<Value<E>, CordisError>>;
}

// ── 插件:dyn 兼容,BoxFuture ABI ─────────────────────────────
pub trait Plugin: Send + Sync + 'static {
    fn name(&self) -> &str;
    fn injects(&self) -> &[TypeKey] { &[] }          // 依赖门控声明
    fn apply<'a>(&'a self, ctx: &'a Ctx) -> BoxFuture<'a, Result<Effect, CordisError>>;
}
pub enum Effect {
    Done,                                            // 无清理
    Disposer(Box<dyn FnOnce(&Ctx) + Send>),          // 同步清理
    AsyncDisposer(Box<dyn FnOnce(&Ctx) -> BoxFuture<'static, ()> + Send>),
    Many(Vec<Effect>),
}

// ── 服务注册表:TypeId 主键 + 显式 key 多实例 ────────────────
impl Ctx {
    pub fn provide<T: Send + Sync + 'static>(&self, value: T) -> Result<Disposer, CordisError>;
    pub fn provide_as<T: ?Sized + Send + Sync + 'static>(&self, key: TypeKey, value: Arc<T>) -> Result<Disposer, CordisError>;  // trait 对象注册
    pub fn get<T: Send + Sync + 'static>(&self) -> Option<Arc<T>>;          // 显式定位器(Bevy Res/Tauri state 先例)
    pub fn plugin(&self, p: impl Plugin) -> FiberView;                      // FiberView: IntoFuture + dispose/restart/update
    pub fn isolate(&self, label: &str) -> Ctx;                              // 作用域子上下文
}
```

## 三、关键设计决策(逐条带依据)

| # | 决策 | 依据(调研/先例) |
|---|---|---|
| D1 | `BoxFuture<'a,T>` 手写别名 = futures crate 同一定义;所有需 dyn 的 trait 用它 | Rust Reference dyn-compat;async-trait 0.1.89 仍维护但宏展开即此形状;显式 ABI 优于宏 |
| D2 | 事件载荷类型化(`Event` trait + 泛型注册),内部 `Arc<dyn Any + Send + Sync>` 擦除;serde_json::Value 仅序列化边界 | bevy cheatbook/bevy#1431(枚举/泛型优先);qubit-event-bus 类型安全主题 |
| D3 | 回调注册表式总线(钩子语义):owned 闭包 `Arc<dyn Fn>`,返回 Disposer;不引入 channel | users.rust-lang EventBus 共识;Tauri listen/emit;钩子=推式即时,channel=拉式缓冲,职责不同 |
| D4 | 五模式:emit 同步调用+async spawn 遏制到 ErrorSink;parallel 聚合 `Aggregate{errors}`;waterfall = CPS(`Next<'_>`),veto=不调 next;全异步无双态 | tower Service 先证(call→Future 形状);范式路线删除 JS 同步前缀保真负担 |
| D5 | fiber 状态机:`transition: Mutex<Transition>` 单锁域内改状态+读 token(内容派生相等键+force 代),绝不持锁跨 await;回调/锁外执行 | tokio shared-state 官方(短临界区 std Mutex);std RwLock 死锁示例→快照后释放 |
| D6 | 恰好一次+多观察者:终态 `watch<FiberState>`(Failed 携 `Arc<CordisError>` identity);重复 dispose join 同一终态 | tokio watch MPMC last-value+晚订阅;Arc<E> 共享 identity(anyhow 非 Clone 共识) |
| D7 | 协作取消统一 `CancellationToken`(tokio-util):fiber 卸载传播、agent stop、graceful shutdown;AtomicBool 仅同步快路径 | tokio graceful-shutdown 官方推荐;cancelled().await cancel-safe |
| D8 | runtime 接入:构造注入 `Handle` 优先;自动路径 `Handle::try_current()` → 明确错误;绝不隐式建 runtime | tokio Handle 文档 |
| D9 | yield_now 仅公平性 hint;stale/epoch 检查只靠锁内 token 比较,无调度顺序假设 | tokio yield_now 文档(轮询顺序非契约) |
| D10 | 插件身份 = 注册返回的 `PluginId` + `Plugin::name()` 显示名;`is_unique` 式去重可选 | Bevy Plugin trait(name/is_unique);闭包指针身份是 JS 特有 |
| D11 | 错误:thiserror 派生 `CordisError`(不泄漏进公共 API),`Error + Send + Sync + 'static`,**不 Clone**;共享 `Arc`;聚合 `Vec` | API guidelines C-GOOD-ERR;thiserror 文档 |
| D12 | 配置校验:插件自带纯 `fn validate(config: &Config) -> Result<Config, Vec<Issue>>`(可选),validate-before-store;不引 schema 库 | 范式自足;Standard Schema 是 TS 生态互操作需求,Rust 侧用户自选 |
| D13 | 服务定位器 `get::<T>() -> Option<Arc<T>>` 合法:显式、类型键、Option 失败 | Bevy `Res<T>`/Tauri `state::<T>()` 先例;定位器反模式指控不适用于类型安全场景 |
| D14 | 依赖门控重载(差异化能力,无 Rust 生态先例;生命周期级先例 = Erlang 热升级/OSGi,见 research-hot-reload):反向依赖索引 `HashMap<TypeKey, Vec<PluginId>>`;provider 卸载 → 依序驱逐消费者(等排干再删自身,保清理期自访问);门控数据在 fiber(声明 deps + 当前就绪位集) | Bevy/Tauri 无热重载;Erlang/OSGi 为范式先例;自建 + 范式契约测试钉死 |
| D15 | tower 兼容 = 可选适配层(不进核心);`Service`+`Layer` 面向请求路径,生命周期钩子不硬套 | tokio blog Inventing the Service trait;poll_ready 背压语义不适用钩子 |

依赖:tokio(rt-multi-thread, sync, macros)+ tokio-util(CancellationToken)+ serde/serde_json(仅 agent 示例与边界)。**不引 futures-util**(D6 改 watch 后无需要)、不引 async-trait(thiserror 可选)。

## 四、与 TS 版的关系(信息性,非承诺)

- 语义考据照 v3(行号锚点可信):EffectRecord 处置契约(LIFO/恰好一次/聚合不压平)、inject 门控、notify 扫描、update 双分支——这些是**范式不变量**,v4 全保留。
- 刻意不保真:internal/* 事件面、字符串事件名互操作、同步 waterfall、JS async 前缀、callback 引用身份、Proxy/traceable。
- TS 96 spec 降级为灵感来源;其中纯范式部分(fiber/dispose/reentrant)可选择性参考移植断言。

## 五、范式契约测试(验收,自证)

| 组 | 契约 | 代表测试 |
|---|---|---|
| lifecycle | 状态机全迁移;init 失败→Failed;dispose 幂等;root dispose 清子树后可重启 | `state_transitions`、`init_failure_marks_failed`、`dispose_idempotent`、`root_restart` |
| gating | 依赖未齐→Pending;后到 provider 激活(顺序无关);provider 卸载驱逐消费者并重载 | `waits_for_dependency`、`late_provider_activates`、`provider_unload_evicts` |
| dispose | record 内 LIFO 串行;单错原样/多错聚合不压平;恰好一次(join 同一 `Arc<E>`);`await effect` 交回 Disposer | `lifo_serial`、`aggregate_no_flatten`、`exactly_once_same_error`、`effect_yields_disposer` |
| events | emit 同步调用+async 遏制;parallel 全错误聚合;waterfall veto/包裹/最外层返回;bail 短路;Disposer 随 fiber 卸载 | `emit_sync_async_contained`、`parallel_aggregates`、`waterfall_veto_around`、`listener_unloads_with_fiber` |
| registry | TypeId 注册/取回/重复错;显式 key 多实例;isolate 作用域隔离 | `typed_roundtrip`、`keyed_multi_instance`、`isolate_scoping` |
| reload | 门控重载顺序;清理期自访问有效;重入(卸载中 provide)不崩 | `eviction_order`、`self_access_during_cleanup`、`reentrant_provide` |
| cancel | CancellationToken 停止 agent;fiber 卸载级联唤醒等待者 | `agent_stop`、`cancel_wakes_awaiters` |
| agent(M3) | python examples 11 项范式语义(直接回答/工具往返/错误回喂/max_steps/stop/门控装配/事件观察) | `agent_*` 系列 |

目标 ~35-45 个;每个范式支柱至少 4 个正例 + 2 个失败模式。

## 六、里程碑

- **M1**:类型系统 + EventBus(五模式)+ ErrorSink + Handle → events/cancel 组测试。
- **M2**:Ctx/Registry(TypeId+key+isolate)+ Fiber(状态机/门控/EffectRecord/watch 终态)+ 依赖重载 → lifecycle/gating/dispose/registry/reload 组。
- **M3**:agent 示例 crate(CancellationToken stop、LLM trait、ToolSpec、事件观察)→ agent 组;可运行 demo。
- **M4(可选)**:tower 适配层、配置 schema 糖、多实例命名作用域进阶、状态迁移式热更(Erlang code_change 对应物,见 [research-hot-reload-2026-08-17.md](research-hot-reload-2026-08-17.md));与代码级热更工具(subsecond/hot-lib-reloader)的组合点:它们供新代码,本框架负责安全换件(dispose→驱逐→装配→重载),正交不耦合。

## 七、开放问题

1. `Event` trait 的 NAME 常量是否保留字符串诊断用途(仅日志)还是纯 TypeId——倾向保留(日志友好,不参与分发)。
2. waterfall 泛型 `Value<E>` 的形状:每事件自定义返回类型 vs 统一 `Option<Arc<dyn Any>>`——倾向前者(类型安全),M1 实现时定。
3. fiber 重载(update)在范式中是否首版需要,或 M2 只做 dispose+restart——倾向后者(范式最小),update 留 M4。
4. 同类型多实例的 key 类型:`&'static str` vs newtype `Key<T>`——倾向后者(shaku Keyed 精神)。

## 八、实现记录

(实现后回填:crate 名、依赖版本、测试计数、契约偏差。)
