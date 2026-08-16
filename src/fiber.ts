import { defineProperty, isNullable } from './utils.ts'
import type { Awaitable, Dict } from './utils.ts'
import type { StandardSchemaV1 } from './utils.ts'
import { Context } from './context.ts'
import type { Plugin } from './registry.ts'
import { buildOuterStack, composeError, DisposableList, getTraceable, isConstructor, isObject, symbols } from './utils.ts'
import type { Impl } from './reflect.ts'

declare module './context.ts' {
  export interface Context extends Pick<Fiber, 'effect'> {
    /** The fiber (plugin runtime instance) that owns this context. */
    fiber: Fiber
  }
}

const kValidationError = Symbol.for('ValidationError')

/** Error raised when plugin configuration fails standard-schema validation. */
export class ValidationError extends TypeError {
  name = 'ValidationError'

  /**
   * Build the aggregated message from schema issues.
   *
   * @param issues 鈥?the standard-schema issues, one message line each.
   */
  constructor(issues: readonly StandardSchemaV1.Issue[]) {
    super(`invalid config:\n` + issues.map(issue => {
      if (issue.path) {
        return `  - ${issue.message} (at ${issue.path.join('.')})`
      } else {
        return `  - ${issue.message}`
      }
    }).join('\n'))
  }
}

Object.defineProperty(ValidationError.prototype, kValidationError, {
  value: true,
})

/**
 * Validate and normalize config for a plugin runtime before it starts.
 *
 * @param runtime 鈥?the plugin runtime whose `Config` schema to apply.
 * @param config 鈥?the raw user config.
 * @returns the validated config, or `config` unchanged if the runtime has no schema.
 * @throws {ValidationError} when validation reports issues.
 */
export function resolveConfig(runtime: Plugin.Runtime, config: any) {
  if (!runtime.Config) return config
  // TODO: async validation
  const result = runtime.Config['~standard'].validate(config)
  if ('then' in result) {
    throw new TypeError('Async config validation is not supported')
  }
  if (result.issues) {
    throw new ValidationError(result.issues)
  } else {
    return result.value
  }
}

interface AsyncDisposable<T extends Awaitable<void> = Awaitable<void>> extends PromiseLike<() => T> {
  (): T
}

/**
 * Function returned by an effect to release resources during disposal.
 *
 * Disposers run in reverse registration order when the owning fiber unloads;
 * they may be async, in which case unloading awaits them.
 */
export type Disposable<T = any> = () => T

/**
 * Effect body result accepted by `ctx.effect()` and plugin startup.
 *
 * Either a single disposer, a promise of one, or a (possibly async) iterable
 * yielding several 鈥?generator effects register each yielded disposer as it
 * is produced.
 */
export type Effect<T = any> =
  | SyncEffect<T>
  | AsyncEffect<T>

type SyncEffect<T = any> =
  | Disposable<T>
  | Iterable<Disposable<T>, void, void>

type AsyncEffect<T = any> =
  | Promise<Disposable<T>>
  | AsyncIterable<Disposable<T>, void, void>

/** Tree node used to expose nested effect labels for diagnostics. */
export interface EffectMeta {
  /** Human-readable effect label, e.g. `ctx.on("event")` or `ctx.provide("name")`. */
  label: string
  /** Metadata of nested effects registered while this effect ran. */
  children: EffectMeta[]
}

interface EffectRunner<T> {
  epoch: T
  execute: () => any
  collect: (dispose: Disposable) => void
  getOuterStack: () => string[]
}

// Public effect disposers remain single-shot, but structural owners and outer
// effects must still be able to join a cleanup that another caller started.
const effectInertia = new WeakMap<Disposable, () => void | Promise<void>>()

// Structural owners consult this instead of raw logging, so a wrapper's
// recorded cleanup failures are reported exactly once (port of the upstream
// reentrant-lifecycle branch's EffectRecord reporting).
const cleanupReporters = new WeakMap<Disposable, (fallback?: unknown) => void>()

function runDisposable(dispose: Disposable) {
  const result = dispose()
  return effectInertia.get(dispose)?.() ?? result
}

