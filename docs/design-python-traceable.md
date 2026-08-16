# Python traceable/caller 设计裁决

> 2026-08-16。回答 HANDOFF「下一步 #2」:Python 里 caller 语义的对应物是什么。
> 结论先行:**既不是 contextvars,也不是显式传参,而是结构化访问期绑定(access-time structural binding)——即 `getTraceable` 的 Python 移植。**

## 一、要复现的契约(不是实现,是可观察行为)

来源:`tests/shadow.spec.ts`(4 tests)+ `tests/invoke.spec.ts` + `src/utils.ts` 的 `createTraceable`。

TS 机制:服务通过 `ctx.name` 被访问时,reflect 层不返回原始实例,而是返回一个绑定当前 ctx 的 traceable 代理。代理对每次方法调用做两件事:把方法体内的 `this.ctx`(tracker.property)重绑为**影子上下文** = 访问 ctx ⊕ `{shadow: provider_ctx}`,并暴露 `[caller]`。

由此得出五条可观察行为,移植必须全部成立:

| # | 契约 | 出处 |
|---|---|---|
| C1 | **访问期绑定**:`svc = ctx.name` 时绑定访问 ctx;之后拿到 `svc` 引用再调用(哪怕动态作用域已变),绑定不变 | shadow.spec #1 结构 |
| C2 | **方法体重绑**:通过视图调用方法,`self.ctx` = 影子 ctx;方法内注册的 effect 落在**消费者**的 fiber 上 | shadow.spec #1 |
| C3 | **嵌套归因**:服务 A 的代码经 `self.ctx.b` 访问服务 B 时,B 的 caller = A 的 provider ctx(不是临时运行态 ctx),且 B 内可见 `self.ctx[shadow]` = B 的 provider ctx | shadow.spec #1(`result.caller === outerOrigin`) |
| C4 | **noShadow 退出**:身份敏感服务(如 logger)声明 `noShadow: true` 后,`self.ctx` = 纯访问 ctx、无 shadow 标记,caller = 访问 ctx | shadow.spec #2 |
| C5 | **可调用服务 + isinstance**:带 invoke body 的服务可 `ctx.foo(...)` 直接调用(invoke 内可见 caller);视图通过 `isinstance(view, Service子类)` 检查 | shadow.spec #3、invoke.spec、shadow.spec #4(`server instanceof Server`) |

