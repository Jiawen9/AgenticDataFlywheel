import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePipelineContext } from './usePipelineContext'
import { loopRequestId } from '@/pipelineLoopApi'
export function useGenerationPipeline() {
  const route = useRoute(), router = useRouter(), context = usePipelineContext({ stepId: 'generation', lookupByBatch: false })
  const id = computed(() => typeof route.query.pipeline_id === 'string' ? route.query.pipeline_id : '')
  const boundJob = computed(() => context.pipeline.value?.generation_job_id || '')
  const feedbackId = computed(() => context.pipeline.value?.feedback_id || (typeof route.query.feedback_id === 'string' ? route.query.feedback_id : ''))
  const mayGenerate = computed(() => !id.value || context.pipeline.value?.status === 'waiting_for_generation_config')
  function submission(identity: unknown) {
    if (!id.value) return {}
    return { pipeline_id: id.value, request_id: loopRequestId('generation.' + id.value, [context.pipeline.value?.generation_attempt || 0, identity]) }
  }
  function back(review = false) { return router.push({ path: '/pipeline', query: { pipeline_id: id.value, ...(review ? { review_generation: '1' } : {}) } }) }
  return { ...context, id, boundJob, feedbackId, mayGenerate, submission, back }
}