/** Combine cleanup failures without flattening user AggregateErrors. */
function combineCleanupErrors(errors: unknown[]) {
  // Do not flatten user AggregateErrors: one cleanup callback owns one error.
  if (!errors.length) return
  if (errors.length === 1) return errors[0]
  return new AggregateError(errors, 'multiple cleanup errors')
}

/** Notify plugin teardown without allowing one observer to break ownership cleanup. */
function emitPluginDisposed(context: Context, fiber: Fiber) {
  const args: any[] = ['internal/plugin', fiber]
  let callbacks: Function[]
  try {
    callbacks = context.events.dispatch('emit', args)
  } catch (error) {
    context.logger.error(error)
    return
  }
  for (const callback of callbacks) {
    try {
      const returned = callback(...args)
      void Promise.resolve(returned).catch(error => context.logger.error(error))
    } catch (error) {
      context.logger.error(error)
    }
  }
}

/**
 * Lifecycle state for one plugin fiber.
 *
 * `PENDING` 鈥?waiting for required services; `LOADING` 鈥?the plugin callback
 * is running; `ACTIVE` 鈥?loaded and providing; `FAILED` 鈥?the callback or its
 * config threw; `UNLOADING` 鈥?disposers are running; `DISPOSED` 鈥?the fiber
 * was removed and cannot restart.
 */
export const enum FiberState {
  PENDING,
  LOADING,
  ACTIVE,
  FAILED,
  DISPOSED,
  UNLOADING,
}

/** Framework error with a stable machine-readable code. */
export class CordisError extends Error {
  /**
   * @param code 鈥?the stable error code; also the default message.
   * @param message 鈥?optional human-readable override.
   */
  constructor(public code: CordisError.Code, message?: string) {
    super(message ?? CordisError.Code[code])
  }
}

/** Cordis error code definitions. */
export namespace CordisError {
  export type Code = keyof typeof Code

  export const Code = {
    INACTIVE_EFFECT: 'cannot create effect on inactive context',
  } as const
}

const INACTIVE = '__INACTIVE__'

/**
 * Runtime instance of one plugin application.
 *
 * A fiber tracks dependency state, validated config, lifecycle effects, and
 * cleanup for the plugin context returned by `ctx.plugin()`.
 */
export class Fiber {
  /** Unique id within the registry; 0 for the root fiber, `null` once disposed. */
  public uid: number | null
  /** The context this fiber's plugin runs in (extends the parent context). */
  public readonly ctx: Context
  /** The validated plugin config (updated by `update()`). */
  public config: any
  /** The raw plugin config, re-resolved before each activation. */
  public _config: any
  /** Current lifecycle state; transitions emit `internal/status`. */
  public state = FiberState.PENDING
  /** Dispose this fiber: unload the plugin, then settle once cleanup finished. */
  public readonly dispose: () => Promise<void>
  /** Snapshot of required service implementations while loaded; `undefined` otherwise. */
  public store: Dict<Impl> | undefined
  /** The in-flight load/unload transition, if one is currently running. */
  public inertia: Promise<void> | undefined

  public readonly _hooks: Dict<DisposableList<Function>> = Object.create(null)
  public readonly _disposables = new DisposableList<Disposable>()

  public context: Context


  private _error: any
  private _runner: EffectRunner<string>
  private _generation = 0
  private _store: Dict<Impl> = Object.create(null)

