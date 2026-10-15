'use strict'
/* ── Config ──────────────────────────────────────────────────────────────────*/
const API = `http://127.0.0.1:${window.APP.port}`

/* ── App state ───────────────────────────────────────────────────────────────*/
const state = {
  deployment: null,
  page:       'dashboard',
  stageInfo:  {},
  stages:     [],
  depStates:  {},          // cache: dep → pipeline_state.json contents
}
const activeRuns  = {}     // stageKey → AbortController
const stageLogs   = {}     // stageKey → string[]  (persists across re-renders)
const stageEls    = {}     // stageKey → { log, badge, btnRun, btnAbort }  (live DOM refs)
let   nicknames   = {}     // dep → short name; loaded in init, shared across functions

// Torn down at the top of navigate() regardless of which page is being left/
// entered, since this app has no per-page "on leave" lifecycle hook -- these
// are the only two things that currently outlive the page that started them.
let _activeCloudPoll  = null   // cleanup fn returned by pollUntilConnected(), or null
let _activeDockerWait = null   // open EventSource from _checkDockerBanner(), or null

/* ── API helpers ─────────────────────────────────────────────────────────────*/
async function apiFetch (url, opts = {}) {
  const r = await fetch(API + url, opts)
  if (!r.ok) {
    let detail = ''
    try { const j = await r.json(); detail = j.error || '' } catch (_) {}
    throw new Error(`${r.status} ${r.statusText}${detail ? ': ' + detail : ''} — ${url}`)
  }
  return r.json()
}

async function loadAppState () {
  const d = await apiFetch('/api/state')
  state.stageInfo = d.stage_info
  state.stages    = d.stages
  document.getElementById('base-dir-lbl').textContent = d.base_dir
  return d
}

async function loadDepState (dep) {
  const d = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/state`)
  state.depStates[dep] = d
  return d
}

async function setStageStatus (dep, stage, status) {
  await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/set-status/${stage}`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ status }),
  })
  delete state.depStates[dep]   // invalidate cache
}

/* ── Streaming stage run ─────────────────────────────────────────────────────*/
async function streamRun (dep, stage, onLog, onDone, opts = {}) {
  const controller = new AbortController()
  activeRuns[stage] = controller

  try {
    const resp = await fetch(`${API}/api/deployment/${encodeURIComponent(dep)}/run/${stage}`, {
      method:  'POST',
      headers: Object.keys(opts).length ? { 'Content-Type': 'application/json' } : {},
      body:    Object.keys(opts).length ? JSON.stringify(opts) : undefined,
      signal:  controller.signal,
    })

    const reader  = resp.body.getReader()
    const decoder = new TextDecoder()
    let   buffer  = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()                  // keep incomplete last line
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const evt = JSON.parse(line.slice(6))
          if (evt.type === 'log')            onLog(evt.line)
          else if (evt.type === 'done')  { onDone(evt.success); break }
          else if (evt.type === 'error') { onLog('ERROR: ' + evt.message); onDone(false); break }
        } catch {}
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') { onLog('Stream error: ' + e.message); onDone(false) }
    else                           onDone(false)
  } finally {
    delete activeRuns[stage]
  }
}

async function abortRun (dep, stage) {
  if (activeRuns[stage]) { activeRuns[stage].abort(); delete activeRuns[stage] }
  await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/abort/${stage}`, { method: 'POST' })
}

/* ── Utility ─────────────────────────────────────────────────────────────────*/
function h (tag, attrs = {}, ...children) {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') el.className = v
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v)
    else el.setAttribute(k, v)
  }
  for (const c of children) {
    if (c == null) continue
    el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c)
  }
  return el
}

function statusClass (s) {
  return s === 'done' ? 'done' : s === 'failed' ? 'failed' : s === 'running' ? 'running' : s === 'needs_review' ? 'needs_review' : 'not_run'
}
function statusLabel (s) {
  return s === 'done' ? '✔ DONE' : s === 'failed' ? '✖ FAILED' : s === 'running' ? 'RUNNING…' : s === 'needs_review' ? '⚠ REVIEW NEEDED' : 'NOT RUN'
}
function logColor (line) {
  const l = line.toLowerCase()
  if (/error|traceback|exception/.test(l)) return 'log-error'
  if (/warning|warn/.test(l))              return 'log-warning'
  if (/^building|^found|^saved/.test(l))  return 'log-info'
  return ''
}

/* ── New Deployment Modal ────────────────────────────────────────────────────*/
function showNewDepModal () {
  const deps    = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const gliders = [...new Set(deps.map(d => d.split('-')[0]))].filter(Boolean)

  // Free-text deployment name field — user types the full name directly.
  // Datalist suggests known glider prefixes as a starting point.
  const nameInput   = h('input', { class: 'form-input', type: 'text', id: 'nd-name',
    placeholder: gliders.length ? gliders[0] + '-' : 'e.g. uvi_01-20250318T2008',
    list: 'nd-name-hints',
    oninput: () => { _ndUpdateFolders(); _ndAutoTemplate() } })
  const nameHints   = h('datalist', { id: 'nd-name-hints' },
    ...gliders.map(g => h('option', { value: g + '-' })))
  const nameHint    = h('div', { class: 'nd-name-hint' }, 'Format: glider-YYYYMMDDThhmm')
  const nicknameInput = h('input', { class: 'form-input', type: 'text', id: 'nd-nickname',
    placeholder: 'e.g. Spring 2026 Survey  (optional)' })

  // Template dropdown — grouped, auto-selects most recent same-glider dep
  const templateSel = _buildTemplateSelect(deps, gliders[0] || '')
  {
    const g = gliders[0] || ''
    const sameGlider = deps.filter(d => d.startsWith(g + '-')).sort()
    if (sameGlider.length) templateSel.value = sameGlider[sameGlider.length - 1]
    else if (deps.length)  templateSel.value = deps[deps.length - 1]
  }

  const foldersList = h('div', { class: 'folders-list', id: 'nd-folders' }, '—')
  const statusMsg   = h('div', { class: 'modal-status', id: 'nd-status' })
  const createBtn   = h('button', { class: 'btn-primary', id: 'btn-create-dep',
    onclick: _ndDoCreate }, '+ Create Deployment')

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal' },
      h('div', { class: 'modal-header' },
        h('h2', { id: 'nd-modal-title' }, 'New Deployment'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body', id: 'nd-modal-body' },
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Deployment name'),
          nameInput, nameHints, nameHint),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Short name'),
          nicknameInput),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Copy config from'), templateSel),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Folders that will be created'), foldersList),
        statusMsg),
      h('div', { class: 'modal-footer', id: 'nd-modal-footer' },
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        createBtn)))

  document.body.appendChild(overlay)
  nameInput.focus()
}

function _buildTemplateSelect (deps, currentGlider) {
  // Group into same-glider and other
  const same  = deps.filter(d => currentGlider && d.startsWith(currentGlider + '-')).sort()
  const other = deps.filter(d => !currentGlider || !d.startsWith(currentGlider + '-')).sort()

  const sel = h('select', { class: 'form-input', id: 'nd-template' })
  sel.appendChild(h('option', { value: '' }, '— None (empty config) —'))

  if (same.length) {
    const grp = document.createElement('optgroup')
    grp.label = `Same glider${currentGlider ? ' — ' + currentGlider : ''}`
    same.forEach(d => grp.appendChild(h('option', { value: d }, d)))
    sel.appendChild(grp)
  }
  if (other.length) {
    const grp = document.createElement('optgroup')
    grp.label = 'Other deployments'
    other.forEach(d => grp.appendChild(h('option', { value: d }, d)))
    sel.appendChild(grp)
  }
  return sel
}

function _ndAutoTemplate () {
  // Extract glider from whatever the user has typed so far
  const name   = _ndDepName()
  const glider = name.includes('-') ? name.split('-')[0] : name
  const sel    = document.getElementById('nd-template')
  if (!sel) return

  const deps   = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const newSel = _buildTemplateSelect(deps, glider)
  sel.innerHTML = newSel.innerHTML

  const sameGlider = deps.filter(d => glider && d.startsWith(glider + '-')).sort()
  if (sameGlider.length) sel.value = sameGlider[sameGlider.length - 1]
  else if (deps.length)  sel.value = deps[deps.length - 1]
}

function _ndDepName () {
  return (document.getElementById('nd-name')?.value || '').trim()
}

function _ndUpdateFolders () {
  const name    = _ndDepName()
  const folders = document.getElementById('nd-folders')
  if (!folders) return
  if (!name) { folders.textContent = '—'; return }
  const glider = name.split('-')[0]
  folders.textContent = [
    `incoming_data/${name}/`,
    `dat_files/${name}/merged_data/`,
    `dat_files/${name}/flight_data/`,
    `glider_config_files_json/${glider}/${name}/science_profile/`,
    `glider_config_files_json/${glider}/${name}/science_trajectory/`,
    `glider_config_files_json/${glider}/${name}/trajectory_flight/`,
  ].join('\n')
}

function closeModal () {
  const el = document.getElementById('modal-overlay')
  if (el) el.remove()
}

async function _ndDoCreate () {
  const name     = _ndDepName()
  const template = document.getElementById('nd-template').value
  const nick     = (document.getElementById('nd-nickname')?.value || '').trim()
  const status   = document.getElementById('nd-status')
  const btn      = document.getElementById('btn-create-dep')
  if (!name) { status.textContent = 'Please fill in all fields.'; return }

  btn.disabled = true; btn.textContent = 'Creating…'; status.textContent = ''
  try {
    await apiFetch('/api/deployment/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, template }),
    })

    // Save nickname if provided
    if (nick) {
      await apiFetch(`/api/deployment/${encodeURIComponent(name)}/nickname`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nickname: nick }),
      })
      nicknames[name] = nick
    }

    // Add to deployment selector (use nickname as label if set)
    const sel = document.getElementById('dep-select')
    sel.appendChild(h('option', { value: name }, name))
    sel.value = name

    // Transition to file import step (step 2)
    _ndShowImportStep(name)
  } catch (e) {
    status.textContent = e.message
    btn.disabled = false; btn.textContent = '+ Create Deployment'
  }
}

/* ── New Deployment step 2: import raw files ─────────────────────────────────*/
function _ndShowImportStep (dep) {
  const body   = document.getElementById('nd-modal-body')
  const footer = document.getElementById('nd-modal-footer')
  const title  = document.getElementById('nd-modal-title')
  if (title) title.textContent = 'Import Raw Files'

  const ALLOWED = new Set(['.dbd', '.ebd', '.sbd', '.tbd', '.mbd', '.nbd'])
  const fileItems = []   // {name, path, size, status, msg?}

  const errorEl    = h('div', { class: 'import-error' })
  const countEl    = h('div', { class: 'import-count', id: 'import-count' })
  const fileListEl = h('div', { class: 'import-file-list' })

  function renderList () {
    fileListEl.innerHTML = ''
    fileItems.forEach(item => {
      const icon = { queued: '…', copying: '…', done: '✓', error: '✗' }[item.status] || '?'
      fileListEl.appendChild(h('div', { class: 'import-file-item' },
        h('span', { class: `import-status import-status-${item.status}` }, icon),
        h('span', { class: 'import-fname' }, item.name),
        h('span', { class: 'import-fsize' }, _fmtBytes(item.size)),
        item.msg ? h('span', { class: 'import-item-msg' }, item.msg) : null))
    })
    const done  = fileItems.filter(i => i.status === 'done').length
    const errs  = fileItems.filter(i => i.status === 'error').length
    const total = fileItems.length
    countEl.textContent = total
      ? `${done} / ${total} copied${errs ? ' · ' + errs + ' error(s)' : ''}`
      : ''
  }

  async function doImport (newItems) {
    const paths = newItems.map(i => i.path).filter(Boolean)
    if (!paths.length) {
      // Browser preview — no real file paths
      newItems.forEach(i => { i.status = 'error'; i.msg = 'Open in Electron app to copy files' })
      renderList(); return
    }
    newItems.forEach(i => { i.status = 'copying' })
    renderList()
    try {
      const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/import-raw`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      })
      r.results.forEach(res => {
        const item = fileItems.find(i => i.path === res.path)
        if (item) { item.status = res.ok ? 'done' : 'error'; if (!res.ok) item.msg = res.error }
      })
    } catch (e) {
      newItems.forEach(i => { i.status = 'error'; i.msg = e.message })
    }
    renderList()
  }

  function handleFiles (files) {
    errorEl.textContent = ''
    const valid   = files.filter(f => ALLOWED.has('.' + f.name.split('.').pop().toLowerCase()))
    const skipped = files.length - valid.length
    if (skipped) errorEl.textContent = `${skipped} file(s) skipped — only ${[...ALLOWED].join(' / ')} accepted`
    if (!valid.length) return

    const newItems = []
    valid.forEach(f => {
      if (!fileItems.find(i => i.name === f.name)) {
        const item = { name: f.name, path: f.path || '', size: f.size, status: 'queued' }
        fileItems.push(item)
        newItems.push(item)
      }
    })
    if (!newItems.length) { errorEl.textContent = 'All dropped files are already in the list.'; return }
    renderList()
    doImport(newItems)
  }

  // Build drop zone
  const fileInput = h('input', { type: 'file', multiple: true, style: 'display:none',
    onchange: e => { handleFiles(Array.from(e.target.files)); e.target.value = '' }})
  const dropzone  = h('div', { class: 'import-dropzone' },
    h('div', { class: 'import-drop-icon' }, '↑'),
    h('div', { class: 'import-drop-text' }, 'Drop files here'),
    h('div', { class: 'import-drop-ext' }, '.dbd  .ebd  .sbd  .tbd  .mbd  .nbd'),
    h('div', { class: 'import-drop-or' }, 'or'),
    h('label', { class: 'btn-secondary import-browse-btn' }, 'Browse…', fileInput))

  dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over') })
  dropzone.addEventListener('dragleave', e => { if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('drag-over') })
  dropzone.addEventListener('drop', e => {
    e.preventDefault(); dropzone.classList.remove('drag-over')
    handleFiles(Array.from(e.dataTransfer.files))
  })

  // Build body
  body.innerHTML = ''
  body.appendChild(h('div', { class: 'nd-created-badge' },
    h('span', { class: 'nd-check' }, '✓'),
    ` ${dep} created`))
  body.appendChild(h('div', { class: 'import-dest-label' },
    'Copying to: ', h('code', {}, `incoming_data/${dep}/`)))
  body.appendChild(dropzone)
  body.appendChild(errorEl)
  body.appendChild(countEl)
  body.appendChild(fileListEl)

  // Build footer
  footer.innerHTML = ''
  footer.appendChild(h('button', { class: 'btn-secondary', onclick: () => {
    closeModal(); state.deployment = dep; navigate('pipeline')
  }}, 'Skip → Pipeline'))
  footer.appendChild(h('button', { class: 'btn-success', onclick: () => {
    closeModal(); state.deployment = dep; navigate('pipeline')
  }}, 'Done → Pipeline'))
}

function _fmtBytes (b) {
  if (!b) return ''
  if (b < 1024)        return b + ' B'
  if (b < 1048576)     return (b / 1024).toFixed(1) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}

/* ── Dashboard page ──────────────────────────────────────────────────────────*/
async function renderDashboard () {
  const page = document.getElementById('page-dashboard')
  page.innerHTML = ''

  let dashSort = localStorage.getItem('dashboardSort') || 'time'

  const sortBtn = h('button', { class: 'btn-secondary dash-sort-btn',
    onclick: () => {
      dashSort = dashSort === 'time' ? 'alpha' : 'time'
      localStorage.setItem('dashboardSort', dashSort)
      renderDashboard()
    }
  })
  const updateSortBtn = () => {
    sortBtn.textContent = dashSort === 'time' ? '⏱ Newest first' : 'A–Z'
    sortBtn.title       = dashSort === 'time' ? 'Currently: newest first — click for A–Z' : 'Currently: A–Z — click for newest first'
  }
  updateSortBtn()

  const header = h('div', { class: 'page-header' },
    h('div', { style: 'display:flex;align-items:flex-start;justify-content:space-between' },
      h('div', {},
        h('h1', {}, 'Deployments'),
        h('p',  {}, 'All glider deployments found in this pipeline directory.')),
      h('div', { style: 'display:flex;gap:8px;margin-top:4px' },
        sortBtn,
        h('button', { class: 'btn-primary', onclick: showNewDepModal }, '+ New Deployment'))))
  const body = h('div', { class: 'page-body' })
  page.appendChild(header)
  page.appendChild(body)

  const deps = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(v => v)
  if (!deps.length) { body.appendChild(h('div', { class: 'empty-state' }, 'No deployments found.')); return }

  const [allStates, allHealth] = await Promise.all([
    Promise.all(deps.map(d => loadDepState(d))),
    apiFetch('/api/deployments/health').catch(() => ({})),
  ])

  // Group by glider name (everything before the first '-')
  const gliderMap = new Map()
  deps.forEach((dep, i) => {
    const glider = dep.split('-')[0]
    if (!gliderMap.has(glider)) gliderMap.set(glider, [])
    gliderMap.get(glider).push({ dep, ds: allStates[i] })
  })

  // Helper: extract sortable timestamp string from dep name
  const depTimestamp = dep => dep.split('-')[1] || ''

  // Sort within each glider group
  gliderMap.forEach(gliderDeps => {
    if (dashSort === 'time') {
      gliderDeps.sort((a, b) => depTimestamp(b.dep).localeCompare(depTimestamp(a.dep)))
    } else {
      gliderDeps.sort((a, b) => a.dep.localeCompare(b.dep))
    }
  })

  // Sort glider groups themselves
  const sortedGroups = [...gliderMap.entries()]
  if (dashSort === 'time') {
    // Sort groups by their most-recent deployment (first item after inner sort)
    sortedGroups.sort(([, a], [, b]) =>
      depTimestamp(b[0].dep).localeCompare(depTimestamp(a[0].dep))
    )
  } else {
    sortedGroups.sort(([a], [b]) => a.localeCompare(b))
  }

  sortedGroups.forEach(([glider, gliderDeps]) => {
    // Most-recent dep is always first after inner sort (time) or first alpha (alpha) — find actual newest for display
    const newestTs  = gliderDeps.reduce((m, { dep }) => { const t = depTimestamp(dep); return t > m ? t : m }, '')
    const lastDate  = newestTs.length >= 8
      ? `${newestTs.slice(0,4)}-${newestTs.slice(4,6)}-${newestTs.slice(6,8)}`
      : ''

    const sectionHeader = h('div', { class: 'glider-section-header' },
      h('span', { class: 'glider-section-name' }, glider),
      h('span', { class: 'glider-section-meta' },
        `${gliderDeps.length} deployment${gliderDeps.length !== 1 ? 's' : ''}`,
        lastDate ? `  ·  Last: ${lastDate}` : ''))

    const cards = gliderDeps.map(({ dep, ds }) => {
      const ps       = ds.state    || {}
      const nc_count = ds.nc_count || 0
      const nc_dir   = ds.nc_dir   || ''
      const health   = allHealth[dep] || {}

      const allNotRun = state.stages.every(sk => !ps[sk] || ps[sk].status === 'not_run')

      const badges = h('div', { class: 'badge-row' },
        ...state.stages.map(sk =>
          h('span', { class: `badge ${statusClass((ps[sk] || {}).status || 'not_run')}` }, shortLabel(sk))
        ))

      // Health indicator row
      const healthRow = _depHealthRow(dep, health)

      // Checklist for brand-new deployments
      const checklist = allNotRun ? _depChecklist(dep, health) : null

      const infoText = !allNotRun
        ? h('div', { class: 'dep-info' },
            nc_count > 0
              ? `${nc_count.toLocaleString()} QC'd profile files  ·  ${nc_dir}`
              : 'QC output not yet generated')
        : null

      const nick = nicknames[dep]
      return h('div', { class: 'dep-card' },
        h('div', { class: 'dep-card-header' },
          h('div', { class: 'dep-name-block' },
            h('span', { class: 'dep-name' }, dep),
            nick ? h('span', { class: 'dep-card-nick' }, nick) : null),
          h('div', { style: 'display:flex;gap:6px' },
            h('button', { class: 'btn-primary',  onclick: () => openDeployment(dep) }, 'Open →'),
            h('button', { class: 'btn-danger-outline', title: 'Delete deployment…',
              onclick: () => showDeleteDepModal(dep) }, 'Delete'))),
        allNotRun ? checklist : badges,
        healthRow,
        infoText)
    })

    body.appendChild(h('div', { class: 'glider-section' }, sectionHeader, ...cards))
  })
}

function shortLabel (stageKey) {
  const map = {
    dbd_to_dat:           'DBD→DAT (Sci)',
    dbd_to_dat_flight:    'DBD→DAT (Flt)',
    dat_to_profile_nc:    'Profile NC',
    dat_to_trajectory_nc:        'Traj NC (Sci)',
    dat_to_trajectory_flight_nc: 'Traj NC (Flt)',
    fix_metadata:         'Fix Meta',
    preflight:            'Pre-flight',
    qc:                   'QC',
    report:               'Report',
  }
  return map[stageKey] || stageKey
}

function _depHealthRow (dep, h_) {
  const dot = (ok, label) => {
    const cls = ok ? 'health-dot--ok' : 'health-dot--warn'
    return h('span', { class: `health-dot ${cls}`, title: ok ? `${label}: OK` : `${label}: missing or incomplete` }, label)
  }
  return h('div', { class: 'dep-health-row' },
    dot(h_.raw_files > 0,         `Raw (${h_.raw_files ?? '?'})`),
    dot((h_.dat_merged > 0 || h_.dat_flight > 0), `DAT (${(h_.dat_merged || 0) + (h_.dat_flight || 0)})`),
    dot(h_.metadata_complete,     'Metadata'),
    dot(h_.sensor_defs_valid,     'Sensor defs'),
    dot(h_.nc_profile > 0,        `NC (${h_.nc_profile ?? 0})`),
  )
}

function _depChecklist (dep, h_) {
  const step = (done, label, note) => h('div', { class: `checklist-step ${done ? 'checklist-step--done' : 'checklist-step--todo'}` },
    h('span', { class: 'checklist-icon' }, done ? '✓' : '○'),
    h('span', { class: 'checklist-label' }, label),
    note ? h('span', { class: 'checklist-note' }, note) : null)

  return h('div', { class: 'dep-checklist' },
    h('div', { class: 'checklist-title' }, 'Getting started'),
    step(h_.raw_files > 0,     'Import raw files',    h_.raw_files > 0 ? `${h_.raw_files} files present` : 'Use Files page to import .dbd / .ebd'),
    step(h_.metadata_complete, 'Add metadata files',  !h_.metadata_complete ? 'Open Metadata page — add deployment.json, global_attributes.json, instruments.json' : null),
    step(h_.sensor_defs_valid, 'Build sensor defs',   !h_.sensor_defs_valid ? 'Metadata page → Build Sensor Defs → Copy to this deployment' : null),
    step(false,                'Run Stage 1 → DAT',   'Pipeline page — convert raw binary files to DAT'),
    step(false,                'Run Stages 2a/2b/2c → NetCDF', 'Pipeline page — convert DAT to NetCDF'),
  )
}

function showDeleteDepModal (dep) {
  const overlay = h('div', { class: 'modal-overlay',
    onclick: e => { if (e.target === overlay) overlay.remove() }
  })

  const input = h('input', {
    class: 'form-input', type: 'text',
    placeholder: `Type "${dep}" to confirm`,
    oninput: () => {
      confirmBtn.disabled = input.value.trim() !== dep
    }
  })

  const statusEl  = h('div', { class: 'modal-status', style: 'color:#e74c3c' })

  const confirmBtn = h('button', {
    class: 'btn-danger', disabled: true,
    onclick: async () => {
      confirmBtn.disabled = true
      confirmBtn.textContent = 'Deleting…'
      try {
        await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/delete`, { method: 'POST' })
        overlay.remove()
        delete nicknames[dep]

        // Rebuild sidebar select from fresh state
        const fresh = await loadAppState()
        const updatedDeps = fresh.deployments || []
        const sel = document.getElementById('dep-select')
        sel.innerHTML = ''
        sel.appendChild(h('option', { value: '' }, '— select deployment —'))
        updatedDeps.forEach(d => sel.appendChild(h('option', { value: d }, d)))

        if (state.deployment === dep) {
          state.deployment = null
          sel.value = ''
          localStorage.removeItem('lastDeployment')
          document.getElementById('dep-nickname-lbl').classList.add('hidden')
          document.getElementById('dep-nickname-btn').disabled = true
          document.getElementById('dep-rename-btn').disabled   = true
        }

        renderDashboard()
      } catch (e) {
        statusEl.textContent = 'Error: ' + e.message
        confirmBtn.disabled  = false
        confirmBtn.textContent = 'Delete deployment'
      }
    }
  }, 'Delete deployment')

  const cancelBtn = h('button', { class: 'btn-secondary',
    onclick: () => overlay.remove()
  }, 'Cancel')

  overlay.appendChild(h('div', { class: 'modal' },
    h('div', { class: 'modal-header' },
      h('h2', { style: 'color:#c0392b' }, 'Delete Deployment'),
      h('button', { class: 'modal-close', onclick: () => overlay.remove() }, '×')),
    h('div', { class: 'modal-body' },
      h('p', { style: 'margin-bottom:10px' },
        'This will permanently delete all files and folders for:'),
      h('p', { class: 'delete-dep-name' }, dep),
      h('p', { style: 'margin:10px 0;color:#7f8c8d;font-size:12px' },
        'Deleted folders: incoming_data, dat_files, deployments, gncutils output, config files, logs, AD2CP data.'),
      h('label', { class: 'form-label', style: 'margin-top:12px;display:block' },
        'Type the deployment name to confirm:'),
      input,
      statusEl),
    h('div', { class: 'modal-footer' }, cancelBtn, confirmBtn)))

  document.body.appendChild(overlay)
  input.focus()
}

function openDeployment (dep) {
  document.getElementById('dep-select').value = dep
  state.deployment = dep
  localStorage.setItem('lastDeployment', dep)
  navigate('pipeline')
}

/* ── Pipeline page ───────────────────────────────────────────────────────────*/
async function renderPipeline () {
  const dep  = state.deployment
  const page = document.getElementById('page-pipeline')
  page.innerHTML = ''

  const header = h('div', { class: 'page-header' },
    h('h1', {}, 'Pipeline'),
    h('p',  {}, dep ? `Deployment: ${dep}` : 'Select a deployment from the sidebar.')
  )
  const body = h('div', { class: 'page-body' })
  page.appendChild(header)

  // Docker status banner
  const dockerBanner = h('div', { class: 'docker-banner docker-banner--checking' }, 'Checking Docker…')
  page.appendChild(dockerBanner)
  page.appendChild(body)

  _checkDockerBanner(dockerBanner)

  if (!dep) return

  const ds = await loadDepState(dep)
  const ps = ds.state || {}

  for (const sk of state.stages) {
    const info       = state.stageInfo[sk] || {}
    const stateEntry = ps[sk] || {}
    const status     = stateEntry.status || 'not_run'
    body.appendChild(buildStageCard(sk, info, status, dep, stateEntry))
  }
}

async function _checkDockerBanner (banner) {
  let running = false, processRunning = false
  try {
    const r = await apiFetch('/api/docker/status')
    running        = r.running
    processRunning = !!r.process_running
  } catch {}

  if (running) {
    banner.className   = 'docker-banner docker-banner--ok'
    banner.textContent = 'Docker is running'
    return
  }

  banner.className = 'docker-banner docker-banner--down'
  banner.innerHTML = ''
  const status = h('span', { class: 'docker-start-status' })

  if (processRunning) {
    // Docker Desktop's process exists but its engine isn't responding to
    // `docker info` -- a plain Start just launches a redundant instance on
    // top of the already-running (just stuck) one and accomplishes nothing.
    banner.appendChild(document.createTextNode('Docker Desktop is running but not responding. '))
    const btn = h('button', { class: 'docker-start-btn' }, 'Force Restart Docker')
    banner.appendChild(btn); banner.appendChild(status)

    btn.onclick = async () => {
      if (!confirm('This will forcibly close Docker Desktop, reset its backend, and restart it. Continue?')) return
      btn.disabled       = true
      status.textContent = ' Restarting…'
      try {
        const r = await apiFetch('/api/docker/force-restart', { method: 'POST' })
        if (!r.ok) { status.textContent = ` Error: ${r.error}`; btn.disabled = false; return }
      } catch (e) {
        status.textContent = ` Error: ${e.message}`
        btn.disabled = false
        return
      }
      _streamDockerWait(banner, btn, status)
    }
    return
  }

  // Not running at all — show plain start button
  banner.appendChild(document.createTextNode('Docker is not running. '))
  const btn = h('button', { class: 'docker-start-btn' }, 'Start Docker Desktop')
  banner.appendChild(btn); banner.appendChild(status)

  btn.onclick = async () => {
    btn.disabled       = true
    status.textContent = ' Starting…'
    try {
      const r = await apiFetch('/api/docker/start', { method: 'POST' })
      if (!r.ok) { status.textContent = ` Error: ${r.error}`; btn.disabled = false; return }
    } catch (e) {
      status.textContent = ` Error: ${e.message}`
      btn.disabled = false
      return
    }
    _streamDockerWait(banner, btn, status)
  }
}

// Shared by both the "Start Docker Desktop" and "Force Restart Docker" flows --
// streams /api/docker/wait and updates the banner/button/status once ready,
// on timeout, or on a dropped connection.
function _streamDockerWait (banner, btn, status) {
  const t0  = Date.now()
  const src = new EventSource(`${API}/api/docker/wait`)
  _activeDockerWait = src
  src.onmessage = (e) => {
    const d = JSON.parse(e.data)
    if (d.ready) {
      src.close()
      _activeDockerWait = null
      banner.className   = 'docker-banner docker-banner--ok'
      banner.textContent = 'Docker is running'
    } else if (d.timeout) {
      src.close()
      _activeDockerWait = null
      status.textContent = ' Timed out — start Docker Desktop manually.'
      btn.disabled = false
    } else {
      const secs = Math.round((Date.now() - t0) / 1000)
      status.textContent = ` Starting… ${secs}s`
    }
  }
  src.onerror = () => {
    src.close()
    _activeDockerWait = null
    status.textContent = ' Connection lost — try again.'
    btn.disabled = false
  }
}

function buildStageCard (sk, info, initialStatus, dep, stateEntry = {}) {
  const src = getStagePaths(sk, dep)

  // Header
  const badge     = h('span', { class: `status-badge ${statusClass(initialStatus)}` }, statusLabel(initialStatus))
  const timingEl  = h('span', { class: 'stage-timing' })
  if (stateEntry.started_at && stateEntry.finished_at) {
    const dur = Math.round((new Date(stateEntry.finished_at) - new Date(stateEntry.started_at)) / 1000)
    timingEl.textContent = `${Math.floor(dur / 60)}m ${String(dur % 60).padStart(2, '0')}s`
  }
  const cardHeader = h('div', { class: 'stage-card-header' },
    h('span', { class: 'stage-title' }, info.label || sk),
    timingEl,
    badge
  )

  // Body
  const desc  = h('div', { class: 'stage-desc' }, info.description || '')
  const paths = h('div', { class: 'stage-paths' },
    h('b', {}, 'Source:'), ' ', src.source, h('br'),
    h('b', {}, 'Output:'), ' ', src.output
  )

  const bodyEl = h('div', { class: 'stage-body' }, desc, paths)

  if (info.manual) {
    // ── Manual stage ────────────────────────────────
    const note = h('div', { class: 'manual-note' }, info.manual_note || 'This step is performed manually.')
    const btnDone   = h('button', { class: 'btn-success',   onclick: () => markManual(dep, sk, 'done',    badge) }, '✔  Mark as Done')
    const btnNotRun = h('button', { class: 'btn-secondary', onclick: () => markManual(dep, sk, 'not_run', badge) }, '↺  Mark as Not Run')
    const btnRow = h('div', { class: 'btn-row' }, btnDone, btnNotRun)
    bodyEl.appendChild(note)
    bodyEl.appendChild(btnRow)

  } else {
    // ── Automated stage ──────────────────────────────
    const log = h('div', { class: 'log-pane hidden' })

    // ── Stage 5 (report) — user-configurable options ─────────────────────────
    let stageOpts = null
    let configRow = null
    if (sk === 'report') {
      const inputMinDepth = h('input', {
        type: 'number', value: '10', min: '0', max: '1000', step: '1',
        class: 'stage-config-input', title: 'Minimum profile depth (m)',
      })
      const inputNProfiles = h('input', {
        type: 'number', value: '20', min: '1', max: '200', step: '1',
        class: 'stage-config-input', title: 'Number of profiles to plot',
      })
      configRow = h('div', { class: 'stage-config-row' },
        h('label', { class: 'stage-config-label' }, 'Min depth (m):'),
        inputMinDepth,
        h('label', { class: 'stage-config-label stage-config-label--gap' }, 'Profile count:'),
        inputNProfiles,
        h('span', { class: 'stage-config-hint' },
          'Profiles shallower than min depth are skipped (removes checkout dives).'
        ),
      )
      stageOpts = () => ({
        min_depth: parseFloat(inputMinDepth.value) || 10,
        nprofiles: parseInt(inputNProfiles.value,  10) || 20,
      })
    }

    const btnRun   = h('button', { class: 'btn-success', onclick: async () => {
      if (sk === 'dbd_to_dat' || sk === 'dbd_to_dat_flight') {
        try {
          const counts = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-count`)
          const count  = sk === 'dbd_to_dat' ? counts.merged : counts.flight
          const folder = sk === 'dbd_to_dat' ? 'merged_data' : 'flight_data'
          if (count > 0 && !confirm(
            `${count} existing DAT file${count !== 1 ? 's' : ''} found in ${folder}.\n\nProceeding will overwrite them. Continue?`
          )) return
        } catch {}
      }
      if (sk === 'dat_to_profile_nc' || sk === 'dat_to_trajectory_nc' || sk === 'dat_to_trajectory_flight_nc') {
        try {
          const v = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/validate-netcdf-config?stage=${sk}`)
          if (!v.ok) {
            _showValidationModal(sk, v.issues)
            return
          }
        } catch {}
      }
      doRun(dep, sk, badge, btnRun, btnAbort, log, stageOpts ? stageOpts() : {}, timingEl)
    }}, '▶  Run Stage')
    const btnAbort = h('button', { class: 'btn-danger hidden', onclick: () => doAbort(dep, sk, badge, btnRun, btnAbort) }, '■  Abort')
    const btnClear = h('button', { class: 'btn-secondary', onclick: () => { log.innerHTML = ''; log.classList.add('hidden'); stageLogs[sk] = [] } }, 'Clear log')
    const btnSetStatus = h('button', { class: 'btn-secondary',
      title: 'Manually override this stage\'s status',
      onclick: e => _stageStatusPopover(e.currentTarget, async st => {
        await setStageStatus(dep, sk, st)
        badge.className   = `status-badge ${statusClass(st)}`
        badge.textContent = statusLabel(st)
        btnRun.textContent = (st === 'done' || st === 'needs_review') ? '↺  Re-run Stage' : '▶  Run Stage'
      })
    }, 'Set status ▾')
    const btnRow   = h('div', { class: 'btn-row' }, btnRun, btnAbort, btnClear, btnSetStatus)

    // Restore state if this stage is currently running or has prior log output
    if (activeRuns[sk]) {
      // This page session started the run — reconnect live DOM refs and replay log.
      // Include timingEl so doRun()'s still-running setInterval (it looks up
      // stageEls[sk]?.timingEl fresh on every tick, since the JS execution that
      // started it isn't torn down by in-app navigation) picks up this rebuilt
      // card's new element instead of silently writing to the orphaned old one.
      stageEls[sk] = { log, badge, btnRun, btnAbort, timingEl }
      if (stateEntry.started_at) {
        const s = Math.max(0, Math.round((Date.now() - new Date(stateEntry.started_at)) / 1000))
        timingEl.textContent = `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
      }
      ;(stageLogs[sk] || []).forEach(line => appendLog(log, line))
      log.classList.remove('hidden')
      btnRun.classList.add('hidden')
      btnAbort.classList.remove('hidden')
      badge.className   = 'status-badge running'
      badge.textContent = 'RUNNING…'
    } else if (initialStatus === 'running') {
      // Run started before this page session (e.g. after a page refresh / server restart).
      // Hide Run, show Abort, offer a Reconnect button.
      btnRun.classList.add('hidden')
      btnAbort.classList.remove('hidden')
      badge.className   = 'status-badge running'
      badge.textContent = 'RUNNING…'

      const note = h('div', { class: 'stage-running-note' })

      // No live JS execution survives a real page reload / app restart, so unlike
      // the same-session branch above there's no existing interval to reconnect --
      // this one has to be created fresh, seeded from the real started_at, and torn
      // down explicitly wherever this run's completion is detected below.
      let _reconnectTimingIv = null
      if (stateEntry.started_at) {
        const s = Math.max(0, Math.round((Date.now() - new Date(stateEntry.started_at)) / 1000))
        timingEl.textContent = `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
      }

      function showReconnectBtn () {
        note.innerHTML = ''
        const btn = h('button', { class: 'btn-secondary', style: 'margin-left:8px',
          onclick: attemptReconnect }, '⟳  Reconnect to logs')
        note.appendChild(document.createTextNode('⏳ Stage is running in the background.'))
        note.appendChild(btn)
      }

      function applyFinishedState (st) {
        clearInterval(_reconnectTimingIv)
        note.remove()
        log.classList.remove('hidden')
        btnRun.classList.remove('hidden')
        btnAbort.classList.add('hidden')
        badge.className   = `status-badge ${statusClass(st)}`
        badge.textContent = statusLabel(st)
        btnRun.textContent = '↺  Re-run Stage'
        delete stageEls[sk]
        delete activeRuns[sk]
      }

      async function attemptReconnect () {
        note.innerHTML = '⏳ Connecting…'
        const controller = new AbortController()
        activeRuns[sk] = controller
        stageEls[sk]   = { log, badge, btnRun, btnAbort, timingEl }
        stageLogs[sk]  = []

        try {
          const resp = await fetch(
            `${API}/api/deployment/${encodeURIComponent(dep)}/stage-logs/${sk}`,
            { signal: controller.signal })

          if (!resp.ok) {
            // Container not found — check real state from server
            delete activeRuns[sk]; delete stageEls[sk]
            try {
              const ds = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/state`)
              const st = (ds.state?.[sk] || {}).status || 'not_run'
              if (st === 'running') {
                // Still running but container name mismatch — let user try again
                showReconnectBtn()
              } else {
                applyFinishedState(st)
              }
            } catch { showReconnectBtn() }
            return
          }

          // Successfully connected — show log pane, hide note
          note.remove()
          log.classList.remove('hidden')

          // (Re)start the live timer, seeded from the real started_at rather than
          // from now, so it reflects true elapsed time, not just time-since-reconnect.
          clearInterval(_reconnectTimingIv)
          const _t0 = stateEntry.started_at ? new Date(stateEntry.started_at).getTime() : Date.now()
          _reconnectTimingIv = setInterval(() => {
            const el = stageEls[sk]
            if (el?.timingEl) {
              const s = Math.max(0, Math.round((Date.now() - _t0) / 1000))
              el.timingEl.textContent = `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
            }
          }, 1000)

          const reader = resp.body.getReader(), decoder = new TextDecoder()
          let buffer = ''
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            const lines = buffer.split('\n'); buffer = lines.pop()
            for (const line of lines) {
              if (!line.startsWith('data: ')) continue
              try {
                const evt = JSON.parse(line.slice(6))
                if (evt.type === 'log') {
                  stageLogs[sk].push(evt.line)
                  const el = stageEls[sk]; if (el) appendLog(el.log, evt.line)
                } else if (evt.type === 'done' || evt.type === 'error') {
                  const success = evt.type === 'done' && evt.success
                  const finalLine = success ? '✔ Finished successfully.' : '✖ Finished with errors.'
                  stageLogs[sk].push(finalLine)
                  const el = stageEls[sk]
                  if (el) appendLog(el.log, finalLine)
                  applyFinishedState(success ? 'done' : 'failed')
                  break
                }
              } catch {}
            }
          }
        } catch (e) {
          clearInterval(_reconnectTimingIv)
          if (e.name !== 'AbortError') { appendLog(log, 'Stream error: ' + e.message); log.classList.remove('hidden') }
          delete activeRuns[sk]; delete stageEls[sk]
          showReconnectBtn()
        }
      }

      showReconnectBtn()
      bodyEl.appendChild(note)
    } else if (stageLogs[sk]?.length) {
      // Stage finished — show last log output
      ;(stageLogs[sk]).forEach(line => appendLog(log, line))
      log.classList.remove('hidden')
      btnRun.textContent = '↺  Re-run Stage'
    }

    // Async validation banner for stages 2a/2b/2c
    const NC_STAGES = ['dat_to_profile_nc', 'dat_to_trajectory_nc', 'dat_to_trajectory_flight_nc']
    if (NC_STAGES.includes(sk)) {
      const banner = h('div', { class: 'stage-validation-banner hidden' })
      bodyEl.appendChild(banner)
      ;(async () => {
        try {
          const v = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/validate-netcdf-config?stage=${sk}`)
          if (!v.ok && v.issues?.length) {
            banner.className = 'stage-validation-banner stage-validation-banner--warn'
            banner.appendChild(h('span', { class: 'stage-validation-icon' }, '⚠'))
            banner.appendChild(h('span', { class: 'stage-validation-text' },
              `${v.issues.length} config issue${v.issues.length !== 1 ? 's' : ''} — stage may fail`))
            banner.appendChild(h('button', { class: 'stage-validation-detail-btn',
              onclick: () => _showValidationModal(sk, v.issues) }, 'Details'))
          }
        } catch {}
      })()
    }

    if (configRow) bodyEl.appendChild(configRow)
    bodyEl.appendChild(btnRow)
    bodyEl.appendChild(log)
  }

  return h('div', { class: 'stage-card' }, cardHeader, bodyEl)
}

