// /quota: every claude-swap account's usage, the pool's average rate and where that rate runs out.
// On demand and under an answer once per interval it prints `cswap-quota`'s markdown into the
// transcript; the short `cswap-quota --lines` form sits in the band above the prompt.

// The markdown form, for the transcript: a table reads well on every surface, where a code block of
// long lines wraps into an unreadable column on a phone
const QUOTA_MARKDOWN = ['cswap-quota']
// The same read as short lines, for the band above the prompt
const QUOTA_LINES = [...QUOTA_MARKDOWN, '--lines']

// The newest short lines drawn above the prompt; null until the first read
let band = null

async function read($, argv) {
  const run = await $.process.run(argv)
  // A failed read prints as a failed read; earlier output is never shown in its place
  return run.exitCode === 0 ? run.stdout.trimEnd() : 'quota: cswap-quota failed (exit ' + run.exitCode + '): ' + run.stderr.trim()
}

// Reads both forms: the short lines into the band, the markdown returned for the transcript
async function refresh($) {
  band = (await read($, QUOTA_LINES)).split('\n')
  $.ui.invalidate('ui.render')
  return read($, QUOTA_MARKDOWN)
}

export function register(on, options) {
  const intervalMs = options.intervalMinutes * 60 * 1000
  let lastPrintedAt = 0

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'quota', description: 'Every claude-swap account\'s usage now: 5-hour and weekly use with time to reset, the pace, and anything that needs action' })
    return next(e)
  })

  on('command.run', { command: 'quota' }, async ($) => {
    lastPrintedAt = await $.clock.now()
    return { text: await refresh($) }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    // Subagent turns end inside the main turn; only the main answer gets the table
    if (e.agentId !== undefined) return result
    const now = await $.clock.now()
    if (now - lastPrintedAt < intervalMs) return result
    lastPrintedAt = now
    const text = await refresh($)
    return { ...result, text: result.text ? result.text + '\n\n' + text : text }
  })

  // /quota's output row is drawn as markdown in the row's place, so the table renders as a table on
  // every surface and the row carries no command-name label; the stored row keeps the text the model reads
  on('ui.render', { component: 'CommandOutput', props: { command: 'quota' } }, async ($, e, next) => {
    if (e.props.isErrored) return next(e)
    const { Markdown } = $.ui.resolve(e)
    return Markdown({ text: e.props.text })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (band === null) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return Box({ flexDirection: 'column', children: band.map((l) => Text({ dimColor: true, children: l })) })
  })
}