  /**
   * Create a fiber. Plugin authors normally obtain fibers from `ctx.plugin()`
   * rather than constructing them directly.
   *
   * @param parent 鈥?the context the plugin was loaded from.
   * @param config 鈥?raw config, validated against the runtime's schema.
   * @param inject 鈥?resolved dependency map (service name 鈫?intercept config).
   * @param runtime 鈥?the shared plugin runtime, or `null` for the root fiber.
   * @param getOuterStack 鈥?captures the caller stack for effect diagnostics.
   */
  constructor(
    public parent: Context,
    config: any,
    public inject: Dict<any>,
    public runtime: Plugin.Runtime | null,
    getOuterStack: () => string[],
  ) {
    this._config = config
    const collect = (dispose: Disposable) => {
      this._disposables.push(dispose)
    }

    if (runtime) {
      this.uid = parent.registry.counter
      this.ctx = this.context = parent.extend({ fiber: this })

      const injectEntries = Object.entries(this.inject)
      if (injectEntries.length) {
        this.ctx[Context.intercept] = Object.create(parent[Context.intercept])
        for (const [name, config] of injectEntries) {
          if (isNullable(config)) continue
          this.ctx[Context.intercept][name] = config
        }
      }

      this._runner = {
        epoch: INACTIVE,
        getOuterStack,
        execute: function () {
          if (isConstructor(runtime.callback)) {
            // eslint-disable-next-line new-cap
            const instance = new runtime.callback(this.ctx, this.config)
            for (const hook of instance?.[symbols.initHooks] ?? []) {
              hook()
            }
            return instance?.[symbols.init]?.()
          } else {
            return runtime.callback(this.ctx, this.config)
          }
        },
        collect,
      }

      this.dispose = parent.fiber.effect(() => {
        const remove = runtime.fibers.push(this)
        return async () => {
          this.uid = null
          emitPluginDisposed(this.context, this)
          if (this.ctx.registry.has(runtime.callback)) {
            remove()
            if (!runtime.fibers.length) {
              this.ctx.registry.delete(runtime.callback)
            }
          }
          this._setEpoch(INACTIVE)
          // A PENDING fiber can already own effects registered by an
          // internal/plugin observer. Its epoch is still INACTIVE, so
          // _setEpoch() has no transition to drive; explicitly unload that
          // pre-activation work before reporting disposal complete.
          if (!this.inertia) {
            this._updateState(() => {
              this.inertia = this._unload()
              return FiberState.UNLOADING
            })
          }
          // `this.inertia` itself should never reject 鈥?both `_reload` and
          // `_unload` swallow their own work errors via `ctx.logger.error`.
          // If it *does* reject, the only remaining cause is the logger
          // itself failing, which we can't recover from in this exact spot
          // (calling the logger again is what just failed). Let the
          // rejection propagate; process-level crash is the honest outcome.
          while (this.inertia) {
            await this.inertia
          }
        }
      }, 'ctx.plugin()')

      try {
        // Publish only after the parent owns a fully assigned disposer. A
        // synchronous observer may dispose either this fiber or its parent.
        this.context.emit('internal/plugin', this)
      } catch (error) {
        // Publication failed synchronously. The disposer removes the child
        // from both the parent and runtime before control escapes.
        void Promise.resolve(this.dispose()).catch(reason => this.ctx.logger.error(reason))
        throw error
      }

      // Keep the initial notification's historical PENDING view. The loader
      // may also extend `inject` in that notification, so resolve dependencies
      // only after publication. A reentrant parent unload makes the child
      // disposer responsible for draining any PENDING effects instead.
      if (this.uid !== null && parent.fiber.state !== FiberState.UNLOADING) {
        for (const name of Object.keys(this.inject)) {
          this._checkImpl(name)
        }
        this._refresh()
      }
    } else {
      this.uid = 0
      this.ctx = this.context = parent
      this.state = FiberState.ACTIVE
      this.store = Object.create(null)
      this._runner = {
        epoch: '',
        getOuterStack,
        execute: () => {},
        collect,
      }
      this.dispose = () => this.restart()
    }
  }

  /** The plugin's display name, inherited from the nearest named ancestor, else `'root'`. */
  get name() {
    let fiber: Fiber = this
    do {
      if (fiber.runtime?.name) return fiber.runtime.name
      fiber = fiber.parent.fiber
    } while (fiber !== fiber.parent.fiber)
    return 'root'
  }

  /**
   * Throw if the fiber has already been disposed.
   *
   * @returns nothing when the fiber is still active.
   * @throws {CordisError} `INACTIVE_EFFECT` when the fiber's uid has been cleared.
   */
  assertActive() {
    if (this.uid !== null) return
    throw new CordisError('INACTIVE_EFFECT')
  }

