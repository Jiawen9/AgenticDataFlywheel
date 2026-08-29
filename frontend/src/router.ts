import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/task-generation' },
    { path: '/task-generation', component: () => import('@/views/TaskGenerationView.vue') },
    { path: '/collection', component: () => import('@/views/CollectionView.vue') },
    { path: '/quality', component: () => import('@/views/QualityView.vue') },
  ],
})
