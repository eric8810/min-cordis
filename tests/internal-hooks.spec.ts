import { Context } from '../src'
import { expect, describe, it } from 'vitest'
import { mock } from 'node:test'

/**
 * Pins the internal/* extension points — including the deliberate
 * divergences from upstream that must not be "simplified" back:
 *
 * - `parallel` reports its true mode to `internal/dispatch` (upstream HEAD
 *   currently reports `emit`).
 * - async observer rejections are contained (never unhandled, never stall
 *   the transition they observe).
 */

describe('internal hooks', () => {
  it('internal/dispatch sees mode, name, args, and thisArg for all five modes', async () => {
    const root = new Context()
    const seen: any[] = []
    root.on('internal/dispatch', (mode, name, args, thisArg) => {
      seen.push([mode, name, args, thisArg])
    })

    root.emit('evt/a', 1, 2)
    await root.parallel('evt/b', 3)
    await root.serial('evt/c', 4)
    root.bail('evt/d', 5)
    root.waterfall('evt/e', 6, (value: any) => value)

    expect(seen.map(([mode, name]: any[]) => [mode, name])).to.deep.equal([
      ['emit', 'evt/a'],
      ['parallel', 'evt/b'],
      ['serial', 'evt/c'],
      ['bail', 'evt/d'],
      ['waterfall', 'evt/e'],
    ])
    // listener args pass through verbatim (waterfall's include the next fn)
    expect(seen[0][2]).to.deep.equal([1, 2])
    expect(seen[1][2]).to.deep.equal([3])
    expect(seen[4][2][0]).to.equal(6)
    expect(typeof seen[4][2][1]).to.equal('function')
  })

  it('internal/dispatch is not emitted for internal events', () => {
    const root = new Context()
    const seen: any[] = []
    root.on('internal/dispatch', (mode, name) => seen.push([mode, name]))
    root.emit('internal/service', 'foo', 1)
    expect(seedIsInternalSilent(seen)).to.be.true
  })

  it('internal/dispatch receives the dispatch this argument', () => {
    const root = new Context()
    const marker = { marker: true }
    let observed: unknown
    root.on('internal/dispatch', (mode, name, args, thisArg) => {
      if (name === 'evt/this') observed = thisArg
    })
    root.emit(marker as any, 'evt/this')
    expect(observed).to.equal(marker)
  })

  it('internal/config transforms raw config before validation and apply', async () => {
    const root = new Context()
    const applied: any[] = []
    const plugin = (ctx: any, config: any) => {
      applied.push(config)
      // body returns nothing: a number return would be an invalid effect
    }
    const dispose = root.on('internal/config', (config, next) => {
      // continue the chain, then carry the transform through the return
      // value (the built-in inner ignores its argument)
      next()
      return { ...config, injected: true }
    })

    const view = root.plugin(plugin, { base: 1 })
    await view
    expect(applied).to.deep.equal([{ base: 1, injected: true }])

    // the transform also applies to later updates
    await view.update({ base: 2 })
    expect(applied).to.deep.equal([
      { base: 1, injected: true },
      { base: 2, injected: true },
    ])
    dispose()
    await view.dispose()
  })

  it('internal/update handlers may replace the restart', async () => {
    const root = new Context()
    const applied: any[] = []
    let restarted = false
    const plugin = (ctx: any, config: any) => {
      applied.push(config.value)
    }
    const view = root.plugin(plugin, { value: 'old' })
    await view

    const dispose = view.ctx.on('internal/update', (config: any, noSave: any, next: any) => {
      restarted = true
      return 'vetoed' as any
    })

    const result = await view.update({ value: 'new' })
    expect(result).to.equal('vetoed')
    expect(restarted).to.be.true
    // the restart was skipped: the old config is still applied exactly once
    expect(applied).to.deep.equal(['old'])
    dispose()
    await view.dispose()
  })

  it('async internal/status observer rejections are contained, not stalling', async () => {
    const root = new Context()
    const errors = mock.fn()
    ;(root.logger as any).error = errors

    const boom = async () => {
      throw new Error('status boom')
    }
    root.on('internal/status', boom)

    const seen: string[] = []
    const provider = root.plugin((ctx) => {
      ctx.provide('svc', 1)
      seen.push('provided')
    })
    await provider
    expect(seen).to.deep.equal(['provided'])
    // the async rejection reached the logger without breaking the transition
    await Promise.resolve()
    await Promise.resolve()
    expect(errors.mock.calls.length).to.be.greaterThanOrEqual(1)
    expect(errors.mock.calls[0].arguments[0]).to.have.property('message', 'status boom')
    await provider.dispose()
  })

  it('public dispatch() consumes the event name and binds this', () => {
    const root = new Context()
    const marker = { marker: true }
    let receivedThis: unknown
    let receivedArg: unknown
    root.on('evt/direct', function (this: any, arg: any) {
      receivedThis = this
      receivedArg = arg
    })
    const callbacks = root.events.dispatch('emit', [marker, 'evt/direct', 42] as any)
    for (const callback of callbacks) callback(42)
    expect(receivedThis).to.equal(marker)
    expect(receivedArg).to.equal(42)
  })
})

function seedIsInternalSilent(seen: any[]) {
  return seen.length === 0
}