  private _execute<T>(runner: EffectRunner<T>) {
    const oldEpoch = runner.epoch
    return composeError((info) => {
      const safeCollect = (dispose: void | Disposable) => {
        if (typeof dispose === 'function') {
          runner.collect(dispose)
        } else if (!isNullable(dispose)) {
          throw new TypeError('Invalid effect')
        }
      }
      const effect: Effect = runner.execute.call(this)
      if (typeof effect === 'function') {
        return runner.collect(effect)
      } else if (isNullable(effect)) {
        // return
      } else if (!isObject(effect)) {
        throw new TypeError('Invalid effect')
      } else if ('then' in effect) {
        return effect.then(safeCollect)
      } else if (Symbol.iterator in effect) {
        info.error = new Error()
        const iter = effect[Symbol.iterator]()
        while (true) {
          const result = iter.next()
          safeCollect(result.value)
          if (result.done) return
        }
      } else if (Symbol.asyncIterator in effect) {
        const iter = effect[Symbol.asyncIterator]()
        return (async () => {
          // force async stack trace
          await Promise.resolve()
          info.error = new Error()
          while (true) {
            if (runner.epoch !== oldEpoch) return
            const result = await iter.next()
            safeCollect(result.value)
            if (result.done) return
          }
        })()
      } else {
        throw new TypeError('Invalid effect')
      }
    }, runner.getOuterStack)
  }

