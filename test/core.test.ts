import assert from 'node:assert/strict'
import test from 'node:test'
import { Context, Fiber, Service, ValidationError } from '../src/index.ts'
import type { StandardSchemaV1 } from '../src/index.ts'
import type { Context as ContextType } from '../src/index.ts'

/** Capture console error output so contained failures stay quiet but asserted. */
function captureConsoleError<T>(run: () => T): { errors: any[], restore(): void } {
  const errors: any[] = []
  const original = console.error
  console.error = (...args: any[]) => { errors.push(args) }
  return {
    errors,
    restore: () => { console.error = original },
  }
}

/** Minimal standard-schema validator: `{ value: number }` must be finite. */
const numberConfig: StandardSchemaV1<any, { value: number }> = {
  '~standard': {
    version: 1,
    vendor: 'test',
    validate(input: any) {
      if (typeof input?.value === 'number' && Number.isFinite(input.value)) {
        return { value: input }
      }
      return { issues: [{ message: 'value must be a finite number' }] }
    },
  },
}

test('plugin mounts, its effects dispose in reverse order, and await resolves to the real fiber', async () => {
  const ctx = new Context()
  const order: string[] = []

  const fiber = ctx.plugin((c) => {
    c.effect(() => { order.push('setup-a'); return () => order.push('dispose-a') })
    c.effect(() => { order.push('setup-b'); return () => order.push('dispose-b') })
    c.effect(() => { order.push('setup-c'); return () => order.push('dispose-c') })
  })

  // `ctx.plugin()` returns a prototype wrapper over the real fiber so `await`
  // can resolve without thenable recursion; lifecycle calls re-anchor inside.
  const real = (fiber as Fiber).ctx.fiber as Fiber
  assert.notStrictEqual(fiber, real)
  assert.strictEqual(Object.getPrototypeOf(fiber), real)
  assert.strictEqual(await fiber, real)

  await fiber.dispose()
  assert.deepStrictEqual(order, [
    'setup-a', 'setup-b', 'setup-c',
    'dispose-c', 'dispose-b', 'dispose-a',
  ])
})

test('provide/inject: consumer waits for the provider, reloads when it changes', async () => {
  const ctx = new Context()
  const seen: string[] = []

  const consumer = ctx.inject(['svc'], (c) => {
    seen.push(c.get('svc') as string)
  })
  assert.strictEqual((consumer as Fiber).state, 0) // PENDING before the provider exists

  let current = 'v1'
  const provider = ctx.plugin((c) => {
    c.provide('svc', current)
  })
  await provider
  await consumer
  assert.strictEqual((consumer as Fiber).state, 2) // ACTIVE
  assert.deepStrictEqual(seen, ['v1'])

  // Re-provide from a new provider fiber: the consumer must reload and see v2.
  current = 'v2'
  await provider.dispose()
  const provider2 = ctx.plugin((c) => {
    c.provide('svc', current)
  })
  await provider2
  await consumer
  assert.deepStrictEqual(seen, ['v1', 'v2'])

  await consumer.dispose()
  await provider2.dispose()
})

test('Service subclass registers on ctx and consumers can call its methods', async () => {
  const ctx = new Context()

  class Counter extends Service {
    count = 0
    constructor(c: ContextType) {
      super(c, 'counter')
    }
    bump() {
      return ++this.count
    }
  }

  const calls: number[] = []
  const provider = ctx.plugin((c) => {
    c.plugin(Counter)
  })
  const consumer = ctx.inject(['counter'], (c) => {
    calls.push((c as any).counter.bump())
  })
  await provider
  await consumer
  assert.deepStrictEqual(calls, [1])

  await consumer.dispose()
  await provider.dispose()
})

test('five dispatch modes behave per contract; listeners unload with their fiber', async () => {
  const ctx = new Context()
  const order: string[] = []

  ctx.on('evt/simple', (arg: string) => order.push(`simple:${arg}`))
  ctx.emit('evt/simple', 'x')
  assert.deepStrictEqual(order, ['simple:x'])

  ctx.on('evt/par', async () => {
    throw new Error('par-boom')
  })
  await assert.rejects(
    () => ctx.parallel('evt/par'),
    (error: AggregateError) => {
      assert.ok(error instanceof AggregateError)
      assert.match(error.errors[0].message, /par-boom/)
      return true
    },
  )

  ctx.on('evt/serial', async () => undefined)
  ctx.on('evt/serial', async () => 'first-bail')
  ctx.on('evt/serial', async () => 'second-bail')
  assert.strictEqual(await ctx.serial('evt/serial'), 'first-bail')

  ctx.on('evt/bail', () => undefined)
  ctx.on('evt/bail', () => 'sync-bail')
  assert.strictEqual(ctx.bail('evt/bail'), 'sync-bail')

  ctx.on('evt/wf', (req: { v: string }, next: () => any) => { req.v += '+a'; return next() })
  ctx.on('evt/wf', (req: { v: string }, next: () => any) => { req.v += '+b'; return next() })
  // First-registered listener is outermost: `a` appends before `b` sees it.
  const wfReq = { v: 'v' }
  assert.strictEqual(ctx.waterfall('evt/wf', wfReq, (req) => req.v + '!'), 'v+a+b!')

  // A listener that returns without calling `next()` vetoes the chain.
  ctx.on('evt/wf', () => 'vetoed')
  const vetoReq = { v: 'v' }
  assert.strictEqual(ctx.waterfall('evt/wf', vetoReq, (req) => req.v + '!'), 'vetoed')

  let onceCount = 0
  ctx.once('evt/once', () => { onceCount++ })
  ctx.emit('evt/once')
  ctx.emit('evt/once')
  assert.strictEqual(onceCount, 1)

  // Listeners registered inside a plugin disappear with the fiber.
  const scoped: string[] = []
  const fiber = ctx.plugin((c) => {
    c.on('evt/scoped', () => scoped.push('hit'))
  })
  await fiber
  ctx.emit('evt/scoped')
  assert.deepStrictEqual(scoped, ['hit'])
  await fiber.dispose()
  ctx.emit('evt/scoped')
  assert.deepStrictEqual(scoped, ['hit'])
})

