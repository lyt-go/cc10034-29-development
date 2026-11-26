/** 结果修订接口封装：提交、复核、改绑、确认与配置轮换，冲突码原样带回页面。 */
import { request } from '@/api/client'
import type {
  RevisionActionResult,
  RevisionConflict,
  RevisionPanelState,
} from '@/domain/result-revision'

export class RevisionConflictError extends Error {
  readonly conflict: RevisionConflict

  constructor(conflict: RevisionConflict) {
    super(conflict.message)
    this.conflict = conflict
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  const payload = (body ?? {}) as { ok?: boolean; conflict?: RevisionConflict; message?: string }
  if (!response.ok || payload.ok === false) {
    const conflict: RevisionConflict = payload.conflict ?? {
      code: `HTTP_${response.status}`,
      message: payload.message ?? `接口返回 ${response.status}，修订未推进`,
      missing: [],
    }
    throw new RevisionConflictError(conflict)
  }
  return payload as T
}

export function loadRevisionPanel(queryId: number): Promise<RevisionPanelState> {
  return call<RevisionPanelState>(`/api/revisions/queries/${queryId}`)
}

export function submitRevision(
  queryId: number,
  values: Record<string, string | number>,
): Promise<RevisionActionResult & { revision: { revision_no: string } }> {
  return call(`/api/revisions/queries/${queryId}/submit`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
}

export function reviewRevision(
  queryId: number,
  revisionNo: string,
  verdict: '通过' | '驳回',
  note: string,
): Promise<RevisionActionResult> {
  return call(`/api/revisions/queries/${queryId}/revisions/${revisionNo}/review`, {
    method: 'POST',
    body: JSON.stringify({ verdict, reviewer: '数据安全组', note }),
  })
}

export function rebindRevision(
  queryId: number,
  revisionNo: string,
  version: string,
): Promise<RevisionActionResult> {
  return call(`/api/revisions/queries/${queryId}/revisions/${revisionNo}/rebind`, {
    method: 'POST',
    body: JSON.stringify({ version, reason: '轮换后改绑当前版本', operator: '数据工程组' }),
  })
}

export function confirmRevision(
  queryId: number,
  revisionNo: string,
): Promise<RevisionActionResult & { receipt?: { receipt_no: string } }> {
  return call(`/api/revisions/queries/${queryId}/revisions/${revisionNo}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ operator: '噪声引擎' }),
  })
}

export function rotateNoiseConfig(
  configId: number,
  targetVersion: string,
  reason: string,
): Promise<RevisionActionResult> {
  return call(`/api/revisions/noise-configs/${configId}/rotate`, {
    method: 'POST',
    body: JSON.stringify({ target_version: targetVersion, reason, operator: '噪声引擎' }),
  })
}
