import { expect, test } from 'claude-code/testing'

// Made-up cswap-quota output, --lines and the default markdown
const LINES = '#1* 5h 2.0% (4h 50m) · 7d 24.0% (4d 8h)\n#2 5h 0.0% · 7d 98.0% (8h 0m) RUNS OUT\n'
const TABLE = 'Claude usage · Thu 10:47 AM CEST\n\n| Account | 5-hour | Week · resets CEST |\n|---|---|---|\n| **now** | 2.0% · 4h 50m | 24.0% · Mon 7 PM |\n\n**Week: lasts**\n- Using 30.0%/day vs plan 42.4%/day (-29.2%)\n'

function formatter(exitCode = 0, stderr = '') {
  return ($, e) => ({ value: { exitCode, stdout: exitCode ? '' : e.argv.includes('--lines') ? LINES : TABLE, stderr } })
}

function stubs(on, run) {
  on('clock.now', () => ({ value: 0 }))
  on('ui.invalidate', () => ({ value: undefined }))
  on('process.run', run)
}

test('Asserts: /quota prints cswap-quota\'s markdown as it came', async ($, on) => {
  stubs(on, formatter())
  const answer = await $.command.run({ command: 'quota', args: '' })
  expect(answer.text).toBe(TABLE.trimEnd())
})

test('Asserts: /quota runs cswap-quota for the markdown and cswap-quota --lines for the band', async ($, on) => {
  const calls = []
  stubs(on, ($, e) => {
    calls.push(e.argv)
    return formatter()($, e)
  })
  await $.command.run({ command: 'quota', args: '' })
  expect(calls).toEqual([['cswap-quota', '--lines'], ['cswap-quota']])
})

test('Asserts: a failed cswap-quota run prints the failure, never earlier output', async ($, on) => {
  stubs(on, formatter(1, 'cswap-quota: cswap is not on PATH, so `cswap list --json` cannot run\n'))
  const answer = await $.command.run({ command: 'quota', args: '' })
  expect(answer.text).toBe('quota: cswap-quota failed (exit 1): cswap-quota: cswap is not on PATH, so `cswap list --json` cannot run')
})

test('Asserts: the table rides under an answer once per interval, and never under a subagent turn', async ($, on) => {
  let now = 31 * 60 * 1000
  on('clock.now', () => ({ value: now }))
  on('ui.invalidate', () => ({ value: undefined }))
  on('process.run', formatter())
  on('turn.complete', () => ({ text: 'answer' }))
  const turn = { turnId: 't', answer: 'answer', durationMs: 1, isAborted: false, reason: 'answer' }

  expect((await $.turn.complete({ ...turn, agentId: 'a1' })).text).toBe('answer')
  expect((await $.turn.complete(turn)).text).toBe('answer\n\n' + TABLE.trimEnd())
  now += 10 * 60 * 1000
  expect((await $.turn.complete(turn)).text).toBe('answer')
  now += 21 * 60 * 1000
  expect((await $.turn.complete(turn)).text).toBe('answer\n\n' + TABLE.trimEnd())
})

test('Asserts: /quota\'s output row draws as markdown on every surface, and an errored row draws the default row', async ($, on) => {
  // The default drawing of a command row, beneath the mod
  on('ui.render', ($, e) => $.ui.resolve(e).Text({ children: 'default row' }))
  for (const surface of ['terminal', 'desktop', 'mobile'] as const) {
    const props = { command: 'quota', args: '', text: TABLE.trimEnd(), isErrored: false }
    const ui = await $.ui.mount({ plugin: 'quota', surface, component: 'CommandOutput', props })
    expect(await ui.drawn()).toMatchObject({ type: 'Markdown', props: { text: TABLE.trimEnd() } })
    await ui.unmount()
    const errored = await $.ui.mount({ plugin: 'quota', surface, component: 'CommandOutput', props: { ...props, isErrored: true } })
    expect(await errored.drawn()).not.toMatchObject({ type: 'Markdown', props: { text: TABLE.trimEnd() } })
    await errored.unmount()
  }
})
