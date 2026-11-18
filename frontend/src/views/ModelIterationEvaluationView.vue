<template><PhoneFactoryCollectionView :key="String(route.query.evaluation_pipeline_id || '')" mode="modeliter" /></template>
<script setup lang="ts">
import { useRoute } from 'vue-router'
import PhoneFactoryCollectionView from './PhoneFactoryCollectionView.vue'
const route = useRoute()
</script>