function getStagePaths (sk, dep) {
  const b = window.APP.baseDir
  const y = dep ? dep.split('-')[1].slice(0, 4) : ''
  const g = dep ? dep.split('-')[0] : ''
  const paths = {
    dbd_to_dat:           { source: `${b}\\incoming_data\\${dep}`,  output: `${b}\\dat_files\\${dep}\\merged_data` },
    dbd_to_dat_flight:    { source: `${b}\\incoming_data\\${dep}`,  output: `${b}\\dat_files\\${dep}\\flight_data` },
    dat_to_profile_nc:    { source: `${b}\\dat_files\\${dep}\\merged_data`,                  output: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile` },
    dat_to_trajectory_nc:        { source: `${b}\\dat_files\\${dep}\\merged_data`,  output: `${b}\\gncutils_slocum_nc_output\\${dep}\\trajectory_science` },
    dat_to_trajectory_flight_nc: { source: `${b}\\dat_files\\${dep}\\flight_data`, output: `${b}\\gncutils_slocum_nc_output\\${dep}\\trajectory_flight` },
    fix_metadata:         { source: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile`,      output: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile  (in-place)` },
    preflight:            { source: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile`,      output: `${b}\\deployments\\${y}\\${dep}\\config\\qc` },
    qc:                   { source: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile`,      output: `${b}\\deployments\\${y}\\${dep}\\data\\out\\nc\\sci-profile\\delayed` },
    report:               { source: `${b}\\deployments\\${y}\\${dep}\\data\\out\\nc\\sci-profile\\delayed`, output: `${b}\\deployments\\${y}\\${dep}\\reports` },
  }
  return paths[sk] || { source: '—', output: '—' }
}

async function markManual (dep, sk, status, badge) {
  await setStageStatus(dep, sk, status)
  badge.className = `status-badge ${statusClass(status)}`
  badge.textContent = statusLabel(status)
}

function doRun (dep, sk, badge, btnRun, btnAbort, log, opts = {}, timingEl = null) {
  // Register live DOM refs — updated by buildStageCard on re-render
  stageEls[sk]  = { log, badge, btnRun, btnAbort, timingEl }
  stageLogs[sk] = []

  log.classList.remove('hidden')
  log.innerHTML = ''
  btnRun.classList.add('hidden')
  btnAbort.classList.remove('hidden')
  badge.className   = 'status-badge running'
  badge.textContent = 'RUNNING…'

  const _t0 = Date.now()
  if (timingEl) timingEl.textContent = '0m 00s'
  const _timingIv = setInterval(() => {
    const el = stageEls[sk]
    if (el?.timingEl) {
      const s = Math.round((Date.now() - _t0) / 1000)
      el.timingEl.textContent = `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
    }
  }, 1000)

  // For preflight stage: capture the PREFLIGHT_JSON line as the analysis runs
  let preflightResult = null

  streamRun(dep, sk,
    (line) => {
      stageLogs[sk].push(line)
      // Intercept preflight JSON marker — don't show this raw line in the log
      if (line.startsWith('=== PREFLIGHT_JSON ===')) {
        try { preflightResult = JSON.parse(line.slice('=== PREFLIGHT_JSON ==='.length)) }
        catch {}
        return  // skip rendering this line
      }
      const el = stageEls[sk]
      if (el) appendLog(el.log, line)
    },
    (success) => {
      clearInterval(_timingIv)
      const el = stageEls[sk]
      delete stageEls[sk]
      if (el?.timingEl) {
        const s = Math.round((Date.now() - _t0) / 1000)
        el.timingEl.textContent = `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
      }
      const finalLine = success ? '✔ Analysis complete.' : '✖ Finished with errors.'
      stageLogs[sk].push(finalLine)
      if (el) {
        appendLog(el.log, finalLine)
        el.btnRun.classList.remove('hidden')
        el.btnAbort.classList.add('hidden')
        const st = success ? (sk === 'preflight' && preflightResult ? 'running' : 'done') : 'failed'
        el.badge.className   = `status-badge ${st}`
        el.badge.textContent = sk === 'preflight' && preflightResult && success
          ? 'REVIEW NEEDED' : statusLabel(success ? 'done' : 'failed')
        el.btnRun.textContent = '↺  Re-run Stage'

        // Show preflight confirmation panel after a successful analysis
        if (sk === 'preflight' && success && preflightResult) {
          const bodyEl = el.log.parentElement
          showPreflightPanel(dep, preflightResult, bodyEl, el.badge)
        }
      }
    },
    opts
  )
}

/* ── Preflight confirmation panel ────────────────────────────────────────────*/
function actionBadgeClass (action) {
  if (action === 'create')    return 'preflight-badge-create'
  if (action === 'update')    return 'preflight-badge-update'
  return 'preflight-badge-ok'
}

function showPreflightPanel (dep, data, bodyEl, badge) {
  // Remove any existing panel first
  const existing = bodyEl.querySelector('.preflight-panel')
  if (existing) existing.remove()

  const sensors     = data.sensors || {}
  const configs     = data.configs || {}
  const anyChanges  = data.any_changes
  const oxygenVars  = data.oxygen_vars || []

  // ── Sensor summary row ────────────────────────────────────────────────────
  function sensorChip (label, info) {
    const ok  = !!info
    const txt = ok ? `${info.maker} ${info.model}`.trim() || label : 'not detected'
    return h('span', { class: `preflight-sensor-chip ${ok ? 'chip-found' : 'chip-missing'}` },
      `${label}: ${txt}`)
  }

  const sensorRow = h('div', { class: 'preflight-sensor-row' },
    sensorChip('CTD', sensors.ctd),
    sensorChip('O₂', sensors.oxygen),
    sensorChip('pH', sensors.ph),
  )

  // Oxygen variable confirmation note
  let oxygenNote = null
  if (sensors.oxygen && oxygenVars.length) {
    oxygenNote = h('div', { class: 'preflight-note' },
      `Oxygen variables detected in NC file: `,
      h('code', {}, oxygenVars.join(', ')),
      '. These will be used in time_shift.yml.')
  } else if (sensors.oxygen && !oxygenVars.length) {
    oxygenNote = h('div', { class: 'preflight-note preflight-warn' },
      '⚠ Oxygen instrument detected but no oxygen variables found in the NC file. ' +
      'time_shift.yml will be created but may be incomplete.')
  }

  // ── Config file cards ──────────────────────────────────────────────────────
  const configCards = []
  for (const [fname, info] of Object.entries(configs)) {
    const actionLabel = { create: 'New file', update: 'Will update', unchanged: 'Up to date' }
    const actionBadge = h('span', { class: `preflight-badge ${actionBadgeClass(info.action)}` },
      actionLabel[info.action] || info.action)

    const headerRow = h('div', { class: 'preflight-file-header' },
      h('span', { class: 'preflight-file-name' }, fname),
      actionBadge,
    )

    let contentEl
    if (info.action === 'unchanged') {
      contentEl = h('div', { class: 'preflight-file-unchanged' }, '✔ Already up to date')
    } else if (info.action === 'update') {
      // Show both old and new
      const tabs = h('div', { class: 'preflight-tabs' })
      const btnNew = h('button', { class: 'preflight-tab active', onclick: () => {
        btnNew.classList.add('active'); btnOld.classList.remove('active')
        newPane.classList.remove('hidden'); oldPane.classList.add('hidden')
      }}, 'New')
      const btnOld = h('button', { class: 'preflight-tab', onclick: () => {
        btnOld.classList.add('active'); btnNew.classList.remove('active')
        oldPane.classList.remove('hidden'); newPane.classList.add('hidden')
      }}, 'Existing')
      tabs.appendChild(btnNew); tabs.appendChild(btnOld)
      const newPane = h('pre', { class: 'preflight-code' }, info.content)
      const oldPane = h('pre', { class: 'preflight-code hidden' }, info.existing_content || '')
      contentEl = h('div', {}, tabs, newPane, oldPane)
    } else {
      contentEl = h('pre', { class: 'preflight-code' }, info.content)
    }

    configCards.push(h('div', { class: 'preflight-file-card' }, headerRow, contentEl))
  }

  // ── Regional boundary card ─────────────────────────────────────────────────
  const rc = data.region_coverage || {}
  const boundaryInadequate = rc && !rc.coverage_ok && rc.proposed_lat && rc.proposed_lon
  let   includeBoundary    = boundaryInadequate  // checkbox default

  let boundaryCard = null
  let inpLatMin = null, inpLatMax = null, inpLonMin = null, inpLonMax = null
  if (boundaryInadequate) {
    const cbBoundary = h('input', { type: 'checkbox' })
    cbBoundary.checked = true
    cbBoundary.addEventListener('change', () => { includeBoundary = cbBoundary.checked })

    const mapEl = rc.map_base64
      ? h('img', { class: 'preflight-boundary-map',
                   src: `data:image/png;base64,${rc.map_base64}`,
                   alt: 'USVI region boundary map' })
      : null

    // Editable proposed boundary inputs
    const fmt = v => (v == null ? '—' : v.toFixed(1))
    const makeNumInput = (val) => {
      const inp = h('input', {
        type: 'number', step: '0.1', class: 'preflight-bound-input',
        value: val.toFixed(1)
      })
      return inp
    }
    inpLatMin = makeNumInput(rc.proposed_lat[0])
    inpLatMax = makeNumInput(rc.proposed_lat[1])
    inpLonMin = makeNumInput(rc.proposed_lon[0])
    inpLonMax = makeNumInput(rc.proposed_lon[1])

    const boundsTable = h('table', { class: 'preflight-bounds-table' },
      h('thead', {},
        h('tr', {},
          h('th', {}, ''),
          h('th', {}, 'Lat min'), h('th', {}, 'Lat max'),
          h('th', {}, 'Lon min'), h('th', {}, 'Lon max'),
        )
      ),
      h('tbody', {},
        h('tr', { class: 'current-row' },
          h('td', {}, 'Current'),
          h('td', {}, fmt(rc.current_lat && rc.current_lat[0])),
          h('td', {}, fmt(rc.current_lat && rc.current_lat[1])),
          h('td', {}, fmt(rc.current_lon && rc.current_lon[0])),
          h('td', {}, fmt(rc.current_lon && rc.current_lon[1])),
        ),
        h('tr', { class: 'proposed-row' },
          h('td', {}, 'Proposed'),
          h('td', {}, inpLatMin), h('td', {}, inpLatMax),
          h('td', {}, inpLonMin), h('td', {}, inpLonMax),
        ),
      )
    )

    boundaryCard = h('div', { class: 'preflight-boundary-card' },
      h('div', { class: 'preflight-file-header' },
        h('span', { class: 'preflight-file-name' }, 'regional_boundaries.yml'),
        h('span', { class: 'preflight-badge preflight-badge-warn' }, 'UPDATE NEEDED'),
      ),
      h('div', { class: 'preflight-note preflight-warn' },
        `⚠ ${rc.n_outside} of ${rc.n_profiles} profiles fall outside the current USVI ` +
        `boundary (lat up to ${rc.data_lat[1].toFixed(3)}°N, lon to ${rc.data_lon[0].toFixed(3)}°). ` +
        'Profiles outside the region use global (less strict) QC thresholds. ' +
        'This file is shared across all USVI deployments.'),
      ...(mapEl ? [mapEl] : []),
      boundsTable,
      h('label', { class: 'preflight-checkbox-label' },
        cbBoundary,
        'Update USVI regional boundary (global — applies to all USVI deployments)'),
    )
  }

  // ── Action buttons ─────────────────────────────────────────────────────────
  const needsApply = anyChanges || boundaryInadequate
  const btnApply   = h('button', { class: 'btn-success' }, '✔  Apply Config')
  const btnSkip    = h('button', { class: 'btn-secondary' },
    needsApply ? 'Skip (continue without applying)' : '✔  Continue')
  const resultMsg  = h('div', { class: 'preflight-result hidden' })

  btnApply.addEventListener('click', async () => {
    btnApply.disabled = true
    btnSkip.disabled  = true
    btnApply.textContent = 'Applying…'
    try {
      // Build payload — attach boundary update flag if user opted in
      const payload = Object.assign({}, data)
      if (boundaryInadequate && includeBoundary) {
        payload.region_coverage = Object.assign({}, rc, {
          update_requested: true,
          // Send user-edited values so server rebuilds YAML from them
          user_lat: [parseFloat(inpLatMin.value), parseFloat(inpLatMax.value)],
          user_lon: [parseFloat(inpLonMin.value), parseFloat(inpLonMax.value)],
        })
      }
      const r = await apiFetch(
        `/api/deployment/${encodeURIComponent(dep)}/preflight/apply`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) })
      if (r.ok) {
        resultMsg.textContent = r.output || 'Config applied successfully.'
        resultMsg.className = 'preflight-result preflight-result-ok'
        badge.className   = 'status-badge done'
        badge.textContent = statusLabel('done')
      } else {
        resultMsg.textContent = `Error: ${r.error || 'apply failed'}`
        resultMsg.className = 'preflight-result preflight-result-err'
        badge.className   = 'status-badge failed'
        badge.textContent = statusLabel('failed')
      }
    } catch (e) {
      resultMsg.textContent = `Error: ${e.message}`
      resultMsg.className = 'preflight-result preflight-result-err'
    }
    resultMsg.classList.remove('hidden')
    btnApply.remove()
    btnSkip.textContent = 'Done'
    btnSkip.disabled = false
  })

  btnSkip.addEventListener('click', async () => {
    // Skip is an explicit acknowledgment — always record it server-side, even when
    // there are pending changes, so the stage doesn't stay stuck at NEEDS_REVIEW.
    btnSkip.disabled = true
    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/set-status/preflight`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'done' }) })
      badge.className   = 'status-badge done'
      badge.textContent = statusLabel('done')
      panel.remove()
    } catch (e) {
      btnSkip.disabled = false
      alert(`Could not update status: ${e.message || e}`)
    }
  })

  const btnRow = h('div', { class: 'btn-row' }, ...(needsApply ? [btnApply, btnSkip] : [btnSkip]))

  // ── Assemble panel ─────────────────────────────────────────────────────────
  const title = h('div', { class: 'preflight-title' },
    needsApply ? '⚙ Config files need to be created or updated'
               : '✔ Config files are up to date')

  const panel = h('div', { class: 'preflight-panel' },
    title,
    sensorRow,
    ...(oxygenNote   ? [oxygenNote]   : []),
    ...configCards,
    ...(boundaryCard ? [boundaryCard] : []),
    resultMsg,
    btnRow,
  )

  bodyEl.appendChild(panel)
}

function _showValidationModal (stage, issues) {
  const STAGE_LABELS = {
    dat_to_profile_nc:           'Stage 2a — Profile NetCDF',
    dat_to_trajectory_nc:        'Stage 2b — Trajectory NC (Science)',
    dat_to_trajectory_flight_nc: 'Stage 2c — Trajectory NC (Flight)',
  }
  const SUBDIR_LABELS = {
    dat_to_profile_nc:           'science_profile/',
    dat_to_trajectory_nc:        'science_trajectory/',
    dat_to_trajectory_flight_nc: 'trajectory_flight/',
  }

  const overlay = h('div', { class: 'modal-overlay' })
  const modal   = h('div', { class: 'modal-box' },
    h('h2', {}, `Cannot run ${STAGE_LABELS[stage] || stage}`),
    h('p',  { style: 'color:#a0b0c0;font-size:13px;margin-bottom:12px' },
      `Fix the following issues in the ${SUBDIR_LABELS[stage] || ''} metadata config before running:`),
    h('ul', { class: 'validation-issue-list' },
      ...issues.map(({ file, message }) =>
        h('li', { class: 'validation-issue-item' },
          h('span', { class: 'validation-issue-file' }, file),
          h('span', { class: 'validation-issue-msg'  }, message)
        )
      )
    ),
    h('p', { style: 'color:#a0b0c0;font-size:12px;margin-top:12px' },
      'Use the Metadata page to add missing files, or run Build Sensor Defs to regenerate sensor_defs.json.'),
    h('div', { class: 'btn-row', style: 'margin-top:16px' },
      h('button', { class: 'btn-primary', onclick: () => overlay.remove() }, 'OK')
    )
  )
  overlay.appendChild(modal)
  document.body.appendChild(overlay)
}

function _stageStatusPopover (anchor, onPick) {
  document.querySelectorAll('.stage-status-popover').forEach(el => el.remove())

  const options = [
    { value: 'done',     label: statusLabel('done'),     cls: 'done' },
    { value: 'failed',   label: statusLabel('failed'),   cls: 'failed' },
    { value: 'not_run',  label: statusLabel('not_run'),  cls: 'not_run' },
  ]

  const rect    = anchor.getBoundingClientRect()
  const popover = h('div', { class: 'stage-status-popover' })
  popover.style.top  = `${rect.bottom + window.scrollY + 4}px`
  popover.style.left = `${rect.left + window.scrollX}px`

  options.forEach(({ value, label, cls }) => {
    const item = h('div', { class: `stage-status-popover-item status-pop-${cls}`,
      onclick: async e => {
        e.stopPropagation()
        popover.remove()
        await onPick(value)
      }
    }, label)
    popover.appendChild(item)
  })

  document.body.appendChild(popover)
  const close = e => { if (!popover.contains(e.target)) { popover.remove(); document.removeEventListener('click', close) } }
  setTimeout(() => document.addEventListener('click', close), 0)
}

async function doAbort (dep, sk, badge, btnRun, btnAbort) {
  const r  = await abortRun(dep, sk)
  const st = r?.restored_status || 'not_run'
  const el = stageEls[sk] || { badge, btnRun, btnAbort }
  el.btnRun.classList.remove('hidden')
  el.btnAbort.classList.add('hidden')
  el.badge.className   = `status-badge ${statusClass(st)}`
  el.badge.textContent = statusLabel(st)
  el.btnRun.textContent = (st === 'done' || st === 'needs_review') ? '↺  Re-run Stage' : '▶  Run Stage'
  delete stageEls[sk]
}

function appendLog (logEl, line) {
  const span = document.createElement('span')
  span.className = logColor(line)
  span.textContent = line + '\n'
  const isAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 50
  logEl.appendChild(span)
  if (isAtBottom) logEl.scrollTop = logEl.scrollHeight
}

/* ── Reports page ────────────────────────────────────────────────────────────*/
async function renderReports () {
  const dep  = state.deployment
  const page = document.getElementById('page-reports')
  page.innerHTML = ''

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'Reports & Plots'),
    h('p',  {}, dep ? `Deployment: ${dep}` : 'Select a deployment.')
  ))

  let _reportLeafletMap = null   // persists between tab switches within this render

  async function switchToVars () {
    tabBar.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === 'variables'))
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-variables'))
    if (varsTab.dataset.loaded) return
    varsTab.dataset.loaded = '1'
    await renderNcVariablesTab(dep, varsTab)
  }

  async function switchToMap () {
    tabBar.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === 'map'))
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-map'))
    if (mapTab.dataset.loaded) {
      // Leaflet needs invalidateSize after container was hidden
      setTimeout(() => { if (_reportLeafletMap) _reportLeafletMap.invalidateSize() }, 60)
      return
    }
    mapTab.dataset.loaded = '1'
    _reportLeafletMap = await renderTrajectoryMap(dep, mapTab)
  }

  async function switchToFlight () {
    tabBar.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === 'flight'))
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-flight'))
    if (flightTab.dataset.loaded) return
    flightTab.dataset.loaded = '1'
    await renderFlightStats(dep, flightTab)
  }

  function switchToAdcp () {
    tabBar.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === 'adcp'))
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-adcp'))
  }

  const tabBar = h('div', { class: 'tab-bar' },
    h('button', { class: 'tab-btn active', 'data-tab': 'report',    onclick: switchTab     }, 'QC Report'),
    h('button', { class: 'tab-btn',        'data-tab': 'plots',     onclick: switchTab     }, 'Plots'),
    h('button', { class: 'tab-btn',        'data-tab': 'variables', onclick: switchToVars  }, 'Variables'),
    h('button', { class: 'tab-btn',        'data-tab': 'map',       onclick: switchToMap   }, 'Map'),
    h('button', { class: 'tab-btn',        'data-tab': 'flight',    onclick: switchToFlight}, 'Flight Stats'),
    h('button', { class: 'tab-btn',        'data-tab': 'adcp',      onclick: switchToAdcp  }, 'AD2CP'),
  )
  page.appendChild(tabBar)

  // Report tab
  const reportSrc = dep ? `${API}/api/deployment/${encodeURIComponent(dep)}/report` : 'about:blank'
  const frame = h('iframe', { id: 'report-frame', src: reportSrc })
  const qcDlBtn = dep ? h('a', {
    class: 'btn-secondary btn-sm',
    href: reportSrc,
    download: `qc_report_${dep}.html`,
  }, '⬇ Download Report') : null
  const reportTab = h('div', { class: 'tab-panel active', id: 'tab-report' },
    h('div', { class: 'report-toolbar' }, qcDlBtn),
    frame,
  )

  // Plots tab
  const plotGrid = h('div', { class: 'plot-grid' })
  const plotsTab = h('div', { class: 'tab-panel', id: 'tab-plots' }, plotGrid)

  // Variables tab
  const varsTab   = h('div', { class: 'tab-panel', id: 'tab-variables' })

  // Map tab
  const mapTab    = h('div', { class: 'tab-panel', id: 'tab-map'    })

  // Flight Stats tab
  const flightTab = h('div', { class: 'tab-panel', id: 'tab-flight' })

  // AD2CP tab — iframe with per-run switcher and loading overlay
  const adcpAllSrc = dep
    ? `${API}/api/adcp/report-html?dep=${encodeURIComponent(dep)}`
    : 'about:blank'
  const adcpReportFrame   = h('iframe', { id: 'adcp-report-frame', src: adcpAllSrc })
  const adcpLoadOverlay   = h('div', { class: 'adcp-report-loading' }, 'Generating report…')

  const adcpDlBtn = dep ? h('a', { class: 'btn-secondary btn-sm', href: `${adcpAllSrc}&dl=1` }, '⬇ Download') : null

  // Shared list of run buttons — populated async; needed by the load handler.
  let _adcpRunBtns = []

  function _switchAdcpReport (url) {
    adcpLoadOverlay.classList.add('visible')
    _adcpRunBtns.forEach(b => { b.disabled = true })
    if (adcpDlBtn) adcpDlBtn.href = `${url}&dl=1`
    adcpReportFrame.src = url
  }

  adcpReportFrame.addEventListener('load', () => {
    adcpLoadOverlay.classList.remove('visible')
    _adcpRunBtns.forEach(b => { b.disabled = false })
  })

  const switcherRow = h('div', { class: 'adcp-run-switcher' })
  if (dep) {
    switcherRow.appendChild(h('span', { class: 'adcp-run-switcher-label' }, 'View:'))
    // Show loading overlay for the initial "All runs" report that fires immediately
    adcpLoadOverlay.classList.add('visible')
    ;(async () => {
      try {
        const statusR = await apiFetch(`/api/adcp/status?dep=${encodeURIComponent(dep)}&is_test=false`)
        const runs = Object.keys(statusR.runs || {})

        const makeBtn = (label, url) => {
          const btn = h('button', { class: 'adcp-run-btn' }, label)
          btn.addEventListener('click', () => {
            _adcpRunBtns.forEach(b => b.classList.remove('active'))
            btn.classList.add('active')
            _switchAdcpReport(url)
          })
          _adcpRunBtns.push(btn)
          return btn
        }

        const allBtn = makeBtn('All runs', adcpAllSrc)
        allBtn.classList.add('active')
        switcherRow.appendChild(allBtn)
        runs.forEach(run => {
          const url = `${API}/api/adcp/report-html?dep=${encodeURIComponent(dep)}&run=${encodeURIComponent(run)}`
          switcherRow.appendChild(makeBtn(run, url))
        })
      } catch (_) {}
    })()
  }

  const adcpReportTab = h('div', { class: 'tab-panel', id: 'tab-adcp' },
    h('div', { class: 'report-toolbar' }, switcherRow, adcpDlBtn),
    h('div', { class: 'adcp-frame-wrapper' }, adcpReportFrame, adcpLoadOverlay),
  )

  page.appendChild(reportTab)
  page.appendChild(plotsTab)
  page.appendChild(varsTab)
  page.appendChild(mapTab)
  page.appendChild(flightTab)
  page.appendChild(adcpReportTab)

  if (dep) {
    try {
      const pd = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/plots`)
      // Deployment-wide interactive plots first, then per-profile plots
      const all = [
        ...(pd.htmls        || []),
        ...(pd.pngs         || []),
        ...(pd.profile_htmls || []),
        ...(pd.profile_pngs  || []),
      ]
      if (!all.length) {
        plotGrid.appendChild(h('div', { class: 'reports-empty' },
          h('div', { class: 'reports-empty-icon' }, '📈'),
          h('div', { class: 'reports-empty-title' }, 'No plots generated yet'),
          h('div', { class: 'reports-empty-hint' }, 'Run the ', h('code', {}, 'Report + Plots'), ' stage to generate plots.')))
      } else {
        // Scale each iframe to fill its preview box, and re-scale on resize
        function _scaleIframe (previewEl) {
          const iframe = previewEl.querySelector('.plot-thumb-iframe')
          if (!iframe) return
          const scale = previewEl.offsetWidth / 960
          iframe.style.transform = `scale(${scale})`
        }
        const scaleObserver = new ResizeObserver(entries => {
          entries.forEach(e => _scaleIframe(e.target))
        })

        // Lazy-load iframes as they scroll into view
        const iframeObserver = new IntersectionObserver((entries) => {
          entries.forEach(entry => {
            if (!entry.isIntersecting) return
            const iframe = entry.target.querySelector('iframe[data-src]')
            if (iframe) { iframe.src = iframe.dataset.src; iframe.removeAttribute('data-src') }
            iframeObserver.unobserve(entry.target)
          })
        }, { rootMargin: '200px 0px' })

        all.forEach((fp, idx) => {
          const name   = fp.split(/[\\/]/).pop()
          const isHtml = fp.endsWith('.html')
          const label  = _plotLabel(name)
          const url    = `${API}/api/file?path=${encodeURIComponent(fp)}`
          let thumb

          if (isHtml) {
            // Embed a scaled-down iframe so the actual chart is visible in the card
            const iframe = h('iframe', {
              'data-src': url,         // loaded lazily by intersection observer
              class: 'plot-thumb-iframe',
              scrolling: 'no',
              tabindex: '-1',
            })
            const preview = h('div', { class: 'plot-thumb-preview' }, iframe)
            thumb = h('div', {
              class: 'plot-thumb plot-thumb-html',
              onclick: () => openPlotViewer(all, idx),
            },
              preview,
              h('div', { class: 'plot-thumb-name' }, label)
            )
            iframeObserver.observe(thumb)
            // Scale iframe to match card width now, and whenever card resizes
            requestAnimationFrame(() => _scaleIframe(preview))
            scaleObserver.observe(preview)
          } else {
            thumb = h('div', { class: 'plot-thumb', onclick: () => openPlotViewer(all, idx) },
              h('img', { src: url, alt: name, loading: 'lazy' }),
              h('div', { class: 'plot-thumb-name' }, label)
            )
          }
          plotGrid.appendChild(thumb)
        })
      }
    } catch {}
  }
}

/* ── Plot card helpers ───────────────────────────────────────────────────────*/
function _plotLabel (filename) {
  const name = filename.replace(/\.(html|png)$/, '')
  // Per-profile: uvi_02_20260429T141417Z_dbd_profiles → "Profile · 2026-04-29 14:14"
  const profMatch = name.match(/(\d{8}T\d{6})Z?.*_profiles?$/)
  if (profMatch) {
    const dt = profMatch[1]
    return `Profile · ${dt.slice(0,4)}-${dt.slice(4,6)}-${dt.slice(6,8)} ${dt.slice(9,11)}:${dt.slice(11,13)}`
  }
  const MAP = {
    timeseries:                   'Time Series',
    qc_flag_summary:              'QC Flag Summary',
    ts_diagram:                   'T–S Diagram',
    section_temperature:          'Section: Temperature',
    section_salinity:             'Section: Salinity',
    section_oxygen_concentration: 'Section: O₂ Concentration',
  }
  return MAP[name] || name.replace(/_/g, ' ')
}


/* ── Plot lightbox viewer ────────────────────────────────────────────────────*/
let _plotViewer = null

function openPlotViewer (files, startIdx) {
  if (_plotViewer) _plotViewer.close()

  let idx = startIdx
  const total = files.length

  const overlay  = h('div', { class: 'plot-viewer-overlay' })
  const modal    = h('div', { class: 'plot-viewer-modal' })
  const btnClose = h('button', { class: 'plot-viewer-close', title: 'Close (Esc)' }, '✕')
  const counter  = h('div', { class: 'plot-viewer-counter' })
  const imgWrap  = h('div', { class: 'plot-viewer-img-wrap' })
  const label    = h('div', { class: 'plot-viewer-label' })
  const btnPrev  = h('button', { class: 'plot-viewer-nav plot-viewer-prev', title: 'Previous (←)' }, '‹')
  const btnNext  = h('button', { class: 'plot-viewer-nav plot-viewer-next', title: 'Next (→)' }, '›')

  modal.append(btnClose, counter, btnPrev, imgWrap, btnNext, label)
  overlay.appendChild(modal)
  document.body.appendChild(overlay)

  function show (i) {
    idx = (i + total) % total
    const fp     = files[idx]
    const name   = fp.split(/[\\/]/).pop()
    const isHtml = fp.endsWith('.html')
    const url    = `${API}/api/file?path=${encodeURIComponent(fp)}`

    imgWrap.innerHTML = ''
    modal.classList.toggle('plot-viewer-modal--html', isHtml)

    if (isHtml) {
      const iframe = h('iframe', {
        src: url,
        class: 'plot-viewer-iframe',
        allowfullscreen: true,
      })
      imgWrap.appendChild(iframe)
    } else {
      const img = h('img', { src: url, alt: name, class: 'plot-viewer-img' })
      imgWrap.appendChild(img)
    }

    label.textContent   = name.replace('.html', '').replace('.png', '').replace(/_/g, ' ')
    counter.textContent = `${idx + 1} / ${total}`
    btnPrev.disabled    = (total === 1)
    btnNext.disabled    = (total === 1)
  }

  function close () {
    document.removeEventListener('keydown', onKey)
    overlay.remove()
    _plotViewer = null
  }

  function onKey (e) {
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); show(idx + 1) }
    if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp'  ) { e.preventDefault(); show(idx - 1) }
    if (e.key === 'Escape') close()
  }

  btnClose.addEventListener('click', close)
  btnPrev.addEventListener('click',  () => show(idx - 1))
  btnNext.addEventListener('click',  () => show(idx + 1))
  overlay.addEventListener('click',  e => { if (e.target === overlay) close() })
  document.addEventListener('keydown', onKey)

  show(startIdx)
  _plotViewer = { close }
}

/* ── NC Variables tab ────────────────────────────────────────────────────────*/
async function renderNcVariablesTab (dep, container) {
  container.innerHTML = '<div class="vars-loading">Loading variable listings…</div>'

  let data
  try { data = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/nc-variables`) }
  catch (e) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'empty-state' }, 'Could not load variables: ' + e.message))
    return
  }

  container.innerHTML = ''

  const SECTIONS = [
    { key: 'profile',  label: 'Profile NC',          stage: '2a' },
    { key: 'traj_sci', label: 'Trajectory NC (Sci)',  stage: '2b' },
    { key: 'traj_flt', label: 'Trajectory NC (Flt)',  stage: '2c' },
    { key: 'qc',       label: 'QC Output NC',         stage: '4'  },
  ]

  const grid = h('div', { class: 'vars-grid' })

  for (const { key, label, stage } of SECTIONS) {
    const sec = data[key]
    const card = h('div', { class: 'vars-card' })

    // Card header
    const varCount = (sec && sec.variables && sec.variables.length)
      ? h('span', { class: 'nc-section-count' }, `${sec.variables.length} variables`)
      : null
    card.appendChild(h('div', { class: 'vars-card-header' },
      h('span', { class: 'vars-card-title' }, label),
      h('span', { class: 'nc-section-stage' }, `Stage ${stage}`),
      varCount,
    ))

    if (!sec || !sec.exists) {
      card.appendChild(h('div', { class: 'vars-empty' }, 'Directory not found — stage not yet run.'))
    } else if (!sec.variables || !sec.variables.length) {
      card.appendChild(h('div', { class: 'vars-empty' }, sec.error || 'No files found.'))
    } else {
      // Sample file label
      card.appendChild(h('div', { class: 'vars-sample-file' }, `Sample: ${sec.file}`))

      // Search box
      const search = h('input', {
        type: 'text', placeholder: `Search ${sec.variables.length} variables…`,
        class: 'vars-search',
      })
      card.appendChild(search)

      // Variable list
      const list = h('div', { class: 'vars-list' })

      function renderList (filter) {
        list.innerHTML = ''
        const q = filter.toLowerCase()
        const filtered = sec.variables.filter(v =>
          !q || v.name.toLowerCase().includes(q) || v.long_name.toLowerCase().includes(q)
        )
        if (!filtered.length) {
          list.appendChild(h('div', { class: 'vars-empty' }, 'No matches.'))
          return
        }
        for (const v of filtered) {
          const isQcFlag = v.name.includes('_qartod_') || v.name.includes('_flag')
          const row = h('div', { class: `vars-row${isQcFlag ? ' vars-row--qc' : ''}` },
            h('span', { class: 'vars-name', title: v.name }, v.name),
            h('span', { class: 'vars-units' }, v.units || '—'),
            h('span', { class: 'vars-longname', title: v.long_name }, v.long_name || ''),
          )
          list.appendChild(row)
        }
      }

      renderList('')
      search.addEventListener('input', () => renderList(search.value))
      card.appendChild(list)
    }

    grid.appendChild(card)
  }

  container.appendChild(grid)
}


