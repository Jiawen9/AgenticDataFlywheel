import type { DatasetFolderUpload, DatasetInternalUpload, DatasetUploadCapabilities } from '@/types'

export function uploadDestinationPrefix(previous: DatasetInternalUpload | undefined, capabilities: DatasetUploadCapabilities): string {
  // A failed transfer resumes its frozen destination, independent of the latest default.
  return previous?.folder_upload && previous.destination
    ? previous.destination.prefix
    : capabilities.internal.default_prefix || ''
}

export function uploadRetryJobId(previous: DatasetInternalUpload | undefined, prefix: string): string | undefined {
  if (!previous?.folder_upload || !previous.destination || !['failed', 'interrupted'].includes(previous.status)) return undefined
  const normalize = (value: string) => value.trim().replace(/\\/g, '/').split('/').filter(Boolean).join('/')
  return normalize(prefix) === normalize(previous.destination.prefix) ? previous.job_id : undefined
}

export function directoryProgress(upload: DatasetFolderUpload): number {
  return upload.total_items && upload.total_items > 0
    ? Math.max(0, Math.min(100, Math.floor(100 * (upload.completed_items || 0) / upload.total_items))) : 0
}

export function byteProgress(upload: DatasetFolderUpload): number | null {
  const done = upload.progress?.transferred_bytes
  const total = upload.progress?.total_bytes
  // A naked percent may refer to one file, a speed sample, or output we don't understand.
  if (typeof done !== 'number' || typeof total !== 'number' || !Number.isFinite(done) || !Number.isFinite(total)
    || total <= 0 || done < 0 || done > total) return null
  return Math.floor(100 * done / total)
}
