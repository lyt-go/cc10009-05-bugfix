import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dispatch'
const columns = [
  "策略编号",
  "目标域名",
  "分配算法",
  "权重因子",
  "节点亲和性",
  "负载阈值",
  "再平衡间隔",
  "溢出处理",
  "策略版本",
  "状态"
]
const actions = [
  "提交策略验证",
  "启用策略",
  "停用策略"
]
const statuses = [
  "草稿",
  "策略验证中",
  "已启用",
  "已停用"
]
const resultColumns = [
  "结果编号",
  "来源策略编号",
  "策略版本",
  "处理结果",
  "失败原因",
  "占用节点数",
  "生成时间",
  "更新时间"
]

// 同一目标域名重复显示时保留的优先级：生效优先，其次流程更靠后的版本。
const STATUS_PRIORITY: Record<string, number> = {
  '已启用': 3,
  '策略验证中': 2,
  '草稿': 1,
  '已停用': 0,
}

type Row = Record<string, string | number | null>

function normalizeDomain(value: unknown): string {
  let text = String(value ?? '').trim().toLowerCase()
  const schemeIndex = text.indexOf('://')
  if (schemeIndex >= 0) {
    text = text.slice(schemeIndex + 3)
  }
  text = text.split('/')[0] ?? ''
  text = text.split(':')[0] ?? ''
  return text.trim().replace(/\.+$/, '')
}

function compareRowPriority(a: Row, b: Row): number {
  const statusDiff = (STATUS_PRIORITY[String(a.status ?? '')] ?? -1) - (STATUS_PRIORITY[String(b.status ?? '')] ?? -1)
  if (statusDiff !== 0) return statusDiff
  const versionDiff = Number(a['策略版本'] ?? 0) - Number(b['策略版本'] ?? 0)
  if (versionDiff !== 0) return versionDiff
  return Number(a.id ?? 0) - Number(b.id ?? 0)
}

function dedupeByDomain(rows: Row[]): Row[] {
  const winners = new Map<string, Row>()
  for (const row of rows) {
    const key = normalizeDomain(row['目标域名']) || `row-${String(row.id)}`
    const existing = winners.get(key)
    if (!existing || compareRowPriority(row, existing) > 0) {
      winners.set(key, row)
    }
  }
  return Array.from(winners.values())
}

function cellValue(row: Row, column: string): string | number {
  if (column === '状态') {
    return String(row.status ?? '—')
  }
  return row[column] ?? '—'
}

export default function DispatchView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detailDomain, setDetailDomain] = useState<string | null>(null)
  const [detailRows, setDetailRows] = useState<Row[]>([])

  const displayRows = useMemo(() => dedupeByDomain(rows), [rows])
  const stats = useMemo(() => ([
    { label: '分配策略总数', value: total },
    { label: '已启用策略', value: rows.filter((row) => row.status === '已启用').length },
    { label: '验证中策略', value: rows.filter((row) => row.status === '策略验证中').length },
  ]), [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams()
    if (filters['策略编号']) query.set('keyword', filters['策略编号'])
    if (filters['目标域名']) query.set('domain', filters['目标域名'])
    if (filters['状态']) query.set('status', filters['状态'])
    const suffix = query.toString() ? `?${query.toString()}` : ''
    try {
      const response = await request(`${ENDPOINT}${suffix}`)
      if (!response.ok) {
        throw new Error('分配策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '请求分配列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error('请求分配动作未生效，请稍后重试')
      }
      if (payload && payload.ok === false) {
        throw new Error(typeof payload.message === 'string' ? payload.message : '请求分配动作被拒绝')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '请求分配操作失败')
    }
  }

  async function openDetail(row: Row) {
    const domain = normalizeDomain(row['目标域名'])
    setDetailDomain(domain)
    setDetailRows([])
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/results?domain=${encodeURIComponent(domain)}`)
      if (!response.ok) {
        throw new Error('目标域名的请求分配结果读取失败')
      }
      const payload = await response.json()
      setDetailRows(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '目标域名详情读取失败')
    }
  }

  async function closeDetail() {
    setDetailDomain(null)
    setDetailRows([])
    await reload()
  }

  if (detailDomain !== null) {
    return (
      <section className="page" data-module="dispatch">
        <header className="page-head">
          <div>
            <h2>目标域名详情：{detailDomain}</h2>
            <p className="page-desc">该域名的唯一请求分配结果数据集，保留来源策略与处理结果，策略版本可追溯。</p>
          </div>
          <div className="page-actions">
            <button className="btn" type="button" onClick={() => void closeDetail()}>
              返回请求分配
            </button>
          </div>
        </header>

        <table className="data-table">
          <thead>
            <tr>
              {resultColumns.map((column) => <th key={column}>{column}</th>)}
            </tr>
          </thead>
          <tbody>
            {detailRows.map((row) => (
              <tr key={String(row.id)}>
                {resultColumns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              </tr>
            ))}
            {detailRows.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={resultColumns.length}>该域名暂无请求分配结果记录</td>
              </tr>
            )}
          </tbody>
        </table>

        <footer className="page-foot">
          <span>共 {detailRows.length} 条请求分配结果</span>
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </footer>
      </section>
    )
  }

  return (
    <section className="page" data-module="dispatch">
      <header className="page-head">
        <div>
          <h2>请求分配管理</h2>
          <p className="page-desc">管理采集请求的分配策略，按域名粒度和节点负载进行动态调度。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分配策略登记入口尚未接入审批流')}>
            登记分配策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出请求分配清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>策略编号</span>
          <input
            value={filters['策略编号'] ?? ''}
            placeholder="按策略编号检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, '策略编号': event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>目标域名</span>
          <input
            value={filters['目标域名'] ?? ''}
            placeholder="按目标域名检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, '目标域名': event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters['状态'] ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, '状态': event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {displayRows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{cellValue(row, column)}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>
                  域名详情
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {displayRows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无请求分配数据，可先登记分配策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条请求分配记录，按目标域名去重后显示 {displayRows.length} 条</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
