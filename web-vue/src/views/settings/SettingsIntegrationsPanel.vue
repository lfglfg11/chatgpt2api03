<template>
  <div class="space-y-4">
    <FormSection
      v-if="mode === 'canvas'"
      title="画布入口"
      subtitle="开启后顶部导航会显示无限画布入口，并自动带上当前接口地址和密钥。"
    >
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings.third_party_apps.infinite_canvas.enabled">
              启用无限画布入口
            </Checkbox>
          </div>
        </div>
      </div>
      <FormField label="无限画布地址">
        <Input
          v-model.trim="settings.third_party_apps.infinite_canvas.url"
          block
          placeholder="https://canvas.best"
        />
      </FormField>
    </FormSection>

    <template v-else>
      <FormSection title="接口接入" subtitle="第三方应用按 OpenAI 兼容接口接入，使用同一套 Bearer 鉴权。">
        <div class="grid gap-3 md:grid-cols-2">
          <SurfaceBox
            v-for="item in accessItems"
            :key="item.label"
            density="compact"
          >
            <p class="text-xs text-muted-foreground">{{ item.label }}</p>
            <p class="mt-1 break-all font-mono text-xs text-foreground">{{ item.value }}</p>
          </SurfaceBox>
        </div>
      </FormSection>

      <FormSection
        title="图片输出格式"
        subtitle="控制 /v1/images、/v1/chat/completions、/v1/responses 等接口在未指定 response_format 时的默认图片返回方式。"
      >
        <div class="settings-check-grid settings-check-grid--single">
          <div class="settings-check-item">
            <div class="settings-check-control">
              <Checkbox
                :model-value="urlOutputEnabled"
                @update:model-value="setUrlOutputEnabled"
              >
                启用图片 URL 输出（默认 base64）
              </Checkbox>
            </div>
          </div>
        </div>

        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <FormField label="图片访问域名" class="md:col-span-2">
            <template #label-extra>
              <HelpTip text="启用 URL 输出时必填。必须是公网可访问的 http(s) 域名，禁止填写裸 IP 或 localhost。例如 https://api.example.com" />
            </template>
            <Input
              v-model.trim="settings.base_url"
              block
              root-class="font-mono"
              placeholder="https://api.example.com"
              :disabled="!urlOutputEnabled"
            />
            <p class="mt-2 text-xs leading-5 text-muted-foreground">
              URL 模式下图片结果会返回链接而不是 base64。请求里仍可显式传 response_format 覆盖默认值。
            </p>
            <p v-if="urlOutputHint" class="mt-1 text-xs leading-5 text-amber-600">
              {{ urlOutputHint }}
            </p>
            <p class="mt-1 font-mono text-xs text-muted-foreground">
              示例：{{ exampleImageUrl }}
            </p>
          </FormField>
        </div>
      </FormSection>

      <FormSection title="常用接口">
        <div class="space-y-2">
          <details
            v-for="item in apiDocItems"
            :key="item.path"
            class="rounded-xl border border-border bg-card px-4 py-3"
          >
            <summary class="flex cursor-pointer list-none items-center justify-between gap-3">
              <span class="min-w-0">
                <span class="block text-sm font-medium text-foreground">{{ item.title }}</span>
                <span class="mt-1 block truncate font-mono text-xs text-muted-foreground">{{ item.method }} {{ item.path }}</span>
              </span>
              <span class="text-xs text-muted-foreground">展开</span>
            </summary>
            <div class="mt-3 space-y-2">
              <p class="text-xs leading-5 text-muted-foreground">{{ item.description }}</p>
              <pre class="overflow-auto whitespace-pre-wrap break-all rounded-xl bg-zinc-950 px-3 py-3 text-xs leading-5 text-zinc-100">{{ item.example }}</pre>
            </div>
          </details>
        </div>
      </FormSection>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Checkbox, FormField, FormSection, HelpTip, Input } from 'nanocat-ui'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import { getAuthToken } from '@/api/client'
import type { Settings } from '@/types/api'
import { buildApiDocItems } from '@/views/settings/settingsView'

const props = defineProps<{
  mode: 'api-docs' | 'canvas'
  settings: Settings
}>()

const serviceBaseUrl = computed(() => window.location.origin)
const openAIBaseUrl = computed(() => `${serviceBaseUrl.value.replace(/\/$/, '')}/v1`)
const currentApiKey = computed(() => getAuthToken() || '<当前密钥>')
const accessItems = computed(() => [
  { label: '服务地址', value: serviceBaseUrl.value },
  { label: 'Base URL（OpenAI）', value: openAIBaseUrl.value },
  { label: 'API Key', value: currentApiKey.value },
  { label: '请求头', value: `Authorization: Bearer ${currentApiKey.value}` },
])
const apiDocItems = computed(() => (
  props.mode === 'api-docs'
    ? buildApiDocItems(serviceBaseUrl.value, currentApiKey.value)
    : []
))

const urlOutputEnabled = computed(() => props.settings.image_generation?.output_format === 'url')

function normalizeDomainInput(value: string): string {
  const text = String(value || '').trim()
  if (!text) return ''
  return text.includes('://') ? text.replace(/\/$/, '') : `https://${text}`.replace(/\/$/, '')
}

function isPublicDomainBaseUrl(value: string): boolean {
  const text = normalizeDomainInput(value)
  if (!text) return false
  try {
    const url = new URL(text)
    if (!['http:', 'https:'].includes(url.protocol)) return false
    const host = url.hostname.trim().toLowerCase()
    if (!host || host === 'localhost') return false
    if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(host)) return false
    if (host.includes(':')) return false
    return true
  } catch {
    return false
  }
}

const exampleImageUrl = computed(() => {
  const base = normalizeDomainInput(props.settings.base_url || '') || 'https://api.example.com'
  return `${base}/images/2026/07/21/example.png`
})

const urlOutputHint = computed(() => {
  if (!urlOutputEnabled.value) return ''
  const base = String(props.settings.base_url || '').trim()
  if (!base) return '已启用 URL 输出：请填写图片访问域名后再保存。'
  if (!isPublicDomainBaseUrl(base)) return '域名无效：请使用 http(s) 域名，禁止 IP 或 localhost。'
  return ''
})

function setUrlOutputEnabled(value: boolean | 'indeterminate') {
  const enabled = value === true
  if (!props.settings.image_generation) {
    props.settings.image_generation = {
      enabled: true,
      supported_models: [],
      model_options: [],
      block_rich_output_on_base_chat_models: true,
      output_format: enabled ? 'url' : 'base64',
      nanobanana_lane: 'fast',
      nanobanana_lane_order: ['fast'],
    }
    return
  }
  props.settings.image_generation.output_format = enabled ? 'url' : 'base64'
}
</script>

<style scoped>
.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
}

.settings-check-grid--single {
  grid-template-columns: minmax(0, 1fr);
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 38px;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
}

.settings-check-item :deep(label) {
  display: flex;
  width: 100%;
  flex: 1;
  min-height: 38px;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
}

.settings-check-item :deep(label > span:last-child) {
  color: hsl(var(--foreground) / 0.78);
  line-height: 1.35;
}
</style>