  /**
   * Register a cleanup-aware effect on this fiber.
   *
   * `execute` runs immediately; the disposers it produces are collected and
   * run (in reverse order) either when the returned disposer is called or
   * when the fiber unloads, whichever comes first. Calling the disposer twice
   * is a no-op. Throws `CordisError('INACTIVE_EFFECT')` if the fiber is
   * already disposed, and `TypeError` if `execute` returns an invalid shape.
   *
   * @param execute 鈥?the effect body; see {@link Effect} for accepted shapes.
   * @param label 鈥?effect label shown in `getEffects()` diagnostics.
   * @returns a disposer that tears the effect down and settles once done.
   */
  effect(execute: () => SyncEffect, label?: string): AsyncDisposable<Promise<void>>
  /** Same as above for async effects; the disposer is also awaitable. */
  effect(execute: () => Effect, label?: string): AsyncDisposable<Promise<void>>
  effect(execute: () => Effect, label = 'anonymous'): any {
    this.assertActive()
    if (this.state === FiberState.UNLOADING) {
      throw new CordisError('INACTIVE_EFFECT')
    }

    // Port of the upstream reentrant-lifecycle branch's EffectRecord:
    // execution and disposal are separate result channels joined through one
    // exactly-once disposal task. Every cleanup runs (strict LIFO, awaited
    // in order); rejections are recorded, never veto the remaining
    // callbacks, and combine into a single AggregateError (user aggregates
    // stay unflattened). Repeated disposal calls join the first task.
    const cleanups: Disposable[] = []
    const cleanupFailures: { error: unknown, source?: Disposable }[] = []
    let cleanupReported = false
    let executionState: 'running' | 'pending' | 'fulfilled' | 'rejected' = 'running'
    let executionError: unknown
    let executionTask: Promise<void> | undefined
    let executionGate: { promise: Promise<void>, resolve: () => void, reject: (reason: unknown) => void } | undefined
    let disposalTask: Promise<void> | undefined
    let removeWrapper = () => false

    const meta: EffectMeta = { label, children: [] }
    const runner: EffectRunner<boolean> = {
      execute,
      epoch: true,
      collect: (dispose) => {
        cleanups.push(dispose)
        this._disposables.delete(dispose)
        if (dispose[symbols.effect]) {
          meta.children.push(dispose[symbols.effect])
        }
      },
      getOuterStack: buildOuterStack(),
    }

    const reportCleanupFailures = (fallback?: unknown) => {
      if (cleanupReported) return
      cleanupReported = true
      if (!cleanupFailures.length) {
        if (fallback !== undefined) this.ctx.logger.error(fallback)
        return
      }
      for (const failure of cleanupFailures) {
        const report = failure.source && cleanupReporters.get(failure.source)
        if (report) {
          // A nested effect's failure is reported by its own record, once.
          report(failure.error)
        } else {
          this.ctx.logger.error(failure.error)
        }
      }
    }

    const waitForExecution = (): Promise<void> => {
      if (executionState === 'fulfilled') return Promise.resolve()
      if (executionState === 'rejected') return Promise.reject(executionError)
      if (executionTask) return executionTask
      if (!executionGate) {
        // Synchronous execution normally allocates no promise. The gate is
        // created only when disposal actually reenters before execution
        // returns.
        let resolve!: () => void
        let reject!: (reason: unknown) => void
        const promise = new Promise<void>((res, rej) => {
          resolve = res
          reject = rej
        })
        promise.catch(() => {})
        executionGate = { promise, resolve, reject }
      }
      return executionGate.promise
    }

    const runCleanups = (): unknown[] | Promise<unknown[]> => {
      const failures = cleanupFailures
      const pending = cleanups.splice(0).reverse()
      let index = 0

      const next = (): void | Promise<void> => {
        while (index < pending.length) {
          const dispose = pending[index++]
          try {
            const result = dispose()
            if (isObject(result) && 'then' in result) {
              // Preserve strict LIFO ordering across async cleanup. Rejections
              // are recorded and do not veto the remaining cleanup callbacks.
              return Promise.resolve(result).then(next, (error) => {
                failures.push({ error, source: dispose })
                return next()
              })
            }
          } catch (error) {
            failures.push({ error, source: dispose })
          }
        }
      }

      const result = next()
      return isObject(result) && 'then' in result
        ? Promise.resolve(result).then(() => failures)
        : failures
    }

    const finishDisposal = () => {
      const finalize = (failures: { error: unknown }[]) => {
        const error = combineCleanupErrors(failures.map(failure => failure.error))
        return error ? Promise.reject<void>(error) : Promise.resolve()
      }
      const result = runCleanups()
      if (isObject(result) && 'then' in result) {
        return { task: Promise.resolve(result).then(finalize), synchronous: false }
      }
      return { task: finalize(result), synchronous: true }
    }

    const startDisposal = (): Promise<void> => {
      // Public callers and structural owners always join the first task;
      // cleanup itself is exactly-once.
      if (disposalTask) return disposalTask
      runner.epoch = false

      let task: Promise<void>
      let synchronous = false
      if (executionState === 'fulfilled' || executionState === 'rejected') {
        ;({ task, synchronous } = finishDisposal())
      } else {
        task = waitForExecution().then(
          () => finishDisposal().task,
          () => finishDisposal().task,
        )
      }

      if (synchronous && !this.inertia) {
        // Outside an owner transition, fully synchronous cleanup can retire
        // the wrapper immediately. During unload it stays joinable until
        // settlement.
        removeWrapper()
        disposalTask = task
      } else {
        disposalTask = task.then(
          () => { removeWrapper() },
          (error) => {
            removeWrapper()
            throw error
          },
        )
      }
      disposalTask.catch(() => {})
      return disposalTask
    }

    const failExecution = (reason: unknown, report = false) => {
      executionState = 'rejected'
      executionError = reason
      executionGate?.reject(reason)
      if (report) this.ctx.logger.error(reason)
      // Execution and disposal are separate result channels. The execution
      // error is delivered by throw/the thenable; structural owners only
      // join cleanup and observe cleanup failures.
      startDisposal().catch(error => reportCleanupFailures(error))
    }

    const settleExecution = (task: void | Promise<void>) => {
      if (!isObject(task) || !('then' in task)) {
        executionState = 'fulfilled'
        executionGate?.resolve()
        return
      }
      executionState = 'pending'
      // Keep one execution task for both the thenable effect API and
      // disposal that starts while asynchronous execution is pending.
      executionTask = Promise.resolve(task).then(() => {
        executionState = 'fulfilled'
        executionGate?.resolve()
      }, (reason) => {
        failExecution(reason, true)
        throw reason
      })
      executionTask.catch(() => {})
    }

    const wrapper = defineProperty(() => startDisposal(), symbols.effect, meta) as AsyncDisposable<Promise<void>>
    effectInertia.set(wrapper, () => disposalTask)
    cleanupReporters.set(wrapper, reportCleanupFailures)

    // Make the effect visible to a reentrant owner unload before execute()
    // runs any plugin code. Async teardown stays owner-visible until it
    // settles, allowing an outer effect to join cleanup another caller began.
    removeWrapper = this._disposables.push(wrapper)
    let task: void | Promise<void>
    try {
      task = this._execute(runner)
    } catch (reason) {
      failExecution(reason)
      throw reason
    }
    settleExecution(task)
    wrapper.then = (onFulfilled, onRejected) => {
      return waitForExecution().then(() => startDisposal).then(onFulfilled, onRejected)
    }
    return wrapper
  }

