import { Context, CordisError, FiberState, ValidationError, type Fiber } from '../src'
import { expect, describe, it, vi } from 'vitest'
import { mock } from 'node:test'
import { sleep, withTimers } from './utils'

/**
 * Adversarial lifecycle tests ported from the (unmerged) upstream branch
 * `origin/feat/reentrant-fiber-lifecycle` of cordis-workspace — the branch
 * whose effect-ownership machinery min-cordis adapts (pre-publication child
 * ownership, setup barriers, joinable in-flight disposal, stale-epoch
 * checks). These pin the reentrant boundaries the regular suites only
 * partially exercise. See docs/audit-upstream-gap-2026-08-16.md §3-C.
 *
 * Deliberately NOT ported from that branch:
 * - "reloads when an implementation is replaced during loading" — encodes the
 *   branch's provider-incarnation epochs, which contradict the mainline
 *   semantics our `inertia lock 2` test pins (same-fiber re-provide mid-load
 *   completes the in-flight load without unloading).
 * - "does not clear config validation failure when dependencies become
 *   available" + "clears config validation failure after a valid update while
 *   dependencies remain unavailable" — both require the branch's eager
 *   entry-config validation (schema resolved in the constructor's ownership
 *   effect, gating loadability via `_configError`). min-cordis deliberately
 *   defers validation to activation (pinned audit fix: "update on a PENDING
 *   fiber defers validation until activation", test/core.test.ts).
 */

