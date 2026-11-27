import type { HourlyRange } from '../api/inventoryOverview'
import { moneyLabel } from './dashboard/format'

export function inventoryRangeLabel(range: HourlyRange, projectId: string) {
  if (!range.total) return '暂无目录规格'
  if (!projectId) return '选择项目后显示价格'
  if (range.minimum === null || range.maximum === null) {
    return `已报价 ${range.quoted}/${range.total} 种` + (range.failed ? ` · ${range.failed} 种暂不可用` : '')
  }
  const amount = Number(range.minimum) === Number(range.maximum)
    ? moneyLabel(range.minimum) : `${moneyLabel(range.minimum)}–${moneyLabel(range.maximum)}`
  return amount + '/实例小时'
}

export const inventoryPriceSources = { account: '账户报价', catalog: '平台目录价', mixed: '部分使用目录价' }
