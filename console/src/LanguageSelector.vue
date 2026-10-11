<script setup lang="ts">
import { locale, setLocale, t } from './locale.ts'
</script>
<template>
  <label class="language-selector"><span>{{ t('Language') }}</span><select :value="locale" :aria-label="t('Language')" @change="setLocale(($event.target as HTMLSelectElement).value)"><option value="zh-CN" lang="zh-CN">中文</option><option value="en" lang="en">English</option></select></label>
</template>