describe('Fiber adversarial lifecycle', () => {
  it('keeps plugin execution failure separate from rollback cleanup failure', async () => {
    const root = new Context()
    const logged: unknown[] = []
    ;(root.logger as any).error = (error: unknown) => { logged.push(error) }
    const executionError = new Error('execution failed')
    const cleanupError = new Error('cleanup failed')
    const fiber = root.plugin((ctx) => {
      ctx.effect(() => () => { throw cleanupError })
      throw executionError
    })

    const error = await fiber.await().catch(error => error)
    expect(error).to.equal(executionError)
    expect(logged).to.deep.equal([executionError, cleanupError])
    expect(fiber.state).to.equal(FiberState.FAILED)
  })

  it('returns the asynchronous internal/update waterfall result', async () => {
    const root = new Context()
    const gate = Promise.withResolvers<void>()
    const started = Promise.withResolvers<void>()
    const configs: string[] = []
    const fiber = root.plugin((_ctx, config: { value: string }) => {
      configs.push(config.value)
    }, { value: 'old' })
    await fiber

    fiber.ctx.on('internal/update', async (_config, _noSave, next) => {
      started.resolve()
      await gate.promise
      return next()
    })
    const update = fiber.update({ value: 'new' })
    await started.promise

    let settled = false
    void Promise.resolve(update).then(() => { settled = true })
    await fiber.await()
    expect(settled).to.be.false
    expect(configs).to.deep.equal(['old'])

    gate.resolve()
    await update
    expect(configs).to.deep.equal(['old', 'new'])
  })

  it('keeps wrapped fiber state canonical across restart and update', async () => {
    const root = new Context()
    const configs: string[] = []
    const fiber = root.plugin((_ctx, config: { value: string }) => {
      configs.push(config.value)
    }, { value: 'first' })

    await fiber
    await fiber.restart()
    await fiber.update({ value: 'second' })

    const canonical = Object.getPrototypeOf(fiber)
    expect(configs).to.deep.equal(['first', 'first', 'second'])
    expect(fiber.state).to.equal(canonical.state)
    expect(fiber.config).to.equal(canonical.config)
    expect(Object.hasOwn(fiber, 'state')).to.be.false
    expect(Object.hasOwn(fiber, 'config')).to.be.false
    expect(Object.hasOwn(fiber, 'inertia')).to.be.false
  })

  it('coalesces an update before initial apply into a new generation', async () => {
    const root = new Context()
    const configs: string[] = []
    const fiber = root.plugin((_ctx, config: { value: string }) => {
      configs.push(config.value)
    }, { value: 'old' })

    await fiber.update({ value: 'new' })

    expect(configs).to.deep.equal(['new'])
    expect(fiber.state).to.equal(FiberState.ACTIVE)
  })

  it('continues the next generation after cleanup errors', async () => {
    const root = new Context()
    const configs: string[] = []
    const logged: unknown[] = []
    ;(root.logger as any).error = (error: unknown) => { logged.push(error) }
    const cleanupError = new Error('cleanup failed')
    let failCleanup = true
    const fiber = root.plugin((_ctx, config: { value: string }) => {
      configs.push(config.value)
      return () => {
        if (failCleanup) throw cleanupError
      }
    }, { value: 'old' })
    await fiber

    await fiber.update({ value: 'blocked' })
    expect(fiber.state).to.equal(FiberState.ACTIVE)
    expect(configs).to.deep.equal(['old', 'blocked'])
    expect(logged).to.deep.equal([cleanupError])

    failCleanup = false
    await fiber.update({ value: 'recovered' })
    expect(fiber.state).to.equal(FiberState.ACTIVE)
    expect(configs).to.deep.equal(['old', 'blocked', 'recovered'])
  })

  it('does not let a stale execution failure poison the current generation', async () => {
    const root = new Context()
    const errors = mock.fn()
    ;(root.logger as any).error = errors
    const gate = Promise.withResolvers<void>()
    const configs: string[] = []
    const fiber = root.plugin(async (_ctx, config: { value: string }) => {
      configs.push(config.value)
      if (config.value === 'old') {
        await gate.promise
        throw new Error('stale execution')
      }
    }, { value: 'old' })

    while (!configs.length) await Promise.resolve()
    const update = fiber.update({ value: 'new' })
    gate.resolve()
    await update

    expect(configs).to.deep.equal(['old', 'new'])
    expect(fiber.state).to.equal(FiberState.ACTIVE)
    expect(errors.mock.calls).to.have.length(1)
  })

  it('coalesces duplicate dependency notifications without a transition', async () => {
    const root = new Context()
    root.provide('foo', 1)
    let calls = 0
    const fiber = root.inject(['foo'], () => { calls += 1 })
    await fiber

    root.reflect.notify(['foo'])
    await fiber

    expect(calls).to.equal(1)
    expect(fiber.state).to.equal(FiberState.ACTIVE)
  })

  it('distinguishes provider incarnations without a global counter', async () => {
    const root1 = new Context()
    const root2 = new Context()
    const values1: number[] = []
    const values2: number[] = []
    const dispose1 = root1.provide('foo', 1)
    root2.provide('foo', 10)
    const fiber1 = root1.inject(['foo'], ctx => { values1.push(ctx.foo) })
    const fiber2 = root2.inject(['foo'], ctx => { values2.push(ctx.foo) })
    await Promise.all([fiber1, fiber2])

    await dispose1()
    root1.provide('foo', 2)
    await fiber1

    expect(values1).to.deep.equal([1, 2])
    expect(values2).to.deep.equal([10])
    expect(fiber2.state).to.equal(FiberState.ACTIVE)
  })
})

