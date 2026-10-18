/**
 * Host half of dsh-valuation-service.
 *
 * Registers one agent tool, `valuation_service`, plus a short system-prompt
 * section. The tool runs the bundled Python service over a workpaper tree
 * (01.json / 02.json / 04.json), computes six ratio groups, forecast ratios,
 * DCF input slots, comparable PS/PE and IRR, and writes one unified schema.
 * Missing inputs are marked missing — never invented.
 *
 * @module dsh-valuation-service
 */
import { fileURLToPath } from 'node:url'
import { dirname, isAbsolute, resolve } from 'node:path'
import { runValuation } from './runner.js'

/** Cordis plugin name. */
export const name = 'dsh-valuation-service'

/** Required host services: tool registry + prompt assembly. */
export const inject = ['tools', 'systemPrompt']

const HERE = dirname(fileURLToPath(import.meta.url))

/* ------------------------------------------------------------------ */
/* defineTool (structure-compatible reimplementation of the SDK's)     */
/* ------------------------------------------------------------------ */

function compileValue(spec) {
  const node = {}
  if (typeof spec.description === 'string' && spec.description.length > 0) node.description = spec.description
  const type = spec.type
  if (type === undefined || type === 'json') return node
  if (type === 'object') {
    node.type = 'object'
    node.additionalProperties = spec.additionalProperties
    if (spec.properties !== undefined) node.properties = compilePropertyMap(spec.properties).properties
    return node
  }
  if (type === 'array') {
    node.type = 'array'
    if (spec.items !== undefined) node.items = compileValue(spec.items)
    return node
  }
  node.type = type
  if (spec.enum !== undefined) node.enum = [...spec.enum]
  if (spec.const !== undefined) node.const = spec.const
  return node
}

function compilePropertyMap(spec) {
  const properties = {}
  const required = []
  for (const [key, entry] of Object.entries(spec)) {
    const { required: isRequired, ...valueSpec } = entry
    properties[key] = compileValue(valueSpec)
    if (isRequired === true) required.push(key)
  }
  return required.length > 0 ? { properties, required } : { properties }
}

function matchesScalarType(value, type) {
  switch (type) {
    case 'string': return typeof value === 'string'
    case 'number': return typeof value === 'number'
    case 'integer': return typeof value === 'number' && Number.isInteger(value)
    case 'boolean': return typeof value === 'boolean'
    case 'null': return value === null
    default: return true
  }
}

function validateValue(schema, value, path) {
  if (typeof schema.type !== 'string' || schema.type.length === 0) return []
  if (schema.type === 'object') {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return [`${path} must be an object`]
    const violations = []
    for (const key of schema.required ?? []) if (!(key in value)) violations.push(`${path}.${key} is required`)
    if (schema.additionalProperties === false) {
      const known = new Set(Object.keys(schema.properties ?? {}))
      for (const key of Object.keys(value)) if (!known.has(key)) violations.push(`${path}.${key} is not a declared property`)
    }
    for (const [key, child] of Object.entries(schema.properties ?? {})) {
      if (key in value) violations.push(...validateValue(child, value[key], `${path}.${key}`))
    }
    return violations
  }
  if (schema.type === 'array') {
    if (!Array.isArray(value)) return [`${path} must be an array`]
    const violations = []
    if (schema.items !== undefined) value.forEach((item, i) => violations.push(...validateValue(schema.items, item, `${path}[${i}]`)))
    return violations
  }
  if (!matchesScalarType(value, schema.type)) return [`${path} must be ${schema.type}`]
  if (schema.enum !== undefined && !schema.enum.some((v) => v === value)) return [`${path} must be one of ${schema.enum.map(String).join(', ')}`]
  if (schema.const !== undefined && schema.const !== value) return [`${path} must be ${String(schema.const)}`]
  return []
}

function defineTool(options) {
  const compiled = compilePropertyMap(options.parameters)
  const parameters = { type: 'object', properties: compiled.properties }
  if (compiled.required !== undefined) parameters.required = compiled.required
  return {
    name: options.name,
    description: options.description,
    parameters,
    output: {
      schema: {},
      render(args, value) {
        return options.output.render(args, value)
      },
    },
    async execute(args, exec) {
      const violations = validateValue(parameters, args, 'arguments')
      if (violations.length > 0) throw new Error(`Error: invalid arguments: ${violations.join('; ')}`)
      return options.execute(args, exec)
    },
  }
}


/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */

function fmt(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number' && Math.abs(v - Math.round(v)) < 1e-9) return String(Math.round(v))
  return String(v)
}