/* ── Trajectory Map ──────────────────────────────────────────────────────────*/
/** Dynamically loads Leaflet from CDN (no-op if already loaded). */
async function loadLeaflet () {
  if (!document.getElementById('leaflet-css')) {
    const link = document.createElement('link')
    link.id   = 'leaflet-css'
    link.rel  = 'stylesheet'
    link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'
    document.head.appendChild(link)
  }
  if (!window.L) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script')
      s.src     = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
      s.onload  = resolve
      s.onerror = () => reject(new Error('Could not load Leaflet from CDN'))
      document.head.appendChild(s)
    })
  }
  return window.L
}

/**
 * Render the interactive glider path map.
 * Returns the Leaflet map object so the caller can call invalidateSize() on tab switch.
 */
async function renderTrajectoryMap (dep, container) {
  container.innerHTML = ''
  container.appendChild(h('div', { class: 'traj-loading' },
    '⏳ Loading GPS surfacings from ' + dep + '…',
    h('br', {}),
    h('span', { style: 'font-size:11px;color:#546e7a' },
      'First load reads all trajectory NC files and may take a minute. Subsequent loads are instant.')
  ))

  let surf
  try {
    surf = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/surfacings`)
  } catch (e) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'empty-state' }, 'Could not load surfacing data: ' + e.message))
    return null
  }

  container.innerHTML = ''

  const lats  = surf.lats  || []
  const lons  = surf.lons  || []
  const times = surf.times || []

  // ── Header bar ─────────────────────────────────────────────────────────────
  const kmzBtn = h('button', { class: 'btn-secondary',
    onclick: () => {
      const a = document.createElement('a')
      a.href     = `${API}/api/deployment/${encodeURIComponent(dep)}/kmz`
      a.download = `${dep}_glider_track.kmz`
      document.body.appendChild(a); a.click(); a.remove()
    }
  }, '⬇ Download KMZ')

  const mapImgBtn = h('button', { class: 'btn-secondary',
    onclick: async () => {
      mapImgBtn.disabled = true
      mapImgBtn.textContent = 'Generating…'
      try {
        await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/generate-map-image`, { method: 'POST' })
        mapImgBtn.textContent = '✓ Map Image Saved'
      } catch (e) {
        mapImgBtn.textContent = 'Failed'
        alert('Could not generate map image: ' + e.message)
      }
    }
  }, '📷 Save Map Image')

  const headerBar = h('div', { class: 'traj-header-bar' },
    h('span', { class: 'traj-stat-chip' }, `${surf.count} GPS surfacings`),
    h('span', { class: 'traj-stat-chip' }, `${(surf.n_records || 0).toLocaleString()} records`),
    h('span', { class: 'traj-stat-chip' }, `${surf.n_files} traj files`),
    kmzBtn,
    mapImgBtn,
  )
  container.appendChild(headerBar)

  if (!lats.length) {
    container.appendChild(h('div', { class: 'empty-state' },
      'No GPS surfacings found in trajectory data.'))
    return null
  }

  // ── Map container ──────────────────────────────────────────────────────────
  const mapDiv = h('div', { id: 'traj-map-canvas', class: 'traj-map-canvas' })
  container.appendChild(mapDiv)

  let leafletMap = null
  try {
    const L = await loadLeaflet()

    leafletMap = L.map(mapDiv)
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
      maxZoom: 19,
    }).addTo(leafletMap)

    // Draw track polyline
    const latLngs = lats.map((lat, i) => [lat, lons[i]])
    L.polyline(latLngs, { color: '#f97316', weight: 2, opacity: 0.8 }).addTo(leafletMap)

    // Markers for each surfacing (subsample if many — only show every Nth)
    const MAX_MARKERS = 300
    const step = Math.max(1, Math.ceil(lats.length / MAX_MARKERS))
    lats.forEach((lat, i) => {
      if (i % step !== 0 && i !== lats.length - 1) return
      const isFirst = (i === 0)
      const isLast  = (i === lats.length - 1)
      const color   = isFirst ? '#22c55e' : isLast ? '#ef4444' : '#3b82f6'
      const size    = (isFirst || isLast) ? 14 : 10
      const icon = L.divIcon({
        className: '',
        html: `<div style="background:${color};border:2px solid rgba(255,255,255,0.9);
                           border-radius:50%;width:${size}px;height:${size}px;
                           box-shadow:0 1px 3px rgba(0,0,0,0.4)"></div>`,
        iconSize:   [size, size],
        iconAnchor: [size / 2, size / 2],
      })
      // bindPopup accepts a DOM element directly, so build it via h() instead
      // of an HTML string -- times[i] is parsed trajectory-NC GPS data, not
      // meant to ever be treated as markup.
      const popupEl = h('span', {}, h('b', {}, `Surfacing ${i + 1} of ${lats.length}`), h('br'), times[i] || '')
      L.marker([lat, lons[i]], { icon }).bindPopup(popupEl).addTo(leafletMap)
    })

    // Fit to track bounds
    leafletMap.fitBounds(latLngs, { padding: [20, 20] })

  } catch (e) {
    const errEl = h('div', { class: 'empty-state' }, 'Map error: ' + e.message)
    container.appendChild(errEl)
  }

  return leafletMap
}


/* ── Flight Stats ────────────────────────────────────────────────────────────*/
/** Dynamically loads Plotly from CDN (no-op if already loaded). */
async function loadPlotlyLib () {
  if (window.Plotly) return window.Plotly
  await new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src     = 'https://cdn.plot.ly/plotly-2.35.2.min.js'
    s.onload  = resolve
    s.onerror = () => reject(new Error('Could not load Plotly from CDN'))
    document.head.appendChild(s)
  })
  return window.Plotly
}

function _statCard (label, value) {
  return h('div', { class: 'flight-stat-card' },
    h('div', { class: 'flight-stat-value' }, value != null ? String(value) : 'N/A'),
    h('div', { class: 'flight-stat-label' }, label),
  )
}

async function renderFlightStats (dep, container) {
  container.innerHTML = ''
  container.appendChild(h('div', { class: 'traj-loading' },
    '⏳ Loading trajectory data…'))

  let trajData
  try {
    trajData = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/trajectory-report`)
  } catch (e) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'empty-state' }, 'Could not load trajectory data: ' + e.message))
    return
  }

  container.innerHTML = ''

  const { timeseries, stats } = trajData
  const times = timeseries.time

  // ── Summary stat cards ─────────────────────────────────────────────────────
  const fmtV = (v, d = 2) => v == null ? 'N/A' : Number(v).toFixed(d)

  container.appendChild(h('div', { class: 'flight-stats-grid' },
    _statCard('Records',        stats.n_records.toLocaleString()),
    _statCard('Battery Min',    fmtV(stats.battery_v.min, 2) + ' V'),
    _statCard('Battery Max',    fmtV(stats.battery_v.max, 2) + ' V'),
    _statCard('Amp-Hours Used', stats.total_ah_used != null ? fmtV(stats.total_ah_used, 1) + ' Ah' : 'N/A'),
    _statCard('Mean Pitch',     fmtV(stats.pitch_deg.mean, 1) + '°'),
    _statCard('Mean Roll',      fmtV(stats.roll_deg.mean,  1) + '°'),
    _statCard('Mean Vacuum',    fmtV(stats.vacuum_inhg.mean, 2) + ' inHg'),
  ))

  const flightRptBtn = h('button', { class: 'btn-secondary',
    onclick: () => {
      const a = document.createElement('a')
      a.href = `${API}/api/deployment/${encodeURIComponent(dep)}/flight-stats-report`
      a.download = `flight_stats_${dep}.html`
      document.body.appendChild(a); a.click(); a.remove()
    }
  }, '⬇ Download Report')

  const flightHeaderBar = h('div', { class: 'traj-header-bar' }, flightRptBtn)
  container.appendChild(flightHeaderBar)

  const chartsWrap = h('div', { class: 'flight-charts' })
  container.appendChild(chartsWrap)

  // ── Shared Plotly theme pieces ─────────────────────────────────────────────
  // NOTE: never spread xaxis into yaxis — xaxis has type:'date' which breaks
  // numeric y-axes by making Plotly interpret every value as a timestamp.
  const PAPER  = '#131c27'
  const PLOT   = '#0f1720'
  const GRID   = '#1e2d40'
  const FONT   = { color: '#9ab4cc', size: 11 }

  const XDATE  = { type: 'date',   gridcolor: GRID, zerolinecolor: GRID, color: '#9ab4cc' }
  const YLIN   = { type: 'linear', gridcolor: GRID, zerolinecolor: GRID, color: '#9ab4cc' }

  // Range-selector button styling (used in pitch/roll chart)
  const RS_STYLE = {
    bgcolor: '#1a2840', activecolor: '#2d4a6a',
    bordercolor: '#2d4a6a', borderwidth: 1,
    font: { color: '#9ab4cc', size: 10 },
  }

  const BASE = {
    paper_bgcolor: PAPER,
    plot_bgcolor:  PLOT,
    font:          FONT,
    margin:        { l: 60, r: 16, t: 8, b: 52 },
    showlegend:    true,
    // Keep legend inside the plot area so the chart wrapper's overflow:hidden
    // doesn't clip it (y:1.12 would place it above the canvas and hide it).
    legend: {
      orientation: 'h',
      x: 0.01, xanchor: 'left',
      y: 0.99, yanchor: 'top',
      bgcolor: 'rgba(13,20,32,0.75)',
      bordercolor: '#1e2d40',
      borderwidth: 1,
      font: { size: 10 },
    },
  }

  function addChart (title, subtitle) {
    const div     = h('div', { class: 'flight-chart' })
    const titleEl = h('div', { class: 'flight-chart-title' }, title)
    if (subtitle) titleEl.appendChild(h('span', { class: 'flight-chart-subtitle' }, subtitle))
    chartsWrap.appendChild(h('div', { class: 'flight-chart-wrap' }, titleEl, div))
    return div
  }

  // ── Per-day average helper (runs client-side from subsampled data) ─────────
  function dailyAvg (xs, ys) {
    const map = {}
    for (let i = 0; i < xs.length; i++) {
      if (!xs[i] || ys[i] == null) continue
      const day = xs[i].slice(0, 10)           // 'YYYY-MM-DD'
      if (!map[day]) map[day] = { sum: 0, n: 0 }
      map[day].sum += ys[i]; map[day].n++
    }
    const days = Object.keys(map).sort()
    return {
      x: days.map(d => d + 'T12:00:00Z'),      // plot at noon each day
      y: days.map(d => +(map[d].sum / map[d].n).toFixed(3)),
    }
  }

  try {
    const Plotly = await loadPlotlyLib()
    const cfg    = { responsive: true, displayModeBar: false }

    // ── 1. Pitch & Roll — with 1-day / 3-day / 7-day / All range buttons ────
    const prDiv = addChart('PITCH & ROLL (DEGREES)')
    Plotly.newPlot(prDiv, [
      { x: times, y: timeseries.pitch, name: 'Pitch', type: 'scattergl', mode: 'lines',
        line: { color: '#60a5fa', width: 1 } },
      { x: times, y: timeseries.roll,  name: 'Roll',  type: 'scattergl', mode: 'lines',
        line: { color: '#f97316', width: 1 } },
    ], {
      ...BASE,
      xaxis: {
        ...XDATE,
        rangeselector: {
          ...RS_STYLE,
          buttons: [
            { count: 1, label: '1d', step: 'day',  stepmode: 'backward' },
            { count: 3, label: '3d', step: 'day',  stepmode: 'backward' },
            { count: 7, label: '7d', step: 'day',  stepmode: 'backward' },
            { step: 'all', label: 'All' },
          ],
        },
      },
      yaxis: { ...YLIN, title: { text: 'degrees', standoff: 4 } },
    }, cfg)

    // ── 2. Battery Voltage — full mission thin line + bold per-day average ───
    // Use scatter (not scattergl) for both traces so Plotly's autorange sees all
    // data; also pin the y-axis to the true min/max from stats so the raw dips
    // (e.g. 14.1 V during dives) are never clipped.
    const dayBatt = dailyAvg(times, timeseries.battery)
    const battDiv = addChart('BATTERY VOLTAGE — FULL MISSION WITH DAILY AVERAGE')
    const bPad = 0.15
    const bMin = stats.battery_v.min != null ? stats.battery_v.min - bPad : null
    const bMax = stats.battery_v.max != null ? stats.battery_v.max + bPad : null
    Plotly.newPlot(battDiv, [
      { x: times,     y: timeseries.battery, name: 'Raw readings',
        type: 'scatter', mode: 'lines',
        line: { color: '#4ade80', width: 0.7 }, opacity: 0.55 },
      { x: dayBatt.x, y: dayBatt.y,          name: 'Daily average',
        type: 'scatter', mode: 'lines+markers',
        line: { color: '#86efac', width: 2.5 },
        marker: { color: '#86efac', size: 6, symbol: 'circle' } },
    ], {
      ...BASE,
      xaxis: XDATE,
      yaxis: {
        ...YLIN,
        title: { text: 'V', standoff: 4 },
        ...(bMin != null ? { range: [bMin, bMax], autorange: false } : {}),
      },
    }, cfg)

    // ── 3. Cumulative Amp-Hours — start/end labels + daily draw bars ─────────
    // Find first & last non-null readings from the subsampled timeseries
    let startAh = null, endAh = null
    for (let i = 0; i < timeseries.coulomb.length; i++)
      if (timeseries.coulomb[i] != null && startAh === null) startAh = timeseries.coulomb[i]
    for (let i = timeseries.coulomb.length - 1; i >= 0; i--)
      if (timeseries.coulomb[i] != null) { endAh = timeseries.coulomb[i]; break }

    // Daily draw: difference between last reading of each consecutive pair of days
    const ahDailyDraw = (() => {
      const last = {}   // day -> last Ah value seen that day
      for (let i = 0; i < times.length; i++) {
        if (!times[i] || timeseries.coulomb[i] == null) continue
        last[times[i].slice(0, 10)] = timeseries.coulomb[i]
      }
      const days = Object.keys(last).sort()
      const x = [], y = []
      for (let i = 1; i < days.length; i++) {
        const delta = +(last[days[i]] - last[days[i - 1]]).toFixed(2)
        if (delta > 0) { x.push(days[i] + 'T12:00:00Z'); y.push(delta) }
      }
      return { x, y }
    })()

    const avgDraw = ahDailyDraw.y.length
      ? (ahDailyDraw.y.reduce((a, b) => a + b, 0) / ahDailyDraw.y.length).toFixed(2)
      : null
    const ahTotal = (startAh != null && endAh != null)
      ? (endAh - startAh).toFixed(1) : null

    const ahSubtitle = [
      startAh != null ? `Start ${startAh.toFixed(1)} Ah` : null,
      endAh   != null ? `End ${endAh.toFixed(1)} Ah`     : null,
      ahTotal != null ? `Total used ${ahTotal} Ah`        : null,
      avgDraw != null ? `Avg ${avgDraw} Ah/day`           : null,
    ].filter(Boolean).join('  ·  ')

    const ahDiv = addChart('CUMULATIVE AMP-HOURS', ahSubtitle)
    Plotly.newPlot(ahDiv, [
      { x: times,           y: timeseries.coulomb, name: 'Cumulative Ah',
        type: 'scatter', mode: 'lines',
        line: { color: '#facc15', width: 1.5 }, yaxis: 'y' },
      { x: ahDailyDraw.x,  y: ahDailyDraw.y,      name: 'Daily draw',
        type: 'bar',
        marker: { color: 'rgba(251,191,36,0.3)', line: { color: 'rgba(251,191,36,0.6)', width: 1 } },
        yaxis: 'y2' },
    ], {
      ...BASE,
      xaxis:  XDATE,
      yaxis:  { ...YLIN, title: { text: 'Ah (cumul.)', standoff: 4 } },
      yaxis2: { ...YLIN, title: { text: 'Ah/day', standoff: 4 },
                overlaying: 'y', side: 'right', showgrid: false,
                rangemode: 'tozero' },
      barmode: 'overlay',
      margin:  { ...BASE.margin, r: 70 },   // extra right margin for y2 label
    }, cfg)

    // ── 4. Vacuum (hull integrity) — full mission + daily average ────────────
    const dayVac = dailyAvg(times, timeseries.vacuum)
    const vacDiv = addChart('VACUUM (inHg) — HULL INTEGRITY WITH DAILY AVERAGE')
    const vPad = 0.05
    const vMin = stats.vacuum_inhg.min != null ? stats.vacuum_inhg.min - vPad : null
    const vMax = stats.vacuum_inhg.max != null ? stats.vacuum_inhg.max + vPad : null
    Plotly.newPlot(vacDiv, [
      { x: times,     y: timeseries.vacuum, name: 'Raw readings',
        type: 'scatter', mode: 'lines',
        line: { color: '#c084fc', width: 0.7 }, opacity: 0.55 },
      { x: dayVac.x,  y: dayVac.y,          name: 'Daily average',
        type: 'scatter', mode: 'lines+markers',
        line: { color: '#d8b4fe', width: 2.5 },
        marker: { color: '#d8b4fe', size: 6, symbol: 'circle' } },
    ], {
      ...BASE,
      xaxis: XDATE,
      yaxis: {
        ...YLIN,
        title: { text: 'inHg', standoff: 4 },
        ...(vMin != null ? { range: [vMin, vMax], autorange: false } : {}),
      },
    }, cfg)

  } catch (e) {
    chartsWrap.appendChild(h('div', { class: 'empty-state' }, 'Could not render charts: ' + e.message))
  }
}


async function renderAdcpReportTab (dep, container) {
  container.innerHTML = ''
  container.style.padding = '16px 20px'

  if (!dep) {
    container.appendChild(h('div', { class: 'empty-state' }, 'Select a deployment.'))
    return
  }

  let statusR
  try {
    statusR = await apiFetch(`/api/adcp/status?dep=${encodeURIComponent(dep)}&is_test=false`)
  } catch (e) {
    container.appendChild(h('div', { class: 'empty-state' }, 'Could not load AD2CP status: ' + e.message))
    return
  }

  const shared = statusR.shared || {}
  const runs   = statusR.runs   || {}
  const baseQs = `dep=${encodeURIComponent(dep)}&is_test=false`

  // Status summary row
  const sharedDone = Object.values(shared).filter(s => s.done).length
  const sharedTotal = Object.keys(shared).length
  const summaryEl = h('div', { class: 'adcp-report-summary' },
    h('span', { class: 'adcp-report-chip' }, `Shared stages: ${sharedDone}/${sharedTotal}`),
    h('span', { class: 'adcp-report-chip' }, `Runs: ${Object.keys(runs).length}`),
  )
  container.appendChild(summaryEl)

  const rptBtn = h('a', {
    class: 'btn-secondary btn-sm',
    href: `${API}/api/adcp/report-html?dep=${encodeURIComponent(dep)}`,
  }, '⬇ Download AD2CP Report')
  container.appendChild(h('div', { style: 'margin-bottom: 16px' }, rptBtn))

  if (Object.keys(runs).length === 0) {
    container.appendChild(h('div', { class: 'empty-state', style: 'margin-top:32px' },
      h('div', { class: 'reports-empty-icon' }, '📡'),
      h('div', { class: 'reports-empty-title' }, 'No AD2CP runs yet'),
      h('div', { class: 'reports-empty-sub'   }, 'Run the AD2CP processing stages to generate plots.'),
    ))
    return
  }

  // Render each run
  for (const [runLabel, runStatus] of Object.entries(runs)) {
    const plotsGenerated = runStatus.generate_plots?.done
    const runQs = `${baseQs}&run=${encodeURIComponent(runLabel)}`

    const runEl = h('div', { class: 'adcp-report-run' })
    const titleRow = h('div', { class: 'adcp-report-run-title' },
      h('span', {}, runLabel),
      h('a', { class: 'btn-secondary btn-sm',
        href: `${API}/api/adcp/export?${runQs}` }, '⬇ Export ZIP'),
    )
    runEl.appendChild(titleRow)

    const reportStagePlots = [
      { stage: 'bias_correction', plots: ['bias_diagnostic.png'] },
      { stage: 'generate_plots',  plots: ['velocity_sections.png', 'speed_section.png',
                                           'shear_sections.png', 'backscatter.png',
                                           'correlation.png', 'beam_amplitude.png', 'snr.png',
                                           'dac_timeseries.png', 'dac_map_cartopy.png'] },
      { stage: 'tidal_filter',    plots: ['tidal_filter.png'] },
      { stage: 'diagnostics',     plots: ['eke.png', 'transport.png', 'richardson_number.png'] },
    ]
    const reportPlots = reportStagePlots.flatMap(({ stage, plots }) =>
      runStatus[stage]?.done ? plots : [])

    if (reportPlots.length > 0) {
      const plotsArea = h('div', { class: 'adcp-plots-area', style: 'margin-top:12px' })
      reportPlots.forEach(fn => {
        const url   = `${API}/api/adcp/plot/${encodeURIComponent(fn)}?${runQs}`
        const dlUrl = `${API}/api/adcp/download/${encodeURIComponent(fn)}?${runQs}`
        const img   = h('img', { class: 'adcp-plot-img', src: url, alt: fn })
        img.onerror = () => img.parentElement.remove()
        const foot  = h('div', { class: 'adcp-plot-footer' },
          h('span', {}, fn),
          h('a', { class: 'btn-secondary btn-sm', href: dlUrl }, '⬇ Download'),
        )
        plotsArea.appendChild(h('div', { class: 'adcp-plot-wrap' }, img, foot))
      })
      runEl.appendChild(plotsArea)
    } else {
      runEl.appendChild(h('div', { class: 'adcp-files-missing', style: 'margin-top:8px' },
        'Plots not yet generated for this run.'))
    }

    container.appendChild(runEl)
  }
}


function switchTab (e) {
  const tabName = e.currentTarget.dataset.tab
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName))
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`))
}

/* ── Metadata page ───────────────────────────────────────────────────────────*/
async function renderMetadata (selectRel) {
  const dep  = state.deployment
  const page = document.getElementById('page-metadata')
  page.innerHTML = ''

  page.appendChild(h('div', { class: 'page-header' },
    h('div', { style: 'display:flex;align-items:flex-start;justify-content:space-between' },
      h('div', {},
        h('h1', {}, 'Metadata'),
        h('p',  {}, dep ? `Config files for ${dep}` : 'Select a deployment.')),
      dep ? h('div', { style: 'display:flex;gap:8px;margin-top:4px' },
        h('button', { class: 'btn-secondary',
          onclick: () => showManageSensorSets(dep) }, 'Manage Sensor Sets'),
        h('button', { class: 'btn-secondary',
          onclick: () => showGenerateConfigModal(dep) }, 'Generate Config'),
        h('button', { class: 'btn-primary',
          onclick: () => showBuildSensorDefs(dep) }, 'Build Sensor Defs')
      ) : null
    )
  ))

  if (!dep) return

  let md
  try { md = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata`) }
  catch { page.appendChild(h('div', { class: 'empty-state' }, 'Could not load metadata.')); return }

  const entries = [
    ...Object.entries(md.files || {}),
    // Files that failed to parse as JSON -- kept visible (not silently dropped)
    // and tagged so the list/viewer can flag them instead of pretending they're
    // valid content. Carries the raw text through so it can be repaired in place.
    ...Object.entries(md.errors || {}).map(([rel, err]) =>
      [rel, { __metaError__: true, message: err.message, raw: err.raw }]),
  ]

  if (!entries.length) {
    page.appendChild(_metaEmptyPanel(dep, md.config_dir))
    return
  }

  const fileList    = h('div', { class: 'meta-file-list' })
  const resizer     = h('div', { class: 'meta-resize-handle' })
  const content     = h('div', { class: 'meta-content' })
  const grid        = h('div', { class: 'meta-grid' }, fileList, resizer, content)
  page.appendChild(grid)

  // Restore saved width
  const savedWidth = parseInt(localStorage.getItem('metaListWidth'), 10)
  fileList.style.width = (savedWidth && savedWidth >= 160 && savedWidth <= 520 ? savedWidth : 240) + 'px'

  // Drag-to-resize
  resizer.addEventListener('mousedown', e => {
    e.preventDefault()
    resizer.classList.add('dragging')
    const startX     = e.clientX
    const startWidth = fileList.getBoundingClientRect().width
    const onMove = ev => {
      const w = Math.min(520, Math.max(160, startWidth + ev.clientX - startX))
      fileList.style.width = w + 'px'
    }
    const onUp = ev => {
      resizer.classList.remove('dragging')
      const w = Math.min(520, Math.max(160, startWidth + ev.clientX - startX))
      localStorage.setItem('metaListWidth', w)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup',   onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup',   onUp)
  })

  // Group entries by top-level subdirectory
  const groupLabels = { science_profile: 'Profile', science_trajectory: 'Trajectory (Science)', trajectory_flight: 'Trajectory (Flight)' }
  const GROUP_ORDER = ['science_profile', 'science_trajectory', 'trajectory_flight']
  const groups = {}
  entries.forEach(([rel, json]) => {
    const group = rel.split(/[\\/]/)[0]
    if (!groups[group]) groups[group] = []
    groups[group].push([rel, json])
  })

  // All other deployments available as copy sources
  const glider    = dep.split('-')[0]
  const allDeps   = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const otherDeps = allDeps.filter(d => d !== dep)

  let firstEntry = null
  GROUP_ORDER.forEach(group => {
    const items = groups[group] || []

    const sectionHdr = h('div', { class: 'meta-section-header' },
      h('span', {}, groupLabels[group] || group))

    if (otherDeps.length) {
      const cpFileBtn = h('button', {
        class: 'meta-section-copy-btn',
        title: 'Copy a specific file from another deployment into this section…',
        onclick: e => {
          e.stopPropagation()
          _metaCopyFileFromDepModal(dep, glider, group, renderMetadata)
        },
      }, 'Copy file from Deployment')
      sectionHdr.appendChild(cpFileBtn)
    }

    fileList.appendChild(sectionHdr)

    if (!items.length) {
      fileList.appendChild(h('div', { class: 'meta-file-empty' }, 'No files yet'))
    }

    items.forEach(([rel, fileJson]) => {
      const filename = rel.split(/[\\/]/).pop()
      const isFirst  = firstEntry === null
      const isError  = !!(fileJson && fileJson.__metaError__)
      if (isFirst) firstEntry = [rel, fileJson]

      // Single ⋯ menu button (shown on hover)
      const menuBtn = h('button', {
        class: 'meta-file-menu-btn',
        title: 'File actions…',
        onclick: e => {
          e.stopPropagation()
          const anchor = e.currentTarget
          const actions = []
          if (fileJson !== null && !isError) {
            actions.push({ label: 'Copy to section…', action: () => _metaCopySectionPopover(anchor, dep, group, filename, fileJson, renderMetadata) })
          }
          actions.push({ label: 'Import from disk…', action: () => _metaImportFileReplace(dep, rel, fileList, content) })
          if (otherDeps.length) {
            actions.push({ label: 'Copy from deployment…', action: async () => _metaCopyFromPrev(dep, glider, rel, fileList, content) })
          }
          actions.push({ label: 'Delete', danger: true, action: async () => {
            if (!confirm(`Delete ${filename} from ${groupLabels[group] || group}?\n\nThis cannot be undone.`)) return
            try {
              await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/delete`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ path: rel }),
              })
              await renderMetadata()
            } catch (ex) {
              alert('Delete failed: ' + ex.message)
            }
          }})
          _metaFileMenuPopover(e.currentTarget, actions)
        }
      }, '⋯')

      const item = h('div', {
        class: 'meta-file-item' + (isFirst ? ' active' : '') + (isError ? ' meta-file-item--error' : ''),
        'data-rel': rel,
        title: isError ? `Invalid JSON: ${fileJson.message}` : '',
        onclick: () => selectMetaFile(rel, fileJson, fileList, content, dep),
      }, h('span', { class: 'meta-file-name' }, (isError ? '⚠ ' : '') + filename), menuBtn)
      fileList.appendChild(item)
    })
  })
  // Add-file bar at the bottom of the file list
  fileList.appendChild(_metaAddBar(dep))

  // Re-select whichever file the caller asked for (e.g. the one just saved/imported/
  // copied), falling back to the first entry -- keeps the view from jumping away from
  // what the user was just working on after a refresh that re-fetches from the server.
  const toSelect = (selectRel && entries.find(([rel]) => rel === selectRel)) || firstEntry
  if (toSelect) selectMetaFile(toSelect[0], toSelect[1], fileList, content, dep)
}

