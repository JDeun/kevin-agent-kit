import { deepStrictEqual } from 'node:assert/strict'
import { readFileSync } from 'node:fs'

export function assertDecisionPin(actual, expected, { ledger, decisionId }) {
  const records = readFileSync(ledger, 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line))
  const matches = records.filter((record) => record?.id === decisionId)
  if (matches.length !== 1) {
    throw new Error(`Expected one decision for ${decisionId}, found ${matches.length}`)
  }

  const decision = matches[0]
  for (const key of ['date', 'decided_by', 'source', 'summary']) {
    if (typeof decision[key] !== 'string' || !decision[key].trim()) {
      throw new Error(`Decision ${decisionId} requires ${key}`)
    }
  }
  const date = new Date(`${decision.date}T00:00:00Z`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(decision.date) || decision.date.startsWith('0000') ||
      Number.isNaN(date.valueOf()) || date.toISOString().slice(0, 10) !== decision.date) {
    throw new Error(`Decision ${decisionId} requires a valid date in YYYY-MM-DD form`)
  }

  deepStrictEqual(actual, expected,
    `Changing decision ${decisionId} requires a human decision (${decision.date}, ${decision.decided_by}, ${decision.source})`)
}
