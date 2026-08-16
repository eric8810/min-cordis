# min-cordis 等价性审计(TS vs 原版,2026-08-16)

> gpt-5.6-sol 执行,逐文件 A/B/C/D 分类 + 双版本动态对照。结论:**无 C 类(意外遗漏/笔误)发现**;分叉全部落在「有意修复」「有意删除」「无害变形」三类。

## 核心结论

min-cordis 不是 vendored Cordis 的严格等价裁剪——主体语义(生命周期、依赖传播、事件过滤、effect 逆序清理)一致,分叉三类:6 项修复 + 2 cherry-pick(预期)、删除面(loader/logger/timer/transform,公开能力差异)、类型层变化(无运行时影响)。

注:vendored `package.json` 实际标注 `4.0.1`(= rc.7 + 本地补丁),非裸 rc.7。

## 实测确认的不变量(两版一致)

依赖级联重载(provider 换血 → consumer 卸载重载)、effect 逆序(restart/update/dispose 全路径)、waterfall 否决、isolate/intercept、once/prepend 顺序、Service 子类 + callable + traceable(`this.ctx` 绑定调用方)、fiber.await 语义、emit 同步异常传播。

## 实测分叉(全部为 A 类,即已知修复)

1. **wrapper 状态归属**(修复①):原版 restart/update 在 wrapper 上产生 own `state/inertia/config`,dispose 后 wrapper.state 仍是 ACTIVE;min 状态始终在真实 fiber,dispose 后 DISPOSED。
2. **parallel 上报模式**(修复⑥):`emit:par` → `parallel:par`。
3. **mixin nullable setter**(额外防御修复,审计时新发现):`ctx.mixin('missing', ['x'])` 后 `ctx.x = 1`,原版 `Reflect.set called on non-object`,min 是 proxy falsish 错误——非严格赋值下 min 静默失败、原版抛错。属可观察差异,方向更安全但未在 README 声明。
4. **logger**:原版从 fiber 名推导(`myPlugin→my-plugin`)、有 buffer/exporter;min 固定 `app` 名、console sink + `errors` 数组。**min 的 `errors` 是无界数组,长进程持续 contained error 会增长**(重写取舍,未列 C,但值得修)。

## 上游测试盲区(62 绿测试在原版上会红的)

- `tests/fiber.spec.ts` 的 own-property 断言(`Object.hasOwn(fiber,'state')===false` 等):原版会在 wrapper 上产生 own 字段,必红。
- `tests/shadow.spec.ts` 前三项依赖 `symbols.caller`:原版无此符号,必红。
- 62 测试**没有**覆盖的分叉:parallel 上报模式、emit async rejection、status 观察者阻断、`__proto__` 事件名、update 拒绝后 restart、logger API、mixin setter——由 `test/core.test.ts` 回归补足(logger API 等价性除外)。

## 删除面依赖检查

- fiber/events/reflect/registry 对 loader/timer **零运行时 import**;loader 只通过 internal 事件和 `Plugin.Transform` 类型挂钩。删除安全。
- 核心 logger 调用点全部是错误遏制(`logger.error`),min logger 覆盖;但 identity/命名语义不等价(上面第 4 条)。

## 严重度汇总

- C 类(critical/high/medium/low):**全部为 0**。
- 非 C 但重要:logger API 不兼容(high B)、Plugin.Transform 删除(medium B)、wrapper/caller 与原版分叉(medium A,方向更正确)、JSDoc 编码破损 `鈥?`(low D,fiber.ts)。

## 待办(由本审计产生)

1. logger `errors` 数组加上界(如 1000)。
2. README 声明 mixin nullable setter 的行为差异。
3. 修 fiber.ts JSDoc 的编码破损字符。
