import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import DatasetFolderUploadProgress from './DatasetFolderUploadProgress.vue'
import { uploadDestinationPrefix, byteProgress } from '@/utils/datasetFolderUpload'
import type { DatasetFolderUpload, DatasetInternalUpload } from '@/types'

const base: DatasetFolderUpload = { folder_upload: true, total_items: 3, completed_items: 1,
  destination: { bucket: 'test-bucket', region: 'test-region', prefix: 'data/批次/' },
  source_summary: { case_count: 1, trajectory_count: 2, excel_count: 1 } }
const render = (upload: DatasetFolderUpload) => renderToString(createSSRApp(DatasetFolderUploadProgress, { upload }))

describe('folder upload progress', () => {
  it('labels directory progress and does not invent byte progress from speed alone', async () => {
    const html = await render({ ...base, progress: { speed_text: '2.4 MiB/s', message: '上传中', percent: 99 } })
    expect(html).toContain('目录进度 33%')
    expect(html).toContain('2 条完整轨迹')
    expect(html).toContain('2.4 MiB/s')
    expect(html).not.toContain('当前脚本字节进度')
    expect(html).not.toContain('99%')
  })
  it('shows real byte counts separately and escapes script output', async () => {
    const html = await render({ ...base, progress: { transferred_bytes: 1024, total_bytes: 2048, message: '<script>oops</script>' } })
    expect(html).toContain('当前脚本字节进度：1.0 KB / 2.0 KB（50%）')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>oops')
    expect(byteProgress({ progress: { transferred_bytes: 3, total_bytes: 2 } })).toBeNull()
    expect(byteProgress({ progress: { transferred_bytes: 0, total_bytes: 0 } })).toBeNull()
  })
  it('keeps thousands of receipts collapsed and limits recent output to twenty lines', async () => {
    const html = await render({ ...base, item_results: Array.from({ length: 5440 }, (_, i) => ({
      idempotency_key: String(i), kind: 'trajectory', filename: `trajectory-${i}`, source_dir: '', bucket_path: '', status: 'pending',
    })), recent_output: Array.from({ length: 30 }, (_, i) => `log-${i}-end`) })
    expect(html).toContain('查看目录上传结果（5440）')
    expect(html).not.toContain('trajectory-0')
    expect(html).not.toContain('log-0-end')
    expect(html).toContain('log-29-end')
  })
  it('prefills the old target for retry even when defaults changed; legacy jobs use the default', () => {
    const cap = { internal: { configured: true, reason: null, default_prefix: 'new/' } }
    expect(uploadDestinationPrefix({ ...base, status: 'failed' } as DatasetInternalUpload, cap)).toBe('data/批次/')
    expect(uploadDestinationPrefix({ status: 'failed' } as DatasetInternalUpload, cap)).toBe('new/')
    expect(uploadDestinationPrefix(undefined, cap)).toBe('new/')
  })
  it('shows independent training counts and actual download links after a transport failure', async () => {
    const html = await render({ ...base, job_id: 'j261117-001', training_jsonl: true, training: {
      status: 'failed', included_count: 9, excluded_count: 1, bbox_fallback_count: 3,
      key_step_count: 2, refined_count: 1, file_path: 'frozen/train.jsonl', report_path: 'frozen/report.json', error: '训练文件上传失败',
    } })
    expect(html).toContain('有效样本 9 · 排除 1 · 动作 JSON 兜底 3')
    expect(html).toContain('/api/dataset-upload-jobs/j261117-001?download=jsonl')
    expect(html).toContain('/api/dataset-upload-jobs/j261117-001?download=report')
    expect(html).toContain('训练文件上传失败')
    expect(html).toContain('已完成目录：1/3')
  })
})