describe('Fiber publication ownership', () => {
  it('resolves dependencies added during publication before activation', async () => {
    const root = new Context()
    root.provide('late', {})
    let calls = 0
    root.on('internal/plugin', (fiber) => {
      if (fiber.name === 'target' && fiber.uid !== null) fiber.inject.late = {}
    })

    const fiber = await root.plugin({
      name: 'target',
      apply() { calls += 1 },
    })

    expect(calls).to.equal(1)
    expect(fiber.state).to.equal(FiberState.ACTIVE)
  })

  it('rolls back runtime ownership when publication throws', () => {
    const root = new Context()
    const plugin = { name: 'broken-publication', apply() {} }
    root.on('internal/plugin', (fiber) => {
      if (fiber.name === plugin.name && fiber.uid !== null) throw new Error('publication failed')
    })

    expect(() => root.plugin(plugin)).to.throw('publication failed')
    expect(root.registry.has(plugin)).to.be.false
  })

  it('logs disposal observer failures without rejecting disposal', async () => {
    const root = new Context()
    const errors = mock.fn()
    ;(root.logger as any).error = errors
    const observed: string[] = []
    root.on('internal/plugin', (fiber) => {
      if (fiber.name === 'observed' && fiber.uid === null) throw new Error('observer failed')
    })
    root.on('internal/plugin', (fiber) => {
      if (fiber.name === 'observed' && fiber.uid === null) observed.push('disposed')
    })
    const fiber = await root.plugin({ name: 'observed', apply() {} })

    await expect(fiber.dispose()).resolves.toBeUndefined()
    expect(observed).to.deep.equal(['disposed'])
    expect(errors.mock.calls).to.have.length(1)
    expect(errors.mock.calls[0].arguments[0]).to.have.property('message', 'observer failed')
    expect(fiber.state).to.equal(FiberState.DISPOSED)
  })

  it('does not await async disposal observers but still observes rejections', async () => {
    const root = new Context()
    const observerGate = Promise.withResolvers<void>()
    const observerLogged = Promise.withResolvers<void>()
    const cleanupGate = Promise.withResolvers<void>()
    const observerError = new Error('observer')
    const cleanupError = new Error('cleanup')
    const logged: unknown[] = []
    ;(root.logger as any).error = (error: unknown) => {
      logged.push(error)
      if (error === observerError) observerLogged.resolve()
    }
    let observerStarted = false
    let cleanupStarted = false
    root.on('internal/plugin', async (fiber) => {
      if (fiber.name !== 'async-disposal' || fiber.uid !== null) return
      observerStarted = true
      await observerGate.promise
      throw observerError
    })
    const fiber = await root.plugin({
      name: 'async-disposal',
      apply() {
        return async () => {
          cleanupStarted = true
          await cleanupGate.promise
          throw cleanupError
        }
      },
    })

    const disposal = fiber.dispose()
    await Promise.resolve()
    expect(observerStarted).to.be.true
    expect(cleanupStarted).to.be.true

    cleanupGate.resolve()
    await expect(disposal).resolves.toBeUndefined()
    expect(fiber.state).to.equal(FiberState.DISPOSED)
    expect(logged).to.deep.equal([cleanupError])

    observerGate.resolve()
    await observerLogged.promise
    expect(logged).to.deep.equal([cleanupError, observerError])
  })

  it('lets parent disposal during publication drain pending child effects', async () => {
    const root = new Context()
    let ownerContext!: Context
    const owner = await root.plugin({
      name: 'owner',
      apply(ctx) { ownerContext = ctx },
    })
    const cleanupGate = Promise.withResolvers<void>()
    const cleanupStarted = Promise.withResolvers<void>()
    let parentDisposal!: Promise<void>
    let childFiber!: Fiber
    let childCalls = 0

    root.on('internal/plugin', (fiber) => {
      if (fiber.name !== 'child' || fiber.uid === null) return
      childFiber = fiber
      fiber.ctx.effect(() => async () => {
        cleanupStarted.resolve()
        await cleanupGate.promise
      })
    })
    root.on('internal/plugin', (fiber) => {
      if (fiber.name === 'child' && fiber.uid !== null) parentDisposal = owner.dispose()
    })

    ownerContext.plugin({
      name: 'child',
      apply() { childCalls += 1 },
    })

    await cleanupStarted.promise
    let settled = false
    void parentDisposal.then(() => { settled = true })
    await Promise.resolve()
    expect(settled).to.be.false

    cleanupGate.resolve()
    await parentDisposal
    expect(childCalls).to.equal(0)
    expect(childFiber.state).to.equal(FiberState.DISPOSED)
  })

  it('makes a loading parent join child cleanup already in progress', async () => {
    const root = new Context()
    const cleanupGate = Promise.withResolvers<void>()
    const cleanupStarted = Promise.withResolvers<void>()
    let ownerFiber!: Fiber
    let ownerDisposal!: Promise<void>
    let childDisposal!: Promise<void>
    let childFiber!: Fiber

    root.on('internal/plugin', (fiber) => {
      if (fiber.name !== 'loading-child' || fiber.uid === null) return
      childFiber = fiber
      fiber.ctx.effect(() => async () => {
        cleanupStarted.resolve()
        await cleanupGate.promise
      })
      ownerDisposal = ownerFiber.dispose()
      childDisposal = fiber.dispose()
    })

    const ownerMount = root.plugin({
      name: 'loading-owner',
      apply(ctx) {
        ownerFiber = ctx.fiber
        ctx.plugin({ name: 'loading-child', apply() {} })
      },
    })

    await cleanupStarted.promise
    let settled = false
    void ownerDisposal.then(() => { settled = true })
    await Promise.resolve()
    expect(settled).to.be.false

    cleanupGate.resolve()
    await Promise.all([ownerDisposal, childDisposal, ownerMount])
    expect(childFiber.state).to.equal(FiberState.DISPOSED)
    expect(ownerFiber.state).to.equal(FiberState.DISPOSED)
  })
})

