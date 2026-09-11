// Match only messages emitted by older system versions. Remote errors and
// identifiers may contain the former name and must otherwise remain verbatim.
const legacySystemMessages: Record<string, string> = {
  '内部网站上传尚未配置': '云道S3上传尚未配置',
  '内部网站上传配置检查失败，请联系维护人员': '云道S3上传配置检查失败，请联系维护人员',
  '内部上传记录与数据集不匹配': '云道S3上传记录与数据集不匹配',
  '内部网站上传异常，请联系维护人员后重试': '云道S3上传异常，请联系维护人员后重试',
  '服务重启导致内部网站上传中断，可重试剩余文件': '服务重启导致云道S3上传中断，可重试剩余文件',
}

export function datasetUploadMessage(message: string | null | undefined): string {
  if (!message) return ''
  return Object.prototype.hasOwnProperty.call(legacySystemMessages, message) ? legacySystemMessages[message]! : message
}
