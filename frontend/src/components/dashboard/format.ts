import type { StockCounts } from '../../api/dashboard'

export const stockRateName = '规格可租率'
export const stockCoverageName = '规格探测完整率'

const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 4 })
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 })

export function valueLabel(value: number | null, suffix = '') {
  return value == null || !Number.isFinite(value) ? '暂无数据' : number.format(value) + suffix
}

export function moneyLabel(value: string | null) {
  return value == null || !Number.isFinite(Number(value)) ? '金额未知' : currency.format(Number(value))
}

export function billMoneyLabel(value: string | null | undefined) {
  const parts = /^(-?)([0-9]+)\.([0-9]{2})$/.exec(value ?? '')
  return parts ? parts[1] + '¥' + number.format(BigInt(parts[2])) + '.' + parts[3] : '金额待核对'
}

export function rateLabel(rate: number | null) {
  if (rate == null || !Number.isFinite(rate)) return '未知'
  if (rate > 0 && rate < 0.001) return '<0.1%'
  if (rate > 0.999 && rate < 1) return '>99.9%'
  return percent.format(rate)
}

export function stockRate(counts: StockCounts) {
  return counts.known_count ? counts.available_count / counts.known_count : null
}

export function coverageRate(counts: StockCounts) {
  return counts.expected_count ? counts.known_count / counts.expected_count : null
}

export function hourLabel(hour: number) {
  return String(hour).padStart(2, '0') + ':00'
}

export function durationLabel(seconds: number | null) {
  if (seconds == null) return '暂无数据'
  if (seconds < 60) return Math.round(seconds) + ' 秒'
  if (seconds < 3600) return valueLabel(seconds / 60, ' 分钟')
  return valueLabel(seconds / 3600, ' 小时')
}
