import { Context } from './context.ts'
import { Service } from './service.ts'
import { defineProperty, symbols } from './utils.ts'

/** Severity ranks; messages below the threshold are dropped. */
const LEVELS = { debug: 0, info: 1, success: 1, warn: 2, error: 3 } as const

type Level = keyof typeof LEVELS

function threshold(): number {
  const raw = (globalThis.process?.env ?? {}).MIN_CORDIS_LOG as Level | undefined
  return LEVELS[raw] ?? LEVELS.info
}

/**
 * Minimal named logger. Replaces the vendored Cordis logger (buffer,
 * exporters, i18n levels) with a level-filtered console sink; embedders can
 * swap `ctx.logger` for their own service since the core only calls
 * `ctx.logger.error` for error containment.
 */
export class Logger {
  /** Create a logger that prefixes every line with `name`. */
  constructor(private name: string) {}

  private output(level: Level, args: any[]) {
    if (LEVELS[level] < threshold()) return
    const sink = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log
    const stamp = new Date().toISOString()
    sink(`[${stamp}] ${level.toUpperCase().padEnd(7)} ${this.name}`, ...args)
  }

  /** Log at debug severity (hidden unless `MIN_CORDIS_LOG=debug`). */
  debug(...args: any[]) { this.output('debug', args) }
  /** Log at info severity. */
  info(...args: any[]) { this.output('info', args) }
  /** Log a success marker at info severity. */
  success(...args: any[]) { this.output('success', args) }
  /** Log at warning severity. */
  warn(...args: any[]) { this.output('warn', args) }
  /** Log at error severity (stderr). */
  error(...args: any[]) { this.output('error', args) }
}

/**
 * Logger service installed as `ctx.logger`.
 *
 * Callable: `ctx.logger('my-plugin')` returns a named `Logger`. The service
 * itself also exposes level methods so `ctx.logger.error(...)` works from
 * framework containment paths.
 */
export class LoggerService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'logger')
    // noShadow: the service is identity-aware (derives names from callers),
    // so tracing must keep the origin context association.
    defineProperty(this, symbols.tracker, {
      property: 'ctx',
      associate: 'logger',
      noShadow: true,
    })
  }

  /** Return a named logger bound to the service sink. */
  [Service.invoke](name = 'app'): Logger {
    return new Logger(name)
  }

  debug(...args: any[]) { this[Service.invoke]().debug(...args) }
  info(...args: any[]) { this[Service.invoke]().info(...args) }
  success(...args: any[]) { this[Service.invoke]().success(...args) }
  warn(...args: any[]) { this[Service.invoke]().warn(...args) }
  error(...args: any[]) { this[Service.invoke]().error(...args) }
}
