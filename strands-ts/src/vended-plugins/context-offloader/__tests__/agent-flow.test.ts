import { describe, it, expect, vi } from 'vitest'
import { Agent } from '../../../agent/agent.js'
import { MockMessageModel } from '../../../__fixtures__/mock-message-model.js'
import { createMockTool } from '../../../__fixtures__/tool-helpers.js'
import { AfterToolCallEvent, BeforeToolCallEvent } from '../../../hooks/events.js'
import { InMemoryStorage } from '../../../storage/in-memory-storage.js'
import { TextBlock, ToolResultBlock } from '../../../types/messages.js'
import { ImageBlock } from '../../../types/media.js'
import { ContextOffloader } from '../plugin.js'

function resultText(result: ToolResultBlock): string {
  return (result.content[0] as TextBlock).text
}

function toolResult(agent: Agent): ToolResultBlock {
  return agent.messages
    .flatMap((message) => message.content)
    .find((block): block is ToolResultBlock => block instanceof ToolResultBlock)!
}

function makeAgent(text: string, previewTokens = 100, maxResultTokens = 300, withImage = false): Agent {
  const model = new MockMessageModel()
    .addTurn({ type: 'toolUseBlock', name: 'read_page', toolUseId: 'page', input: {} })
    .addTurn({ type: 'textBlock', text: 'Done' })
  if (withImage) vi.spyOn(model, 'countTokens').mockResolvedValue(1000)
  return new Agent({
    model,
    printer: false,
    storage: new InMemoryStorage(),
    tools: [
      createMockTool(
        'read_page',
        () =>
          new ToolResultBlock({
            toolUseId: 'page',
            status: 'success',
            content: withImage
              ? [new TextBlock(text), new ImageBlock({ format: 'png', source: { bytes: new Uint8Array(1000) } })]
              : [new TextBlock(text)],
          })
      ),
    ],
    plugins: [new ContextOffloader({ maxResultTokens, previewTokens })],
  })
}

// https://github.com/strands-agents/harness-sdk/issues/5604
describe('ContextOffloader', () => {
  describe('agent hook flow', () => {
    it('lets default audit hooks see complete output before offloading and preserves the pagination notice', async () => {
      const notice = '[Showing lines 1-200 of 5000. Use offset/limit to read more.]'
      const text = Array.from({ length: 200 }, (_, index) => `line ${index + 1}: data`).join('\n') + '\n' + notice
      const agent = makeAgent(text)
      const audit: Array<{ hook: string; text: string }> = []
      agent.addHook(AfterToolCallEvent, (event) => {
        audit.push({ hook: 'first', text: resultText(event.result) })
      })
      agent.addHook(AfterToolCallEvent, (event) => {
        audit.push({ hook: 'second', text: resultText(event.result) })
      })

      await agent.invoke('Read the page')

      expect(audit).toEqual([
        { hook: 'second', text },
        { hook: 'first', text },
      ])
      const placeholder = resultText(toolResult(agent))
      expect(placeholder).toContain('[Offloaded:')
      expect(placeholder).toContain('line 1: data')
      expect(placeholder).toContain(notice)
      expect(placeholder).not.toContain('line 100: data')
      const reference = placeholder.split('[Stored references:]\n  ')[1]!.split(' ')[0]!
      const retrieved = await agent.tool.retrieve_offloaded_content!.invoke({ reference })
      expect(resultText(retrieved)).toBe(text)
    })

    it.each(['error', 'cancel'] as const)('keeps %s results out of storage', async (mode) => {
      const text = 'failure'.repeat(1000)
      const agent = makeAgent(text)
      if (mode === 'cancel') {
        agent.addHook(BeforeToolCallEvent, (event) => {
          event.cancel = text
        })
      } else {
        agent.addHook(AfterToolCallEvent, (event) => {
          event.result = new ToolResultBlock({
            toolUseId: event.result.toolUseId,
            status: 'error',
            content: [new TextBlock(text)],
          })
        })
      }
      await agent.invoke('Read the page')
      const result = toolResult(agent)
      expect(result.status).toBe('error')
      expect(resultText(result)).toContain(text)
      expect(resultText(result)).not.toContain('[Offloaded:')
      expect(await agent.storage!.list('')).toEqual([])
    })
  })

  describe('preview budget', () => {
    it.each([
      { text: 'a'.repeat(100) + 'z'.repeat(100), tokens: 10, expected: 'a'.repeat(21) + '\n...\n' + 'z'.repeat(14) },
      { text: '界'.repeat(100) + '😀'.repeat(100), tokens: 10, expected: '界'.repeat(21) + '\n...\n' + '😀'.repeat(7) },
      { text: 'short', tokens: 10, expected: 'short' },
      { text: 'a'.repeat(40), tokens: 10, expected: 'a'.repeat(40) },
      { text: '😀'.repeat(100), tokens: 3, expected: '😀😀\n...\n\ude00😀' },
      { text: '', tokens: 10, expected: '' },
      { text: 'a'.repeat(200), tokens: 0, expected: '' },
      { text: 'abcdefghij'.repeat(20), tokens: 1, expected: 'abcd' },
      { text: 'abcdefghij'.repeat(20), tokens: 1.3, expected: 'abcde' },
      { text: 'abcdefghij'.repeat(20), tokens: 1.5, expected: '\n...\nj' },
    ])('keeps the exact bounded preview for $tokens tokens and $expected', async ({ text, tokens, expected }) => {
      const agent = makeAgent(text, tokens, 100, true)
      await agent.invoke('Read the page')
      const result = toolResult(agent)
      expect(resultText(result)).toContain('[Offloaded:')
      const preview = resultText(result).split('as a last resort.\n\n')[1]!.split('\n\n[Stored references:]')[0]!
      expect(preview).toBe(expected)
      expect(preview.length).toBeLessThanOrEqual(tokens * 4)
    })
  })
})
