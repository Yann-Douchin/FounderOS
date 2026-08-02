<template>
  <div class="panel">
    <div class="card glass">
      <h2 class="card-title">
        <span class="badge" v-html="icons.list"></span>Obligation ledger
        <button class="pill" style="margin-left:auto;font-size:11px;padding:2px 10px" @click="load">Refresh</button>
      </h2>
      <div v-if="snapshot.available" class="obligation-metrics">
        <div v-for="metric in metrics" :key="metric.label" class="obligation-metric">
          <strong>{{ metric.value }}</strong><span>{{ metric.label }}</span>
        </div>
      </div>
      <div v-if="snapshot.available" class="app-search">
        <span class="app-search-ic" v-html="icons.search"></span>
        <input v-model="query" type="text" placeholder="Filter by outcome, owner, project or source" aria-label="Filter obligations" />
      </div>
      <div v-if="snapshot.available && snapshot.stale" class="muted-note obligation-stale">
        The last governed snapshot is stale. Live ranking may be unavailable, so this view must not be treated as an all-clear.
      </div>
      <div v-if="loading" class="muted-note">Loading the governed ledger...</div>
      <div v-else-if="!snapshot.available" class="muted-note">
        The closure engine has not published a snapshot yet. {{ snapshot.error || '' }}
      </div>
      <div v-else class="obligations-list">
        <article v-for="item in filtered" :key="item.id" class="obligation-row">
          <div class="obligation-head">
            <span class="obligation-state" :class="item.state">{{ item.state }}</span>
            <span class="obligation-priority">P{{ item.priority }}</span>
          </div>
          <h3>{{ item.title }}</h3>
          <div class="obligation-route">
            <span>{{ item.owner || 'Unassigned' }}</span>
            <span class="obligation-arrow">→</span>
            <span>{{ item.next_actor || 'No next actor' }}</span>
          </div>
          <div class="obligation-meta">
            <span v-if="item.project">{{ item.project }}</span>
            <span v-if="item.due_at">Due {{ formatDate(item.due_at) }}</span>
            <span>{{ (item.sources || []).join(', ') || 'operator' }}</span>
          </div>
          <div v-if="missingGates(item).length" class="obligation-gates">
            <span v-for="gate in missingGates(item)" :key="gate.name" class="obligation-gate" :class="gate.state">
              {{ gate.name }}: {{ gate.state }}
            </span>
          </div>
        </article>
        <div v-if="!filtered.length" class="muted-note">No obligations match this filter.</div>
      </div>
      <div v-if="snapshot.generated_at" class="obligation-updated">Updated {{ formatDate(snapshot.generated_at) }}</div>
    </div>
    <div v-if="snapshot.available && filteredRelationships.length" class="card glass">
      <h2 class="card-title"><span class="badge" v-html="icons.list"></span>Relationship memory</h2>
      <div class="obligations-list">
        <article v-for="item in filteredRelationships" :key="item.key" class="obligation-row relationship-row">
          <div class="obligation-head">
            <span class="obligation-state open">{{ item.stage || 'active' }}</span>
            <span class="obligation-priority">{{ (item.open_obligation_ids || []).length }} open</span>
          </div>
          <h3>{{ item.name }}</h3>
          <div v-if="item.next_decision" class="relationship-decision">Next decision: {{ item.next_decision }}</div>
          <div class="obligation-meta">
            <span v-if="item.last_interaction_at">Last interaction {{ formatDate(item.last_interaction_at) }}</span>
            <span v-if="item.resume_after">Resume {{ formatDate(item.resume_after) }}</span>
            <span v-if="item.cooling_off_until">Cooling until {{ formatDate(item.cooling_off_until) }}</span>
          </div>
        </article>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiGet } from '../../composables/useDevice'
import { icons } from '../../icons'

const snapshot = ref({ available: false, obligations: [], relationships: [], error: '' })
const loading = ref(true)
const query = ref('')
let timer = null

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase()
  const items = [...(snapshot.value.obligations || [])].sort((a, b) => (b.priority || 0) - (a.priority || 0))
  if (!needle) return items
  return items.filter((item) => [
    item.title, item.owner, item.next_actor, item.project, item.counterparty,
    ...(item.sources || []), ...(item.gates || []).map((gate) => `${gate.name} ${gate.state}`),
  ].join(' ').toLowerCase().includes(needle))
})

const metrics = computed(() => {
  const items = snapshot.value.obligations || []
  const count = (states) => items.filter((item) => states.includes(item.state)).length
  return [
    { label: 'active', value: items.length },
    { label: 'blocked', value: count(['blocked']) },
    { label: 'waiting', value: count(['waiting', 'deferred']) },
    { label: 'ready to close', value: count(['ready']) },
  ]
})

const filteredRelationships = computed(() => {
  const needle = query.value.trim().toLowerCase()
  const items = [...(snapshot.value.relationships || [])].sort((a, b) =>
    String(b.last_interaction_at || '').localeCompare(String(a.last_interaction_at || ''))
  )
  if (!needle) return items
  return items.filter((item) => [
    item.name, item.key, item.stage, item.next_decision,
  ].join(' ').toLowerCase().includes(needle))
})

function missingGates(item) {
  return (item.gates || []).filter((gate) => gate.required !== false && !['satisfied', 'waived'].includes(gate.state))
}

function formatDate(value) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

async function load() {
  const value = await apiGet('/api/_founderos/obligations')
  snapshot.value = value && typeof value === 'object'
    ? value
    : { available: false, obligations: [], relationships: [], error: 'snapshot unavailable' }
  loading.value = false
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 5000)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>