  /**
   * Return metadata for currently registered effects.
   *
   * @returns one {@link EffectMeta} tree per labeled live effect.
   */
  getEffects() {
    return [...this._disposables]
      .map<EffectMeta>(dispose => dispose[symbols.effect])
      .filter(Boolean)
  }

  private _getState() {
    if (this.uid === null) return FiberState.DISPOSED
    if (this._error) return FiberState.FAILED
    if (this._runner.epoch !== INACTIVE) return FiberState.ACTIVE
    return FiberState.PENDING
  }

  /** Notify `internal/status` without letting one observer break dependency notification. */
  private emitStatusChanged(oldState: FiberState) {
    const args: any[] = ['internal/status', this, oldState]
    let callbacks: Function[]
    try {
      callbacks = this.context.events.dispatch('emit', args)
    } catch (error) {
      this.ctx.logger.error(error)
      return
    }
    for (const callback of callbacks) {
      try {
        const returned = callback(...args)
        void Promise.resolve(returned).catch(error => this.ctx.logger.error(error))
      } catch (error) {
        this.ctx.logger.error(error)
      }
    }
  }

  private _updateState(callback: () => void | FiberState) {
    const oldState = this.state
    this.state = callback() ?? this._getState()
    if (oldState === this.state) return
    this.emitStatusChanged(oldState)

    // only notify changes between ACTIVE and NON-ACTIVE states
    if (oldState !== FiberState.ACTIVE && this.state !== FiberState.ACTIVE) return
    for (const key of Reflect.ownKeys(this.ctx.reflect.store)) {
      const impl = this.ctx.reflect.store[key as symbol]
      if (impl.fiber !== this) continue
      this.ctx.reflect.notify([impl.name])
    }
  }

  _checkImpl(name: string) {
    const impl = this.ctx.reflect._getImpl(name, true)
    if (!impl) return delete this._store[name]
    try {
      if (impl.check && !impl.check.call(getTraceable(this.ctx, impl.value))) {
        return delete this._store[name]
      }
    } catch (error) {
      impl.fiber.ctx.logger.error(error)
      return delete this._store[name]
    }
    this._store[name] = impl
  }


  private _setEpoch(epoch: string) {
    const oldEpoch = this._runner.epoch
    if (epoch === oldEpoch) return
    this._runner.epoch = epoch
    if (this.inertia) return
    this._updateState(() => {
      if (epoch !== INACTIVE && oldEpoch === INACTIVE) {
        this.inertia = this._reload()
        return FiberState.LOADING
      } else {
        this.inertia = this._unload()
        return FiberState.UNLOADING
      }
    })
  }

  /**
   * Recompute the epoch from current dependency availability.
   *
   * Non-forced refreshes use the content-derived epoch (provider uids), so
   * equal notifications coalesce — mainline semantics. A forced refresh
   * allocates a fresh generation token instead: an in-flight load carrying
   * an older token becomes stale (its late failures cannot poison the newer
   * generation) even when the dependency content is unchanged.
   */
  _refresh(force = false) {
    let epoch = ''
    for (const name of Object.keys(this.inject)) {
      const impl = this._store[name]
      if (!impl) {
        epoch = INACTIVE
        break
      }
      epoch += ':' + impl.fiber.uid
    }
    if (force && epoch !== INACTIVE) epoch += '#' + ++this._generation
    this._setEpoch(epoch)
  }

  /** Resolve raw config through `internal/config` and the runtime schema. */
  _resolveConfig(config: any) {
    config = this.context.waterfall(this, 'internal/config', config, () => config)
    return this.runtime ? resolveConfig(this.runtime, config) : config
  }

  private async _reload() {
    this.store = { ...this._store }
    const oldEpoch = this._runner.epoch
    try {
      await Promise.resolve()
      // A disposer queued before this checkpoint may already have invalidated
      // the load. Do not run plugin code for a stale epoch; the state update
      // below will drain any effects collected while the fiber was PENDING.
      if (this._runner.epoch === oldEpoch) {
        this.config = this._resolveConfig(this._config)
        await this._execute(this._runner)
        this._error = undefined
      }
    } catch (reason) {
      this.ctx.logger.error(reason)
      // A stale generation's late failure cannot poison the current one:
      // only own the failure when no newer token took over while the body ran.
      if (this._runner.epoch === oldEpoch) {
        this._error = reason
        this._runner.epoch = INACTIVE
      }
    }
    this._updateState(() => {
      if (this._runner.epoch === oldEpoch) {
        this.inertia = undefined
      } else {
        this.inertia = this._unload()
        return FiberState.UNLOADING
      }
    })
  }