/* Replace a metadata file with one chosen from disk ─────────────────────────*/
function _metaImportFileReplace (dep, rel, fileList, content) {
  const filename = rel.split(/[\\/]/).pop()

  // Invisible file input — triggers native OS file picker
  const input = document.createElement('input')
  input.type    = 'file'
  input.accept  = '.json'
  input.style.display = 'none'
  document.body.appendChild(input)

  input.onchange = async () => {
    const file = input.files[0]
    input.remove()
    if (!file) return

    // Read content in the renderer — works in both Electron and browser
    const text = await new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload  = e  => resolve(e.target.result)
      reader.onerror = () => reject(new Error('Could not read file'))
      reader.readAsText(file)
    }).catch(err => { alert(err.message); return null })
    if (text === null) return

    // Validate JSON before touching anything
    let parsed
    try { parsed = JSON.parse(text) }
    catch (e) { alert(`"${file.name}" is not valid JSON:\n${e.message}`); return }

    if (!confirm(`Replace ${filename} with "${file.name}"?\n\nThis will overwrite the current file.`))
      return

    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ path: rel, content: JSON.stringify(parsed, null, 2) }),
      })
      // Full refresh from the server (re-selecting this file) -- a local-only update
      // here left the file list's stale closure-captured snapshot out of sync, so
      // switching to another file and back would silently show the pre-import content.
      await renderMetadata(rel)
      // Flash the row green (re-query -- renderMetadata() rebuilds the file list, so
      // the original `fileList`/`row` references above are now stale)
      const row = document.getElementById('page-metadata').querySelector('.meta-file-item.active')
      if (row) {
        row.classList.add('meta-file-copied')
        setTimeout(() => row.classList.remove('meta-file-copied'), 1500)
      }
    } catch (e) {
      alert('Save failed: ' + e.message)
    }
  }

  input.oncancel = () => input.remove()
  input.click()
}

/* ── Metadata empty-state panel + add-file bar ───────────────────────────────*/
function _metaEmptyPanel (dep, configDir) {
  const allDeps   = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const otherDeps = allDeps.filter(d => d !== dep)
  const glider    = dep.split('-')[0]

  return h('div', { class: 'meta-empty-panel' },
    h('div', { class: 'meta-empty-title' }, 'No config files yet'),
    h('div', { class: 'meta-empty-path' }, configDir),
    h('div', { class: 'meta-empty-actions' },
      otherDeps.length
        ? h('button', { class: 'btn-secondary', onclick: () => _metaCopyAllFromDep(dep, glider) },
            'Copy all from deployment')
        : null,
      h('button', { class: 'btn-secondary', onclick: () => _metaImportJsonFiles(dep) },
        '↑  Import JSON files'),
      h('button', { class: 'btn-primary', onclick: () => _metaNewFileModal(dep) },
        '+  Create new file')))
}

function _metaAddBar (dep) {
  const allDeps   = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const otherDeps = allDeps.filter(d => d !== dep)
  const glider    = dep.split('-')[0]

  return h('div', { class: 'meta-add-bar' },
    otherDeps.length
      ? h('button', { class: 'meta-add-action', title: 'Copy all config files from another deployment',
          onclick: () => _metaCopyAllFromDep(dep, glider) }, 'Copy all from dep')
      : null,
    h('button', { class: 'meta-add-action', title: 'Import JSON files from disk',
      onclick: () => _metaImportJsonFiles(dep) }, '↑ Import files'),
    h('button', { class: 'meta-add-action', title: 'Create a new blank config file',
      onclick: () => _metaNewFileModal(dep) }, '+ New file'))
}

/* Per-file ⋯ actions menu ───────────────────────────────────────────────────*/
function _metaFileMenuPopover (anchor, actions) {
  document.querySelectorAll('.meta-file-menu-popover').forEach(el => el.remove())

  const pop = document.createElement('div')
  pop.className = 'meta-file-menu-popover'

  actions.forEach(({ label, danger, action }) => {
    const item = document.createElement('button')
    item.className = 'meta-file-menu-item' + (danger ? ' meta-file-menu-item--danger' : '')
    item.textContent = label
    item.addEventListener('click', e => {
      e.stopPropagation()
      pop.remove()
      document.removeEventListener('mousedown', dismiss)
      action()
    })
    pop.appendChild(item)
  })

  document.body.appendChild(pop)
  const rect = anchor.getBoundingClientRect()
  pop.style.top  = (rect.bottom + 4) + 'px'
  pop.style.left = Math.min(rect.left, window.innerWidth - pop.offsetWidth - 8) + 'px'

  function dismiss (e) { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener('mousedown', dismiss) } }
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0)
}

/* Copy a file to another section within the same deployment ─────────────────*/
function _metaCopySectionPopover (anchor, dep, srcGroup, filename, fileJson, onDone) {
  document.querySelectorAll('.meta-section-popover').forEach(el => el.remove())

  const SECTION_LABELS = {
    science_profile:   'Profile',
    science_trajectory:'Trajectory (Science)',
    trajectory_flight: 'Trajectory (Flight)',
  }
  const targets = Object.keys(SECTION_LABELS).filter(g => g !== srcGroup)

  const rect    = anchor.getBoundingClientRect()
  const popover = h('div', { class: 'meta-section-popover' })
  popover.style.top  = `${rect.bottom + window.scrollY + 4}px`
  popover.style.left = `${rect.left + window.scrollX}px`

  targets.forEach(targetGroup => {
    const opt = h('button', { class: 'meta-section-popover-item',
      onclick: async e => {
        e.stopPropagation()
        popover.remove()
        const targetPath = `${targetGroup}/${filename}`
        try {
          await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ path: targetPath, content: JSON.stringify(fileJson, null, 2) }),
          })
          await onDone()
        } catch (ex) {
          alert('Copy failed: ' + ex.message)
        }
      }
    }, `→ ${SECTION_LABELS[targetGroup]}`)
    popover.appendChild(opt)
  })

  document.body.appendChild(popover)
  const close = e => { if (!popover.contains(e.target)) { popover.remove(); document.removeEventListener('click', close) } }
  setTimeout(() => document.addEventListener('click', close), 0)
}

/* Copy ALL config files from another deployment into this one ────────────────*/
async function _metaCopyAllFromDep (dep, glider) {
  const allDeps = Array.from(document.getElementById('dep-select').options)
    .map(o => o.value).filter(Boolean).filter(d => d !== dep)
  if (!allDeps.length) { alert('No other deployments available.'); return }

  const srcDep = await _pickSourceDeployment(dep, allDeps, glider, 'all config files')
  if (!srcDep) return

  try {
    const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/copy-all`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ source: srcDep }),
    })
    if (r.ok) await renderMetadata()
  } catch (e) {
    alert('Copy failed: ' + e.message)
  }
}

/* Import JSON files from disk — two side-by-side drop zones ─────────────────*/
function _metaImportJsonFiles (dep) {
  // Per-subdir file lists
  const fileItems = { science_profile: [], science_trajectory: [], trajectory_flight: [] }
  const renderFns = {}   // subdir → renderList fn, populated by makeZone

  function makeZone (subdir) {
    const items  = fileItems[subdir]
    const listEl  = h('div', { class: 'import-file-list' })
    const errorEl = h('div', { class: 'import-error' })

    function renderList () {
      listEl.innerHTML = ''
      items.forEach(item => {
        const icon = { queued: '…', copying: '…', done: '✓', error: '✗' }[item.status] || '?'
        listEl.appendChild(h('div', { class: 'import-file-item' },
          h('span', { class: `import-status import-status-${item.status}` }, icon),
          h('span', { class: 'import-fname' }, item.name),
          item.msg ? h('span', { class: 'import-item-msg' }, item.msg) : null))
      })
    }
    renderFns[subdir] = renderList

    function handleFiles (files) {
      errorEl.textContent = ''
      const valid   = files.filter(f => f.name.toLowerCase().endsWith('.json'))
      const skipped = files.length - valid.length
      if (skipped) errorEl.textContent = `${skipped} skipped (not .json)`
      valid.forEach(f => {
        if (!items.find(i => i.name === f.name))
          items.push({ name: f.name, path: f.path || '', size: f.size, subdir, status: 'queued' })
      })
      renderList()
    }

    const fileInput = h('input', { type: 'file', multiple: true, accept: '.json', style: 'display:none',
      onchange: e => { handleFiles(Array.from(e.target.files)); e.target.value = '' }})

    const dropzone = h('div', { class: 'import-dropzone import-dropzone-sm' },
      h('div', { class: 'import-drop-icon' }, '↑'),
      h('div', { class: 'import-drop-text' }, 'Drop .json files here'),
      h('label', { class: 'btn-secondary import-browse-btn' }, 'Browse…', fileInput))

    dropzone.addEventListener('dragover',  e => { e.preventDefault(); dropzone.classList.add('drag-over') })
    dropzone.addEventListener('dragleave', e => { if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('drag-over') })
    dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('drag-over'); handleFiles(Array.from(e.dataTransfer.files)) })

    return h('div', { class: 'import-zone' },
      h('div', { class: 'import-zone-label' }, subdir),
      dropzone, errorEl, listEl)
  }

  const statusEl  = h('div', { class: 'modal-status' })
  const importBtn = h('button', { class: 'btn-success' }, 'Import')

  importBtn.onclick = async () => {
    const allItems = [...fileItems.science_profile, ...fileItems.science_trajectory]
    const queued   = allItems.filter(i => i.status === 'queued')
    if (!queued.length) { statusEl.textContent = 'Drop files into one or both zones first.'; statusEl.style.color = '#e74c3c'; return }

    const paths = queued.map(i => i.path).filter(Boolean)
    if (!paths.length) {
      queued.forEach(i => { i.status = 'error'; i.msg = 'Open in Electron app to import files' })
      Object.values(renderFns).forEach(fn => fn()); return
    }

    queued.forEach(i => { i.status = 'copying' })
    Object.values(renderFns).forEach(fn => fn())
    importBtn.disabled = true

    try {
      const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/import-files`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ files: queued.map(i => ({ path: i.path, subdir: i.subdir })) }),
      })
      r.results.forEach(res => {
        const item = allItems.find(i => i.path === res.path)
        if (item) { item.status = res.ok ? 'done' : 'error'; if (!res.ok) item.msg = res.error }
      })
      Object.values(renderFns).forEach(fn => fn())
      const doneCount = allItems.filter(i => i.status === 'done').length
      if (doneCount) {
        statusEl.textContent = `${doneCount} file(s) imported.`
        statusEl.style.color = '#7ec8a0'
        importBtn.textContent = 'Close & Refresh'
        importBtn.disabled = false
        importBtn.onclick = async () => { closeModal(); await renderMetadata() }
      } else {
        importBtn.disabled = false
      }
    } catch (e) {
      queued.forEach(i => { if (i.status === 'copying') { i.status = 'error'; i.msg = e.message } })
      Object.values(renderFns).forEach(fn => fn())
      importBtn.disabled = false
    }
  }

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'Import JSON Config Files'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' },
        h('div', { class: 'import-zones' },
          makeZone('science_profile'),
          makeZone('science_trajectory'),
          makeZone('trajectory_flight'))),
      h('div', { class: 'modal-footer' },
        statusEl,
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        importBtn)))
  document.body.appendChild(overlay)
}

/* Create a new blank JSON config file ───────────────────────────────────────*/
function _metaNewFileModal (dep) {
  const SUBDIRS = ['science_profile', 'science_trajectory', 'trajectory_flight']

  const nameInput = h('input', { class: 'form-input', type: 'text', placeholder: 'e.g. instruments.json' })
  const subdirSel = h('select', { class: 'form-input' },
    ...SUBDIRS.map(s => h('option', { value: s }, s)))
  const statusEl  = h('div', { class: 'modal-status' })

  async function doCreate () {
    let name = nameInput.value.trim()
    if (!name) { statusEl.textContent = 'Enter a file name.'; statusEl.style.color = '#e74c3c'; return }
    if (!name.endsWith('.json')) name += '.json'
    const rel = subdirSel.value + '\\' + name
    statusEl.textContent = ''
    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ path: rel, content: '{}' }),
      })
      closeModal()
      await renderMetadata()
    } catch (e) {
      statusEl.textContent = 'Error: ' + e.message
      statusEl.style.color = '#e74c3c'
    }
  }

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal', style: 'max-width:380px' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'New Config File'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' },
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'File name'), nameInput),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Folder'), subdirSel),
        statusEl),
      h('div', { class: 'modal-footer' },
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        h('button', { class: 'btn-primary',
          onclick: doCreate }, 'Create'))))
  document.body.appendChild(overlay)
  nameInput.focus()
}

/* Copy a specific file from another deployment into a target section ─────────*/
async function _metaCopyFileFromDepModal (dep, glider, targetGroup, onDone) {
  const SECTION_LABELS = {
    science_profile:    'Profile',
    science_trajectory: 'Trajectory (Science)',
    trajectory_flight:  'Trajectory (Flight)',
  }
  const allDeps = Array.from(document.getElementById('dep-select').options)
    .map(o => o.value).filter(Boolean).filter(d => d !== dep)
  if (!allDeps.length) { alert('No other deployments available.'); return }

  const same  = allDeps.filter(d => glider && d.startsWith(glider + '-')).sort().reverse()
  const other = allDeps.filter(d => !glider || !d.startsWith(glider + '-')).sort().reverse()

  const depSel = h('select', { class: 'form-input' })
  if (same.length) {
    const grp = document.createElement('optgroup')
    grp.label = `Same glider — ${glider}`
    same.forEach(d => grp.appendChild(h('option', { value: d }, d)))
    depSel.appendChild(grp)
  }
  if (other.length) {
    const grp = document.createElement('optgroup')
    grp.label = 'Other deployments'
    other.forEach(d => grp.appendChild(h('option', { value: d }, d)))
    depSel.appendChild(grp)
  }
  depSel.value = same.length ? same[0] : other[0]

  const fileListWrap = h('div', {})
  const statusEl     = h('div', { class: 'modal-status' })
  let checkboxes     = []

  async function loadFiles (srcDep) {
    fileListWrap.innerHTML = ''
    checkboxes = []
    if (!srcDep) return
    fileListWrap.appendChild(h('div', { style: 'color:#8a9bac;font-size:12px;padding:4px 0' }, 'Loading…'))
    try {
      const meta  = await apiFetch(`/api/deployment/${encodeURIComponent(srcDep)}/metadata`)
      const files = Object.entries(meta.files || {})
      fileListWrap.innerHTML = ''
      if (!files.length) {
        fileListWrap.appendChild(h('div', { style: 'color:#8a9bac;font-size:12px' }, 'No config files found in this deployment.'))
        return
      }
      // Group by section
      const bySection = {}
      files.forEach(([rel, fileJson]) => {
        const sec = rel.split(/[\\/]/)[0]
        if (!bySection[sec]) bySection[sec] = []
        bySection[sec].push([rel, fileJson])
      })
      Object.entries(bySection).forEach(([sec, entries]) => {
        fileListWrap.appendChild(h('div', {
          style: 'font-size:11px;color:#8a9bac;text-transform:uppercase;letter-spacing:.05em;margin:8px 0 3px'
        }, SECTION_LABELS[sec] || sec))
        entries.forEach(([rel, fileJson]) => {
          const filename = rel.split(/[\\/]/).pop()
          const isError  = !!(fileJson && fileJson.__metaError__)
          const cbAttrs  = { type: 'checkbox', id: `cpff-${rel}`, style: 'margin-right:6px' }
          if (isError) cbAttrs.disabled = ''
          const cb = h('input', cbAttrs)
          checkboxes.push({ cb, filename, isError })
          fileListWrap.appendChild(h('label', {
            for: `cpff-${rel}`,
            style: 'display:flex;align-items:center;cursor:pointer;font-size:13px;margin-bottom:3px' +
                   (isError ? ';color:#e74c3c;opacity:.6' : '')
          }, cb, filename + (isError ? ' (invalid JSON)' : '')))
        })
      })
    } catch (e) {
      fileListWrap.innerHTML = ''
      fileListWrap.appendChild(h('div', { style: 'color:#e74c3c;font-size:12px' }, 'Failed to load: ' + e.message))
    }
  }

  depSel.onchange = () => { statusEl.textContent = ''; loadFiles(depSel.value) }
  await loadFiles(depSel.value)

  const copyBtn = h('button', { class: 'btn-primary' }, 'Copy selected')
  copyBtn.onclick = async () => {
    statusEl.textContent = ''
    const selected = checkboxes.filter(({ cb, isError }) => cb.checked && !isError)
    if (!selected.length) {
      statusEl.textContent = 'Check at least one file.'
      statusEl.style.color = '#e74c3c'
      return
    }
    copyBtn.disabled    = true
    copyBtn.textContent = 'Copying…'
    const srcDep = depSel.value
    let errors = 0
    try {
      const meta = await apiFetch(`/api/deployment/${encodeURIComponent(srcDep)}/metadata`)
      for (const { filename } of selected) {
        // Find the file in any section of the source dep
        const srcEntry = Object.entries(meta.files || {})
          .find(([rel]) => rel.split(/[\\/]/).pop() === filename && !(meta.files[rel] && meta.files[rel].__metaError__))
        if (!srcEntry) { errors++; continue }
        const [, fileJson] = srcEntry
        const destRel = targetGroup + '\\' + filename
        try {
          await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ path: destRel, content: JSON.stringify(fileJson, null, 2) }),
          })
        } catch (_) { errors++ }
      }
    } catch (e) {
      statusEl.textContent = 'Error: ' + e.message
      statusEl.style.color = '#e74c3c'
      copyBtn.disabled    = false
      copyBtn.textContent = 'Copy selected'
      return
    }
    closeModal()
    await onDone()
    if (errors) alert(`${errors} file(s) could not be copied.`)
  }

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() } },
    h('div', { class: 'modal', style: 'max-width:480px;max-height:88vh;overflow-y:auto' },
      h('div', { class: 'modal-header' },
        h('h2', {}, `Copy file into ${SECTION_LABELS[targetGroup] || targetGroup}`),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' },
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Source deployment'), depSel),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Files to copy'), fileListWrap),
        statusEl),
      h('div', { class: 'modal-footer' },
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        copyBtn)))
  document.body.appendChild(overlay)
}

/* Generate global_attributes.json + deployment.json from a form ─────────────*/
async function showGenerateConfigModal (dep) {
  const glider   = dep.split('-')[0]
  const allDeps  = Array.from(document.getElementById('dep-select').options)
    .map(o => o.value).filter(Boolean).filter(d => d !== dep)
  const sameDeps = allDeps.filter(d => d.startsWith(glider + '-')).sort().reverse()
  const prevDep  = sameDeps[0] || null

  let gcSettings = { presets: [], active: null, static_fields: {} }
  try {
    const r = await apiFetch('/api/settings/generator')
    if (r && r.ok !== false) gcSettings = r
  } catch (_) {}
  let hasPrevConfigDep = false
  try {
    const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/prev-dep`)
    hasPrevConfigDep = !!r.prev_dep
  } catch (_) {}
  const noStaticSource = !hasPrevConfigDep && !gcSettings.active

  const statusEl = h('div', { class: 'modal-status' })

  const erddapInput = h('input', { class: 'form-input', type: 'url',
    placeholder: 'Paste any ERDDAP dataset or info URL',
    style: 'flex:1;min-width:0' })
  const fetchBtn = h('button', { class: 'btn-secondary btn-sm',
    style: 'white-space:nowrap' }, 'Fetch')

  const SEA_NAMES = ['Caribbean Sea', 'North Atlantic Ocean', 'Gulf of Mexico',
                     'Atlantic Ocean', 'Pacific Ocean']
  const fields = {
    summary:          h('textarea', { class: 'form-input', rows: 3, style: 'resize:vertical' }),
    comment:          h('textarea', { class: 'form-input', rows: 2, style: 'resize:vertical' }),
    project:          h('input',    { class: 'form-input', type: 'text' }),
    program:          h('input',    { class: 'form-input', type: 'text' }),
    acknowledgment:   h('input',    { class: 'form-input', type: 'text' }),
    contributor_name: h('input',    { class: 'form-input', type: 'text' }),
    contributor_role: h('input',    { class: 'form-input', type: 'text' }),
    sea_name:         h('select',   { class: 'form-input' },
      ...SEA_NAMES.map(n => h('option', { value: n }, n))),
  }

  function setFields (vals) {
    Object.entries(vals).forEach(([k, v]) => {
      if (!fields[k] || !v) return
      if (fields[k].tagName === 'SELECT') {
        Array.from(fields[k].options).forEach(o => { o.selected = o.value === v })
      } else {
        fields[k].value = v
      }
    })
  }

  fetchBtn.onclick = async () => {
    const url = erddapInput.value.trim()
    if (!url) return
    fetchBtn.disabled = true
    fetchBtn.textContent = 'Fetching…'
    statusEl.textContent = ''
    try {
      const r = await apiFetch(`/api/erddap/fetch?url=${encodeURIComponent(url)}`)
      if (r.error) throw new Error(r.error)
      setFields(r)
      statusEl.textContent = 'Fields updated from ERDDAP.'
      statusEl.style.color = '#27ae60'
    } catch (e) {
      statusEl.textContent = 'ERDDAP fetch failed: ' + e.message
      statusEl.style.color = '#e74c3c'
    } finally {
      fetchBtn.disabled = false
      fetchBtn.textContent = 'Fetch'
    }
  }

  const fieldRows = [
    { key: 'summary',          label: 'Summary' },
    { key: 'comment',          label: 'Comment (crew, vessel, port)' },
    { key: 'project',          label: 'Project' },
    { key: 'program',          label: 'Program' },
    { key: 'acknowledgment',   label: 'Acknowledgment' },
    { key: 'contributor_name', label: 'Contributor names (comma-separated)' },
    { key: 'contributor_role', label: 'Contributor roles (comma-separated)' },
    { key: 'sea_name',         label: 'Sea name' },
  ].map(({ key, label }) => h('div', { class: 'form-group' },
    h('label', { class: 'form-label' }, label), fields[key]))

  const SUBDIR_OPTS = [
    { value: 'science_profile',    label: 'Profile (science_profile)' },
    { value: 'science_trajectory', label: 'Trajectory — Science (science_trajectory)' },
    { value: 'trajectory_flight',  label: 'Trajectory — Flight (trajectory_flight)' },
  ]
  const subdirChecks = SUBDIR_OPTS.map(({ value, label }) => {
    const cb = h('input', { type: 'checkbox', checked: true, value, id: `gen-sd-${value}`,
      style: 'margin-right:6px' })
    return { cb, el: h('label', { for: `gen-sd-${value}`,
      style: 'display:flex;align-items:center;cursor:pointer;font-size:13px;margin-bottom:4px' },
      cb, label) }
  })

  // Copy-from selector (instruments.json + sensor_defs)
  const copyFromSel = h('select', { class: 'form-input' })
  copyFromSel.appendChild(h('option', { value: '' }, '— none —'))
  if (sameDeps.length) {
    const grp = document.createElement('optgroup')
    grp.label = `Same glider — ${glider}`
    sameDeps.forEach(d => grp.appendChild(
      h('option', { value: d, selected: d === prevDep }, d)))
    copyFromSel.appendChild(grp)
  }
  allDeps.filter(d => !d.startsWith(glider + '-')).sort().reverse().forEach(d =>
    copyFromSel.appendChild(h('option', { value: d }, d)))

  const generateBtn = h('button', { class: 'btn-primary' }, 'Generate')
  generateBtn.onclick = async () => {
    statusEl.textContent = ''
    const subdirs = subdirChecks.filter(({ cb }) => cb.checked).map(({ cb }) => cb.value)
    if (!subdirs.length) {
      statusEl.textContent = 'Select at least one folder.'
      statusEl.style.color = '#e74c3c'
      return
    }
    const payload = {
      summary:          fields.summary.value.trim(),
      comment:          fields.comment.value.trim(),
      project:          fields.project.value.trim(),
      program:          fields.program.value.trim(),
      acknowledgment:   fields.acknowledgment.value.trim(),
      contributor_name: fields.contributor_name.value.trim(),
      contributor_role: fields.contributor_role.value.trim(),
      sea_name:         fields.sea_name.value,
      copy_from:        copyFromSel.value,
      subdirs,
    }
    generateBtn.disabled    = true
    generateBtn.textContent = 'Generating…'
    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/generate`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      })
      closeModal()
      await renderMetadata()
    } catch (e) {
      statusEl.textContent    = 'Error: ' + e.message
      statusEl.style.color    = '#e74c3c'
      generateBtn.disabled    = false
      generateBtn.textContent = 'Generate'
    }
  }

  const sep = h('hr', { style: 'border:none;border-top:1px solid #2a3540;margin:10px 0' })

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() } },
    h('div', { class: 'modal', style: 'max-width:600px;max-height:90vh;overflow-y:auto' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'Generate Config Files'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' },
        noStaticSource ? h('div', {
          style: 'background:#4a3800;border:1px solid #8a6500;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#ffd888;line-height:1.5'
        },
          h('div', { style: 'font-weight:600;margin-bottom:4px' }, '⚠ Creator / publisher fields not configured'),
          h('div', {}, 'No previous deployment found and no preset is active. These fields will fall back to default values. Go to Settings → Metadata Static Fields to enter your organization\'s info and save a preset.'),
          h('button', { class: 'btn-secondary btn-sm', style: 'margin-top:8px',
            onclick: () => { closeModal(); navigate('settings') } }, 'Go to Settings')
        ) : null,
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Generate for'),
          h('div', {}, ...subdirChecks.map(({ el }) => el))),
        sep.cloneNode(),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'ERDDAP metadata URL'),
          h('div', { style: 'display:flex;gap:6px;align-items:center' }, erddapInput, fetchBtn),
          h('div', { style: 'font-size:11px;color:#8a9bac;margin-top:4px' },
            'Paste any ERDDAP URL. Fetch populates the fields below. Creator, publisher, and vertical units are set automatically — configure them in Settings.')),
        sep.cloneNode(),
        ...fieldRows,
        sep.cloneNode(),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Copy instruments & sensor defs from'),
          copyFromSel),
        statusEl),
      h('div', { class: 'modal-footer' },
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        generateBtn)))
  document.body.appendChild(overlay)
}

function selectMetaFile (rel, jsonData, fileList, content, dep) {
  fileList.querySelectorAll('.meta-file-item').forEach(el =>
    el.classList.toggle('active', el.dataset.rel === rel)
  )
  renderMetaView(rel, jsonData, content, dep)
}

async function _metaCopyFromPrev (dep, glider, rel, fileList, content) {
  const filename = rel.split(/[\\/]/).pop()

  // Collect all other deployments
  const allDeps   = Array.from(document.getElementById('dep-select').options)
    .map(o => o.value).filter(Boolean).filter(d => d !== dep)
  if (!allDeps.length) { alert('No other deployments to copy from.'); return }

  // Let user pick the source deployment
  const srcDep = await _pickSourceDeployment(dep, allDeps, glider, filename)
  if (!srcDep) return   // user cancelled

  try {
    const srcMeta = await apiFetch(`/api/deployment/${encodeURIComponent(srcDep)}/metadata`)
    if (srcMeta.errors && srcMeta.errors[rel]) {
      alert(`"${filename}" in ${srcDep} has invalid JSON and can't be copied: ${srcMeta.errors[rel].message}`)
      return
    }
    const srcJson = srcMeta.files[rel]
    if (srcJson === undefined || srcJson === null) {
      alert(`"${filename}" not found in ${srcDep}`)
      return
    }

    const text = JSON.stringify(srcJson, null, 2)
    await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: rel, content: text }),
    })

    // Full refresh from the server (re-selecting this file) -- a local-only update
    // here left the file list's stale closure-captured snapshot out of sync, so
    // switching to another file and back would silently show the pre-copy content.
    await renderMetadata(rel)
    // Flash the active item (re-query -- renderMetadata() rebuilds the file list, so
    // the original `fileList` reference above is now stale)
    const activeItem = document.getElementById('page-metadata').querySelector('.meta-file-item.active')
    if (activeItem) {
      activeItem.classList.add('meta-file-copied')
      setTimeout(() => activeItem.classList.remove('meta-file-copied'), 1500)
    }
  } catch (e) {
    alert('Copy failed: ' + e.message)
  }
}

/** Opens a small modal so the user can choose which deployment to copy a file from.
 *  Returns a Promise that resolves to the chosen dep name, or null if cancelled. */
function _pickSourceDeployment (currentDep, allDeps, currentGlider, filename) {
  return new Promise(resolve => {
    // Group: same glider (newest first) then others (newest first)
    const same  = allDeps.filter(d => currentGlider && d.startsWith(currentGlider + '-')).sort().reverse()
    const other = allDeps.filter(d => !currentGlider || !d.startsWith(currentGlider + '-')).sort().reverse()

    const sel = h('select', { class: 'form-input' })
    if (same.length) {
      const grp = document.createElement('optgroup')
      grp.label = `Same glider — ${currentGlider}`
      same.forEach(d => grp.appendChild(h('option', { value: d }, d)))
      sel.appendChild(grp)
    }
    if (other.length) {
      const grp = document.createElement('optgroup')
      grp.label = 'Other deployments'
      other.forEach(d => grp.appendChild(h('option', { value: d }, d)))
      sel.appendChild(grp)
    }
    // Default: most recent same-glider, else first available
    if (same.length)       sel.value = same[0]
    else if (other.length) sel.value = other[0]

    function finish (chosen) { picker.remove(); resolve(chosen) }

    const confirmBtn = h('button', { class: 'btn-primary',
      onclick: () => finish(sel.value || null)
    }, 'Copy from this deployment')

    const cancelBtn = h('button', { class: 'btn-secondary',
      onclick: () => finish(null)
    }, 'Cancel')

    const picker = h('div', { class: 'modal-overlay meta-copy-overlay',
      onclick: e => { if (e.target === picker) finish(null) }
    },
      h('div', { class: 'modal meta-copy-picker' },
        h('div', { class: 'modal-header' },
          h('h2', {}, `Copy ${filename}`),
          h('button', { class: 'modal-close', onclick: () => finish(null) }, '×')),
        h('div', { class: 'modal-body' },
          h('p', { class: 'meta-copy-subtext' },
            `Replace in ${currentDep} with version from:`),
          h('div', { class: 'form-group' },
            h('label', { class: 'form-label' }, 'Source deployment'),
            sel)),
        h('div', { class: 'modal-footer' },
          cancelBtn, confirmBtn)))

    document.body.appendChild(picker)
    sel.focus()
  })
}

function renderMetaView (rel, jsonData, content, dep) {
  content.innerHTML = ''

  // File failed to parse server-side -- go straight into an editable view of
  // its actual raw text (not a fake "null" placeholder) so it can be repaired
  // in place, with a persistent banner explaining what's wrong.
  if (jsonData && jsonData.__metaError__) {
    renderMetaEdit(rel, jsonData.raw, content, dep, () => renderMetaView(rel, jsonData, content, dep))
    content.prepend(h('div', { class: 'meta-broken-banner' },
      h('strong', {}, 'Invalid JSON — '), jsonData.message,
      h('br'), 'Fix the syntax below and Save to repair this file.'))
    return
  }

  const text = JSON.stringify(jsonData, null, 2) || '{}'

  // sensor_defs.json gets a variable table instead of raw JSON
  if (rel.endsWith('sensor_defs.json') && jsonData && typeof jsonData === 'object') {
    renderSensorDefsTable(rel, jsonData, text, content, dep)
    return
  }

  const editBtn = h('button', { class: 'btn-primary',
    onclick: () => renderMetaEdit(rel, text, content, dep) }, 'Edit')
  content.appendChild(h('div', { class: 'meta-edit-bar' }, editBtn))
  content.appendChild(h('pre', {}, text))
}

function renderSensorDefsTable (rel, jsonData, rawText, content, dep) {
  const sensors = Object.entries(jsonData).sort(([a], [b]) => a.localeCompare(b))

  const rows = sensors.map(([name, def]) => {
    const attrs = def.attrs || {}
    return h('tr', {},
      h('td', { class: 'sd-name'  }, name),
      h('td', { class: 'sd-long'  }, attrs.long_name      || '—'),
      h('td', { class: 'sd-units' }, attrs.units           || '—'),
      h('td', { class: 'sd-std'   }, attrs.standard_name  || '—'),
      h('td', { class: 'sd-type'  }, def.type || '—'),
    )
  })

  const table = h('table', { class: 'sensor-table' },
    h('thead', {},
      h('tr', {},
        h('th', {}, 'Variable'), h('th', {}, 'Long Name'),
        h('th', {}, 'Units'),    h('th', {}, 'Standard Name'), h('th', {}, 'Type'),
      )
    ),
    h('tbody', {}, ...rows)
  )

  const count   = h('span', { class: 'sd-count' }, `${sensors.length} variables`)
  const editBtn = h('button', { class: 'btn-primary',
    onclick: () => renderMetaEdit(rel, rawText, content, dep) }, 'Edit Raw JSON')

  content.appendChild(h('div', { class: 'sd-toolbar' }, count, editBtn))
  content.appendChild(table)
}

