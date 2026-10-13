import { afterEach, expect, it, vi } from 'vitest'
import { request } from './http'

afterEach(() => {
  vi.restoreAllMocks()
})

it('returns parsed JSON and sends the API path, options, and default JSON header', async () => {
  const data = { id: 1, title: 'Write tests' }
  const fetchMock = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify(data), { status: 201 }))
  const controller = new AbortController()
  const body = JSON.stringify({ title: 'Write tests' })

  await expect(
    request<typeof data>('/todos', {
      method: 'POST',
      body,
      signal: controller.signal,
      headers: { Authorization: 'Bearer token' },
    }),
  ).resolves.toEqual(data)

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock).toHaveBeenCalledWith('/api/todos', {
    method: 'POST',
    body,
    signal: controller.signal,
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer token',
    },
  })
})

it('allows an explicit content type to override the default', async () => {
  const fetchMock = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response('{}'))

  await request('/todos', { headers: { 'Content-Type': 'text/plain' } })

  expect(fetchMock).toHaveBeenCalledWith('/api/todos', {
    headers: { 'Content-Type': 'text/plain' },
  })
})

it('returns undefined for a successful 204 response without parsing its empty body', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(null, { status: 204 }),
  )

  await expect(request<void>('/todos/1')).resolves.toBeUndefined()
})

it('reports a string detail from a server error', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ detail: 'Task not found' }), { status: 404 }),
  )

  await expect(request('/todos/1')).rejects.toThrow('Task not found')
})

it.each([
  ['a non-string detail', JSON.stringify({ detail: [{ message: 'Invalid' }] })],
  ['a missing detail', '{}'],
  ['a null body', 'null'],
  ['malformed JSON', 'Server unavailable'],
])('reports a fallback server error for %s', async (_label, body) => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body, { status: 500 }),
  )

  await expect(request('/todos')).rejects.toThrow(
    'The operation failed. Please try again.',
  )
})

it.each([
  ['a server detail', JSON.stringify({ detail: 'Other validation message' })],
  ['malformed JSON', 'Invalid request'],
])(
  'reports task validation guidance for a 422 response with %s',
  async (_label, body) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body, { status: 422 }),
    )

    await expect(request('/todos')).rejects.toThrow(
      'Check the task details: title up to 120 characters, description up to 2000 characters, and a valid due date.',
    )
  },
)

it.each([false, true])(
  'reports a connection failure with a non-aborted signal present: %s',
  async (withSignal) => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(
      new TypeError('Failed to fetch'),
    )
    const init = withSignal
      ? { signal: new AbortController().signal }
      : undefined

    await expect(request('/todos', init)).rejects.toThrow(
      'Cannot connect to the server. Please try again.',
    )
  },
)

it('preserves the original rejection when the request signal is aborted', async () => {
  const controller = new AbortController()
  const error = new DOMException('Request aborted', 'AbortError')
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    controller.abort()
    throw error
  })

  await expect(request('/todos', { signal: controller.signal })).rejects.toBe(
    error,
  )
})