  private async _unload() {
    await Promise.all(this._disposables.clear().map(async (dispose) => {
      try {
        await composeError(async (info) => {
          await Promise.resolve()
          info.error = new Error()
          await runDisposable(dispose)
        }, this._runner.getOuterStack)
      } catch (reason) {
        // A wrapper reports its recorded cleanup failures exactly once;
        // raw disposables log the combined rejection directly.
        const report = cleanupReporters.get(dispose)
        if (report) {
          report(reason)
        } else {
          this.ctx.logger.error(reason)
        }
      }
    }))
    this.store = undefined
    this._updateState(() => {
      if (this._runner.epoch === INACTIVE) {
        this.inertia = undefined
      } else {
        this.inertia = this._reload()
        return FiberState.LOADING
      }
    })
  }

  /**
   * Wait for current lifecycle work and rethrow startup errors.
   *
   * @returns this fiber, once it has settled into a stable state.
   * @throws the config-validation or plugin-startup error, if any.
   */
  async await() {
    while (this.inertia) {
      await this.inertia
    }
    if (this._error) throw this._error
    return this
  }

  /**
   * Dispose and immediately reload this plugin with its current config.
   *
   * Re-anchors to `this.ctx.fiber` first: `ctx.plugin()` returns a prototype
   * wrapper, and lifecycle state must mutate the real fiber, not the wrapper.
   *
   * @returns a promise resolving once the reload settled.
   * @throws {CordisError} `INACTIVE_EFFECT` when the fiber is already disposed.
   */
  async restart() {
    // `this` may be the registry wrapper; re-anchor to the real fiber.
    const fiber: Fiber = this.ctx.fiber
    fiber.assertActive()
    fiber._setEpoch(INACTIVE)
    fiber._refresh()
    await fiber.await()
  }

  /**
   * Validate and apply new config, then restart the plugin.
   *
   * Re-anchors to `this.ctx.fiber` first (see {@link restart}). Runs the
   * `internal/update` waterfall before restarting, so update hooks can veto
   * or replace the restart. Config is validated before any stored state is
   * touched: a rejected update leaves the fiber's previous config intact for
   * later dependency-driven reloads.
   *
   * @param config 鈥?the new raw config; validated before anything restarts.
   * @param noSave 鈥?hint for persistence hooks not to write the change back.
   * @returns the update waterfall result; the default restart returns a promise.
   * @throws when validation, an update listener, or the restarted plugin fails.
   */
  update(config: any, noSave = false) {
    // `this` may be the registry wrapper; re-anchor to the real fiber.
    const fiber: Fiber = this.ctx.fiber
    fiber.assertActive()
    if (fiber.state !== FiberState.ACTIVE) {
      // Config resolution may access injected services, so defer it until the
      // fiber can activate. The raw value is re-resolved then.
      fiber._config = config
      fiber._error = undefined
      fiber._setEpoch(INACTIVE)
      // Force a fresh generation token: an in-flight load carrying the old
      // config becomes stale (and its late failures cannot poison this one)
      // even though the dependency content is unchanged.
      fiber._refresh(true)
      // Join the transition this update coalesced into: the returned promise
      // settles once the in-flight generation (loading or unloading) is done.
      // Rejections are the fiber's channel (await the fiber for errors), so
      // callers that ignore the result never see an unhandled rejection.
      return fiber.await().catch(() => {})
    }
    // Validate and resolve before touching stored state: a rejected update
    // must not poison the config a later reload would consume.
    const resolved = fiber._resolveConfig(config)
    return fiber.context.waterfall(fiber, 'internal/update', resolved, noSave, () => {
      fiber._config = config
      fiber.config = resolved
      fiber._error = undefined
      return fiber.restart()
    })
  }
}

