import { createRouter, createWebHistory } from 'vue-router'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/collection' },
    { path: '/collection', component: () => import('@/views/CollectionView.vue') },
    { path: '/quality', component: () => import('@/views/QualityView.vue') },
  ],
})