function renderValuation(_args, value) {
  if (value === null || typeof value !== 'object') return [{ type: 'text', text: String(value) }]
  if (value.ok === false) return [{ type: 'text', text: `valuation_service failed: ${value.error ?? 'unknown error'}` }]

  const lines = []
  lines.push(`# Valuation service`)
  lines.push(`base: ${value.base ?? ''}`)
  if (value.written) lines.push(`written: ${value.output ?? ''}`)
  else lines.push('dry run (no file written)')

  const projects = value.projects ?? []
  if (projects.length > 0) {
    lines.push('')
    lines.push('| project | DCF | missing | PS | PE | IRR closed | assumption diff | gate |')
    lines.push('|---|---|---|---|---|---|---|---|')
    for (const p of projects) {
      lines.push(`| ${p.project_name} | ${p.dcf?.status ?? ''} | ${(p.dcf?.missing ?? []).length} | ${p.comparable?.ps ?? ''} | ${p.comparable?.pe ?? ''} | ${p.irr_closed ? 'yes' : 'no'} | ${p.assumption_diff ?? 0} | ${Object.keys(p.gate ?? {}).length} |`)
    }
  }

  const errors = value.errors ?? []
  if (errors.length > 0) {
    lines.push('', '## Errors')
    for (const e of errors) lines.push(`- ${e.project}: ${e.error}`)
  }

  return [{ type: 'text', text: lines.join('\n') }]
}

/* ------------------------------------------------------------------ */
/* plugin apply                                                        */
/* ------------------------------------------------------------------ */

const PROMPT_SECTION = `## Financial data & valuation (valuation_service)

When a workpaper tree contains 01.json / 02.json / 04.json and the user asks for ratios, forecast ratios, DCF, comparable PS/PE, IRR or a standardized valuation dataset, call valuation_service instead of hand-computing.

- base: workpaper root containing <project>/json/{01,02,04}.json (default: $DSH_VALUATION_BASE or ./数据底表).
- projects: optional comma-separated project directory names.
- write: false for a dry run (default), true to write <base>/valuations-service.json.
- out: optional output path override.

Rules:
1. Dry-run first and inspect DCF / comparable / IRR status.
2. Never fill a missing WACC, g, capex, D&A, ΔWC, tax rate or net debt with a guess. Report them as missing.
3. \`assumption_diff\` items are flags for human review, not automatic corrections.
4. Do not describe an assumption-based PE as a market multiple.`

function resolveBase(args, exec) {
  if (typeof args.base !== 'string' || args.base.trim().length === 0) return undefined
  if (isAbsolute(args.base)) return args.base
  const cwd = exec?.agent?.session?.header?.cwd
  if (typeof cwd !== 'string' || cwd.length === 0) throw new Error('Error: invalid_input: relative base given but the session has no working directory')
  return resolve(cwd, args.base)
}

export function apply(ctx) {
  const disposers = []

  disposers.push(ctx.tools.register(defineTool({
    name: 'valuation_service',
    description: 'Deterministic financial-data and valuation service. Reads <base>/<project>/json/{01,02,04}.json, computes six ratio groups, forecast ratios, DCF input slots, comparable PS/PE and IRR, and writes one unified schema. Missing DCF inputs stay blank and are marked missing. Dry-run by default.',
    parameters: {
      base: {
        type: 'string',
        description: 'Workpaper root containing <project>/json/{01,02,04}.json. Absolute, or relative to the session working directory. Default: $DSH_VALUATION_BASE or ./数据底表.',
      },
      projects: {
        type: 'string',
        description: 'Optional comma-separated project directory names. Omit to auto-discover every project with 01/02/04 JSON.',
      },
      write: {
        type: 'boolean',
        description: 'Write the aggregate JSON file. Default false (dry run).',
      },
      out: {
        type: 'string',
        description: 'Optional output path for the aggregate JSON file. Default: <base>/valuations-service.json.',
      },
    },
    output: { render: renderValuation },
    async execute(args, exec) {
      return runValuation({
        base: resolveBase(args, exec),
        projects: args.projects,
        write: args.write === true,
        out: typeof args.out === 'string' && args.out.trim().length > 0 ? args.out : undefined,
        engineRoot: HERE,
        cwd: exec?.agent?.session?.header?.cwd,
      })
    },
  })))

  const disposeSection = ctx.systemPrompt.section({
    name: 'dsh-valuation-service',
    order: 188,
    text: PROMPT_SECTION,
  })
  ctx.effect(() => disposeSection, 'dsh-valuation-service: prompt section')

  ctx.effect(() => () => {
    for (const dispose of disposers.splice(0)) dispose()
  }, 'dsh-valuation-service: tools')
}
