/** 结果修订的类型定义：与 `app/services/result_revision.py` 的返回结构一一对应。 */

export type RevisionConflict = {
  code: string
  message: string
  missing: string[]
  current_version?: string | null
  pinned_version?: string
  artifact?: string
  revision_no?: string
  rotation?: {
    from_version: string
    to_version: string
    operator: string
    reason: string
    time: string
  }
}

export type RevisionRecord = {
  revision_no: string
  status: string
  reason: string
  params: Record<string, string>
  corrected_result: string
  config_id: number
  config_no: string
  pinned_version: string
  version_produced: string | null
  original_fingerprint: string
  original_check: { provided: string; expected: string; match: boolean; checked_at: string }
  review: { 结论: string; 核对人: string; 备注: string; 时间: string } | null
  receipt: RevisionReceipt | null
  artifact: string | null
  correction: string | null
  charge_id: string | null
  effective: boolean
  superseded_by: string | null
  conflict: RevisionConflict | null
  conflicts: RevisionConflict[]
  missing?: string[]
  费用?: number
  events: { 序号: number; 事件: string; 说明: string; 操作人: string; 时间: string }[]
  submitted_at: string
  approved_at?: string
  repeated_confirm?: boolean
}

export type RevisionReceipt = {
  receipt_no: string
  revision_no: string
  查询编号: string
  参数版本: string
  产物编号: string
  更正编号: string
  执行费用: number
  原执行指纹: string
  状态: string
  签发人: string
  签发时间: string
  执行资格: { 状态: string; 参数版本: string; 允许参数: Record<string, string> }
}

export type RevisionEvent = { 序号: number; 事件: string; 说明: string; 操作人: string; 时间: string }

export type RevisionPanelState = {
  ok: boolean
  query_id: number
  query: {
    查询编号: string
    查询语句: string
    查询用户: string
    status: string
    噪声配置编号: string
    budget: string
  }
  can_revise: boolean
  reason_unavailable: string | null
  audited: boolean
  original: {
    fields: Record<string, string>
    fingerprint: string
    immutable: boolean
    unchanged: boolean
    delivered_status: string
  }
  effective_result: {
    来源: string
    修订号: string | null
    参数版本: string | null
    产物编号: string | null
    更正编号: string | null
    结果: string
    生效时间: string | null
  }
  noise_config: {
    id: number
    配置编号: string
    status: string
    current_version: string
    Epsilon值: string
    噪声机制: string
  } | null
  noise_rotations: {
    轮次: number
    from_version: string
    to_version: string
    操作人: string
    原因: string
    时间: string
  }[]
  revisions: RevisionRecord[]
  corrections: {
    correction_no: string
    revision_no: string
    参数版本: string
    产物编号: string
    变更前: string
    变更后: string
    状态: string
    superseded_by?: string
    时间: string
  }[]
  receipts: RevisionReceipt[]
  charges: {
    流水编号: string
    修订号: string
    回执编号: string
    参数版本: string
    金额: number
    状态: string
    时间: string
  }[]
  artifacts: {
    artifact_no: string
    revision_no: string
    param_key: string
    参数版本: string
    配置编号: string
    生成时间: string
  }[]
  events: RevisionEvent[]
  artifact_consistent: boolean
  totals: { revisions: number; approved: number; corrections: number; charges: number }
}

export type RevisionSubmitValues = {
  原执行核对值: string
  修订原因: string
  更正结果: string
  noise_config_id: number
  noise_version: string
  消耗Epsilon: string
  噪声注入量: string
  返回行数: string
  执行时间: string
}

export type RevisionActionResult = {
  ok: boolean
  message?: string
  duplicate?: boolean
  charged?: boolean
  conflict?: RevisionConflict
}