另有一条负向契约:影子 ctx 不得破坏 fiber 操作——服务方法内 `self.ctx.plugin(...)` 正常加载到真实(剥离影子后的)上下文的 fiber 上(shadow.spec #4)。

## 二、三个候选方案

### A. contextvars(环境动态作用域)

服务方法内 `current_ctx.get()` 读"当前 caller"。

**否决理由**:
1. **绑定时机错位**。契约 C1 要求访问期绑定;contextvars 是调用期动态读值,值取决于"当前跑在哪个 task 里"。asyncio task 创建时快照 context——同步回调直接看到 ambient 值、跨 task 调用看到快照值,同一段代码在 sync/async 两条路径下语义不同。
2. **嵌套归因做不出来**。C3 要求 B 的 caller = A 的 provider ctx。用 contextvars 就必须在每个服务方法调用前后 set/reset——那等于又要建包装层,contextvars 只剩全局可变状态的坏处,没有好处。
3. **审计敌对**。本项目的价值主张是可审计等价性。包装器是局部数据流;contextvar 是跨 await 边界的隐藏全局状态,证明等价需要推理 task 快照语义。

### B. 显式传参(`def query(self, ctx, ...)`)

**否决理由**:
1. 改变所有服务的公开 API 面,且与 TS 契约不符——TS 重绑的是 `self.ctx` **属性**,不是参数;捕获 `svc = ctx.db` 后延迟调用的代码(captured-reference 语义)无法用参数表达。
2. 上游测试逐条对照会全部失败,等价性审计无从谈起。
3. 显式逃生口已经存在:`ctx.get(name)`(与 HANDOFF 决策 #3 的 Python 收紧方向一致),不需要把主路径也显式化。

### C. 结构化访问期绑定(✅ 采纳)

在 `Context.__getattr__` 拿到原始实现后,经 `get_traceable(ctx, value)` 返回绑定视图。与 TS 的 `Reflect.get → getTraceable` 挂点一一对应。

## 三、TS → Python 机制映射

| TS 机制 | Python 对应物 | 备注 |
|---|---|---|
| `new Proxy(value, {get, set, apply})` | wrapper 类 + `__getattr__` / `__setattr__` / `__call__` | get/set/apply 三个 trap 全覆盖,无需通用 Proxy |
| `Object.create(proto)` 原型链影子 ctx | `Context.extend()` 产出的子 ctx + `_shadow` 标记属性(指向 provider ctx) | Python 版已有 parent 指针,`caller = ctx._shadow or ctx`、剥离 = 取 `_parent` |
| `createShadowMethod`(apply trap 换 thisArg) | `__getattr__` 返回闭包:构造 shadow 服务视图(原服务 + `.ctx` 覆写),以 shadow 为 self 调用类上的原函数 | `getattr(type(svc), name)` 取未绑定函数再传 shadow |
| `joinPrototype(Service.proto, Function.proto)` 可调用服务 | **天然不需要**:任何带 `__call__` 的对象即可调用 | TS 的原型拼接是语言缺陷的 workaround,Python 直接消解 |
| `Symbol.hasInstance` 走类链 | wrapper 暴露 `__class__` 属性(委托到原服务类) | `isinstance` 天然尊重 `__class__`,比 TS 方案干净 |
| `symbols.caller/shadow/original/tracker` | `_utils.py` 里 `CALLER/SHADOW/ORIGINAL/TRACKER` 常量 | **已就位**,当时就是为此预留 |
| `Tracker.associate`(点号子服务,`db.tables` → reflect key `db.tables`) | wrapper 的 get/set 先查 `f"{associate}.{name}"` 是否在 `reflect.props` | 与 accessor/mixin(HANDOFF #3)同批落地 |
| 每次访问新建代理 | 同样每次访问新建 wrapper(不做缓存) | **有意**:TS 不保跨访问恒等(`ctx.a is ctx.a` 为假),缓存反而引入偏离;性能优化(按 `(id(ctx), id(svc))` 缓存)留到等价测试全绿之后 |

## 四、落地计划

1. **顺序**:`Service` 基类(HANDOFF #1)先行——其构造器安装 tracker(associate=name, property='ctx');traceable 消费 tracker。两者是同一次交付的两半,拆开无法测 C1–C5。
2. **模块**:`python/min_cordis/_service.py`(Service 基类 + `@inject` 装饰器)+ `_traceable.py`(get_traceable / shadow 视图 / callable 分发)。不动现有 6 个模块的公共面。
3. **测试**:移植 shadow.spec 4 个 + invoke.spec 语义(caller 归因、noShadow、callable+caller、影子下 plugin、intercept 合并视图)≈ 10–12 个用例,新增 `tests/test_traceable.py`。
4. **不做**:TS 的 `composeError` 长栈拼接(V8 栈帧手术在 CPython 无对应物,Python 用标准 traceback + `raise ... from` 链即可,README 声明为有意偏离)。

## 五、裁决一句话

caller 语义的本质是"**绑定随引用走,不随动态作用域走**"——这是词法属性,只有结构化包装能表达;contextvars 表达的是动态属性,显式传参表达的是调用点属性,都不在同一语义域。

## 六、实现记录(2026-08-16,已落地)

交付物:`python/min_cordis/_traceable.py`(视图/overlay/派生)、`_service.py`(Service + `@Inject`)、`_context.py`/`_fiber.py`/`_registry.py` 接线。测试 19 → 37(新增 `tests/test_traceable.py` 9 个、`tests/test_service.py` 9 个)。

实现期间确认/修复的语义点(都带回归测试):

1. **Service 注册必须走 ctx facade**(`ctx.provide`)。直接调 `ctx.reflect.provide(...)` 会落到 root fiber(ReflectService 实例自身绑定 root);TS 通过 `ctx.reflect` 属性读拿到绑定调用方的 traceable 视图实现同样的重定向。修复前服务注册在 root fiber 上,dispose 提供者 fiber 不会注销服务。
2. **shadow 剥壳位置在 `createTraceable`**(utils.ts:217-219 的 `ctx = Object.getPrototypeOf(ctx)`),不在 `extend`——extend 反而**保留** own shadow。探针(shadow.spec #4 场景)确认:方法体内 `this.ctx` 带 shadow,经它创建的插件 ctx 整链无 shadow。Python 对应:`_stripped()` 用于 plugin/inject/provide/get/set/on/once 的锚定。
3. **派生对象(`extend`)的属性解析序**:own → **影子 overlay props(ctx/_caller/_original)** → 类成员 → 裸实例。若裸实例 `__dict__` 的 `ctx`(Service 存的)先于 overlay 被查到,intercept 合并就会丢条目(invoke.spec foo3 用例抓出)。
4. **包装链直通**:`get_traceable` 遇到 view/overlay/派生直接返回(不重包)。重包会拿解包后的裸实例当 target,丢掉 overlay 的 own props。
5. **`intercept()` 原型链语义补齐**:Python 原实现扁平复制会在同名时丢祖先条目;改为 own-entry map + ctx 父链遍历(`_intercept_entries`)。同时补上 TS fiber.ts:239-245 的语义:dict 形式 inject 声明的 config 落成插件 ctx 的 intercept 条目。
6. **`_label_object` 改走 ctx `_parent` 链**:原 `_label_for` 依赖从未被设置的 `__parent__` map 标记,最多走两层;补齐了"root map 在插件 ctx 创建之后才 intern 的 label"这条路径。

有意偏离(与审计决策一致,测试已适配):

- 属性读在任何 ctx 都要求 inject;`ctx.get(name)` 为显式逃生口。invoke.spec / service.spec 里 TS 直接 `root.foo` 的地方改为 `root.get("foo")`。
- `composeError` 长栈拼接不移植(CPython 无对应物)。
- associate 直读(`ctx['foo.bar']`)在 TS 的瀑布 walk 下对已提供但未声明 inject 的名字也能解析;Python 保持收紧——测试断言未提供的 `foo.qux` 抛错、已提供的经 `ctx.get("foo.bar")` 读值。**关联路径(`ctx.foo.bar`)内部仍走 TS 式 fiber 链 walk**(`_resolve_walk`),不受收紧影响。

时序注记:TS 的微任务语义让 `await plugin(...)` 顺带排干 notify 触发的依赖重载;Python 的 ensure_future 链需要额外 loop tick,涉及"provider 加载后断言消费者已跑"的测试补了 `await asyncio.sleep(0.05)`(Windows 下 `asyncio.wait` 定时器粒度 ~15ms)。

未移植(顺延):`ctx.accessor`/`ctx.mixin`(associate.spec #3/#4)、`internal/get`/`internal/set` 瀑布钩子、R3(registry.delete fire-and-forget,service.spec #4 快照对账测试依赖它)。