/* ── Manage Sensor Sets modal ────────────────────────────────────────────────*/
async function showManageSensorSets (dep) {
  // Show loading modal first
  const loadingEl = h('div', { style: 'color:#7fb3d3;padding:20px;text-align:center' }, 'Loading sensor universe…')
  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal ss-modal' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'Manage Sensor Sets'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body', id: 'ss-modal-body' }, loadingEl),
      h('div', { class: 'modal-footer', id: 'ss-modal-footer' })))
  document.body.appendChild(overlay)

  // Fetch sensor sets + DAT sensors in parallel
  let data, datData
  try {
    ;[data, datData] = await Promise.all([
      apiFetch('/api/sensor-sets'),
      dep ? apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-sensors`).catch(() => null) : Promise.resolve(null),
    ])
  } catch (e) {
    loadingEl.textContent = 'Error loading sensor sets: ' + e.message
    loadingEl.style.color = '#ff6b6b'
    return
  }

  const allSensors = data.all_sensors       // {name: {source, long_name, units}}
  const allNames   = Object.keys(allSensors).sort()

  // Per-source DAT sensor sets: merged → profile + traj science; flight → traj flight
  const _merged = datData?.merged
  const _flight = datData?.flight
  const datSets = {
    profile:           _merged ? { set: new Set(_merged.sensors), count: _merged.files_scanned } : null,
    trajectory:        _merged ? { set: new Set(_merged.sensors), count: _merged.files_scanned } : null,
    trajectory_flight: _flight ? { set: new Set(_flight.sensors), count: _flight.files_scanned } : null,
  }

  function getDat (tabKey) { return datSets[tabKey] || null }

  // Mutable working copies
  const selected = {
    profile:            new Set(data.profile),
    trajectory:         new Set(data.trajectory),
    trajectory_flight:  new Set(data.trajectory_flight),
  }

  let activeTab   = 'profile'
  let filterToDat = false  // toggle: show only sensors present in DAT files

  function buildPane (tabKey) {
    const sel  = selected[tabKey]
    const dat  = getDat(tabKey)
    const datSet   = dat ? dat.set   : null
    const datCount = dat ? dat.count : 0
    let search = ''

    // "My data only" toggle button
    const datToggle = datSet
      ? h('button', {
          class: 'ss-dat-toggle' + (filterToDat ? ' active' : ''),
          title: `${datCount} DAT file${datCount !== 1 ? 's' : ''} scanned — ${datSet.size} unique sensors found`,
          onclick: () => {
            filterToDat = !filterToDat
            datToggle.classList.toggle('active', filterToDat)
            datToggle.textContent = filterToDat ? 'My data only' : 'My data only'
            renderAvailable()
          }
        }, 'My data only')
      : null

    const searchInput = h('input', {
      class: 'ss-search', type: 'text', placeholder: 'Search available sensors…',
      oninput: e => {
        search = e.target.value.toLowerCase()
        renderAvailable()
      }
    })

    const availList = h('div', { class: 'ss-list', id: `ss-avail-${tabKey}` })
    const selList   = h('div', { class: 'ss-list', id: `ss-sel-${tabKey}` })

    function renderAvailable () {
      availList.innerHTML = ''
      const universe = filterToDat && datSet
        ? allNames.filter(n => datSet.has(n))
        : allNames
      const filtered = universe.filter(n =>
        !sel.has(n) &&
        (!search || n.toLowerCase().includes(search) ||
          (allSensors[n]?.long_name || '').toLowerCase().includes(search))
      )
      filtered.forEach(name => {
        const info    = allSensors[name] || {}
        const inDat   = datSet && datSet.has(name)
        const row = h('div', {
          class: 'ss-item ss-available',
          title: info.long_name
            ? `${info.long_name}${info.units ? ' [' + info.units + ']' : ''}` + (inDat ? '\n(in your DAT files)' : '')
            : name,
          onclick: () => { sel.add(name); renderAvailable(); renderSelected(); updateCounts() }
        },
          h('span', { class: 'ss-name' }, name),
          h('span', { class: 'ss-badges' },
            inDat ? h('span', { class: 'ss-badge-dat', title: 'In your DAT files' }, 'DAT') : null,
            h('span', { class: 'ss-src' }, info.source === 'sci' ? 'sci' : '')
          )
        )
        availList.appendChild(row)
      })
      if (!filtered.length) {
        const msg = filterToDat && datSet
          ? (search ? 'No DAT-file matches.' : 'All DAT-file sensors already selected.')
          : (search ? 'No matches.' : 'All sensors selected.')
        availList.appendChild(h('div', { style: 'padding:12px;color:#546e7a;font-size:12px' }, msg))
      }
    }

    function renderSelected () {
      selList.innerHTML = ''
      const sorted = [...sel].sort()
      sorted.forEach(name => {
        const info  = allSensors[name] || {}
        const inDat = datSet && datSet.has(name)
        const row = h('div', {
          class: 'ss-item ss-selected',
          title: info.long_name
            ? `${info.long_name}${info.units ? ' [' + info.units + ']' : ''}` + (inDat ? '\n(in your DAT files)' : '')
            : name
        },
          h('span', { class: 'ss-name' }, name),
          h('span', { class: 'ss-badges' },
            inDat ? h('span', { class: 'ss-badge-dat' }, 'DAT') : null,
            h('button', { class: 'ss-remove', title: 'Remove',
              onclick: e => { e.stopPropagation(); sel.delete(name); renderAvailable(); renderSelected(); updateCounts() }
            }, '×')
          )
        )
        selList.appendChild(row)
      })
      if (!sorted.length) {
        selList.appendChild(h('div', { style: 'padding:12px;color:#546e7a;font-size:12px' }, 'No sensors selected.'))
      }
    }

    renderAvailable()
    renderSelected()

    const availHeader = h('div', { class: 'ss-pane-header ss-pane-header-row' },
      h('span', {}, 'Available  (click to add)'),
      datToggle)

    return h('div', { class: 'ss-dual-pane' },
      h('div', { class: 'ss-pane' },
        availHeader,
        searchInput,
        availList),
      h('div', { class: 'ss-pane' },
        h('div', { class: 'ss-pane-header', id: `ss-sel-header-${tabKey}` }, `Selected — ${sel.size}`),
        selList))
  }

  function updateCounts () {
    const el = document.getElementById(`ss-sel-header-${activeTab}`)
    if (el) el.textContent = `Selected — ${selected[activeTab].size}`
  }

  // Tab buttons + pane container
  const paneContainer = h('div', { id: 'ss-pane-container' })

  const TAB_LABELS = { profile: 'Profile', trajectory: 'Trajectory (Science)', trajectory_flight: 'Trajectory (Flight)' }
  const tabs = ['profile', 'trajectory', 'trajectory_flight'].map(key => {
    const btn = h('button', {
      class: 'ss-tab' + (key === activeTab ? ' active' : ''),
      onclick: () => {
        activeTab = key
        document.querySelectorAll('.ss-tab').forEach(b => b.classList.remove('active'))
        btn.classList.add('active')
        paneContainer.innerHTML = ''
        paneContainer.appendChild(buildPane(key))
        updateDatStatus()
      }
    }, TAB_LABELS[key])
    return btn
  })

  // DAT status line — updates when tab changes
  const datStatus = h('span', {})
  function updateDatStatus () {
    const dat = getDat(activeTab)
    if (dat && dat.count > 0) {
      datStatus.className   = 'ss-dat-status'
      datStatus.textContent = `${dat.count} DAT file${dat.count !== 1 ? 's' : ''} scanned — ${dat.set.size} sensors in your data`
    } else {
      datStatus.className   = 'ss-dat-status ss-dat-status-none'
      datStatus.textContent = 'No DAT files found for this tab'
    }
  }
  updateDatStatus()

  paneContainer.appendChild(buildPane(activeTab))

  const statusEl = h('div', { class: 'modal-status', id: 'ss-save-status' })
  const saveBtn  = h('button', { class: 'btn-success',
    onclick: async () => {
      saveBtn.disabled = true; saveBtn.textContent = 'Saving…'
      try {
        const r = await apiFetch('/api/sensor-sets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            profile:           [...selected.profile].sort(),
            trajectory:        [...selected.trajectory].sort(),
            trajectory_flight: [...selected.trajectory_flight].sort(),
          })
        })
        saveBtn.disabled = false; saveBtn.textContent = 'Save'
        statusEl.textContent = `Saved — ${r.profile_count} profile, ${r.traj_count} traj (sci), ${r.traj_flight_count} traj (flt) sensors. Run "Build Sensor Defs" to apply.`
        statusEl.style.color = '#1da462'
      } catch (e) {
        saveBtn.disabled = false; saveBtn.textContent = 'Save'
        statusEl.textContent = 'Error: ' + e.message
        statusEl.style.color = '#ff6b6b'
      }
    }
  }, 'Save')

  const body   = document.getElementById('ss-modal-body')
  const footer = document.getElementById('ss-modal-footer')
  body.innerHTML = ''
  body.appendChild(h('div', { class: 'ss-tabs-row' },
    h('div', { class: 'ss-tabs' }, ...tabs),
    datStatus))
  body.appendChild(paneContainer)
  footer.appendChild(statusEl)
  footer.appendChild(saveBtn)
  footer.appendChild(h('button', { class: 'btn-secondary', onclick: closeModal }, 'Close'))
}

/* ── Build Sensor Defs modal ─────────────────────────────────────────────────*/
async function showBuildSensorDefs (dep) {
  const logEl  = h('div', { class: 'sd-build-log' }, 'Running build_sensor_defs.py…')
  const copyBtn = h('button', { class: 'btn-success hidden',
    onclick: async () => {
      copyBtn.disabled = true; copyBtn.textContent = 'Copying…'
      try {
        const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/copy-sensor-defs`, { method: 'POST' })
        copyBtn.textContent = `Copied (${r.copied.join(', ')})`
        // Invalidate metadata cache so next visit reloads
        delete state.depStates[dep]
      } catch (e) { copyBtn.textContent = 'Error: ' + e.message }
    }
  }, 'Copy to this deployment')

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'Build Sensor Defs'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' }, logEl,
        h('p', { style: 'font-size:11px;color:#8a9bac;margin-top:6px' },
          'Output written to glider_config_files_json/updated_sensor_defs/')),
      h('div', { class: 'modal-footer' },
        copyBtn,
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Close'))))

  document.body.appendChild(overlay)

  try {
    const r = await apiFetch('/api/build-sensor-defs', { method: 'POST' })
    logEl.textContent = (r.stdout + (r.stderr ? '\n' + r.stderr : '')).trim() || '(no output)'
    logEl.style.color = r.ok ? '#7ec8a0' : '#ff6b6b'
    if (r.ok) copyBtn.classList.remove('hidden')
  } catch (e) {
    logEl.textContent = 'Error: ' + e.message
    logEl.style.color = '#ff6b6b'
  }
}

function renderMetaEdit (rel, originalText, content, dep, onCancel) {
  content.innerHTML = ''
  content.scrollTop = 0

  const textarea = h('textarea', { class: 'meta-editor', spellcheck: 'false' })
  textarea.value = originalText

  const statusMsg = h('div', { class: 'meta-editor-status' })

  const saveBtn = h('button', { class: 'btn-success', onclick: async () => {
    const raw = textarea.value
    // Validate JSON client-side first
    try { JSON.parse(raw) } catch (e) {
      statusMsg.textContent = 'Invalid JSON: ' + e.message
      statusMsg.style.color = '#e74c3c'
      return
    }
    saveBtn.disabled = true
    saveBtn.textContent = 'Saving…'
    statusMsg.textContent = ''
    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: rel, content: raw }),
      })
      // Full refresh from the server (re-selecting this same file) instead of a
      // local-only view update -- the file list's click handlers close over the
      // metadata snapshot loaded when the page opened, so a local-only update left
      // that snapshot stale: switching to another file and back would silently show
      // the pre-edit content again even though the save itself succeeded correctly.
      await renderMetadata(rel)
    } catch (e) {
      statusMsg.textContent = e.message
      statusMsg.style.color = '#e74c3c'
      saveBtn.disabled = false
      saveBtn.textContent = 'Save'
    }
  }}, 'Save')

  const cancelBtn = h('button', { class: 'btn-secondary', onclick: () => {
    if (onCancel) onCancel()
    else renderMetaView(rel, JSON.parse(originalText), content, dep)
  }}, 'Cancel')

  const btnRow = h('div', { class: 'btn-row', style: 'margin-bottom:8px' },
    saveBtn, cancelBtn, statusMsg)

  content.appendChild(btnRow)
  content.appendChild(textarea)
  textarea.focus()
}

/* ── DAT Browser page ────────────────────────────────────────────────────────*/
/* ── Files page (Raw binary + DAT) ──────────────────────────────────────────*/
async function renderDatBrowser () {
  const dep  = state.deployment
  const page = document.getElementById('page-dat-browser')
  page.innerHTML = ''

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'Files'),
    h('p',  {}, dep ? `Raw and processed data files for ${dep}` : 'Select a deployment.')
  ))

  if (!dep) return

  // Tab bar
  let activeTab = 'raw'
  const tabBody = h('div', { class: 'files-tab-body' })

  function setTab (key) {
    activeTab = key
    tabBar.querySelectorAll('.files-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === key))
    tabBody.innerHTML = ''
    if      (key === 'raw')        renderRawTab(dep, tabBody)
    else if (key === 'dat')        renderDatTab(dep, tabBody)
    else if (key === 'profile-nc') renderProfileNcTab(dep, tabBody)
    else if (key === 'traj-nc')    renderTrajNcTab(dep, tabBody)
    else if (key === 'qc-output')  renderQcOutputTab(dep, tabBody)
    else if (key === 'cac')        renderCacTab(tabBody)
    else if (key === 'adcp')       renderAdcpFilesTab(dep, tabBody)
  }

  const tabBar = h('div', { class: 'files-tab-bar' },
    h('button', { class: 'files-tab active', 'data-tab': 'raw',        onclick: () => setTab('raw')        }, 'Raw Files'),
    h('button', { class: 'files-tab',        'data-tab': 'dat',        onclick: () => setTab('dat')        }, 'DAT Files'),
    h('button', { class: 'files-tab',        'data-tab': 'profile-nc', onclick: () => setTab('profile-nc') }, 'Profile NC'),
    h('button', { class: 'files-tab',        'data-tab': 'traj-nc',    onclick: () => setTab('traj-nc')    }, 'Trajectory NC'),
    h('button', { class: 'files-tab',        'data-tab': 'qc-output',  onclick: () => setTab('qc-output')  }, 'QC Output'),
    h('button', { class: 'files-tab',        'data-tab': 'cac',        onclick: () => setTab('cac')        }, 'CAC Files'),
    h('button', { class: 'files-tab',        'data-tab': 'adcp',       onclick: () => setTab('adcp')       }, 'AD2CP'),
  )

  page.appendChild(tabBar)
  page.appendChild(tabBody)
  setTab('raw')
}