test('audit fix: `__proto__`/`constructor` event names register and dispatch without crashing', () => {
  const ctx = new Context()
  const seen: string[] = []
  ctx.on('__proto__', () => seen.push('proto'))
  ctx.on('constructor', () => seen.push('ctor'))
  ctx.emit('__proto__')
  ctx.emit('constructor')
  assert.deepStrictEqual(seen, ['proto', 'ctor'])
})

test('audit fix: rejected update does not poison later reloads', async () => {
  const ctx = new Context()
  const calls: number[] = []

  const plugin = (c: any, config: { value: number }) => {
    calls.push(config.value)
    c.effect(() => () => calls.push(-config.value))
  }
  ;(plugin as any).Config = numberConfig

  const fiber = ctx.plugin(plugin, { value: 1 })
  await fiber

  await fiber.update({ value: 2 })
  assert.deepStrictEqual(calls, [1, -1, 2])

  // A rejected update must leave the previous config in place.
  await assert.rejects(async () => fiber.update({ value: 'bad' } as any), ValidationError)
  await fiber.restart()
  assert.deepStrictEqual(calls, [1, -1, 2, -2, 2])

  await fiber.dispose()
})

test('audit fix: a throwing `internal/status` observer cannot stall dependency notification', async () => {
  const ctx = new Context()
  const capture = captureConsoleError(() => {})

  try {
    ctx.on('internal/status', () => {
      throw new Error('observer boom')
    })

    const seen: string[] = []
    const consumer = ctx.inject(['svc'], (c) => {
      seen.push(c.get('svc') as string)
    })
    const provider = ctx.plugin((c) => {
      c.provide('svc', 'value')
    })
    await provider
    await consumer
    assert.strictEqual((consumer as Fiber).state, 2)
    assert.deepStrictEqual(seen, ['value'])
    assert.ok(capture.errors.length > 0) // the observer error was contained and logged

    await consumer.dispose()
    await provider.dispose()
  } finally {
    capture.restore()
  }
})

test('audit fix: emit routes async listener rejections to the logger instead of the process', async () => {
  const ctx = new Context()
  const capture = captureConsoleError(() => {})
  let unhandled = false
  const onUnhandled = () => { unhandled = true }
  process.on('unhandledRejection', onUnhandled)

  try {
    ctx.on('evt/async', async () => {
      throw new Error('async boom')
    })
    ctx.emit('evt/async')
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.strictEqual(unhandled, false)
    assert.ok(capture.errors.length > 0)
  } finally {
    process.off('unhandledRejection', onUnhandled)
    capture.restore()
  }
})

test('isolate scopes a service name to the subtree below it', async () => {
  const root = new Context()
  const iso = root.isolate('svc')

  const fiber = iso.plugin((c) => {
    c.provide('svc', 'scoped-value')
  })
  await fiber

  assert.strictEqual(iso.get('svc'), 'scoped-value')
  assert.strictEqual(root.get('svc'), undefined)

  await fiber.dispose()
})

test('update on a PENDING fiber defers validation until activation', async () => {
  const ctx = new Context()
  const calls: number[] = []

  const plugin = (c: any, config: { value: number }) => {
    calls.push(config.value)
  }
  ;(plugin as any).Config = numberConfig

  const fiber = ctx.plugin(plugin, { value: 1 }, )
  await fiber

  const consumer = ctx.inject(['svc'], plugin)
  assert.strictEqual((consumer as Fiber).state, 0)

  // Defer an invalid config while PENDING: it must fail on activation, not silently load.
  consumer.update({ value: 'bad' } as any)
  const provider = ctx.plugin((c) => {
    c.provide('svc', 'ok')
  })
  await provider
  await assert.rejects(() => (consumer as Fiber).await(), ValidationError)

  await fiber.dispose()
  await provider.dispose()
})
