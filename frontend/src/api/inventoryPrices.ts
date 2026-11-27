import { query, request, type ChargeType } from './client'
import { waitForNextRead } from './refresh'

export interface InventoryPrice {
  state: 'queued' | 'ready' | 'failed'
  amount: string | null
  source: 'account' | 'catalog' | null
  observed_at: number | null
  error: string | null
  stale: boolean
  currency: 'CNY'
  billing_unit: 'hour' | 'day' | 'month'
}

export async function requestInventoryPrice(projectId: string, specId: string, charge: ChargeType, signal: AbortSignal) {
  const scope = { project_id: projectId, charge_type: charge }
  let result = await request<InventoryPrice>('/inventory/prices', { method: 'POST', body: { ...scope, spec_id: specId }, signal })
  while (result.state === 'queued') {
    await waitForNextRead(signal, 1500)
    result = await request<InventoryPrice>('/inventory/prices/' + encodeURIComponent(specId) + query(scope), { signal })
  }
  if (result.state === 'failed') throw new Error(result.error || '平台暂未返回可用价格')
  if (result.state !== 'ready' || result.amount == null || !Number.isFinite(Number(result.amount))
    || result.observed_at == null || !['account', 'catalog'].includes(result.source ?? '')
    || !['hour', 'day', 'month'].includes(result.billing_unit) || result.currency !== 'CNY') {
    throw new Error('服务器返回的库存报价格式无效')
  }
  return result
}