async function renderRawTab (dep, container) {
  container.innerHTML = ''

  // Load file list
  let data
  try { data = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/raw-files`) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load raw files: ' + e.message)); return }

  const files = data.files  // [{name, size, ext}]

  // Summary badges by extension
  const extCounts = {}
  files.forEach(f => { extCounts[f.ext] = (extCounts[f.ext] || 0) + 1 })
  const totalSize = files.reduce((s, f) => s + f.size, 0)
  const extBadges = Object.entries(extCounts).sort().map(([ext, n]) =>
    h('span', { class: 'raw-ext-badge' }, `${ext.slice(1).toUpperCase()}: ${n}`))
  const summaryEl = h('div', { class: 'raw-summary' },
    ...extBadges,
    h('span', { class: 'raw-total-size' }, files.length ? `— ${_fmtBytes(totalSize)} total` : '')
  )

  // Drop zone (reuses existing import machinery)
  const ALLOWED = new Set(['.dbd', '.ebd', '.sbd', '.tbd', '.mbd', '.nbd'])
  const errorEl   = h('div', { class: 'import-error' })
  const statusEl  = h('div', { class: 'raw-import-status' })

  async function doAddFiles (fsFiles) {
    errorEl.textContent = ''
    const valid   = fsFiles.filter(f => ALLOWED.has('.' + f.name.split('.').pop().toLowerCase()))
    const skipped = fsFiles.length - valid.length
    if (skipped) errorEl.textContent = `${skipped} file(s) skipped — only ${[...ALLOWED].join(' ')} accepted`
    if (!valid.length) return

    const paths = valid.map(f => f.path).filter(Boolean)
    if (!paths.length) { errorEl.textContent = 'Open in Electron app to copy files.'; return }

    statusEl.textContent = `Copying ${paths.length} file(s)…`
    try {
      const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/import-raw`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      })
      const done = r.results.filter(x => x.ok).length
      const errs = r.results.filter(x => !x.ok)
      statusEl.textContent = `${done} file(s) copied.`
      if (errs.length) errorEl.textContent = errs.map(e => `${e.path}: ${e.error}`).join('\n')
    } catch (e) {
      errorEl.textContent = 'Copy failed: ' + e.message
    }
    // Refresh
    renderRawTab(dep, container)
  }

  const fileInput = h('input', { type: 'file', multiple: true, style: 'display:none',
    onchange: e => { doAddFiles(Array.from(e.target.files)); e.target.value = '' } })
  const dropzone = h('div', { class: 'import-dropzone import-dropzone-sm' },
    h('div', { class: 'import-drop-icon' }, '↑'),
    h('div', { class: 'import-drop-text' }, 'Drop files or browse to add more'),
    h('div', { class: 'import-drop-ext' }, '.dbd  .ebd  .sbd  .tbd  .mbd  .nbd'),
    h('label', { class: 'btn-secondary import-browse-btn', style: 'margin-top:6px' }, 'Browse…', fileInput))
  dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over') })
  dropzone.addEventListener('dragleave', e => { if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('drag-over') })
  dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('drag-over'); doAddFiles(Array.from(e.dataTransfer.files)) })

  // File list
  const listEl = h('div', { class: 'raw-file-list' })

  function buildFileRows (fileArr) {
    listEl.innerHTML = ''
    if (!fileArr.length) {
      listEl.appendChild(h('div', { class: 'raw-empty' }, `No binary files in incoming_data/${dep}/`))
      return
    }
    fileArr.forEach(f => {
      const extClass = `raw-ext-${f.ext.slice(1).toLowerCase()}`
      const row = h('div', { class: 'raw-file-row' },
        h('span', { class: `raw-ext-tag ${extClass}` }, f.ext.slice(1).toUpperCase()),
        h('span', { class: 'raw-file-name' }, f.name),
        h('span', { class: 'raw-file-size' }, _fmtBytes(f.size)),
        h('button', { class: 'btn-danger-sm',
          onclick: async () => {
            if (!confirm(`Delete ${f.name}?`)) return
            try {
              await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/raw-files/${encodeURIComponent(f.name)}`, { method: 'DELETE' })
              renderRawTab(dep, container)
            } catch (e) { alert('Delete failed: ' + e.message) }
          }
        }, '✕')
      )
      listEl.appendChild(row)
    })
  }

  buildFileRows(files)

  // Drop zone for adding more files
  container.appendChild(h('div', { class: 'raw-controls' }, dropzone, errorEl, statusEl))

  // File list as a card
  const card = h('div', { class: 'raw-section-card' })
  card.appendChild(h('div', { class: 'raw-section-header' },
    h('span', { class: 'raw-section-title' }, 'Raw Binary Files'),
    summaryEl,
  ))
  card.appendChild(listEl)
  container.appendChild(card)
}


async function renderDatTab (dep, container, initialSub) {
  container.innerHTML = ''

  let data
  try { data = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-files`) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load DAT files: ' + e.message)); return }

  // Sub-tabs: merged_data and flight_data
  let activeSub = initialSub || 'merged_data'
  const subBody = h('div', { class: 'dat-sub-body' })

  function setSubTab (key) {
    activeSub = key
    subBar.querySelectorAll('.dat-sub-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.sub === key))
    buildDatView(key)
  }

  const subBar = h('div', { class: 'dat-sub-bar' },
    h('button', { class: `dat-sub-tab${activeSub === 'merged_data' ? ' active' : ''}`, 'data-sub': 'merged_data', onclick: () => setSubTab('merged_data') },
      `merged_data (${data.merged_data.files.length})`),
    h('button', { class: `dat-sub-tab${activeSub === 'flight_data' ? ' active' : ''}`, 'data-sub': 'flight_data', onclick: () => setSubTab('flight_data') },
      `flight_data (${data.flight_data.files.length})`),
  )

  function buildDatView (subdir) {
    subBody.innerHTML = ''
    const { files, dir } = data[subdir]
    if (!files.length) {
      subBody.appendChild(h('div', { class: 'empty-state' }, `No DAT files in:\n${dir}`))
      return
    }

    const checkedNames = new Set()
    let lastClickedIdx = -1
    const rowCbs = []

    async function deleteNames (names) {
      const results = await Promise.allSettled(names.map(name =>
        apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-files/${encodeURIComponent(subdir)}/${encodeURIComponent(name)}`,
          { method: 'DELETE' })
      ))
      const failed = results.filter(r => r.status === 'rejected').length
      if (failed) alert(`${failed} of ${names.length} file${names.length === 1 ? '' : 's'} failed to delete.`)
      renderDatTab(dep, container, subdir)
    }

    // ── Selection bar: select-all + delete-selected (mirrors the cloud file list) ──
    const headerCb = h('input', { type: 'checkbox', class: 'dat-header-cb',
      title: 'Select / deselect all' })
    const deleteSelBtn = h('button', { class: 'btn-danger btn-sm', style: 'display:none' })

    function updateSelBar () {
      const n = checkedNames.size
      deleteSelBtn.style.display = n ? '' : 'none'
      deleteSelBtn.textContent   = `✕ Delete ${n} Selected`
      headerCb.checked       = n === files.length && files.length > 0
      headerCb.indeterminate = n > 0 && n < files.length
    }

    headerCb.addEventListener('click', () => {
      const allSel = checkedNames.size === files.length
      if (allSel) checkedNames.clear()
      else files.forEach(f => checkedNames.add(f.name))
      rowCbs.forEach((cb, i) => { cb.checked = checkedNames.has(files[i].name) })
      updateSelBar()
    })

    deleteSelBtn.addEventListener('click', () => {
      const names = Array.from(checkedNames)
      if (!names.length) return
      if (!confirm(`Delete ${names.length} selected DAT file${names.length === 1 ? '' : 's'}?`)) return
      deleteSelBtn.disabled = true
      deleteSelBtn.textContent = 'Deleting…'
      deleteNames(names)
    })

    subBody.appendChild(h('div', { class: 'dat-sel-bar' },
      headerCb, h('span', { class: 'dat-sel-label' }, 'Select all'), deleteSelBtn))

    const fileList = h('div', { class: 'dat-file-list' })
    const preview  = h('div', { class: 'dat-preview' }, 'Select a file to preview its header.')
    subBody.appendChild(h('div', { class: 'dat-layout' }, fileList, preview))

    files.forEach((f, idx) => {
      const cb = h('input', { type: 'checkbox' })
      cb.checked = checkedNames.has(f.name)
      rowCbs.push(cb)
      cb.addEventListener('click', e => {
        e.stopPropagation()
        const nowChecked = !checkedNames.has(f.name)
        if (e.shiftKey && lastClickedIdx >= 0 && lastClickedIdx !== idx) {
          const lo = Math.min(lastClickedIdx, idx)
          const hi = Math.max(lastClickedIdx, idx)
          for (let i = lo; i <= hi; i++) {
            if (nowChecked) checkedNames.add(files[i].name)
            else            checkedNames.delete(files[i].name)
            rowCbs[i].checked = nowChecked
          }
        } else {
          if (nowChecked) checkedNames.add(f.name)
          else            checkedNames.delete(f.name)
          cb.checked = nowChecked
        }
        lastClickedIdx = idx
        updateSelBar()
      })

      const item = h('div', { class: 'dat-file-item' },
        cb,
        h('span', { class: 'dat-file-name', title: f.name }, f.name),
        h('span', { class: 'dat-file-size' }, _fmtBytes(f.size)))
      item.addEventListener('click', () => previewDat(dir, f.name, item, fileList, preview))
      fileList.appendChild(item)
    })

    updateSelBar()
  }

  container.appendChild(subBar)
  container.appendChild(subBody)
  setSubTab(activeSub)
}


async function previewDat (dir, name, item, fileList, preview) {
  fileList.querySelectorAll('.dat-file-item').forEach(el => el.classList.remove('active'))
  item.classList.add('active')
  preview.textContent = 'Loading…'
  try {
    const fp   = `${dir}\\${name}`.replace(/\//g, '\\')
    const resp = await fetch(`${API}/api/file?path=${encodeURIComponent(fp)}`)
    if (!resp.ok) throw new Error(resp.statusText)
    const text = await resp.text()
    preview.textContent = text.split('\n').slice(0, 80).join('\n')
  } catch (e) {
    preview.textContent = 'Error loading file: ' + e.message
  }
}


/* ── QC Config editor ───────────────────────────────────────────────────────
   Two independent panels, both YAML, both raw-text edit (never round-tripped
   through a YAML parser on save, so comments survive):
     - This Deployment: deployments/<year>/<dep>/config/qc/ -- what every
       Stage 8 rugliderqc script actually reads at runtime.
     - Regional QC Configs: qc/config/global_regional/ at the repo root --
       what glider_qartod_qc.py's regional streams tests actually read. NOT
       the stale, diverged copy vendored inside rugliderqc/qc/config/global_regional/,
       which nothing in the pipeline reads.
*/
async function renderQcConfig () {
  const dep  = state.deployment
  const page = document.getElementById('page-qc-config')
  page.innerHTML = ''

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'QC Config'),
    h('p',  {}, 'View and edit the YAML files that control QARTOD QC thresholds.')))

  const body = h('div', { class: 'page-body' })
  page.appendChild(body)

  const depSection = h('div', { class: 'qc-config-section' },
    h('h2', {}, 'This Deployment'))
  body.appendChild(depSection)
  if (!dep) {
    depSection.appendChild(h('div', { class: 'empty-state' }, 'Select a deployment.'))
  } else {
    depSection.appendChild(h('p', { class: 'qc-config-hint' },
      `deployments/…/${dep}/config/qc/ — read directly by every Stage 8 QC script.`))
    await buildQcConfigPanel(depSection,
      `/api/deployment/${encodeURIComponent(dep)}/qc-config`,
      `/api/deployment/${encodeURIComponent(dep)}/qc-config/save`)
  }

  const regionalSection = h('div', { class: 'qc-config-section' },
    h('h2', {}, 'Regional QC Configs'),
    h('p', { class: 'qc-config-hint' },
      'qc/config/global_regional/ — region-matched bounds applied by glider_qartod_qc.py, shared across all deployments.'))
  body.appendChild(regionalSection)
  await buildQcConfigPanel(regionalSection, '/api/qc-config-regional', '/api/qc-config-regional/save')
}

async function buildQcConfigPanel (container, getUrl, saveUrl) {
  let data
  try { data = await apiFetch(getUrl) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load QC config: ' + e.message)); return }

  const entries = Object.entries(data.files || {})
  if (!entries.length) {
    container.appendChild(h('div', { class: 'empty-state' }, `No .yml files in:\n${data.config_dir}`))
    return
  }

  const fileList = h('div', { class: 'dat-file-list' })
  const content  = h('div', { class: 'dat-preview qc-config-content' }, 'Select a file to view it.')
  container.appendChild(h('div', { class: 'dat-layout qc-config-layout' }, fileList, content))

  function showView (rel, text, item) {
    fileList.querySelectorAll('.dat-file-item').forEach(el => el.classList.remove('active'))
    item.classList.add('active')
    content.innerHTML = ''
    content.appendChild(h('div', { class: 'btn-row', style: 'margin-bottom:8px' },
      h('button', { class: 'btn-primary btn-sm', onclick: () => showEdit(rel, text, item) }, 'Edit')))
    content.appendChild(h('pre', { class: 'qc-config-pre' }, text ?? '(could not read file)'))
  }

  function showEdit (rel, originalText, item) {
    content.innerHTML = ''
    const textarea = h('textarea', { class: 'meta-editor qc-config-editor', spellcheck: 'false' })
    textarea.value = originalText
    const statusMsg = h('div', { class: 'meta-editor-status' })

    const saveBtn = h('button', { class: 'btn-success', onclick: async () => {
      const raw = textarea.value
      saveBtn.disabled = true
      saveBtn.textContent = 'Saving…'
      statusMsg.textContent = ''
      try {
        await apiFetch(saveUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: rel, content: raw }),
        })
        // Full refresh from the server instead of a local-only update -- see
        // the metadata editor's identical fix for why (stale file-list snapshots).
        container.innerHTML = ''
        await buildQcConfigPanel(container, getUrl, saveUrl)
      } catch (e) {
        statusMsg.textContent = e.message
        statusMsg.style.color = '#e74c3c'
        saveBtn.disabled = false
        saveBtn.textContent = 'Save'
      }
    }}, 'Save')

    const cancelBtn = h('button', { class: 'btn-secondary', onclick: () => showView(rel, originalText, item) }, 'Cancel')

    content.appendChild(h('div', { class: 'btn-row', style: 'margin-bottom:8px' }, saveBtn, cancelBtn))
    content.appendChild(textarea)
    content.appendChild(statusMsg)
    textarea.focus()
  }

  let firstItem = null
  entries.forEach(([rel, text]) => {
    const item = h('div', { class: 'dat-file-item' }, h('span', { class: 'dat-file-name', title: rel }, rel))
    item.addEventListener('click', () => showView(rel, text, item))
    fileList.appendChild(item)
    if (!firstItem) firstItem = item
  })
  if (firstItem) firstItem.click()
}


/* ── NC file sections (Stages 2a / 2b / 2c / 4) ─────────────────────────────*/
function renderNcSection (title, stageLabel, data, container) {
  const { files = [], dir = '', exists = false } = data
  const totalSize = files.reduce((s, f) => s + f.size, 0)

  const card = h('div', { class: 'nc-section-card' })
  container.appendChild(card)

  const canOpen = exists && window.APP && window.APP.shellOpen
  card.appendChild(h('div', { class: 'nc-section-header' },
    h('div', { class: 'nc-section-meta' },
      h('span', { class: 'nc-section-title' }, title),
      h('span', { class: 'nc-section-stage' }, stageLabel),
      files.length
        ? h('span', { class: 'nc-section-count' }, `${files.length} files · ${_fmtBytes(totalSize)}`)
        : h('span', { class: 'nc-section-count nc-section-empty' }, 'no files yet'),
    ),
    canOpen ? h('button', { class: 'btn-secondary btn-xs', onclick: () => window.APP.shellOpen(dir) }, 'Open ↗') : null,
  ))

  if (!files.length) {
    card.appendChild(h('div', { class: 'nc-empty' },
      exists ? 'No .nc files in this directory.' : 'Directory does not exist yet — run the stage first.'))
    return
  }

  const listEl = h('div', { class: 'nc-file-list' })
  files.forEach(f => {
    const mtime = f.mtime ? new Date(f.mtime * 1000).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    }) : ''
    listEl.appendChild(h('div', { class: 'nc-file-row' },
      h('span', { class: 'nc-file-ext' }, 'NC'),
      h('span', { class: 'nc-file-name' }, f.name),
      h('span', { class: 'nc-file-mtime' }, mtime),
      h('span', { class: 'nc-file-size' }, _fmtBytes(f.size)),
    ))
  })
  card.appendChild(listEl)
}

async function _fetchNcFiles (dep) {
  return apiFetch(`/api/deployment/${encodeURIComponent(dep)}/nc-files`)
}

async function renderProfileNcTab (dep, container) {
  container.innerHTML = ''
  let data
  try { data = await _fetchNcFiles(dep) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load files: ' + e.message)); return }
  renderNcSection('Profile NC', 'Stage 2a', data.profile, container)
}

async function renderTrajNcTab (dep, container) {
  container.innerHTML = ''
  let data
  try { data = await _fetchNcFiles(dep) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load files: ' + e.message)); return }
  renderNcSection('Science Trajectory NC', 'Stage 2b', data.traj_sci, container)
  renderNcSection('Flight Trajectory NC',  'Stage 2c', data.traj_flt, container)
}

async function renderQcOutputTab (dep, container) {
  container.innerHTML = ''
  let data
  try { data = await _fetchNcFiles(dep) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load files: ' + e.message)); return }
  renderNcSection('QC Output', 'Stage 4', data.qc, container)
}

/* ── CAC Files tab ───────────────────────────────────────────────────────────*/
async function renderCacTab (container) {
  container.innerHTML = ''

  // Scrollable wrapper (tab body has overflow:hidden) -- same pattern as
  // renderAdcpFilesTab's .adcp-files-scroll, needed once the file list +
  // upload section combined exceed the visible tab height.
  const scroll = h('div', { class: 'cac-tab-scroll' })
  container.appendChild(scroll)

  // ── File list ─────────────────────────────────────────────────────────────
  const listWrap = h('div', { class: 'cac-list-wrap' })
  scroll.appendChild(listWrap)

  async function refreshList () {
    listWrap.innerHTML = ''
    let data
    try { data = await apiFetch('/api/cac-files') }
    catch (e) {
      listWrap.appendChild(h('div', { class: 'empty-state' }, 'Could not load CAC files: ' + e.message))
      return
    }

    const files = data.files
    const card = h('div', { class: 'cac-section-card' })
    listWrap.appendChild(card)

    card.appendChild(h('div', { class: 'cac-section-header' },
      h('div', { class: 'cac-section-meta' },
        h('span', { class: 'cac-section-title' }, 'CAC Files'),
        h('span', { class: 'cac-section-subtitle' }, 'Shared cache files for Stage 1 decoding · master_cac/'),
      ),
      h('span', { class: 'cac-count' }, `${files.length} files`),
    ))

    if (!files.length) {
      card.appendChild(h('div', { class: 'nc-empty' }, 'No CAC files found. Upload some using the drop zone below.'))
      return
    }

    const table = h('div', { class: 'cac-table' })
    for (const f of files) {
      const ext = f.name.split('.').pop().toUpperCase()
      const mtime = new Date(f.mtime * 1000).toLocaleString()
      const row = h('div', { class: 'cac-row' },
        h('span', { class: `cac-ext cac-ext--${ext.toLowerCase()}` }, ext),
        h('span', { class: 'cac-name' }, f.name),
        h('span', { class: 'cac-mtime' }, mtime),
        h('span', { class: 'cac-size' },  formatBytes(f.size)),
        h('button', { class: 'btn-xs btn-danger-ghost', title: 'Delete', onclick: async () => {
          if (!confirm(`Delete ${f.name}?`)) return
          try {
            await apiFetch(`/api/cac-files/${encodeURIComponent(f.name)}`, { method: 'DELETE' })
            refreshList()
          } catch (e) { alert('Delete failed: ' + e.message) }
        }}, '✕')
      )
      table.appendChild(row)
    }
    card.appendChild(table)
  }

  await refreshList()

  // ── Drop zone ─────────────────────────────────────────────────────────────
  const statusEl = h('div', { class: 'cac-upload-status hidden' })

  const fileInput = h('input', {
    type: 'file', multiple: true,
    accept: '.cac,.ccc',
    style: 'display:none',
  })

  async function uploadFiles (files, conflict = 'skip') {
    if (!files.length) return
    statusEl.textContent = `Uploading ${files.length} file${files.length > 1 ? 's' : ''}…`
    statusEl.className = 'cac-upload-status uploading'
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    fd.append('conflict', conflict)
    try {
      const resp = await fetch(`${API}/api/cac-files/upload`, { method: 'POST', body: fd })
      const result = await resp.json()
      if (result.ok) {
        const n = result.saved.length
        const savedMsg = n ? `Saved ${n} file${n !== 1 ? 's' : ''}${result.saved.length ? ': ' + result.saved.join(', ') : ''}` : ''
        if (result.skipped.length) {
          const skippedNames = result.skipped
          statusEl.textContent = ''
          statusEl.className = 'cac-upload-status warning'
          statusEl.appendChild(h('span', {},
            `${savedMsg ? savedMsg + '. ' : ''}${skippedNames.length} already exist, not overwritten: ${skippedNames.join(', ')}. `))
          statusEl.appendChild(h('button', {
            class: 'btn-xs btn-secondary',
            onclick: () => {
              const retryFiles = files.filter(f => skippedNames.includes(f.name))
              uploadFiles(retryFiles, 'overwrite')
            },
          }, 'Overwrite anyway'))
        } else {
          statusEl.textContent = savedMsg
          statusEl.className = 'cac-upload-status success'
        }
        refreshList()
      } else {
        statusEl.textContent = 'Upload failed'
        statusEl.className = 'cac-upload-status error'
      }
    } catch (e) {
      statusEl.textContent = 'Upload error: ' + e.message
      statusEl.className = 'cac-upload-status error'
    }
  }

  fileInput.addEventListener('change', () => { uploadFiles([...fileInput.files]); fileInput.value = '' })

  const browseBtn = h('button', { class: 'btn-secondary cac-browse-btn',
    onclick: () => fileInput.click()
  }, '📂  Browse files')

  const dropZone = h('div', { class: 'cac-drop-zone' },
    h('div', { class: 'cac-drop-icon' }, '📥'),
    h('div', { class: 'cac-drop-text' },
      h('strong', {}, 'Drag & drop .cac / .ccc files here'),
      h('span',   {}, ' or '),
    ),
    browseBtn,
    fileInput,
  )

  dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag-over') })
  dropZone.addEventListener('dragleave', e => { if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('drag-over') })
  dropZone.addEventListener('drop', e => {
    e.preventDefault()
    dropZone.classList.remove('drag-over')
    uploadFiles([...e.dataTransfer.files])
  })

  scroll.appendChild(h('div', { class: 'cac-upload-section' }, dropZone, statusEl))
}


async function renderAdcpFilesTab (dep, container) {
  container.innerHTML = ''

  // Scrollable wrapper (tab body has overflow:hidden)
  const scroll = h('div', { class: 'adcp-files-scroll' })
  container.appendChild(scroll)

  let statusR, inputR
  try {
    [statusR, inputR] = await Promise.all([
      apiFetch(`/api/adcp/status?dep=${encodeURIComponent(dep)}&is_test=false`),
      apiFetch(`/api/adcp/files?dep=${encodeURIComponent(dep)}&is_test=false`),
    ])
  } catch (e) {
    scroll.appendChild(h('div', { class: 'empty-state' }, 'Could not load AD2CP data: ' + e.message))
    return
  }

  const shared = statusR.shared || {}
  const runs   = statusR.runs   || {}
  const baseQs = `dep=${encodeURIComponent(dep)}&is_test=false`

  function fmtSize (n) {
    if (!n) return ''
    return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB`
         : n >= 1e3 ? `${(n / 1e3).toFixed(0)} KB`
         : `${n} B`
  }

  function extBadge (filename) {
    const ext = filename.split('.').pop().toLowerCase()
    return h('span', { class: `adcp-file-ext adcp-file-ext--${ext}` }, ext.toUpperCase())
  }

  function dlBtn (filename, run) {
    const qs = run ? `${baseQs}&run=${encodeURIComponent(run)}` : baseQs
    const a  = h('a', { class: 'btn-secondary btn-xs',
      href: `${API}/api/adcp/download/${encodeURIComponent(filename)}?${qs}` }, '⬇')
    a.setAttribute('download', filename)
    return a
  }

  // fileInfo is {exists, size} (new API) or bool (legacy fallback)
  function fileRow (filename, fileInfo, run) {
    const exists = (typeof fileInfo === 'object') ? fileInfo.exists : !!fileInfo
    const size   = (typeof fileInfo === 'object') ? fileInfo.size   : 0
    return h('div', { class: `adcp-file-row${exists ? '' : ' adcp-file-row--missing'}` },
      extBadge(filename),
      h('span', { class: 'adcp-file-name' }, filename),
      exists
        ? h('span', { class: 'adcp-file-size' }, fmtSize(size))
        : h('span', { class: 'adcp-file-pending' }, 'not yet generated'),
      exists ? dlBtn(filename, run) : h('span', {}),
    )
  }

  function buildSection (title, subtitle, bodyEl, actionEl) {
    const sec = h('div', { class: 'adcp-files-section' },
      h('div', { class: 'adcp-section-header' },
        h('div', { class: 'adcp-section-header-left' },
          h('span', { class: 'adcp-section-title' }, title),
          subtitle ? h('span', { class: 'adcp-section-count' }, subtitle) : null,
        ),
        actionEl || null,
      ),
      bodyEl,
    )
    return sec
  }

  function fileList (rows) {
    if (!rows.length) return h('div', { class: 'adcp-file-empty' }, 'No files yet.')
    const list = h('div', { class: 'adcp-file-list' })
    rows.forEach(r => list.appendChild(r))
    return list
  }

  // ── Input files ─────────────────────────────────────────────────────────────
  const inputFiles = inputR.files || []
  const inputRows = inputFiles.map(fn => h('div', { class: 'adcp-file-row' },
    h('span', { class: 'adcp-file-ext adcp-file-ext--nc' }, 'NC'),
    h('span', { class: 'adcp-file-name' }, fn),
    h('span', {}), h('span', {}),
  ))
  if (!inputFiles.length) inputRows.push(
    h('div', { class: 'adcp-file-empty' }, 'No AD2CP .nc files imported yet.'))
  scroll.appendChild(buildSection(
    'Input Files', `${inputFiles.length} file${inputFiles.length !== 1 ? 's' : ''}`,
    fileList(inputRows)))

  // ── Shared processing files ──────────────────────────────────────────────────
  const sharedDefs = [
    { stage: 'load_nc',         file: 'adcp_glider_combined.csv'           },
    { stage: 'gps_pairing',     file: 'gps_data.npz'                       },
    { stage: 'write_glider_nc', file: 'glider_ad2cp_datetime_timecoord.nc' },
  ]
  scroll.appendChild(buildSection('Shared Processing Files', null,
    fileList(sharedDefs.map(({ stage, file }) =>
      fileRow(file, shared[stage]?.files?.[file] ?? false, null)))))

  // ── Per-run sections (grouped by stage) ─────────────────────────────────────
  const stageGroups = [
    { stage: 'process_shear',    label: 'Process Shear',    files: ['shear_processed.nc'] },
    { stage: 'compute_currents', label: 'Compute Currents', files: ['currents.nc', 'dac.npz'] },
    { stage: 'bias_correction',  label: 'Bias Correction',  files: ['currents_bias_corrected.nc', 'bias_diagnostic.png'] },
    { stage: 'generate_plots',   label: 'Generate Plots',   files: ['velocity_sections.png', 'speed_section.png', 'shear_sections.png', 'backscatter.png', 'dac_timeseries.png', 'dac_map_cartopy.png'] },
    { stage: 'tidal_filter',     label: 'Tidal Filter',     files: ['currents_subtidal.nc', 'tidal_filter.png'] },
    { stage: 'diagnostics',      label: 'Diagnostics',      files: ['eke.png', 'transport.png', 'richardson_number.png'] },
  ]

  if (Object.keys(runs).length === 0) {
    scroll.appendChild(buildSection('Processing Runs', 'no runs yet',
      h('div', { class: 'adcp-file-empty' }, 'No processing runs completed yet.')))
  } else {
    Object.entries(runs).forEach(([runLabel, runStatus]) => {
      const zipBtn = h('a', { class: 'btn-primary btn-xs',
        href: `${API}/api/adcp/export?${baseQs}&run=${encodeURIComponent(runLabel)}` },
        '⬇ Export ZIP')

      const list = h('div', { class: 'adcp-file-list' })
      stageGroups.forEach(({ stage, label, files }) => {
        list.appendChild(h('div', { class: 'adcp-stage-divider' }, label))
        files.forEach(file => {
          const prefixed = `${runLabel}_${file}`
          list.appendChild(fileRow(prefixed, runStatus[stage]?.files?.[prefixed] ?? false, runLabel))
        })
      })

      scroll.appendChild(buildSection(`Run: ${runLabel}`, null, list, zipBtn))
    })
  }
}


/* ── Logs page ───────────────────────────────────────────────────────────────*/
function formatBytes (bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function showLogFileViewer (entry) {
  const ac = new AbortController()

  function closeViewer () { ac.abort(); overlay.remove() }

  const overlay = h('div', { class: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeViewer() }
  })
  const modal = h('div', { class: 'modal log-viewer-modal' })

  const titleBadge = h('span', {
    class: `badge ${entry.result === 'pass' ? 'done' : 'failed'}`,
    style: 'margin-right:8px'
  }, entry.result === 'pass' ? '✔ Pass' : '✖ Fail')

  const hdr = h('div', { class: 'modal-header' },
    h('h2', {}, titleBadge, `${entry.dep}  /  ${state.stageInfo[entry.stage]?.label || entry.stage}  —  ${entry.timestamp}`),
    h('button', { class: 'modal-close', onclick: closeViewer }, '×')
  )

  const logPane = h('div', { class: 'log-pane log-viewer-pane' })
  logPane.textContent = 'Loading…'

  modal.appendChild(hdr)
  modal.appendChild(logPane)
  overlay.appendChild(modal)
  document.body.appendChild(overlay)

  try {
    const d = await apiFetch(`/api/logs/read?path=${encodeURIComponent(entry.path)}`, { signal: ac.signal })
    if (!overlay.isConnected) return   // X was clicked while loading — discard result
    logPane.textContent = ''
    // Use a single pre for efficient rendering (avoids per-line DOM churn on large logs)
    const lines = d.content.split('\n')
    const pre   = document.createElement('pre')
    pre.style.cssText = 'margin:0;white-space:pre-wrap;word-break:break-all;'
    // Apply colour to error/warning lines; bundle into a fragment for a single reflow
    const frag = document.createDocumentFragment()
    for (const line of lines) {
      const cls = logColor(line)
      if (cls) {
        const span = document.createElement('span')
        span.className = cls
        span.textContent = line + '\n'
        frag.appendChild(span)
      } else {
        frag.appendChild(document.createTextNode(line + '\n'))
      }
    }
    pre.appendChild(frag)
    logPane.appendChild(pre)
    logPane.scrollTop = 0   // start at the top for archived logs
  } catch (e) {
    if (e.name === 'AbortError' || !overlay.isConnected) return
    logPane.textContent = `Error loading log: ${e.message}`
  }
}

async function renderLogs () {
  const page = document.getElementById('page-logs')
  page.innerHTML = ''

  const dep = state.deployment || ''

  const header = h('div', { class: 'page-header' },
    h('h1', {}, 'Run Logs'),
    h('p',  {}, 'History of every pipeline stage run — pass and fail.')
  )
  const body = h('div', { class: 'page-body' })
  page.appendChild(header)
  page.appendChild(body)

  let logs
  try {
    const url = dep ? `/api/logs?dep=${encodeURIComponent(dep)}` : '/api/logs'
    const d = await apiFetch(url)
    logs = d.logs
  } catch (e) {
    body.appendChild(h('p', { style: 'color:#c0392b;padding:8px' }, `Error loading logs: ${e.message}`))
    return
  }

  if (!logs.length) {
    body.appendChild(h('div', { class: 'logs-empty' },
      h('div', { class: 'logs-empty-icon' }, '📋'),
      h('div', {}, 'No run logs yet.'),
      h('div', { style: 'font-size:12px;margin-top:4px;color:#aab' }, 'Logs are saved automatically after each stage run.')
    ))
    return
  }

  const table = h('table', { class: 'log-table' })
  table.appendChild(h('thead', {},
    h('tr', {},
      h('th', {}, 'Result'),
      h('th', {}, 'Stage'),
      h('th', {}, 'Date / Time'),
      h('th', {}, 'Size'),
      h('th', {}, '')
    )
  ))
  const tbody = h('tbody', {})
  logs.forEach(entry => {
    const badge = h('span', {
      class: `badge ${entry.result === 'pass' ? 'done' : 'failed'}`
    }, entry.result === 'pass' ? '✔ Pass' : '✖ Fail')

    const viewBtn = h('button', { class: 'btn-secondary btn-sm' }, 'View')
    viewBtn.onclick = () => showLogFileViewer(entry)

    tbody.appendChild(h('tr', {},
      h('td', {}, badge),
      h('td', {}, state.stageInfo[entry.stage]?.label || entry.stage),
      h('td', { style: 'font-family:monospace;font-size:11px' }, entry.timestamp),
      h('td', { style: 'color:#8a9bac;font-size:11px' }, formatBytes(entry.size)),
      h('td', {}, viewBtn)
    ))
  })
  table.appendChild(tbody)
  body.appendChild(table)
}

/* ── Folder Picker Modal ─────────────────────────────────────────────────────*/
/**
 * Opens a modal that lets the user browse, create, and rename folders on the
 * connected cloud service, then pick a destination.
 *
 * @param {string}   service   'google' | 'dropbox'
 * @param {function} onConfirm Called with { id, path } when user clicks Upload Here
 * @param {object}   options   Optional: { catId } for per-category localStorage memory
 */
function showFolderPicker (service, onConfirm, options = {}) {
  const lsKey    = options.catId ? `cloud_dest_${service}_${options.catId}` : null
  const savedRaw = lsKey ? localStorage.getItem(lsKey) : null
  let   savedDest = null
  try { if (savedRaw) savedDest = JSON.parse(savedRaw) } catch (_) {}

  const serviceLabel = service === 'google' ? 'Google Drive' : 'Dropbox'

  // State
  let breadcrumb = [service === 'google'
    ? { id: 'root', name: 'My Drive' }
    : { id: '',     name: 'Dropbox'  }]
  let folders    = []
  let renamingId = null
  let loading    = false

  // DOM refs updated by multiple render sub-functions
  let folderListEl = null
  let breadcrumbEl = null
  let footerDestEl = null

  const overlay = h('div', { class: 'modal-overlay' })
  const modal   = h('div', { class: 'folder-picker-modal' })
  overlay.appendChild(modal)
  document.body.appendChild(overlay)

  function currentId   () { return breadcrumb[breadcrumb.length - 1].id }
  function currentPath () { return breadcrumb.map(b => b.name).join(' / ') }

  // ── Render folder rows ──────────────────────────────────────────────────────
  function renderFolderRows () {
    if (!folderListEl) return
    folderListEl.innerHTML = ''

    if (loading) {
      folderListEl.appendChild(h('div', { class: 'fp-empty' }, '⏳ Loading…'))
      return
    }
    if (!folders.length) {
      folderListEl.appendChild(h('div', { class: 'fp-empty' }, '(No subfolders here)'))
      return
    }

    folders.forEach(f => {
      const row = h('div', { class: 'fp-folder-row' })

      if (renamingId === f.id) {
        // ── Inline rename ─────────────────────────────────────────────────────
        const inp = h('input', { type: 'text', class: 'fp-rename-input' })
        inp.value = f.name
        inp.addEventListener('click', e => e.stopPropagation())

        const doSave = async e => {
          e.stopPropagation()
          const nm = inp.value.trim()
          if (!nm || nm === f.name) { renamingId = null; renderFolderRows(); return }
          try {
            const r = await apiFetch(`/api/cloud/folders/${service}/rename`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: f.id, name: nm }),
            })
            f.id   = r.id
            f.name = r.name
          } catch (err) { alert('Rename failed: ' + err.message) }
          renamingId = null
          renderFolderRows()
          renderBreadcrumb()
          renderFooterDest()
        }
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') doSave(e) })

        row.appendChild(h('span', { class: 'fp-folder-icon' }, '📁'))
        row.appendChild(inp)
        row.appendChild(h('button', {
          class: 'btn-secondary fp-rename-btn', onclick: doSave,
        }, 'Save'))
        row.appendChild(h('button', {
          class: 'btn-secondary fp-rename-btn',
          onclick: e => { e.stopPropagation(); renamingId = null; renderFolderRows() },
        }, '✕'))

      } else {
        // ── Normal row ────────────────────────────────────────────────────────
        row.appendChild(h('span', { class: 'fp-folder-icon' }, '📁'))
        row.appendChild(h('span', { class: 'fp-folder-name' }, f.name))
        row.appendChild(h('button', {
          class: 'fp-rename-trigger',
          onclick: e => { e.stopPropagation(); renamingId = f.id; renderFolderRows() },
        }, 'Rename'))
        row.addEventListener('click', () => {
          breadcrumb.push({ id: f.id, name: f.name })
          renamingId = null
          loadFolders(f.id)
          renderBreadcrumb()
          renderFooterDest()
        })
      }
      folderListEl.appendChild(row)
    })
  }

  // ── Render breadcrumb ───────────────────────────────────────────────────────
  function renderBreadcrumb () {
    if (!breadcrumbEl) return
    breadcrumbEl.innerHTML = ''
    breadcrumb.forEach((b, i) => {
      if (i > 0) breadcrumbEl.appendChild(h('span', { class: 'fp-bc-sep' }, ' / '))
      breadcrumbEl.appendChild(h('span', {
        class: `fp-bc-seg${i === breadcrumb.length - 1 ? ' active' : ''}`,
        onclick: () => {
          if (i === breadcrumb.length - 1) return
          breadcrumb = breadcrumb.slice(0, i + 1)
          renamingId = null
          loadFolders(currentId())
          renderBreadcrumb()
          renderFooterDest()
        },
      }, b.name))
    })
  }

  function renderFooterDest () {
    if (footerDestEl) footerDestEl.textContent = `Upload to: ${currentPath()}`
  }

  // ── Load folders from API ───────────────────────────────────────────────────
  async function loadFolders (parentId) {
    loading = true
    renderFolderRows()
    try {
      const r = await apiFetch(
        `/api/cloud/folders/${service}?parent=${encodeURIComponent(parentId)}`)
      folders = r.folders || []
    } catch (err) {
      folders = []
      if (folderListEl) {
        folderListEl.innerHTML = ''
        folderListEl.appendChild(h('div', { class: 'fp-empty' }, '⚠ ' + err.message))
      }
      loading = false
      return
    }
    loading = false
    renderFolderRows()
  }

  // ── Build modal DOM ─────────────────────────────────────────────────────────
  // Header
  modal.appendChild(h('div', { class: 'fp-header' },
    h('span', {}, `Choose Folder — ${serviceLabel}`),
    h('button', { class: 'fp-close', onclick: () => overlay.remove() }, '×')
  ))

  // Breadcrumb bar
  breadcrumbEl = h('div', { class: 'fp-breadcrumb' })
  modal.appendChild(breadcrumbEl)
  renderBreadcrumb()

  // "Last used" banner (shown only when a saved destination exists)
  if (savedDest) {
    const banner = h('div', { class: 'fp-saved-dest' },
      `📌 Last used: ${savedDest.path}`,
      h('button', { class: 'btn-secondary btn-sm',
        style: 'margin-left:10px',
        onclick: () => {
          breadcrumb = savedDest.breadcrumb || [
            service === 'google'
              ? { id: 'root', name: 'My Drive' }
              : { id: '',     name: 'Dropbox'  }
          ]
          renderBreadcrumb()
          renderFooterDest()
          loadFolders(currentId())
        },
      }, 'Jump here')
    )
    modal.appendChild(banner)
  }

  // Folder list (scrollable)
  folderListEl = h('div', { class: 'folder-list' })
  modal.appendChild(folderListEl)

  // New folder input row
  const newFolderInput = h('input', {
    type: 'text', class: 'fp-new-folder-input',
    placeholder: 'New folder name…',
  })
  const addBtn = h('button', { class: 'btn-secondary',
    onclick: async () => {
      const name = newFolderInput.value.trim()
      if (!name) return
      addBtn.disabled = true
      try {
        const r = await apiFetch(`/api/cloud/folders/${service}/create`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, parent: currentId() }),
        })
        newFolderInput.value = ''
        folders.push({ id: r.id, name: r.name })
        folders.sort((a, b) => a.name.localeCompare(b.name))
        renderFolderRows()
      } catch (err) { alert('Could not create folder: ' + err.message) }
      finally { addBtn.disabled = false }
    },
  }, '+ New Folder')
  modal.appendChild(h('div', { class: 'fp-new-folder-row' }, newFolderInput, addBtn))

  // Footer (path display + action buttons)
  footerDestEl = h('div', { class: 'fp-dest' })
  renderFooterDest()

  let mirrorFolders = false
  let subdirToggleEl = null
  if (options.showSubdirToggle) {
    const subdirCb = h('input', { type: 'checkbox' })
    subdirToggleEl = h('div', { class: 'fp-subdir-toggle' },
      subdirCb,
      h('label', {}, ' Create subdirectories for each run folder'))
    subdirCb.addEventListener('change', () => { mirrorFolders = subdirCb.checked })
  }

  modal.appendChild(h('div', { class: 'fp-footer' },
    footerDestEl,
    ...(subdirToggleEl ? [subdirToggleEl] : []),
    h('div', { class: 'fp-footer-btns' },
      h('button', { class: 'btn-secondary', onclick: () => overlay.remove() }, 'Cancel'),
      h('button', { class: 'btn-primary',
        onclick: () => {
          overlay.remove()
          const dest = { id: currentId(), path: currentPath(), breadcrumb: [...breadcrumb],
                         mirrorFolders }
          if (lsKey) localStorage.setItem(lsKey, JSON.stringify(dest))
          onConfirm(dest)
        },
      }, 'Upload Here')
    )
  ))

  // Kick off initial load
  loadFolders(breadcrumb[0].id)
}


/* ── Cloud Storage page ──────────────────────────────────────────────────────*/
async function renderCloud () {
  const page = document.getElementById('page-cloud')
  page.innerHTML = ''
  page.style.overflowY = 'auto'

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'Cloud Storage'),
    h('p',  {}, 'Connect Google Drive or Dropbox to upload reports and data files.')
  ))

  const body = h('div', { class: 'page-body' })
  page.appendChild(body)

  // ── Helper: open OAuth URL in the system browser ──────────────────────────
  function openUrl (url) {
    if (window.APP?.openUrl) window.APP.openUrl(url)
    else window.open(url, '_blank')
  }

  // ── Helper: poll until the account becomes connected (max 90 s) ───────────
  // Returns a cancel function that stops polling and resets the card. Also
  // tracked in the module-level _activeCloudPoll so navigate() can tear this
  // down automatically if the user leaves the page mid-poll instead of only
  // stopping via this card's own Cancel button or the 45-try cap.
  function pollUntilConnected (service, onConnect, onCancel) {
    // Cancel any still-running previous poll first -- without this, clicking
    // Connect/Switch Account twice in a row stacked duplicate intervals with
    // no de-dup.
    if (_activeCloudPoll) _activeCloudPoll()
    let tries = 0
    const cancel = () => { clearInterval(iv); _activeCloudPoll = null }
    const iv = setInterval(async () => {
      tries++
      if (tries > 45) { cancel(); onCancel?.(); return }
      try {
        const ac = await apiFetch('/api/cloud/accounts')
        if (ac[service]?.connected) { cancel(); onConnect(ac) }
      } catch (_) {}
    }, 2000)
    _activeCloudPoll = cancel
    return cancel
  }

  // ── Helper: show "Waiting…" spinner + Cancel button inside a card body ────
  function showWaiting (cardBody, service, onCancel) {
    cardBody.innerHTML = ''
    cardBody.appendChild(h('div', { class: 'cloud-polling' }, '⏳ Waiting for sign-in…'))
    cardBody.appendChild(h('button', {
      class: 'btn-secondary', style: 'margin-top:4px',
      onclick: () => { onCancel(); }
    }, 'Cancel / Try Again'))
  }

  // ── Build a service card ──────────────────────────────────────────────────
  function buildServiceCard (service, label, logoText, acctInfo) {
    const card = h('div', { class: 'cloud-card' })

    // Header row
    card.appendChild(h('div', { class: 'cloud-card-header' },
      h('span', { class: 'cloud-logo' }, logoText),
      h('span', { class: 'cloud-label' }, label)
    ))

    const cardBody = h('div', { class: 'cloud-card-body' })
    card.appendChild(cardBody)

    function renderCardBody (info) {
      cardBody.innerHTML = ''

      if (info.connected) {
        // ── Connected ────────────────────────────────────────────────────────
        cardBody.appendChild(h('div', { class: 'cloud-account-row' },
          h('span', { class: 'cloud-dot connected' }),
          h('div', {},
            h('div', { class: 'cloud-account-email' }, info.email || 'Connected'),
            info.name ? h('div', { class: 'cloud-account-name' }, info.name) : null
          )
        ))
        cardBody.appendChild(h('div', { class: 'cloud-btn-row' },
          h('button', { class: 'btn-danger',
            onclick: async () => {
              await apiFetch('/api/cloud/disconnect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ service }),
              })
              renderCardBody({ connected: false, has_creds: info.has_creds })
            }
          }, 'Disconnect'),
          h('button', { class: 'btn-secondary',
            onclick: async () => {
              // Switch = disconnect then re-auth
              await apiFetch('/api/cloud/disconnect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ service }),
              })
              const r = await apiFetch(`/api/oauth/${service}/start`)
              openUrl(r.url)
              const cancelPoll = pollUntilConnected(service,
                ac  => renderCardBody(ac[service]),
                ()  => renderCardBody({ connected: false, has_creds: true })
              )
              showWaiting(cardBody, service, () => {
                cancelPoll()
                renderCardBody({ connected: false, has_creds: true })
              })
            }
          }, 'Switch Account')
        ))

      } else if (info.has_creds) {
        // ── Has credentials but not connected ────────────────────────────────
        cardBody.appendChild(h('div', { class: 'cloud-account-row' },
          h('span', { class: 'cloud-dot disconnected' }),
          h('span', { class: 'cloud-account-email', style: 'color:#6b7a8d' }, 'Not connected')
        ))
        const connectLabel = service === 'google' ? 'Connect to Google Drive' : 'Connect to Dropbox'
        cardBody.appendChild(h('button', { class: 'btn-primary cloud-connect-btn',
          onclick: async () => {
            const r = await apiFetch(`/api/oauth/${service}/start`)
            // If the server returned the redirect URI, show it so the user can
            // verify it exactly matches what's registered in the provider console.
            if (r.redirect_uri) {
              cardBody.innerHTML = ''
              cardBody.appendChild(h('p', { class: 'cloud-setup-note' },
                'Make sure this URI is registered exactly in your app console:'))
              const uriEl = h('code', { class: 'cloud-config-path',
                style: 'cursor:pointer; user-select:all',
                title: 'Click to copy' })
              uriEl.textContent = r.redirect_uri
              uriEl.onclick = () => navigator.clipboard.writeText(r.redirect_uri)
              cardBody.appendChild(uriEl)
              cardBody.appendChild(h('br'))
              await new Promise(res => setTimeout(res, 1500))
            }
            openUrl(r.url)
            const cancelPoll = pollUntilConnected(service,
              ac => renderCardBody(ac[service]),
              ()  => renderCardBody({ connected: false, has_creds: true })
            )
            showWaiting(cardBody, service, () => {
              cancelPoll()
              renderCardBody({ connected: false, has_creds: true })
            })
          }
        }, connectLabel))

      } else {
        // ── Credentials not found in config file ─────────────────────────────
        const configPath = 'config/cloud/credentials.json'
        const snippet = service === 'google'
          ? `{\n  "google": {\n    "client_id": "…",\n    "client_secret": "…"\n  }\n}`
          : `{\n  "dropbox": {\n    "app_key": "…"\n  }\n}`

        cardBody.appendChild(h('div', { class: 'cloud-setup-note' },
          `${label} credentials not found. Add them to:`))
        cardBody.appendChild(h('code', { class: 'cloud-config-path' }, configPath))
        cardBody.appendChild(h('pre',  { class: 'cloud-config-snippet' }, snippet))
        cardBody.appendChild(h('p', { class: 'cloud-setup-note' },
          'Restart the app after saving the file.'))
      }
    }

    renderCardBody(acctInfo)
    return card
  }

  // ── Load account state and render cards ───────────────────────────────────
  let accounts
  try {
    accounts = await apiFetch('/api/cloud/accounts')
  } catch (e) {
    body.appendChild(h('div', { class: 'empty-state' },
      'Could not load cloud status: ' + e.message))
    return
  }

  const cardsRow = h('div', { class: 'cloud-cards-row' })
  body.appendChild(cardsRow)
  cardsRow.appendChild(buildServiceCard('google',  'Google Drive',
    '🔵', accounts.google))
  cardsRow.appendChild(buildServiceCard('dropbox', 'Dropbox',
    '🔷', accounts.dropbox))

  // ── Upload section (only shown if at least one service is connected) ───────
  const anyConnected = accounts.google.connected || accounts.dropbox.connected
  if (!anyConnected || !state.deployment) return

  body.appendChild(h('div', { class: 'cloud-section-title' }, 'Upload Files'))

  // Load file categories from server
  let catData = {}
  try {
    const r = await apiFetch(`/api/cloud/files/${encodeURIComponent(state.deployment)}`)
    catData = r.categories || {}
  } catch (e) {
    body.appendChild(h('div', { class: 'empty-state' }, 'Could not list files: ' + e.message))
    return
  }

  const UPLOAD_CATEGORIES = [
    { id: 'dat_flight', label: 'Flight DAT Files'     },
    { id: 'dat_merged', label: 'Merged DAT Files'     },
    { id: 'profile',    label: 'Profile NC'           },
    { id: 'traj',       label: 'Trajectory NC'        },
    { id: 'qc',         label: 'QC Reporting'         },
    { id: 'qc_nc',      label: 'QC Output NC'         },
    { id: 'plots',      label: 'QC Plots'             },
    { id: 'adcp',       label: 'AD2CP Files & Plots'  },
  ]

  function fmtSize (n) {
    return n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB`
         : n >= 1e3 ? `${(n / 1e3).toFixed(0)} KB`
         : `${n} B`
  }

  function askConflictMode (statusEl, total, totalBytes) {
    return new Promise(resolve => {
      statusEl.innerHTML = ''
      statusEl.appendChild(h('div', { class: 'upload-conflict-prompt' },
        h('div', { class: 'upload-conflict-summary' },
          `${total} file${total === 1 ? '' : 's'} · ${fmtSize(totalBytes)}`),
        h('div', { class: 'upload-conflict-text' },
          'What should happen if a file already exists at the destination?'),
        h('div', { class: 'cloud-btn-row', style: 'margin-top:8px; flex-wrap:wrap' },
          h('button', { class: 'btn-primary btn-sm',
            onclick: () => resolve('overwrite') }, 'Overwrite All'),
          h('button', { class: 'btn-secondary btn-sm',
            onclick: () => resolve('skip') }, 'Skip Duplicates'),
          h('button', { class: 'btn-secondary btn-sm',
            onclick: () => resolve(null) }, 'Cancel')
        )
      ))
    })
  }

  // fileObjs: [{path, size, label}]  destPath: human-readable folder path
  // options.conflict: pre-set conflict mode ('overwrite'|'skip') — skips the prompt
  async function doUploadWithStatus (svc, fileObjs, destId, destPath, statusEl, options = {}) {
    const total      = fileObjs.length
    const totalBytes = fileObjs.reduce((s, f) => s + (f.size || 0), 0)
    const useSubdirs = options.autoSubdirs || false
    const items      = fileObjs.map(f => {
      if (!useSubdirs || !f.label || !/[\\/]/.test(f.label)) return f.path
      return { path: f.path, subdir: f.label.split(/[\\/]/)[0] }
    })
    if (!total) return

    const conflict = options.conflict != null
      ? options.conflict
      : await askConflictMode(statusEl, total, totalBytes)
    if (conflict === null) { statusEl.innerHTML = ''; return }

    // Build progress UI
    statusEl.innerHTML = ''
    const wrap     = h('div', { class: 'upload-progress-wrap' })
    const barOuter = h('div', { class: 'upload-progress-bar' })
    const barFill  = h('div', { class: 'upload-progress-fill' })
    barOuter.appendChild(barFill)
    const counter = h('div', { class: 'upload-progress-counter' }, `0 / ${total}`)
    const curFile = h('div', { class: 'upload-progress-file' }, '…')
    wrap.appendChild(barOuter)
    wrap.appendChild(counter)
    wrap.appendChild(curFile)
    statusEl.appendChild(wrap)

    let cancelled = false
    const cancelBtn = h('button', { class: 'btn-secondary btn-sm',
      style: 'margin-top:6px',
      onclick: () => { cancelled = true; cancelBtn.disabled = true; cancelBtn.textContent = 'Cancelling…' },
    }, 'Cancel')
    statusEl.appendChild(cancelBtn)

    const CONCURRENCY  = 4
    const MAX_RETRIES  = 2
    const RETRY_DELAY  = 1500  // ms; multiplied by attempt number
    let queueIndex = 0, completed = 0, ok = 0, failed = 0, skipped = 0

    async function tryUpload (item) {
      for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
        try {
          return await apiFetch('/api/cloud/upload', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service: svc, files: [item], dest: destId, conflict }),
          })
        } catch (err) {
          if (attempt === MAX_RETRIES) throw err
          await new Promise(r => setTimeout(r, RETRY_DELAY * (attempt + 1)))
        }
      }
    }

    curFile.textContent = `Uploading up to ${Math.min(CONCURRENCY, total)} files in parallel…`

    const workers = Array.from({ length: Math.min(CONCURRENCY, total) }, async () => {
      while (!cancelled) {
        const i = queueIndex++
        if (i >= total) break
        try {
          const r = await tryUpload(items[i])
          if (r.skipped) skipped++; else ok++
        } catch (_) { failed++ }
        completed++
        barFill.style.width = `${Math.round((completed / total) * 100)}%`
        counter.textContent = `${completed} / ${total}`
        const itemPath = typeof items[i] === 'string' ? items[i] : items[i].path
        curFile.textContent = itemPath.split(/[\\/]/).pop()
      }
    })
    await Promise.all(workers)

    // Final state
    barFill.style.width = '100%'
    const parts = []
    if (ok)      parts.push(`${ok} uploaded`)
    if (skipped) parts.push(`${skipped} skipped`)
    if (failed)  parts.push(`${failed} failed`)
    counter.textContent = parts.join(', ') || 'Done'
    curFile.textContent = cancelled ? 'Cancelled.' : ''
    cancelBtn.remove()
    if (!cancelled && !failed) barFill.style.background = '#4ade80'
    else if (failed)           barFill.style.background = '#f87171'

    if (!cancelled) addHistory({ ts: Date.now(), service: svc, dest: destPath || destId,
      total, ok, skipped, failed })
  }

  function buildCategorySection (catId, catLabel, files) {
    let expanded          = false
    let rendered          = false
    let currentVisible    = []
    let currentFilterTerm = ''
    const checkedPaths    = new Set()

    let sectionStatusEl
    let catBody
    let lastClickedIdx = -1
    const hasSubdirs = files.some(f => /[\\/]/.test(f.label))
    const folderExpandState = new Map() // folderName -> expanded bool, persists across re-renders

    const section = h('div', { class: 'cloud-cat-section' })

    // ── Header ──────────────────────────────────────────────────────────────
    const toggleIcon   = h('span', { class: 'cloud-cat-toggle' }, '▶')
    const countEl      = h('span', { class: 'cloud-cat-count' }, `${files.length} files`)
    const uploadAllRow = h('div',  { class: 'cloud-cat-upload-all' })

    // Select-all checkbox in header
    const headerCb = h('input', { type: 'checkbox', class: 'cloud-header-cb',
      title: 'Select / deselect all visible files' })
    headerCb.addEventListener('click', e => {
      e.stopPropagation()
      if (!expanded) expandSection()
      const allSel = currentVisible.length > 0
        && currentVisible.every(f => checkedPaths.has(f.path))
      currentVisible.forEach(f => {
        if (allSel) checkedPaths.delete(f.path)
        else        checkedPaths.add(f.path)
      })
      if (rendered) renderFiles(currentFilterTerm)
      updateSelBtns()
    })

    function updateHeaderCb () {
      if (!currentVisible.length) {
        headerCb.checked = false; headerCb.indeterminate = false; return
      }
      const n = currentVisible.filter(f => checkedPaths.has(f.path)).length
      headerCb.indeterminate = n > 0 && n < currentVisible.length
      headerCb.checked       = n === currentVisible.length
    }

    function expandSection () {
      if (!expanded) {
        expanded = true
        toggleIcon.textContent = '▼'
        catBody.style.display  = ''
        if (!rendered) { renderFiles(); rendered = true }
      }
    }

    function makeUploadAllBtn (svc, btnLabel) {
      return h('button', { class: 'btn-secondary btn-sm',
        onclick: e => {
          e.stopPropagation()
          if (!files.length) return
          expandSection()
          showFolderPicker(svc, dest => {
            doUploadWithStatus(svc, files, dest.id, dest.path, sectionStatusEl,
              { autoSubdirs: dest.mirrorFolders })
          }, { catId, showSubdirToggle: hasSubdirs })
        },
      }, btnLabel)
    }

    function makeResumeBtn (svc, btnLabel) {
      return h('button', { class: 'btn-secondary btn-sm',
        onclick: async e => {
          e.stopPropagation()
          if (!files.length) return
          expandSection()
          showFolderPicker(svc, async dest => {
            const mirrorFolders = dest.mirrorFolders || false
            sectionStatusEl.innerHTML = ''
            sectionStatusEl.appendChild(
              h('div', { class: 'cloud-resume-checking' }, '⏳ Checking destination…'))
            try {
              const r = await apiFetch(
                `/api/cloud/existing?service=${svc}&dest=${encodeURIComponent(dest.id)}`)
              const existingSet = new Set(r.filenames.map(n => n.toLowerCase()))
              const missing = files.filter(
                f => !existingSet.has(f.path.split(/[\\/]/).pop().toLowerCase()))

              sectionStatusEl.innerHTML = ''
              if (!missing.length) {
                sectionStatusEl.appendChild(
                  h('div', { class: 'cloud-resume-info' },
                    `✓ All ${files.length} files are already at the destination.`))
                return
              }

              const alreadyDone = files.length - missing.length
              sectionStatusEl.appendChild(
                h('div', { class: 'cloud-resume-info' },
                  `${alreadyDone} already at destination · uploading ${missing.length} missing files…`))

              // Auto-select the missing files so checkboxes reflect resume state
              checkedPaths.clear()
              missing.forEach(f => checkedPaths.add(f.path))
              if (rendered) renderFiles(currentFilterTerm)
              updateSelBtns()

              doUploadWithStatus(svc, missing, dest.id, dest.path, sectionStatusEl,
                { conflict: 'overwrite', autoSubdirs: mirrorFolders })
            } catch (err) {
              sectionStatusEl.innerHTML = ''
              sectionStatusEl.appendChild(
                h('div', { class: 'cloud-resume-info error' },
                  '⚠ Could not check destination: ' + err.message))
            }
          }, { catId, showSubdirToggle: hasSubdirs })
        },
      }, btnLabel)
    }

    if (accounts.google.connected) {
      uploadAllRow.appendChild(makeUploadAllBtn('google', '⬆ Drive'))
      uploadAllRow.appendChild(makeResumeBtn('google', '↺ Drive'))
    }
    if (accounts.dropbox.connected) {
      uploadAllRow.appendChild(makeUploadAllBtn('dropbox', '⬆ Dropbox'))
      uploadAllRow.appendChild(makeResumeBtn('dropbox', '↺ Dropbox'))
    }

    const header = h('div', { class: 'cloud-cat-header' },
      headerCb, toggleIcon, h('span', { class: 'cloud-cat-label' }, catLabel), countEl,
      uploadAllRow)
    section.appendChild(header)

    // ── Body (lazy render, shown on first expand) ────────────────────────────
    catBody = h('div', { class: 'cloud-cat-body', style: 'display:none' })
    section.appendChild(catBody)

    const searchInput = h('input', { type: 'text', class: 'cloud-search',
      placeholder: `Search ${catLabel}…` })
    catBody.appendChild(searchInput)

    const fileListEl = h('div', { class: 'cloud-cat-file-list' })
    catBody.appendChild(fileListEl)

    const selBtnsRow = h('div', { class: 'cloud-btn-row', style: 'margin-top:8px' })
    catBody.appendChild(selBtnsRow)

    sectionStatusEl = h('div', { class: 'cloud-upload-status' })
    catBody.appendChild(sectionStatusEl)

    function updateSelBtns () {
      selBtnsRow.innerHTML = ''
      const n = checkedPaths.size
      if (!n) return
      if (accounts.google.connected)
        selBtnsRow.appendChild(h('button', { class: 'btn-primary btn-sm',
          onclick: () => showFolderPicker('google', dest => {
            const sel = files.filter(f => checkedPaths.has(f.path))
            doUploadWithStatus('google', sel, dest.id, dest.path, sectionStatusEl,
              { autoSubdirs: dest.mirrorFolders })
          }, { catId, showSubdirToggle: hasSubdirs }),
        }, `⬆ Upload ${n} to Drive`))
      if (accounts.dropbox.connected)
        selBtnsRow.appendChild(h('button', { class: 'btn-primary btn-sm',
          onclick: () => showFolderPicker('dropbox', dest => {
            const sel = files.filter(f => checkedPaths.has(f.path))
            doUploadWithStatus('dropbox', sel, dest.id, dest.path, sectionStatusEl,
              { autoSubdirs: dest.mirrorFolders })
          }, { catId, showSubdirToggle: hasSubdirs }),
        }, `⬆ Upload ${n} to Dropbox`))
    }

    function renderFiles (filterTerm) {
      currentFilterTerm = filterTerm || ''
      fileListEl.innerHTML = ''
      const term = currentFilterTerm.toLowerCase()
      currentVisible = files.filter(f => !term || f.label.toLowerCase().includes(term))
      updateHeaderCb()

      if (!currentVisible.length) {
        fileListEl.appendChild(h('div', { class: 'fp-empty' },
          term ? 'No files match search.' : 'No files in this category.'))
        return
      }

      if (hasSubdirs) {
        // ── Grouped by first path segment (run folder) ────────────────────────
        const groups = new Map()
        currentVisible.forEach(f => {
          const sep    = f.label.search(/[\\/]/)
          const folder = sep >= 0 ? f.label.slice(0, sep) : ''
          if (!groups.has(folder)) groups.set(folder, [])
          groups.get(folder).push(f)
        })

        groups.forEach((groupFiles, folderName) => {
          const isExp    = folderExpandState.get(folderName) ?? true
          const groupCbs = []

          const folderCb = h('input', { type: 'checkbox', class: 'cloud-folder-cb' })
          function syncFolderCb () {
            const n = groupFiles.filter(f => checkedPaths.has(f.path)).length
            folderCb.checked       = n === groupFiles.length && groupFiles.length > 0
            folderCb.indeterminate = n > 0 && n < groupFiles.length
          }
          syncFolderCb()
          folderCb.addEventListener('click', e => {
            e.stopPropagation()
            const allSel = groupFiles.every(f => checkedPaths.has(f.path))
            groupFiles.forEach((f, i) => {
              if (allSel) { checkedPaths.delete(f.path); groupCbs[i].checked = false }
              else        { checkedPaths.add(f.path);    groupCbs[i].checked = true  }
            })
            syncFolderCb(); updateHeaderCb(); updateSelBtns()
          })

          const colIcon    = h('span', { class: 'cloud-folder-toggle' }, isExp ? '▼' : '▶')
          const totalSize  = groupFiles.reduce((s, f) => s + f.size, 0)
          const folderBody = h('div', { class: 'cloud-folder-body',
            style: isExp ? '' : 'display:none' })

          const folderHdr = h('div', { class: 'cloud-folder-header' },
            folderCb, colIcon,
            h('span', { class: 'cloud-folder-name' }, folderName || '(shared files)'),
            h('span', { class: 'cloud-folder-meta' },
              `${groupFiles.length} file${groupFiles.length !== 1 ? 's' : ''} · ${fmtSize(totalSize)}`)
          )
          folderHdr.addEventListener('click', e => {
            if (e.target === folderCb) return
            const nowExp = !(folderExpandState.get(folderName) ?? true)
            folderExpandState.set(folderName, nowExp)
            colIcon.textContent      = nowExp ? '▼' : '▶'
            folderBody.style.display = nowExp ? '' : 'none'
          })
          fileListEl.appendChild(folderHdr)
          fileListEl.appendChild(folderBody)

          groupFiles.forEach(f => {
            const cb = h('input', { type: 'checkbox' })
            cb.checked = checkedPaths.has(f.path)
            groupCbs.push(cb)

            const sep   = f.label.search(/[\\/]/)
            const fname = sep >= 0 ? f.label.slice(sep + 1) : f.label
            const row = h('label', { class: 'cloud-file-row cloud-file-row--nested' },
              cb,
              h('span', { class: 'cloud-file-name' }, fname),
              h('span', { class: 'cloud-file-size' }, fmtSize(f.size))
            )
            row.addEventListener('click', e => {
              if (e.target === cb) {
                if (cb.checked) checkedPaths.add(f.path)
                else            checkedPaths.delete(f.path)
              } else {
                e.preventDefault()
                const nowChecked = !checkedPaths.has(f.path)
                if (nowChecked) checkedPaths.add(f.path)
                else            checkedPaths.delete(f.path)
                cb.checked = nowChecked
              }
              syncFolderCb(); updateHeaderCb(); updateSelBtns()
            })
            folderBody.appendChild(row)
          })
        })

      } else {
        // ── Flat list ─────────────────────────────────────────────────────────
        const rowCbs = []
        currentVisible.forEach((f, idx) => {
          const cb = h('input', { type: 'checkbox' })
          cb.checked = checkedPaths.has(f.path)
          rowCbs.push(cb)

          const row = h('label', { class: 'cloud-file-row' },
            cb,
            h('span', { class: 'cloud-file-name' }, f.label),
            h('span', { class: 'cloud-file-size' }, fmtSize(f.size))
          )
          row.addEventListener('click', e => {
            if (e.target === cb) {
              if (cb.checked) checkedPaths.add(f.path)
              else            checkedPaths.delete(f.path)
              lastClickedIdx = idx
              updateSelBtns()
              updateHeaderCb()
              return
            }
            e.preventDefault()
            const nowChecked = !checkedPaths.has(f.path)
            if (e.shiftKey && lastClickedIdx >= 0 && lastClickedIdx !== idx) {
              const lo = Math.min(lastClickedIdx, idx)
              const hi = Math.max(lastClickedIdx, idx)
              for (let i = lo; i <= hi; i++) {
                if (nowChecked) checkedPaths.add(currentVisible[i].path)
                else            checkedPaths.delete(currentVisible[i].path)
                rowCbs[i].checked = nowChecked
              }
            } else {
              if (nowChecked) checkedPaths.add(f.path)
              else            checkedPaths.delete(f.path)
              cb.checked = nowChecked
              lastClickedIdx = idx
            }
            updateSelBtns()
            updateHeaderCb()
          })
          fileListEl.appendChild(row)
        })
      }
    }

    searchInput.addEventListener('input', e => { lastClickedIdx = -1; renderFiles(e.target.value) })

    header.addEventListener('click', () => {
      expanded = !expanded
      toggleIcon.textContent = expanded ? '▼' : '▶'
      catBody.style.display  = expanded ? '' : 'none'
      if (expanded && !rendered) { renderFiles(); rendered = true }
    })

    return section
  }

  const uploadSection = h('div', { class: 'cloud-upload-section' })
  body.appendChild(uploadSection)

  UPLOAD_CATEGORIES.forEach(({ id, label }) => {
    const cat = catData[id] || { files: [] }
    uploadSection.appendChild(buildCategorySection(id, label, cat.files || []))
  })

  // ── Upload history ─────────────────────────────────────────────────────────
  const HIST_KEY = 'cloud_upload_history'
  function getHistory () {
    try { return JSON.parse(localStorage.getItem(HIST_KEY) || '[]') } catch (_) { return [] }
  }

  let historyBodyEl

  function addHistory (entry) {
    const hist = getHistory()
    hist.unshift(entry)
    if (hist.length > 50) hist.length = 50
    localStorage.setItem(HIST_KEY, JSON.stringify(hist))
    renderHistoryEntries()
  }

  function renderHistoryEntries () {
    if (!historyBodyEl) return
    historyBodyEl.innerHTML = ''
    const hist = getHistory()
    if (!hist.length) {
      historyBodyEl.appendChild(h('div', { class: 'cloud-hist-empty' }, 'No uploads yet.'))
      return
    }
    hist.forEach(e => {
      const svcLabel = e.service === 'google' ? '📤 Google Drive' : '📦 Dropbox'
      const parts    = []
      if (e.ok)      parts.push(`${e.ok} uploaded`)
      if (e.skipped) parts.push(`${e.skipped} skipped`)
      if (e.failed)  parts.push(`${e.failed} failed`)
      historyBodyEl.appendChild(h('div', { class: 'cloud-hist-entry' },
        h('div', { class: 'cloud-hist-row' },
          h('span', { class: 'cloud-hist-svc' }, svcLabel),
          h('span', { class: 'cloud-hist-dest' }, `→ ${e.dest}`),
          h('span', { class: 'cloud-hist-ts' }, new Date(e.ts).toLocaleString()),
        ),
        h('div', { class: 'cloud-hist-detail' },
          `${e.total} file${e.total === 1 ? '' : 's'} · ${parts.join(', ') || 'done'}`),
      ))
    })
  }

  const histPanel = h('div', { class: 'cloud-hist-panel' })
  body.appendChild(histPanel)
  histPanel.appendChild(h('div', { class: 'cloud-hist-title' },
    h('span', {}, 'Recent Uploads'),
    h('button', { class: 'btn-secondary btn-sm',
      onclick: () => { localStorage.removeItem(HIST_KEY); renderHistoryEntries() },
    }, 'Clear'),
  ))
  historyBodyEl = h('div', { class: 'cloud-hist-body' })
  histPanel.appendChild(historyBodyEl)
  renderHistoryEntries()
}

/* ── AD2CP page ──────────────────────────────────────────────────────────────*/
async function renderAdcp () {
  const page = document.getElementById('page-adcp')
  page.innerHTML = ''
  page.style.overflowY = 'auto'

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'AD2CP Processing'),
    h('p',  {}, 'Process Nortek AD2CP data for glider deployments or standalone lab tests.'),
  ))

  const body = h('div', { class: 'page-body' })
  page.appendChild(body)

  // ── Shared state ────────────────────────────────────────────────────────────
  let isTest       = false
  let testDep      = null
  let selectedFile = null
  const _ADCP_OPTS_KEY  = 'adcp_opts'
  const _adcpOptsDflt   = { xaxis: 1, yaxis: null, depth_min: 10, depth_max: 1000,
                             qc_corr: 80, qc_amp: 80, qc_vel: 0.8, qc_snr: 3,
                             mounting: 'auto', weight_bias: false, vel_dep_bias: false,
                             pitch_offset: 0, roll_offset: 0, heading_offset: 0 }
  const _savedAdcpOpts  = (() => {
    try { return JSON.parse(localStorage.getItem(_ADCP_OPTS_KEY) || 'null') } catch (_) { return null }
  })() || {}
  const adcpOpts   = { ..._adcpOptsDflt, ..._savedAdcpOpts }
  function saveAdcpOpts () { localStorage.setItem(_ADCP_OPTS_KEY, JSON.stringify(adcpOpts)) }
  const adcpLogs   = {}    // stage -> [lines]
  const adcpCtrl   = {}    // stage -> AbortController
  const adcpIvs    = {}    // stage -> setInterval id (timer)

  function adcpRunLabel (opts) {
    const xaxis = parseInt(opts.xaxis ?? 1)
    const dmin  = parseInt(parseFloat(opts.depth_min ?? 10))
    const dmax  = parseInt(parseFloat(opts.depth_max ?? 1000))
    let label = `xaxis${xaxis}_d${dmin}-${dmax}`
    if (opts.yaxis != null) label += `_y${parseInt(parseFloat(opts.yaxis))}`
    const corr = parseFloat(opts.qc_corr ?? 80)
    const amp  = parseFloat(opts.qc_amp  ?? 80)
    const vel  = parseFloat(opts.qc_vel  ?? 0.8)
    const snr  = parseFloat(opts.qc_snr  ?? 3)
    if (corr !== 80 || amp !== 80 || vel !== 0.8 || snr !== 3)
      label += `_qc${corr}-${amp}-${vel}-${snr}`
    const mount = opts.mounting ?? 'auto'
    if (mount !== 'auto') label += `_${mount}`
    const p = parseFloat(opts.pitch_offset ?? 0)
    const r = parseFloat(opts.roll_offset  ?? 0)
    const hd = parseFloat(opts.heading_offset ?? 0)
    if (p !== 0 || r !== 0 || hd !== 0) label += `_off${p}p${r}r${hd}h`
    return label
  }

  // ── Mode toggle ─────────────────────────────────────────────────────────────
  const btnDeploy = h('button', { class: 'btn-primary btn-sm' }, 'Deployment')
  const btnTest   = h('button', { class: 'btn-secondary btn-sm' }, 'Lab Test')
  btnDeploy.addEventListener('click', () => setMode(false))
  btnTest.addEventListener('click',   () => setMode(true))
  body.appendChild(h('div', { class: 'adcp-mode-row' }, btnDeploy, btnTest))

  const mainArea = h('div', {})
  body.appendChild(mainArea)

  function setMode (test) {
    isTest = test
    btnDeploy.className = test ? 'btn-secondary btn-sm' : 'btn-primary btn-sm'
    btnTest.className   = test ? 'btn-primary btn-sm'   : 'btn-secondary btn-sm'
    selectedFile = null
    renderMain()
  }

  // ── Main area renderer ───────────────────────────────────────────────────────
  async function renderMain () {
    mainArea.innerHTML = ''

    if (isTest) {
      const testSec = h('div', { class: 'adcp-section' })
      mainArea.appendChild(testSec)
      await renderTestSelector(testSec)
    } else {
      if (!state.deployment) {
        mainArea.appendChild(h('div', { class: 'adcp-empty' },
          'Select a deployment from the sidebar to continue.'))
        return
      }
    }

    const dep = isTest ? testDep : state.deployment
    if (!dep) return

    const fileSec = h('div', { class: 'adcp-section' })
    mainArea.appendChild(fileSec)
    await renderFilePicker(fileSec, dep)
  }

  // ── Test selector ────────────────────────────────────────────────────────────
  async function renderTestSelector (container) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'adcp-section-title' }, 'Lab Test'))

    const newInput = h('input', { type: 'text', class: 'adcp-input',
      placeholder: 'Test label  (e.g. Lab_Test_2026-07-15)' })
    const newBtn = h('button', { class: 'btn-secondary btn-sm' }, '+ New Test')
    newBtn.addEventListener('click', async () => {
      const label = newInput.value.trim()
      if (!label) return
      await apiFetch('/api/adcp/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      })
      newInput.value = ''
      await renderTestSelector(container)
    })
    container.appendChild(h('div', { class: 'adcp-new-test-row' }, newInput, newBtn))

    let r
    try { r = await apiFetch('/api/adcp/tests') } catch { r = { tests: [] } }
    const tests = r.tests || []

    if (!tests.length) {
      container.appendChild(h('div', { class: 'adcp-empty' }, 'No test runs yet. Create one above.'))
      return
    }
    const list = h('div', { class: 'adcp-test-list' })
    tests.forEach(t => {
      const delBtn = h('button', { class: 'adcp-file-del', title: 'Delete test' }, '×')
      delBtn.addEventListener('click', async e => {
        e.stopPropagation()
        if (!confirm(`Delete test "${t}" and all its files?`)) return
        try {
          await apiFetch(`/api/adcp/tests/${encodeURIComponent(t)}`, { method: 'DELETE' })
          if (testDep === t) { testDep = null }
          await renderTestSelector(container)
          if (!testDep) mainArea.querySelectorAll('.adcp-section:not(:first-child)').forEach(el => el.remove())
        } catch (err) { alert('Delete failed: ' + err.message) }
      })
      const row = h('div', { class: `adcp-test-item${t === testDep ? ' active' : ''}` },
        h('span', { class: 'adcp-file-name' }, t), delBtn)
      row.addEventListener('click', () => { testDep = t; renderMain() })
      list.appendChild(row)
    })
    container.appendChild(list)
  }

  // ── File picker ──────────────────────────────────────────────────────────────
  async function renderFilePicker (container, dep) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'adcp-section-title' }, 'AD2CP Input File'))

    let r
    try {
      r = await apiFetch(`/api/adcp/files?dep=${encodeURIComponent(dep)}&is_test=${isTest}`)
    } catch { r = { files: [] } }
    const files = r.files || []

    // Drop zone — always shown; shows file list when files exist
    const dropStatus = h('div', { class: 'adcp-drop-status hidden' })

    async function importAndRefresh (droppedFiles) {
      const ncFiles = droppedFiles.filter(f => f.name.toLowerCase().endsWith('.nc'))
      if (!ncFiles.length) { dropStatus.textContent = 'Only .nc files are accepted.'; dropStatus.classList.remove('hidden'); return }
      dropStatus.textContent = `Copying ${ncFiles.length} file(s)…`
      dropStatus.classList.remove('hidden')
      try {
        await apiFetch('/api/adcp/import', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ dep, is_test: isTest, paths: ncFiles.map(f => f.path) }),
        })
        dropStatus.classList.add('hidden')
        // Re-render the file picker so the new file appears
        await renderFilePicker(container, dep)
      } catch (e) {
        dropStatus.textContent = 'Copy failed: ' + e.message
      }
    }

    const dropZone = h('div', { class: 'adcp-dropzone' },
      h('div', { class: 'adcp-drop-icon' }, '↑'),
      h('div', { class: 'adcp-drop-text' }, files.length ? 'Drop .nc file to add' : 'Drop Nortek .nc file here'),
      files.length ? null : h('div', { class: 'adcp-drop-hint' }, 'or browse to file location'),
    )
    dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag-over') })
    dropZone.addEventListener('dragleave', e => { if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('drag-over') })
    dropZone.addEventListener('drop', e => {
      e.preventDefault(); dropZone.classList.remove('drag-over')
      importAndRefresh(Array.from(e.dataTransfer.files))
    })

    let fileList = null
    if (files.length) {
      fileList = h('div', { class: 'adcp-file-list' })
      files.forEach(f => {
        const isActive = f === selectedFile
        const nameSpan = h('span', { class: 'adcp-file-name' }, f)
        const delBtn   = h('button', { class: 'adcp-file-del', title: 'Remove file' }, '×')

        delBtn.addEventListener('click', async e => {
          e.stopPropagation()
          if (!confirm(`Remove "${f}" from the input folder?`)) return
          try {
            const qs = `dep=${encodeURIComponent(dep)}&is_test=${isTest}`
            await apiFetch(`/api/adcp/files/${encodeURIComponent(f)}?${qs}`, { method: 'DELETE' })
            if (selectedFile === f) selectedFile = null
            await renderFilePicker(container, dep)
          } catch (err) {
            alert('Delete failed: ' + err.message)
          }
        })

        const row = h('div', { class: `adcp-file-item${isActive ? ' active' : ''}` }, nameSpan, delBtn)
        row.addEventListener('click', () => {
          selectedFile = f
          fileList.querySelectorAll('.adcp-file-item').forEach(el => el.classList.remove('active'))
          row.classList.add('active')
          renderBelow(dep)
        })
        fileList.appendChild(row)
      })
      container.appendChild(fileList)
      container.appendChild(h('div', { class: 'adcp-file-hint' },
        files.length === 1 && !selectedFile
          ? 'Click the file above, then scroll down to run stages.'
          : ''))
    }

    container.appendChild(dropZone)
    container.appendChild(dropStatus)

    // Options + stages go below; auto-select if only one file
    const belowArea = h('div', {})
    container.appendChild(belowArea)

    async function renderBelow (dep) {
      belowArea.innerHTML = ''

      // Options
      const optsSec = h('div', { class: 'adcp-section' })
      belowArea.appendChild(optsSec)
      renderOptions(optsSec, () => renderBelow(dep))

      // Stages
      belowArea.appendChild(h('div', { class: 'adcp-section-title', style: 'margin-top:20px' }, 'Processing Stages'))

      let statusR, liveR
      try {
        ;[statusR, liveR] = await Promise.all([
          apiFetch(`/api/adcp/status?dep=${encodeURIComponent(dep)}&is_test=${isTest}`),
          apiFetch(`/api/adcp/live-stages?dep=${encodeURIComponent(dep)}&is_test=${isTest}`).catch(() => ({ running: [] })),
        ])
      } catch { statusR = { shared: {}, runs: {} }; liveR = { running: [] } }

      // Stages currently streaming in this JS context or still running on the server
      const liveStages = new Set([
        ...(liveR.running || []),
        ...Object.keys(adcpCtrl),   // active fetch loops pointing at old DOM
      ])

      // Merge shared-stage statuses with the current run's option-stage statuses
      const currentRun = adcpRunLabel(adcpOpts)
      const statusMap  = {
        ...(statusR.shared || {}),
        ...((statusR.runs || {})[currentRun] || {}),
      }

      const stagesDefs = isTest
        ? [
            { id: 'process_shear',   label: 'Process Shear',  needs: [] },
            { id: 'generate_plots',  label: 'Generate Plots', needs: ['process_shear'] },
            { id: 'lab_diagnostics', label: 'Lab Diagnostics', needs: ['process_shear'] },
          ]
        : [
            { id: 'load_nc',          label: 'Load & Combine Profile NCs', needs: [],                                        section: 'Shared stages — run once per deployment, regardless of processing options' },
            { id: 'gps_pairing',      label: 'GPS Pairing',                needs: ['load_nc'] },
            { id: 'write_glider_nc',  label: 'Write Glider NetCDF',        needs: ['load_nc'] },
            { id: 'process_shear',    label: 'Process Shear',              needs: ['write_glider_nc'],                       section: 'Option-specific stages — re-run when X-axis, Y-axis, or Depth slice changes' },
            { id: 'compute_currents', label: 'Compute Absolute Currents',  needs: ['process_shear', 'gps_pairing'] },
            { id: 'bias_correction',  label: 'Bias Correction',            needs: ['compute_currents'] },
            { id: 'generate_plots',   label: 'Generate Plots',             needs: ['bias_correction'] },
            { id: 'tidal_filter',     label: 'Tidal Filtering (40-h low-pass)',       needs: ['bias_correction'] },
            { id: 'diagnostics',      label: 'Diagnostics (EKE · Transport · Ri)',    needs: ['bias_correction', 'process_shear'] },
          ]

      const stagesContainer = h('div', {})
      belowArea.appendChild(stagesContainer)

      stagesDefs.forEach(({ id, label, needs, section }) => {
        if (section) {
          stagesContainer.appendChild(
            h('div', { class: 'adcp-section-title', style: 'margin-top:18px' }, section))
        }
        const inputsMet   = needs.every(n => statusMap[n]?.done)
        const initStatus  = statusMap[id]?.done ? 'done' : 'not_run'
        stagesContainer.appendChild(
          buildAdcpStageCard(id, label, initStatus, dep, inputsMet, stagesContainer, stagesDefs, statusMap,
            () => renderBelow(dep), liveStages.has(id)))
      })

      // Export panel
      const allRuns = statusR.runs || {}
      if (Object.values(statusMap).some(s => s.done) || Object.keys(allRuns).length > 0) {
        belowArea.appendChild(buildAdcpExportPanel(dep, statusMap, allRuns, currentRun))
      }
    }

    if (files.length === 1) {
      selectedFile = files[0]
      fileList && fileList.querySelector('.adcp-file-item').classList.add('active')
      renderBelow(dep)
    } else if (selectedFile && files.includes(selectedFile)) {
      renderBelow(dep)
    }
  }

  // ── Options section ──────────────────────────────────────────────────────────
  function renderOptions (container, onBinChange) {
    container.innerHTML = ''

    const xSel = h('select', { class: 'adcp-input' })
    ;[['1','1 — Time (1 profile per column, max resolution)'],['2','2 — Profile (2 profiles per column, balanced)'],['3','3 — Distance (3 profiles per column, smoother)']].forEach(([v, l]) => {
      const o = h('option', { value: v }, l)
      if (String(adcpOpts.xaxis) === v) o.selected = true
      xSel.appendChild(o)
    })
    xSel.addEventListener('change', () => { adcpOpts.xaxis = parseInt(xSel.value); saveAdcpOpts(); onBinChange?.() })

    const yIn = h('input', { type: 'number', class: 'adcp-input',
      placeholder: 'auto', value: adcpOpts.yaxis ?? '' })
    yIn.addEventListener('input', () => { adcpOpts.yaxis = yIn.value ? parseInt(yIn.value) : null; saveAdcpOpts() })
    yIn.addEventListener('change', () => onBinChange?.())

    const dMinIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.depth_min, min: '0', max: '2000' })
    dMinIn.addEventListener('input', () => { adcpOpts.depth_min = parseFloat(dMinIn.value) || 10; saveAdcpOpts() })
    dMinIn.addEventListener('change', () => onBinChange?.())

    const dMaxIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.depth_max, min: '0', max: '2000' })
    dMaxIn.addEventListener('input', () => { adcpOpts.depth_max = parseFloat(dMaxIn.value) || 1000; saveAdcpOpts() })
    dMaxIn.addEventListener('change', () => onBinChange?.())

    const qcCorrIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.qc_corr, min: '0', max: '128', step: '5' })
    qcCorrIn.addEventListener('input', () => { adcpOpts.qc_corr = parseFloat(qcCorrIn.value) || 80; saveAdcpOpts() })

    const qcAmpIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.qc_amp, min: '0', max: '255', step: '5' })
    qcAmpIn.addEventListener('input', () => { adcpOpts.qc_amp = parseFloat(qcAmpIn.value) || 80; saveAdcpOpts() })

    const qcVelIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.qc_vel, min: '0', max: '5', step: '0.1' })
    qcVelIn.addEventListener('input', () => { adcpOpts.qc_vel = parseFloat(qcVelIn.value) || 0.8; saveAdcpOpts() })

    const qcSnrIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.qc_snr, min: '0', max: '20', step: '1' })
    qcSnrIn.addEventListener('input', () => { adcpOpts.qc_snr = parseFloat(qcSnrIn.value) || 3; saveAdcpOpts() })

    // Mounting direction
    const mountSel = h('select', { class: 'adcp-input' })
    ;[['auto','Auto-detect'],['top','Top-mounted (faces up)'],['bottom','Bottom-mounted (faces down)']].forEach(([v, l]) => {
      const o = h('option', { value: v }, l)
      if ((adcpOpts.mounting ?? 'auto') === v) o.selected = true
      mountSel.appendChild(o)
    })
    mountSel.addEventListener('change', () => { adcpOpts.mounting = mountSel.value; saveAdcpOpts() })

    // Attitude offsets
    const pitchIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.pitch_offset, min: '-30', max: '30', step: '0.5' })
    pitchIn.addEventListener('input', () => { adcpOpts.pitch_offset = parseFloat(pitchIn.value) || 0; saveAdcpOpts() })

    const rollIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.roll_offset, min: '-30', max: '30', step: '0.5' })
    rollIn.addEventListener('input', () => { adcpOpts.roll_offset = parseFloat(rollIn.value) || 0; saveAdcpOpts() })

    const headingIn = h('input', { type: 'number', class: 'adcp-input adcp-input-sm',
      value: adcpOpts.heading_offset, min: '-180', max: '180', step: '1' })
    headingIn.addEventListener('input', () => { adcpOpts.heading_offset = parseFloat(headingIn.value) || 0; saveAdcpOpts() })

    // Bias correction toggles
    const weightChk = h('input', { type: 'checkbox', class: 'adcp-checkbox' })
    weightChk.checked = !!adcpOpts.weight_bias
    weightChk.addEventListener('change', () => { adcpOpts.weight_bias = weightChk.checked; saveAdcpOpts() })

    const velDepChk = h('input', { type: 'checkbox', class: 'adcp-checkbox' })
    velDepChk.checked = !!adcpOpts.vel_dep_bias
    velDepChk.addEventListener('change', () => { adcpOpts.vel_dep_bias = velDepChk.checked; saveAdcpOpts() })

    const optBody = h('div', { class: 'adcp-opts-body' },
      h('div', { class: 'adcp-opt-group-title' }, 'Binning / Output Grid  (changing these requires re-running Process Shear, Compute Currents, and Bias Correction)'),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'X-axis:'), xSel,
        h('span', { class: 'adcp-opt-hint' }, 'Number of consecutive half-profiles (dive legs) averaged into each output column. Time/Profile/Distance are descriptive names — the library parameter is "x-axis resolution in number of profiles."')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Y-axis (m):'), yIn,
        h('span', { class: 'adcp-opt-hint' }, 'blank = use ADCP cell size')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Depth slice (m):'),
        dMinIn, h('span', { class: 'adcp-opt-sep' }, ' – '), dMaxIn,
        h('span', { class: 'adcp-opt-hint' }, 'depth range used for shear-bias regression only — does not clip the output depth range')),
      h('div', { class: 'adcp-opt-group-title' }, 'Instrument  (changing these requires re-running Process Shear)'),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Mounting direction:'), mountSel),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Pitch offset (°):'), pitchIn,
        h('span', { class: 'adcp-opt-hint' }, 'transducer misalignment correction')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Roll offset (°):'), rollIn,
        h('span', { class: 'adcp-opt-hint' }, 'transducer misalignment correction')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Heading offset (°):'), headingIn,
        h('span', { class: 'adcp-opt-hint' }, 'transducer misalignment correction')),
      h('div', { class: 'adcp-opt-group-title' }, 'Quality Control  (changing these requires re-running Process Shear)'),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Min correlation:'), qcCorrIn,
        h('span', { class: 'adcp-opt-hint' }, 'default 80 — lower = more data, more noise')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Max amplitude:'), qcAmpIn,
        h('span', { class: 'adcp-opt-hint' }, 'default 80 — screens side-lobe contamination')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Max velocity (m/s):'), qcVelIn,
        h('span', { class: 'adcp-opt-hint' }, 'default 0.8 — rejects obviously bad pings')),
      h('div', { class: 'adcp-opt-row' },
        h('label', { class: 'adcp-opt-label' }, 'Min SNR (dB):'), qcSnrIn,
        h('span', { class: 'adcp-opt-hint' }, 'default 3 — min signal above noise floor')),
      h('div', { class: 'adcp-opt-group-title' }, 'Bias Correction  (changing these requires re-running Bias Correction only)'),
      h('div', { class: 'adcp-opt-row' },
        weightChk, h('label', { class: 'adcp-opt-label adcp-opt-label-chk' }, 'Weight bias regression by dive distance'),
        h('span', { class: 'adcp-opt-hint' }, 'improves correction when dive lengths vary significantly')),
      h('div', { class: 'adcp-opt-row' },
        velDepChk, h('label', { class: 'adcp-opt-label adcp-opt-label-chk' }, 'Velocity-dependent bias correction'),
        h('span', { class: 'adcp-opt-hint' }, 'fits velocity-dependent coefficients instead of a constant bias')),
    )

    container.appendChild(optBody)
  }

  // ── Stage card ───────────────────────────────────────────────────────────────
  function buildAdcpStageCard (id, label, initStatus, dep, inputsMet, stagesContainer, stagesDefs, statusMap, refreshFn, liveRunning = false) {
    const badge = h('span', {
      class: `status-badge ${liveRunning ? 'running' : (inputsMet ? statusClass(initStatus) : 'not_run')}`,
    }, liveRunning ? 'RUNNING…' : (inputsMet ? statusLabel(initStatus) : 'WAITING'))

    const logEl   = h('div', { class: 'log-pane hidden' })
    const elapsed = h('span', { class: `adcp-elapsed${liveRunning ? '' : ' hidden'}` })

    if (!liveRunning && adcpLogs[id]?.length) {
      adcpLogs[id].forEach(l => appendLog(logEl, l))
      logEl.classList.remove('hidden')
    }

    const btnRun = h('button', {
      class: `btn-success${(inputsMet && !liveRunning) ? '' : ' hidden'}`,
    }, initStatus === 'done' ? 'Re-run' : 'Run')
    btnRun.disabled = !inputsMet || liveRunning

    const btnAbort = h('button', { class: `btn-danger${liveRunning ? '' : ' hidden'}` }, 'Cancel')
    const btnClear = h('button', { class: 'btn-secondary' }, 'Clear log')

    btnClear.addEventListener('click', () => {
      logEl.innerHTML = ''; logEl.classList.add('hidden'); adcpLogs[id] = []
    })

    btnRun.addEventListener('click', () =>
      runAdcpStage(id, label, dep, badge, btnRun, btnAbort, logEl, elapsed,
                   stagesContainer, stagesDefs, statusMap, refreshFn))

    btnAbort.addEventListener('click', () => {
      if (adcpIvs[id])  { clearInterval(adcpIvs[id]);  delete adcpIvs[id] }
      const ctrl = adcpCtrl[id]
      if (ctrl) { ctrl.abort(); delete adcpCtrl[id] }
      badge.className   = 'status-badge failed'
      badge.textContent = 'CANCELLED'
      btnRun.classList.remove('hidden')
      btnAbort.classList.add('hidden')
      elapsed.classList.add('hidden')
    })

    // Xaxis recommendation — shown for process_shear stage; falls back to any
    // existing run's shear file so the tip appears even before the current
    // config has been processed.
    const recBox = h('div', {})
    if (id === 'process_shear') {
      const runLabel = adcpRunLabel(adcpOpts)
      const qs = `dep=${encodeURIComponent(dep)}&run=${encodeURIComponent(runLabel)}&is_test=${isTest}`;
      (async () => {
        try {
          const r = await apiFetch(`/api/adcp/shear_stats?${qs}`)
          if (r.rec_xaxis != null) {
            const isCurrent = adcpOpts.xaxis === r.rec_xaxis
            const borderColor = r.rec_xaxis === 1 ? '#1e8449' : r.rec_xaxis === 2 ? '#2471a3' : '#b7770d'
            const currentNoteEl = isCurrent
              ? h('span', { style: 'color:#1e8449' }, ' ✔ already set')
              : h('span', {}, ' — ', h('em', {}, `currently set to xaxis=${adcpOpts.xaxis}`))
            const sourceNoteEl = (r.source_run && r.source_run !== runLabel)
              ? h('div', { style: 'margin-top:6px;font-size:11.5px;color:#888' },
                  'Based on shear stats from existing run ', h('code', {}, r.source_run),
                  ' — ping rates are consistent across xaxis settings so this estimate is valid.')
              : null
            const actionEl = (r.rec_action && !isCurrent)
              ? h('div', { style: 'margin-top:8px;padding:7px 10px;background:rgba(0,0,0,0.04);border-radius:3px;font-size:12px' },
                  h('strong', {}, 'To apply:'), ' ' + r.rec_action.replace('To apply: ', ''))
              : null
            recBox.innerHTML = ''
            recBox.appendChild(h('div',
              { style: `margin:12px 0 4px;padding:12px 16px;background:var(--card-bg,#f4f6fb);border-left:4px solid ${borderColor};border-radius:3px;font-size:12.5px;line-height:1.65` },
              h('div', { style: 'font-weight:700;margin-bottom:6px' },
                `X-axis recommendation: xaxis=${r.rec_xaxis}`, currentNoteEl),
              h('div', { style: 'color:var(--text-muted,#555)' }, r.rec_reason),
              actionEl,
              sourceNoteEl,
            ))
          }
        } catch (_) {}
      })()
    }

    // ── File I/O row ──────────────────────────────────────────────────────────
    const fmtSz = n => !n ? '' : n >= 1e9 ? `${(n/1e9).toFixed(1)} GB`
                  : n >= 1e6 ? `${(n/1e6).toFixed(1)} MB`
                  : n >= 1e3 ? `${(n/1e3).toFixed(0)} KB` : `${n} B`

    const currentRunLabel = adcpRunLabel(adcpOpts)
    const runPrefix       = currentRunLabel + '_'
    const dlBaseQs        = `dep=${encodeURIComponent(dep)}&is_test=${isTest}`
    const dlRunQs         = `${dlBaseQs}&run=${encodeURIComponent(currentRunLabel)}`

    const makeChip = (fname, fi) => {
      const isRunFile   = fname.startsWith(runPrefix)
      const displayName = isRunFile ? fname.slice(runPrefix.length) : fname
      const sizeStr     = (fi?.exists && fi.size) ? ` · ${fmtSz(fi.size)}` : ''
      if (fi?.exists) {
        const a = h('a', {
          class: 'stage-file-chip chip-exists',
          href: `${API}/api/adcp/download/${encodeURIComponent(displayName)}?${isRunFile ? dlRunQs : dlBaseQs}`,
          download: displayName,
        }, displayName + sizeStr)
        return a
      }
      return h('span', { class: 'stage-file-chip chip-pending' }, displayName)
    }

    const stageDef      = stagesDefs.find(s => s.id === id)
    const needIds       = stageDef?.needs || []
    const usesEntries   = needIds.flatMap(nid => Object.entries(statusMap[nid]?.files || {}))
    const writesEntries = Object.entries(statusMap[id]?.files || {})

    const fileInfoRow = h('div', { class: 'stage-file-info' })
    if (id === 'process_shear' && selectedFile) {
      const rawName = selectedFile.split(/[\\/]/).pop()
      fileInfoRow.appendChild(h('div', { class: 'stage-file-group' },
        h('span', { class: 'stage-file-label' }, 'Uses'),
        h('span', { class: 'stage-file-chip chip-exists', title: selectedFile }, rawName),
        ...usesEntries.map(([f, fi]) => makeChip(f, fi))))
    } else if (usesEntries.length) {
      fileInfoRow.appendChild(h('div', { class: 'stage-file-group' },
        h('span', { class: 'stage-file-label' }, 'Uses'),
        ...usesEntries.map(([f, fi]) => makeChip(f, fi))))
    }
    if (writesEntries.length) {
      fileInfoRow.appendChild(h('div', { class: 'stage-file-group' },
        h('span', { class: 'stage-file-label' }, 'Writes'),
        ...writesEntries.map(([f, fi]) => makeChip(f, fi))))
    }

    const card = h('div', { class: 'stage-card' },
      h('div', { class: 'stage-card-header' },
        h('span', { class: 'stage-title' }, label), badge),
      h('div', { class: 'stage-body' },
        fileInfoRow,
        h('div', { class: 'btn-row' }, btnRun, btnAbort, btnClear, elapsed),
        logEl, recBox),
    )

    if (liveRunning) {
      // Abort any old fetch loop that has closures over detached DOM elements,
      // then reconnect using the freshly built DOM refs.
      setTimeout(() => {
        if (adcpIvs[id]) { clearInterval(adcpIvs[id]); delete adcpIvs[id] }
        if (adcpCtrl[id]) { adcpCtrl[id].abort(); delete adcpCtrl[id] }
        runAdcpStage(id, label, dep, badge, btnRun, btnAbort, logEl, elapsed,
                     stagesContainer, stagesDefs, statusMap, refreshFn, true)
      }, 0)
    }

    return card
  }

  // ── Run a stage via SSE ──────────────────────────────────────────────────────
  async function runAdcpStage (id, label, dep, badge, btnRun, btnAbort, logEl, elapsed,
                                stagesContainer, stagesDefs, statusMap, refreshFn, reconnect = false) {
    // On reconnect the backend replays the full log buffer, so clear local state
    // and let the replay fill it in fresh (avoids duplicates).
    adcpLogs[id] = []
    logEl.innerHTML = ''
    if (reconnect) {
      const sep = '↩ Reconnected — replaying log from server…'
      adcpLogs[id].push(sep)
      appendLog(logEl, sep)
    }
    logEl.classList.remove('hidden')
    if (!reconnect) {
      btnRun.classList.add('hidden')
      btnAbort.classList.remove('hidden')
      badge.className   = 'status-badge running'
      badge.textContent = 'RUNNING…'
    }

    // Clear any leftover interval from a previous run on this stage
    if (adcpIvs[id]) { clearInterval(adcpIvs[id]); delete adcpIvs[id] }

    const t0 = Date.now()
    elapsed.classList.remove('hidden')
    adcpIvs[id] = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000)
      elapsed.textContent = `${Math.floor(s / 60)}m ${s % 60}s`
    }, 1000)

    const stopTimer = () => {
      if (adcpIvs[id]) { clearInterval(adcpIvs[id]); delete adcpIvs[id] }
      elapsed.classList.add('hidden')
    }

    const ctrl = new AbortController()
    adcpCtrl[id] = ctrl

    let receivedDone = false
    try {
      const resp = await fetch(`${API}/api/adcp/run/${id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dep, is_test: isTest, adcp_file: selectedFile, options: adcpOpts }),
        signal: ctrl.signal,
      })

      const reader  = resp.body.getReader()
      const decoder = new TextDecoder()
      let   buf     = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n'); buf = lines.pop()
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const evt = JSON.parse(line.slice(6))
            if (evt.type === 'log') {
              adcpLogs[id].push(evt.line)
              appendLog(logEl, evt.line)
            } else if (evt.type === 'done') {
              receivedDone = true
              stopTimer()
              const success = evt.success
              // If compute_currents detected the actual cell size, update opts so
              // all subsequent stages target the renamed run directory.
              if (success && evt.actual_yaxis != null && adcpOpts.yaxis == null) {
                adcpOpts.yaxis = evt.actual_yaxis; saveAdcpOpts()
                const note = `Y-axis auto-detected: ${evt.actual_yaxis} m (ADCP cell size) — run directory renamed to include _y${evt.actual_yaxis}.`
                adcpLogs[id].push(note)
                appendLog(logEl, note)
              }
              const msg = success ? 'Completed successfully.' : 'Finished with errors.'
              adcpLogs[id].push(msg)
              appendLog(logEl, msg)
              badge.className   = `status-badge ${success ? 'done' : 'failed'}`
              badge.textContent = success ? '✔ DONE' : '✖ FAILED'
              btnRun.textContent = 'Re-run'
              btnRun.disabled    = false
              btnRun.classList.remove('hidden', 'disabled')
              btnAbort.classList.add('hidden')
              delete adcpCtrl[id]
              // Delay refresh so logs are readable before the stage area rebuilds.
              // Save and restore scroll position so the page doesn't jump to the top.
              if (success && refreshFn) setTimeout(() => {
                const pageBody = document.querySelector('.page-body')
                const savedScroll = pageBody ? pageBody.scrollTop : 0
                refreshFn()
                if (pageBody) requestAnimationFrame(() => { pageBody.scrollTop = savedScroll })
              }, 800)
            }
          } catch (_) {}
        }
      }

      // Stream ended without a done event — connection dropped while backend
      // may still be running. Reset UI so the user can reload or re-run.
      if (!receivedDone) {
        stopTimer()
        const msg = 'Connection lost — the stage may still be running on the server. Navigate away and back to reconnect.'
        adcpLogs[id].push(msg)
        appendLog(logEl, msg)
        badge.className   = 'status-badge failed'
        badge.textContent = '⚠ DISCONNECTED'
        btnRun.textContent = 'Re-run'
        btnRun.disabled    = false
        btnRun.classList.remove('hidden', 'disabled')
        btnAbort.classList.add('hidden')
        delete adcpCtrl[id]
      }
    } catch (e) {
      stopTimer()
      if (e.name !== 'AbortError') {
        appendLog(logEl, 'Stream error: ' + e.message)
        badge.className   = 'status-badge failed'
        badge.textContent = '✖ FAILED'
      }
      btnRun.classList.remove('hidden')
      btnAbort.classList.add('hidden')
      delete adcpCtrl[id]
    }
  }

  // ── Export panel ─────────────────────────────────────────────────────────────
  function buildAdcpExportPanel (dep, statusMap, allRuns, currentRun) {
    const panel = h('div', { class: 'adcp-export-panel' })
    panel.appendChild(h('div', { class: 'adcp-section-title', style: 'margin-top:24px' }, 'Outputs & Export'))

    const baseQs    = `dep=${encodeURIComponent(dep)}&is_test=${isTest}`
    const currentQs = `${baseQs}&run=${encodeURIComponent(currentRun)}`

    // Shared data files (no run param needed — live in base_dir)
    const sharedDataMap = isTest ? {} : {
      load_nc:         ['adcp_glider_combined.csv'],
      write_glider_nc: ['glider_ad2cp_datetime_timecoord.nc'],
    }
    // Option-specific data files (need run= param)
    const runDataMap = isTest
      ? { process_shear: ['shear_processed.nc'] }
      : { compute_currents: ['currents.nc'], bias_correction: ['currents_bias_corrected.nc'],
          generate_plots: ['beam_quality.nc'] }

    const dataRow = h('div', { class: 'adcp-export-row' })
    Object.entries(sharedDataMap).forEach(([stageId, files]) => {
      if (!statusMap[stageId]?.done) return
      files.forEach(fn => {
        const a = h('a', { class: 'btn-secondary btn-sm',
          href: `${API}/api/adcp/download/${encodeURIComponent(fn)}?${baseQs}` }, `⬇ ${fn}`)
        a.setAttribute('download', fn)
        dataRow.appendChild(a)
      })
    })
    Object.entries(runDataMap).forEach(([stageId, files]) => {
      if (!statusMap[stageId]?.done) return
      files.forEach(fn => {
        const a = h('a', { class: 'btn-secondary btn-sm',
          href: `${API}/api/adcp/download/${encodeURIComponent(fn)}?${currentQs}` }, `⬇ ${fn}`)
        a.setAttribute('download', fn)
        dataRow.appendChild(a)
      })
    })
    if (dataRow.children.length) panel.appendChild(dataRow)

    // Plots for current run (inline preview + download) — collected from all stages
    const stagePlots = isTest
      ? [{ stage: 'generate_plots', plots: ['velocity_sections.png', 'speed_section.png'] }]
      : [
          { stage: 'bias_correction', plots: ['bias_diagnostic.png'] },
          { stage: 'generate_plots',  plots: ['velocity_sections.png', 'speed_section.png',
                                               'shear_sections.png', 'backscatter.png',
                                               'correlation.png', 'beam_amplitude.png', 'snr.png',
                                               'dac_timeseries.png', 'dac_map_cartopy.png'] },
          { stage: 'tidal_filter',    plots: ['tidal_filter.png'] },
          { stage: 'diagnostics',     plots: ['eke.png', 'transport.png', 'richardson_number.png'] },
        ]

    const allPlots = stagePlots.flatMap(({ stage, plots }) =>
      statusMap[stage]?.done ? plots.map(fn => ({ stage, fn })) : [])

    if (allPlots.length > 0) {
      const plotsArea = h('div', { class: 'adcp-plots-area' })
      allPlots.forEach(({ stage, fn }) => {
        // Cache-bust by the plot's real mtime instead of Date.now() -- an
        // unchanged plot then reuses the browser's image cache across a
        // re-render (e.g. navigating away from AD2CP and back) instead of
        // being force-refetched every single time regardless of whether the
        // underlying stage actually produced anything new. Falls back to
        // Date.now() defensively if mtime isn't available for some reason.
        const mtime = statusMap[stage]?.files?.[`${currentRun}_${fn}`]?.mtime
        const url   = `${API}/api/adcp/plot/${encodeURIComponent(fn)}?${currentQs}&_t=${mtime || Date.now()}`
        const dlUrl = `${API}/api/adcp/download/${encodeURIComponent(fn)}?${currentQs}`
        const img   = h('img', { class: 'adcp-plot-img', src: url, alt: fn })
        img.onerror = () => img.parentElement?.remove()
        const foot  = h('div', { class: 'adcp-plot-footer' },
          h('span', {}, fn),
          h('a', { class: 'btn-secondary btn-sm', href: dlUrl }, '⬇ Download'),
        )
        plotsArea.appendChild(h('div', { class: 'adcp-plot-wrap' }, img, foot))
      })
      panel.appendChild(plotsArea)
    }

    // Per-run export buttons for all saved runs
    const runLabels = Object.keys(allRuns)
    if (runLabels.length > 0) {
      panel.appendChild(h('div', { class: 'adcp-section-title', style: 'margin-top:16px;font-size:12px' }, 'Saved Runs'))
      const runsRow = h('div', { class: 'adcp-export-row' })
      runLabels.forEach(run => {
        const runQs = `${baseQs}&run=${encodeURIComponent(run)}`
        const isCurrent = run === currentRun
        const a = h('a', {
          class: `btn-secondary btn-sm${isCurrent ? ' adcp-run-current' : ''}`,
          href:  `${API}/api/adcp/export?${runQs}`,
          title: isCurrent ? 'Current options' : '',
        }, `⬇ ${run}${isCurrent ? ' ★' : ''}`)
        runsRow.appendChild(a)
      })
      panel.appendChild(runsRow)
    }

    // Export everything as ZIP
    const exportRow = h('div', { class: 'adcp-export-all-row' })
    const zipLink   = h('a', { class: 'btn-primary btn-sm',
      href: `${API}/api/adcp/export?${baseQs}` }, '⬇ Export All (ZIP)')
    exportRow.appendChild(zipLink)
    panel.appendChild(exportRow)

    // Cloud upload (only for deployment mode, not test mode)
    if (!isTest && dep) {
      const cloudRow = h('div', { class: 'adcp-export-all-row', style: 'margin-top:8px' })
      const uploadStatus = h('span', { class: 'adcp-cloud-status' })
      cloudRow.appendChild(uploadStatus)

      ;(async () => {
        let accounts
        try { accounts = await apiFetch('/api/cloud/accounts') } catch { return }
        const services = []
        if (accounts.google?.connected)  services.push({ key: 'google',  label: '↑ Drive' })
        if (accounts.dropbox?.connected) services.push({ key: 'dropbox', label: '↑ Dropbox' })
        if (!services.length) return

        services.forEach(({ key, label }) => {
          const btn = h('button', { class: 'btn-secondary btn-sm' }, label)
          btn.addEventListener('click', async () => {
            btn.disabled       = true
            uploadStatus.textContent = 'Uploading…'

            // Collect relative paths for all generated files in this run + shared files
            const filesToUpload = []
            const adcpBase = isTest
              ? `ad2cp/tests/${dep}/output`
              : `post_processing/${dep}/adcp`

            const sharedFiles = ['adcp_glider_combined.csv', 'gps_data.npz', 'glider_ad2cp_datetime_timecoord.nc']
            sharedFiles.forEach(f => filesToUpload.push(`${adcpBase}/${f}`))

            if (currentRun) {
              const runFiles = ['shear_processed.nc', 'currents.nc', 'dac.npz',
                                'currents_bias_corrected.nc', 'velocity_sections.png',
                                'speed_section.png', 'dac_map_cartopy.png']
              runFiles.forEach(f => filesToUpload.push(`${adcpBase}/${currentRun}/${f}`))
            }

            try {
              const r = await apiFetch('/api/cloud/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ service: key, files: filesToUpload, dest: '', conflict: 'overwrite' }),
              })
              const ok = (r.ok_count ?? r.ok) || 0
              uploadStatus.textContent = `✔ ${r.ok} file(s) uploaded${r.skipped ? `, ${r.skipped} skipped` : ''}`
            } catch (e) {
              uploadStatus.textContent = `✖ Upload failed: ${e.message}`
            }
            btn.disabled = false
          })
          cloudRow.appendChild(btn)
        })
      })()

      panel.appendChild(cloudRow)
    }

    return panel
  }

  // Initial render
  renderMain()
}

/* ── Settings page ───────────────────────────────────────────────────────────*/
const _SF_META = [
  { key: 'creator_name',          label: 'Creator name' },
  { key: 'creator_email',         label: 'Creator email' },
  { key: 'creator_institution',   label: 'Creator institution' },
  { key: 'creator_type',          label: 'Creator type' },
  { key: 'creator_url',           label: 'Creator URL' },
  { key: 'publisher_email',       label: 'Publisher email' },
  { key: 'publisher_institution', label: 'Publisher institution' },
]

async function renderSettings () {
  const page = document.getElementById('page-settings')
  page.innerHTML = ''
  page.style.overflowY = 'auto'

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'Settings'),
    h('p',  {}, 'App-wide configuration.'),
  ))

  const body = h('div', { class: 'page-body' })
  page.appendChild(body)

  // Load settings
  let gcSettings = { presets: [], active: null, static_fields: {} }
  try {
    const r = await apiFetch('/api/settings/generator')
    if (r.ok !== false) gcSettings = r
  } catch (_) {}

  // ── Static Fields section ──────────────────────────────────────────────────
  const sfSection = h('div', { class: 'settings-section' })
  body.appendChild(sfSection)
  sfSection.appendChild(h('h2', { class: 'settings-section-title' }, 'Metadata Static Fields'))
  sfSection.appendChild(h('p', { class: 'settings-section-desc' },
    'Creator and publisher values to use when generating config files. When no preset is active these are pulled automatically from the most recent same-glider deployment. ' +
    'Geospatial vertical units are always pulled from the previous deployment.'))

  renderStaticFieldsEditor(sfSection, gcSettings)
}

function renderStaticFieldsEditor (container, gcSettings) {
  const existing = container.querySelector('.sf-editor')
  if (existing) existing.remove()

  const wrap = h('div', { class: 'sf-editor' })
  container.appendChild(wrap)

  // ── Preset bar ────────────────────────────────────────────────────────────
  const presetBar = h('div', { class: 'gc-preset-bar' })
  wrap.appendChild(presetBar)

  const presetSel = h('select', { class: 'form-input gc-preset-select' })
  presetSel.appendChild(h('option', { value: '' }, '— no preset (use previous deployment) —'))
  ;(gcSettings.presets || []).forEach(p => {
    const opt = h('option', { value: p.name }, p.name)
    if (p.name === gcSettings.active) opt.selected = true
    presetSel.appendChild(opt)
  })

  const loadBtn   = h('button', { class: 'btn-secondary btn-sm' }, 'Load')
  const saveBtn   = h('button', { class: 'btn-primary btn-sm' },   'Save as preset…')
  const deleteBtn = h('button', { class: 'btn-secondary btn-sm' }, 'Delete')
  presetBar.append(
    h('label', { class: 'form-label', style: 'margin:0;align-self:center' }, 'Preset'),
    presetSel, loadBtn, saveBtn, deleteBtn)

  // ── Field inputs ──────────────────────────────────────────────────────────
  const sfFields = {}
  const fieldsWrap = h('div', { class: 'sf-fields-wrap', style: 'margin-top:12px' })
  wrap.appendChild(fieldsWrap)

  _SF_META.forEach(({ key, label }) => {
    const input = h('input', { class: 'form-input', type: 'text',
      value: (gcSettings.static_fields || {})[key] || '' })
    sfFields[key] = input
    fieldsWrap.appendChild(h('div', { class: 'form-group sf-form-group' },
      h('label', { class: 'form-label' }, label), input))
  })

  const statusEl = h('div', { class: 'gc-status' })
  const saveFieldsBtn = h('button', { class: 'btn-primary', style: 'margin-top:16px' }, 'Save')
  fieldsWrap.appendChild(saveFieldsBtn)
  fieldsWrap.appendChild(statusEl)

  function currentFields () {
    const sf = {}
    _SF_META.forEach(({ key }) => { sf[key] = sfFields[key].value.trim() })
    return sf
  }

  async function persist (patch) {
    await apiFetch('/api/settings/generator', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...gcSettings, ...patch }) })
    Object.assign(gcSettings, patch)
  }

  saveFieldsBtn.addEventListener('click', async () => {
    try {
      await persist({ static_fields: currentFields() })
      statusEl.textContent = 'Saved.'
      setTimeout(() => { statusEl.textContent = '' }, 2000)
    } catch (e) { statusEl.textContent = 'Error: ' + e.message }
  })

  loadBtn.addEventListener('click', () => {
    const name = presetSel.value
    if (!name) return
    const preset = (gcSettings.presets || []).find(p => p.name === name)
    if (!preset) return
    _SF_META.forEach(({ key }) => { sfFields[key].value = (preset.static_fields || {})[key] || '' })
    gcSettings.active = name
  })

  saveBtn.addEventListener('click', async () => {
    const name = prompt('Preset name:')
    if (!name || !name.trim()) return
    const trimmed = name.trim()
    const sf = currentFields()
    const newPreset = { name: trimmed, static_fields: sf }
    const idx = (gcSettings.presets || []).findIndex(p => p.name === trimmed)
    const presets = [...(gcSettings.presets || [])]
    if (idx >= 0) presets[idx] = newPreset; else presets.push(newPreset)
    try {
      await persist({ presets, active: trimmed, static_fields: sf })
      renderStaticFieldsEditor(container, gcSettings)
    } catch (e) { statusEl.textContent = 'Error: ' + e.message }
  })

  deleteBtn.addEventListener('click', async () => {
    const name = presetSel.value
    if (!name) return
    if (!confirm(`Delete preset "${name}"?`)) return
    const presets = (gcSettings.presets || []).filter(p => p.name !== name)
    const active  = gcSettings.active === name ? null : gcSettings.active
    try {
      await persist({ presets, active })
      renderStaticFieldsEditor(container, gcSettings)
    } catch (e) { statusEl.textContent = 'Error: ' + e.message }
  })
}

/* ── Navigation ──────────────────────────────────────────────────────────────*/
const pageRenderers = {
  dashboard:     renderDashboard,
  pipeline:      renderPipeline,
  logs:          renderLogs,
  reports:       renderReports,
  metadata:      renderMetadata,
  'qc-config':   renderQcConfig,
  'dat-browser': renderDatBrowser,
  cloud:         renderCloud,
  adcp:          renderAdcp,
  settings:      renderSettings,
}

async function navigate (page) {
  // No per-page "on leave" lifecycle hook exists in this app -- these are the
  // only two things that currently outlive the page that started them, so
  // tear them down here unconditionally rather than build a generic hook for
  // just two call sites.
  if (_activeCloudPoll)  { _activeCloudPoll(); _activeCloudPoll = null }
  if (_activeDockerWait) { _activeDockerWait.close(); _activeDockerWait = null }

  state.page = page

  // Update active page panel
  document.querySelectorAll('.page').forEach(el =>
    el.classList.toggle('active', el.id === `page-${page}`)
  )
  // Update nav highlight
  document.querySelectorAll('.nav-item').forEach(el =>
    el.classList.toggle('active', el.dataset.page === page)
  )

  const renderer = pageRenderers[page]
  if (renderer) await renderer()
}

/* ── Init ────────────────────────────────────────────────────────────────────*/
async function init () {
  const [{ deployments }, nicknamesRaw] = await Promise.all([
    loadAppState(),
    apiFetch('/api/nicknames').catch(() => ({})),
  ])
  nicknames = nicknamesRaw || {}

  const sel         = document.getElementById('dep-select')
  const nicknameLbl = document.getElementById('dep-nickname-lbl')

  function optionLabel (dep) {
    return dep
  }

  function rebuildSelect (deps, selectVal) {
    sel.innerHTML = ''
    sel.appendChild(h('option', { value: '' }, '— select deployment —'))
    deps.forEach(dep => sel.appendChild(h('option', { value: dep }, optionLabel(dep))))
    sel.value = selectVal || ''
  }

  function updateNicknameLabel () {
    const dep  = state.deployment
    const nick = dep && nicknames[dep]
    if (nick) {
      nicknameLbl.textContent = nick
      nicknameLbl.classList.remove('hidden')
    } else {
      nicknameLbl.classList.add('hidden')
    }
  }

  if (!deployments.length) {
    sel.appendChild(h('option', { value: '' }, 'No deployments'))
  } else {
    rebuildSelect(deployments, '')
    const lastDep = localStorage.getItem('lastDeployment')
    if (lastDep && deployments.includes(lastDep)) {
      state.deployment = lastDep
      sel.value = lastDep
    } else {
      state.deployment = null
    }
  }
  updateNicknameLabel()

  sel.addEventListener('change', e => {
    state.deployment = e.target.value || null
    if (state.deployment) localStorage.setItem('lastDeployment', state.deployment)
    updateRenameBtn()
    updateNicknameLabel()
    navigate(state.page)
  })

  // ── Deployment nickname ────────────────────────────────────────────────────
  const nicknameBtn    = document.getElementById('dep-nickname-btn')
  const nicknameForm   = document.getElementById('dep-nickname-form')
  const nicknameInput  = document.getElementById('dep-nickname-input')
  const nicknameSave   = document.getElementById('dep-nickname-save')
  const nicknameCancel = document.getElementById('dep-nickname-cancel')

  function updateNicknameBtn () {
    nicknameBtn.disabled = !state.deployment
    renameBtn.disabled   = !state.deployment
  }

  function openNicknameForm () {
    nicknameInput.value = (state.deployment && nicknames[state.deployment]) || ''
    nicknameForm.classList.remove('hidden')
    nicknameBtn.style.display = 'none'
    renameBtn.style.display   = 'none'
    sel.style.display         = 'none'
    nicknameInput.focus()
    nicknameInput.select()
  }

  function closeNicknameForm () {
    nicknameForm.classList.add('hidden')
    nicknameBtn.style.display = ''
    renameBtn.style.display   = ''
    sel.style.display         = ''
  }

  nicknameBtn.addEventListener('click', openNicknameForm)
  nicknameCancel.addEventListener('click', closeNicknameForm)

  nicknameSave.addEventListener('click', async () => {
    const dep      = state.deployment
    const newNick  = nicknameInput.value.trim()
    if (!dep) { closeNicknameForm(); return }

    nicknameSave.disabled    = true
    nicknameSave.textContent = 'Saving…'
    try {
      await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/nickname`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ nickname: newNick }),
      })
      if (newNick) nicknames[dep] = newNick
      else         delete nicknames[dep]

      // Rebuild select so option labels refresh
      const { deployments: deps } = await loadAppState()
      rebuildSelect(deps, dep)
      updateNicknameLabel()
      closeNicknameForm()
    } catch (err) {
      nicknameInput.setCustomValidity(err.message)
      nicknameInput.reportValidity()
    } finally {
      nicknameSave.disabled    = false
      nicknameSave.textContent = 'Save'
    }
  })

  nicknameInput.addEventListener('keydown', e => {
    if (e.key === 'Enter')  nicknameSave.click()
    if (e.key === 'Escape') closeNicknameForm()
  })

  // ── Deployment rename ──────────────────────────────────────────────────────
  const renameBtn    = document.getElementById('dep-rename-btn')
  const renameForm   = document.getElementById('dep-rename-form')
  const renameInput  = document.getElementById('dep-rename-input')
  const renameSave   = document.getElementById('dep-rename-save')
  const renameCancel = document.getElementById('dep-rename-cancel')

  function updateRenameBtn () {
    updateNicknameBtn()
  }
  updateNicknameBtn()

  function openRenameForm () {
    renameInput.value = state.deployment || ''
    renameForm.classList.remove('hidden')
    sel.style.display         = 'none'
    nicknameBtn.style.display = 'none'
    renameBtn.style.display   = 'none'
    renameInput.focus()
    renameInput.select()
  }

  function closeRenameForm () {
    renameForm.classList.add('hidden')
    sel.style.display         = ''
    nicknameBtn.style.display = ''
    renameBtn.style.display   = ''
    renameInput.setCustomValidity('')
  }

  renameBtn.addEventListener('click', openRenameForm)
  renameCancel.addEventListener('click', closeRenameForm)

  renameSave.addEventListener('click', async () => {
    const newName = renameInput.value.trim()
    if (!newName || newName === state.deployment) { closeRenameForm(); return }

    renameSave.disabled    = true
    renameSave.textContent = 'Saving…'
    try {
      const r = await apiFetch(`/api/deployment/${encodeURIComponent(state.deployment)}/rename`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ new_name: newName }),
      })

      // Transfer nickname in local cache too
      if (nicknames[state.deployment]) {
        nicknames[newName] = nicknames[state.deployment]
        delete nicknames[state.deployment]
      }

      const { deployments: deps } = await loadAppState()
      rebuildSelect(deps, r.name)
      const oldName = state.deployment
      state.deployment = r.name
      localStorage.setItem('lastDeployment', r.name)
      delete state.depStates[oldName]   // the actually-stale entry, not the new (empty) one
      updateNicknameLabel()
      closeRenameForm()
      navigate(state.page)
    } catch (err) {
      renameInput.setCustomValidity(err.message)
      renameInput.reportValidity()
    } finally {
      renameSave.disabled    = false
      renameSave.textContent = 'Save'
    }
  })

  renameInput.addEventListener('keydown', e => {
    if (e.key === 'Enter')  renameSave.click()
    if (e.key === 'Escape') closeRenameForm()
  })

  document.querySelectorAll('.nav-item').forEach(li =>
    li.addEventListener('click', () => navigate(li.dataset.page))
  )

  // Silently verify cloud tokens in background; show warning dot if reconnect needed
  apiFetch('/api/cloud/verify').then(v => {
    const needsReconnect =
      (v.google.has_creds  && v.google.has_token  && !v.google.live) ||
      (v.dropbox.has_creds && v.dropbox.has_token && !v.dropbox.live)
    if (needsReconnect) {
      const navCloud = document.querySelector('.nav-item[data-page="cloud"]')
      if (navCloud && !navCloud.querySelector('.nav-warn-dot')) {
        const dot = document.createElement('span')
        dot.className = 'nav-warn-dot'
        dot.title = 'Cloud storage: reconnection needed'
        navCloud.appendChild(dot)
      }
    }
  }).catch(() => {})

  await navigate('dashboard')
}

init().catch(err => {
  document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif;color:#c00">
    <h2>Failed to connect to Python backend</h2>
    <p>${err.message}</p>
    <p style="margin-top:12px;color:#888">Make sure Flask is installed: <code>pip install flask</code></p>
  </div>`
})
