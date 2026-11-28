<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <div>{{ row.status }}</div>
            <div v-if="row['入库凭据号']" class="sub-text" data-testid="credential-no">
              入库凭据：{{ row['入库凭据号'] }}
            </div>
            <div v-if="row['库存资格回执号']" class="sub-text" data-testid="receipt-no">
              库存资格回执：{{ row['库存资格回执号'] }}
            </div>
            <div v-if="row['补办提示']" class="remedy-text" data-testid="remedy-hint">
              {{ row['补办提示'] }}
            </div>
          </td>
          <td class="row-actions">
            <button v-for="action in meta.actions" :key="action" class="link" type="button"
              :data-testid="`action-${row.id}-${action}`" @click="runAction(action, row)">{{ action }}</button>
            <button v-if="meta.key === 'analysis_request'" class="link" type="button"
              data-testid="recheck-receipt" @click="openReceipt(row)">重查回执</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">暂无{{ meta.name }}数据，可先登记{{ meta.entity }}</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="receiptVisible" class="modal-backdrop" data-testid="receipt-modal">
      <div class="modal">
        <h3>库存资格回执 · 申请单 {{ receipt?.request_id }}</h3>
        <p v-if="!receipt || !receipt.latest" class="sub-text">该申请尚未发起过批准，暂无审批答复。</p>
        <template v-else>
          <p>
            样品 {{ receipt.sample_no }}（{{ receipt.cruise_no }}）最近一次答复：
            <strong :class="receipt.latest.qualified ? 'ok-text' : 'remedy-text'">
              {{ receipt.latest.qualified ? '已取得可用于取样的库存资格' : '未取得库存资格' }}
            </strong>
          </p>
          <p class="sub-text">{{ receipt.latest.message }}</p>
          <p v-if="receipt.latest.remedy_hint" class="remedy-text">补办指引：{{ receipt.latest.remedy_hint }}</p>
          <p v-if="receipt.pickup_link" class="sub-text">
            取样关联：凭据 {{ receipt.pickup_link.credential_no }} · 登记记录 #{{ receipt.pickup_link.registry_id }}
          </p>
          <h4>历次答复</h4>
          <ul class="receipt-history">
            <li v-for="item in receipt.history" :key="item.id">
              {{ item.replied_at }} — {{ item.qualified ? '通过' : '未通过' }}（{{ item.reason_code }}）
            </li>
          </ul>
        </template>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="receiptVisible = false">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  createEntry,
  downloadEntries,
  fetchApprovalReceipt,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { ApprovalReceipt, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const receiptVisible = ref(false)
const receipt = ref<ApprovalReceipt | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((r) => r.status === meta.statuses[1]).length },
  { label: meta.metrics[2], value: rows.value.filter((r) => r.status === meta.statuses[meta.statuses.length - 1]).length },
])

const statusSummary = computed(() =>
  meta.statuses.map((s) => ({ status: s, count: rows.value.filter((r) => String(r.status) === s).length })),
)

function resetFilters() { filters.value = {}; void reload() }

async function exportRows() {
  errorMessage.value = ''
  try { await downloadEntries(meta.key, filters.value) }
  catch (e) { errorMessage.value = e instanceof Error ? e.message : '导出失败' }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((f) => [f, '']))
  errorMessage.value = ''; showCreate.value = true
}

async function submitCreate() {
  creating.value = true; errorMessage.value = ''
  try {
    const r = await createEntry(meta.key, createValues.value)
    if (!r.ok) throw new Error(r.message)
    showCreate.value = false; await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '登记失败' }
  finally { creating.value = false }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const r = await applyAction(meta.key, Number(row.id), action)
    if (!r.ok) throw new Error(r.message)
    await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '操作失败' }
}

async function openReceipt(row: EntryRow) {
  errorMessage.value = ''
  receipt.value = null
  receiptVisible.value = true
  try {
    receipt.value = await fetchApprovalReceipt(Number(row.id))
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '回执读取失败' }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items; total.value = payload.total
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '业务列表读取失败' }
}

onMounted(reload)
</script>

<style scoped>
.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.35); display:flex; align-items:center; justify-content:center; z-index:100; }
.modal { background:#fff; border-radius:8px; padding:24px; width:480px; max-height:80vh; overflow-y:auto; }
.create-field { display:flex; flex-direction:column; gap:4px; margin-bottom:12px; }
.create-field span { font-size:13px; color:var(--text-secondary,#4a5568); }
.create-field input { padding:6px 8px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; font-size:13px; }
.sub-text { font-size:12px; color:var(--text-secondary,#4a5568); margin-top:2px; }
.remedy-text { font-size:12px; color:#c05621; margin-top:2px; }
.ok-text { color:#2f855a; }
.receipt-history { padding-left:18px; font-size:13px; line-height:1.8; }
</style>