describe('Effect adversarial disposal', () => {
  it('returns one disposal promise and joins cleanup already in progress', async () => {
    const root = new Context()
    const gate = Promise.withResolvers<void>()
    let cleanupStarted = false
    const dispose = root.effect(() => async () => {
      cleanupStarted = true
      await gate.promise
    })

    const first = dispose()
    const second = dispose()
    expect(first).to.equal(second)
    expect(cleanupStarted).to.be.true

    const restarting = root.fiber.restart()
    let settled = false
    void restarting.then(() => { settled = true })
    await Promise.resolve()
    expect(settled).to.be.false

    gate.resolve()
    await Promise.all([first, restarting])
    expect(dispose()).to.equal(first)
  })

  it('attempts every cleanup in LIFO order and aggregates failures deterministically', async () => {
    const root = new Context()
    const sequence: number[] = []
    const first = new Error('first')
    const third = new Error('third')
    const dispose = root.effect(function* () {
      yield () => {
        sequence.push(1)
        throw first
      }
      yield async () => {
        await Promise.resolve()
        sequence.push(2)
      }
      yield () => {
        sequence.push(3)
        throw third
      }
    })

    const error = await dispose().catch(error => error)
    expect(sequence).to.deep.equal([3, 2, 1])
    expect(error).to.be.instanceOf(AggregateError)
    expect(error.errors).to.deep.equal([third, first])
  })

  it('preserves an AggregateError thrown by user cleanup as one failure', async () => {
    const root = new Context()
    const nested = new AggregateError([new Error('a'), new Error('b')], 'user aggregate')
    const other = new Error('other')
    const dispose = root.effect(function* () {
      yield () => { throw nested }
      yield () => { throw other }
    })

    const error = await dispose().catch(error => error)
    expect(error).to.be.instanceOf(AggregateError)
    expect(error.errors).to.deep.equal([other, nested])
  })

  it('keeps a direct cleanup failure observable through the shared promise', async () => {
    const root = new Context()
    const error = new Error('cleanup failed')
    const dispose = root.effect(() => () => { throw error })

    const task = dispose()
    expect(dispose()).to.equal(task)
    await expect(task).rejects.toBe(error)
    await expect(dispose()).rejects.toBe(error)
  })

  it('contains cleanup failure at structural restart', async () => {
    const root = new Context()
    const error = new Error('cleanup failed')
    const logged: unknown[] = []
    ;(root.logger as any).error = (value: unknown) => { logged.push(value) }
    root.effect(() => () => { throw error })

    await expect(root.fiber.restart()).resolves.toBeUndefined()
    expect(root.fiber.state).to.equal(FiberState.ACTIVE)
    expect(logged).to.deep.equal([error])
  })

  it('separates synchronous execution and rollback cleanup failures', async () => {
    const root = new Context()
    const logged: unknown[] = []
    ;(root.logger as any).error = (error: unknown) => { logged.push(error) }
    const executionError = new Error('execution failed')
    const cleanupError = new Error('cleanup failed')
    let restarting!: Promise<void>

    expect(() => root.effect(function* () {
      yield () => { throw cleanupError }
      restarting = root.fiber.restart()
      throw executionError
    })).to.throw(executionError)

    await expect(restarting).resolves.toBeUndefined()
    expect(root.fiber.state).to.equal(FiberState.ACTIVE)
    expect(logged).to.deep.equal([cleanupError])
  })

  it('removes a synchronously failed effect after rolling back collected cleanup', () => {
    const root = new Context()
    let cleanups = 0

    expect(() => root.effect(function* () {
      yield () => { cleanups += 1 }
      throw new Error('execution failed')
    })).to.throw('execution failed')

    expect(cleanups).to.equal(1)
    expect(root.fiber.getEffects()).to.deep.equal([])
  })

  it('makes reentrant restart await async rollback without replaying the execution failure', async () => {
    const root = new Context()
    const cleanupGate = Promise.withResolvers<void>()
    const cleanupStarted = Promise.withResolvers<void>()
    const executionError = new Error('execution failed')
    let restarting!: Promise<void>

    expect(() => root.effect(function* () {
      yield async () => {
        cleanupStarted.resolve()
        await cleanupGate.promise
      }
      restarting = root.fiber.restart()
      throw executionError
    })).to.throw(executionError)

    await cleanupStarted.promise
    let settled = false
    void restarting.finally(() => { settled = true }).catch(() => {})
    await Promise.resolve()
    expect(settled).to.be.false

    cleanupGate.resolve()
    await expect(restarting).resolves.toBeUndefined()
    expect(root.fiber.getEffects()).to.deep.equal([])
  })

  it('separates asynchronous execution and disposal failures', async () => {
    const root = new Context()
    const executionError = new Error('execution failed')
    const cleanupError = new Error('cleanup failed')
    const effect = root.effect(async function* () {
      yield () => { throw cleanupError }
      throw executionError
    })

    await expect(Promise.resolve(effect)).rejects.toBe(executionError)
    await expect(effect()).rejects.toBe(cleanupError)
  })

  it('logs auto-rollback cleanup failure once when a structural owner joins', async () => {
    const root = new Context()
    const logged: unknown[] = []
    ;(root.logger as any).error = (error: unknown) => { logged.push(error) }
    const restartStarted = Promise.withResolvers<void>()
    const executionError = new Error('execution failed')
    const cleanupError = new Error('cleanup failed')
    let restarting!: Promise<void>
    const effect = root.effect(async function* () {
      yield () => { throw cleanupError }
      restarting = root.fiber.restart()
      restartStarted.resolve()
      throw executionError
    })

    await restartStarted.promise
    await expect(Promise.resolve(effect)).rejects.toBe(executionError)
    await expect(restarting).resolves.toBeUndefined()
    expect(logged).to.deep.equal([executionError, cleanupError])
  })

  it('makes reentrant restart await async execution and cleanup', async () => {
    const root = new Context()
    const executionGate = Promise.withResolvers<void>()
    const cleanupGate = Promise.withResolvers<void>()
    const cleanupStarted = Promise.withResolvers<void>()
    let restarting!: Promise<void>

    root.effect(async () => {
      restarting = root.fiber.restart()
      await executionGate.promise
      return async () => {
        cleanupStarted.resolve()
        await cleanupGate.promise
      }
    })

    executionGate.resolve()
    await cleanupStarted.promise
    let settled = false
    void restarting.then(() => { settled = true })
    await Promise.resolve()
    expect(settled).to.be.false

    cleanupGate.resolve()
    await restarting
    expect(root.fiber.getEffects()).to.deep.equal([])
  })

  it('rejects effect registration during unload', async () => {
    const root = new Context()
    let registrationError: unknown
    root.effect(() => () => {
      try {
        root.effect(() => () => {})
      } catch (error) {
        registrationError = error
      }
    })

    await root.fiber.restart()
    expect(registrationError).to.be.instanceOf(CordisError)
    expect((registrationError as CordisError).code).to.equal('INACTIVE_EFFECT')
    expect(root.fiber.state).to.equal(FiberState.ACTIVE)
  })

  it('accepts effects while a child is pending or loading', async () => {
    const root = new Context()
    const cleaned: string[] = []
    root.on('internal/plugin', (fiber) => {
      if (fiber.name !== 'state-probe' || fiber.uid === null) return
      expect(fiber.state).to.equal(FiberState.PENDING)
      fiber.ctx.effect(() => () => { cleaned.push('pending') })
    })

    const fiber = await root.plugin({
      name: 'state-probe',
      apply(ctx) {
        expect(ctx.fiber.state).to.equal(FiberState.LOADING)
        ctx.effect(() => () => { cleaned.push('loading') })
      },
    })
    await fiber.dispose()

    expect(cleaned).to.have.members(['pending', 'loading'])
  })
})
