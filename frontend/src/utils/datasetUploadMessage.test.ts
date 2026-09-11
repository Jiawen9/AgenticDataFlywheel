import { describe, expect, it } from 'vitest'
import { datasetUploadMessage } from './datasetUploadMessage'

describe('dataset upload display messages', () => {
  it.each([
    ['内部网站上传尚未配置', '云道S3上传尚未配置'],
    ['内部网站上传配置检查失败，请联系维护人员', '云道S3上传配置检查失败，请联系维护人员'],
    ['内部上传记录与数据集不匹配', '云道S3上传记录与数据集不匹配'],
    ['内部网站上传异常，请联系维护人员后重试', '云道S3上传异常，请联系维护人员后重试'],
    ['服务重启导致内部网站上传中断，可重试剩余文件', '服务重启导致云道S3上传中断，可重试剩余文件'],
  ])('displays a known historical system message using the current name', (stored, displayed) => {
    const record = { error: stored }
    expect(datasetUploadMessage(record.error)).toBe(displayed)
    expect(record.error).toBe(stored)
  })

  it.each([
    '内部网站拒绝接收：请调整表格字段',
    '内部网站上传尚未配置.xlsx：文件不存在',
    '远端原文：内部网站上传异常，请联系维护人员后重试',
    '云道S3上传尚未配置',
    'constructor',
  ])('leaves arbitrary remote or file-specific messages unchanged', message => {
    expect(datasetUploadMessage(message)).toBe(message)
  })

  it('supports absent optional errors', () => {
    expect(datasetUploadMessage(null)).toBe('')
    expect(datasetUploadMessage(undefined)).toBe('')
  })
})
