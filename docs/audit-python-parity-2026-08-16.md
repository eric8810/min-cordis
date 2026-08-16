# min-cordis Python 版等价性审计(2026-08-16)

> deepseek-v4-pro 执行,逐模块 A/B/C/D 对照 + 双版本同构分叉测试。TS 版经 gpt-5.6-sol 审计确认无 C 类偏离后作为基准。结论:**Python 版存在 3 处严重语义偏离 + 4 处高危偏离**,「同一语义」的声明目前不成立。

## 严重(C 类 critical)

| # | 问题 | 位置 | 实测分叉 |
|---|---|---|---|
| F1 | **父卸载不销毁子 fiber**:子销毁器塞进全局 `registry.parent_fiber_disposables`(只 push 不清),未挂父 fiber effect | `_fiber.py:130` vs TS `fiber.ts:265-297` | 父 dispose 后 TS registry 2→0、子停止;Python 2→1、子继续响应 |
| F2 | **provider 先挂 → consumer 永不激活**:构造缺逐依赖 `_checkImpl` 初始化扫描 | `_fiber.py:132` vs TS `fiber.ts:314-319` | TS `seen=[42] ACTIVE`;Python `seen=[] PENDING` 永久 |
| F3 | **async effect 提前 dispose 泄漏 disposer**:`effect()` 无 setup barrier | `_fiber.py:201-220` vs TS `fiber.ts:467-513` | TS `["setup","dispose"]`;Python `[]`,disposer 永不执行 |

## 高(C 类 high)

| # | 问题 | 位置 |
|---|---|---|
| F4 | generator effect 不支持(TypeError);generator 插件体**静默丢弃** disposer | `_fiber.py:189-199,325-341` |
| E1 | `emit` 吞掉**同步** listener 异常(TS 向上抛,只遏制异步) | `_events.py:44-55` |
| E2 | `internal/update` 丢失 per-fiber 作用域:全局 hooks + 随 reload 累积 | `_events.py:208-218` |
| E3 | dispatch 首参判定差异:普通对象首参 → `TypeError: unhashable type` | `_events.py:28-42` |

## 中 / 低 / 待验证

- 中:C1 缺 accessor/mixin/trace/bind;C2 notify 不发 `internal/service`;E4 once 绕过 on 的拦截链;E5 on disposer 契约不同;R2 无 Service 类/@Inject/标准 schema 协议(**README 宣称 services 但没有 Service 基类,范围陈述不符**)
- 低/待验证:F5 getEffects 恒空;C4 `id(label)` 键复用风险;C5 `__setattr__` 绕过写校验;R3 delete fire-and-forget;F6 effectInertia 缺失

## 忠实的部分

五种 dispatch 模式形态、逆序销毁、consumer 先挂的依赖级联、isolate 基础作用域、update 拒绝校验、状态机主体、6 项审计修复的移植意图。

## 测试缺口(前 10 项,★ = 缺口即 bug)

★provider 先挂载、★嵌套插件父销毁、★async effect dispose-before-setup、★generator effect、★inertia lock、★emit 同步异常、★thisArg/filter、★internal/update per-fiber、★internal/service、★getEffects;然后是整组 Service/@Inject/shadow-caller 未移植。

## 修复优先级建议

1. F2(一行级:构造时逐依赖 check 后 refresh)
2. F1(子销毁挂到 parent.fiber.effect)
3. E1(emit 只遏制 async,同步异常重抛)
4. F3(setup barrier)+ F4(generator 支持)
5. E2/E3/R2 按需
