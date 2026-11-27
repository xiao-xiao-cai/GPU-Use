import { request } from './client'

export interface CollectionRequest {
  kind: 'projects' | 'catalog' | 'instances' | 'stock' | 'pool'
  project_id?: string
  watch_id?: string
}

export interface CollectionReceipt {
  id: string
  state: 'queued' | 'succeeded' | 'failed'
  outcomes: { scope: string; finished_at: number | null; error: string | null }[]
}

export function waitForNextRead(signal: AbortSignal, delay: number) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted()
    const abort = () => {
      window.clearTimeout(timer)
      reject(signal.reason)
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, delay)
    signal.addEventListener('abort', abort, { once: true })
  })
}

export async function queueCollection(body: CollectionRequest, signal: AbortSignal, queued: () => void) {
  const { id } = await request<{ id: string }>('/refresh', { method: 'POST', body, signal })
  signal.throwIfAborted()
  queued()
  return readCollectionReceipt(id, signal)
}

export async function readCollectionReceipt(id: string, signal: AbortSignal) {
  const path = '/refresh/' + encodeURIComponent(id)
  const start = Date.now()
  while (true) {
    const receipt = await request<CollectionReceipt>(path, { signal })
    if (receipt.state !== 'queued') return receipt
    await waitForNextRead(signal, Date.now() - start < 30_000 ? 2_000 : 5_000)
  }
}
