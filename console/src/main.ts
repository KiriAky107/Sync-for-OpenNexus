import { createApp } from 'vue'
import App from './App.vue'
import { initializeLocale } from './locale.ts'
import './style.css'

initializeLocale()
createApp(App).mount('#app')
