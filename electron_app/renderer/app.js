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
const activeRuns = {}      // stageKey → AbortController

/* ── API helpers ─────────────────────────────────────────────────────────────*/
async function apiFetch (url, opts = {}) {
  const r = await fetch(API + url, opts)
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${url}`)
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
async function streamRun (dep, stage, onLog, onDone) {
  const controller = new AbortController()
  activeRuns[stage] = controller

  try {
    const resp = await fetch(`${API}/api/deployment/${encodeURIComponent(dep)}/run/${stage}`, {
      method: 'POST',
      signal: controller.signal,
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
  return s === 'done' ? 'done' : s === 'failed' ? 'failed' : s === 'running' ? 'running' : 'not_run'
}
function statusLabel (s) {
  return s === 'done' ? '✔ DONE' : s === 'failed' ? '✖ FAILED' : s === 'running' ? 'RUNNING…' : 'NOT RUN'
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

  const now     = new Date()
  const dateStr = now.toISOString().slice(0, 10)
  const timeStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0')

  const gliderInput = h('input', { class: 'form-input', type: 'text', id: 'nd-glider',
    placeholder: gliders[0] || 'uvi_01', value: gliders[0] || '', list: 'nd-glider-list',
    oninput: () => { _ndUpdatePreview(); _ndAutoTemplate() } })
  const gliderList  = h('datalist', { id: 'nd-glider-list' },
    ...gliders.map(g => h('option', { value: g })))
  const dateInput   = h('input', { class: 'form-input', type: 'date',  id: 'nd-date',
    value: dateStr, oninput: _ndUpdatePreview })
  const timeInput   = h('input', { class: 'form-input', type: 'time',  id: 'nd-time',
    value: timeStr, oninput: _ndUpdatePreview })
  const preview     = h('div',    { class: 'dep-name-preview', id: 'nd-preview' }, '—')
  // Build template dropdown grouped by glider
  const templateSel = _buildTemplateSelect(deps, gliders[0] || '')
  // Auto-select most recent same-glider deployment as default
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
          h('label', { class: 'form-label' }, 'Glider'), gliderInput, gliderList),
        h('div', { class: 'form-row' },
          h('div', { class: 'form-group' }, h('label', { class: 'form-label' }, 'Date'), dateInput),
          h('div', { class: 'form-group' }, h('label', { class: 'form-label' }, 'Time (UTC)'), timeInput)),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Deployment name'), preview),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Copy config from'), templateSel),
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Folders that will be created'), foldersList),
        statusMsg),
      h('div', { class: 'modal-footer', id: 'nd-modal-footer' },
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        createBtn)))

  document.body.appendChild(overlay)
  _ndUpdatePreview()
  gliderInput.focus()
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
  const glider = (document.getElementById('nd-glider').value || '').trim()
  const sel    = document.getElementById('nd-template')
  if (!sel) return

  // Rebuild grouped options for new glider
  const deps = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(Boolean)
  const newSel = _buildTemplateSelect(deps, glider)
  sel.innerHTML = newSel.innerHTML  // swap options in place

  // Auto-select most recent same-glider dep
  const sameGlider = deps.filter(d => glider && d.startsWith(glider + '-')).sort()
  if (sameGlider.length) sel.value = sameGlider[sameGlider.length - 1]
  else if (deps.length)  sel.value = deps[deps.length - 1]
}

function _ndDepName () {
  const glider = (document.getElementById('nd-glider').value || '').trim()
  const date   = document.getElementById('nd-date').value   // YYYY-MM-DD
  const time   = document.getElementById('nd-time').value   // HH:MM
  if (!glider || !date || !time) return ''
  return `${glider}-${date.replace(/-/g, '')}T${time.replace(':', '')}`
}

function _ndUpdatePreview () {
  const name    = _ndDepName()
  const preview = document.getElementById('nd-preview')
  const folders = document.getElementById('nd-folders')
  if (!name) { preview.textContent = '—'; folders.textContent = '—'; return }
  const glider = name.split('-')[0]
  preview.textContent = name
  folders.textContent = [
    `incoming_data/${name}/`,
    `dat_files/${name}/merged_data/`,
    `dat_files/${name}/flight_data/`,
    `glider_config_files_json/${glider}/${name}/science_profile/`,
    `glider_config_files_json/${glider}/${name}/science_trajectory/`,
  ].join('\n')
}

function closeModal () {
  const el = document.getElementById('modal-overlay')
  if (el) el.remove()
}

async function _ndDoCreate () {
  const name     = _ndDepName()
  const template = document.getElementById('nd-template').value
  const status   = document.getElementById('nd-status')
  const btn      = document.getElementById('btn-create-dep')
  if (!name) { status.textContent = 'Please fill in all fields.'; return }

  btn.disabled = true; btn.textContent = 'Creating…'; status.textContent = ''
  try {
    await apiFetch('/api/deployment/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, template }),
    })
    // Add to deployment selector
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

  const header = h('div', { class: 'page-header' },
    h('div', { style: 'display:flex;align-items:flex-start;justify-content:space-between' },
      h('div', {},
        h('h1', {}, 'Deployments'),
        h('p',  {}, 'All glider deployments found in this pipeline directory.')),
      h('button', { class: 'btn-primary', style: 'margin-top:4px', onclick: showNewDepModal },
        '+ New Deployment')))
  const body = h('div', { class: 'page-body' })
  page.appendChild(header)
  page.appendChild(body)

  const deps = Array.from(document.getElementById('dep-select').options).map(o => o.value)
  if (!deps.length) { body.appendChild(h('div', { class: 'empty-state' }, 'No deployments found.')); return }

  const allStates = await Promise.all(deps.map(d => loadDepState(d)))

  // Group deployments by glider name (everything before the first '-')
  const gliderMap = new Map()
  deps.forEach((dep, i) => {
    const glider = dep.split('-')[0]
    if (!gliderMap.has(glider)) gliderMap.set(glider, [])
    gliderMap.get(glider).push({ dep, ds: allStates[i] })
  })

  gliderMap.forEach((gliderDeps, glider) => {
    // Parse last deployment date for display
    const lastDep  = gliderDeps[gliderDeps.length - 1].dep
    const datePart = lastDep.split('-')[1] || ''
    const lastDate = datePart.length >= 8
      ? `${datePart.slice(0,4)}-${datePart.slice(4,6)}-${datePart.slice(6,8)}`
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

      const badges = h('div', { class: 'badge-row' },
        ...state.stages.map(sk =>
          h('span', { class: `badge ${statusClass((ps[sk] || {}).status || 'not_run')}` }, shortLabel(sk))
        ))

      const infoText = nc_count > 0
        ? `${nc_count.toLocaleString()} QC'd profile files  ·  ${nc_dir}`
        : 'QC output not yet generated'

      return h('div', { class: 'dep-card' },
        h('div', { class: 'dep-card-header' },
          h('span', { class: 'dep-name' }, dep),
          h('button', { class: 'btn-primary', onclick: () => openDeployment(dep) }, 'Open →')),
        badges,
        h('div', { class: 'dep-info' }, infoText))
    })

    body.appendChild(h('div', { class: 'glider-section' }, sectionHeader, ...cards))
  })
}

function shortLabel (stageKey) {
  const map = {
    dbd_to_dat:           'DBD→DAT',
    dat_to_profile_nc:    'Profile NC',
    dat_to_trajectory_nc: 'Traj NC',
    fix_metadata:         'Fix Meta',
    qc:                   'QC',
    report:               'Report',
  }
  return map[stageKey] || stageKey
}

function openDeployment (dep) {
  document.getElementById('dep-select').value = dep
  state.deployment = dep
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
  page.appendChild(body)

  if (!dep) return

  const ds = await loadDepState(dep)
  const ps = ds.state || {}

  for (const sk of state.stages) {
    const info   = state.stageInfo[sk] || {}
    const status = (ps[sk] || {}).status || 'not_run'
    body.appendChild(buildStageCard(sk, info, status, dep))
  }
}

function buildStageCard (sk, info, initialStatus, dep) {
  const src = getStagePaths(sk, dep)

  // Header
  const badge = h('span', { class: `status-badge ${statusClass(initialStatus)}` }, statusLabel(initialStatus))
  const cardHeader = h('div', { class: 'stage-card-header' },
    h('span', { class: 'stage-title' }, info.label || sk),
    badge
  )

  // Body
  const desc  = h('div', { class: 'stage-desc' }, info.description || '')
  const paths = h('div', { class: 'stage-paths' })
  paths.innerHTML = `<b>Source:</b> ${src.source}<br><b>Output:</b> ${src.output}`

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

    const btnRun   = h('button', { class: 'btn-success', onclick: () => doRun(dep, sk, badge, btnRun, btnAbort, log) }, '▶  Run Stage')
    const btnAbort = h('button', { class: 'btn-danger hidden', onclick: () => doAbort(dep, sk, badge, btnRun, btnAbort) }, '■  Abort')
    const btnClear = h('button', { class: 'btn-secondary', onclick: () => { log.innerHTML = ''; log.classList.add('hidden') } }, 'Clear log')
    const btnRow   = h('div', { class: 'btn-row' }, btnRun, btnAbort, btnClear)

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
    dbd_to_dat:           { source: `${b}\\incoming_data\\${dep}`,                           output: `${b}\\dat_files\\${dep}\\merged_data` },
    dat_to_profile_nc:    { source: `${b}\\dat_files\\${dep}\\merged_data`,                  output: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile` },
    dat_to_trajectory_nc: { source: `${b}\\dat_files\\${dep}\\flight_data`,                  output: `${b}\\gncutils_slocum_nc_output\\${dep}\\trajectory` },
    fix_metadata:         { source: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile`,      output: `${b}\\gncutils_slocum_nc_output\\${dep}\\profile  (in-place)` },
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

function doRun (dep, sk, badge, btnRun, btnAbort, log) {
  log.classList.remove('hidden')
  log.innerHTML = ''
  btnRun.classList.add('hidden')
  btnAbort.classList.remove('hidden')
  badge.className   = 'status-badge running'
  badge.textContent = 'RUNNING…'

  streamRun(dep, sk,
    (line) => appendLog(log, line),
    (success) => {
      btnRun.classList.remove('hidden')
      btnAbort.classList.add('hidden')
      const st = success ? 'done' : 'failed'
      badge.className   = `status-badge ${st}`
      badge.textContent = statusLabel(st)
      btnRun.textContent = '↺  Re-run Stage'
      appendLog(log, success ? '✔ Finished successfully.' : '✖ Finished with errors.')
    }
  )
}

async function doAbort (dep, sk, badge, btnRun, btnAbort) {
  await abortRun(dep, sk)
  btnRun.classList.remove('hidden')
  btnAbort.classList.add('hidden')
  badge.className   = 'status-badge failed'
  badge.textContent = '✖ FAILED'
}

function appendLog (logEl, line) {
  const span = document.createElement('span')
  span.className = logColor(line)
  span.textContent = line + '\n'
  logEl.appendChild(span)
  logEl.scrollTop = logEl.scrollHeight
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

  const tabBar = h('div', { class: 'tab-bar' },
    h('button', { class: 'tab-btn active', 'data-tab': 'report', onclick: switchTab }, 'QC Report'),
    h('button', { class: 'tab-btn',        'data-tab': 'plots',  onclick: switchTab }, 'Plots')
  )
  page.appendChild(tabBar)

  // Report tab
  const frame = h('iframe', { id: 'report-frame', src: dep ? `${API}/api/deployment/${encodeURIComponent(dep)}/report` : 'about:blank' })
  const reportTab = h('div', { class: 'tab-panel active', id: 'tab-report' }, frame)

  // Plots tab
  const plotGrid   = h('div', { class: 'plot-grid' })
  const plotsTab   = h('div', { class: 'tab-panel', id: 'tab-plots' }, plotGrid)

  page.appendChild(reportTab)
  page.appendChild(plotsTab)

  if (dep) {
    try {
      const pd = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/plots`)
      const all = [...pd.pngs, ...pd.profile_pngs]
      if (!all.length) {
        plotGrid.appendChild(h('div', { class: 'empty-state' }, 'No plots generated yet.'))
      } else {
        all.forEach(fp => {
          const name  = fp.split(/[\\/]/).pop()
          const src   = `${API}/api/file?path=${encodeURIComponent(fp)}`
          const thumb = h('div', { class: 'plot-thumb', onclick: () => window.APP.shellOpen(fp) },
            h('img', { src, alt: name, loading: 'lazy' }),
            h('div', { class: 'plot-thumb-name' }, name)
          )
          plotGrid.appendChild(thumb)
        })
      }
    } catch {}
  }
}

function switchTab (e) {
  const tabName = e.currentTarget.dataset.tab
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName))
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `tab-${tabName}`))
}

/* ── Metadata page ───────────────────────────────────────────────────────────*/
async function renderMetadata () {
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
        h('button', { class: 'btn-primary',
          onclick: () => showBuildSensorDefs(dep) }, 'Build Sensor Defs')
      ) : null
    )
  ))

  if (!dep) return

  let md
  try { md = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata`) }
  catch { page.appendChild(h('div', { class: 'empty-state' }, 'Could not load metadata.')); return }

  const entries = Object.entries(md.files || {})

  if (!entries.length) {
    page.appendChild(_metaEmptyPanel(dep, md.config_dir))
    return
  }

  const fileList = h('div', { class: 'meta-file-list' })
  const content  = h('div', { class: 'meta-content' })
  const grid     = h('div', { class: 'meta-grid' }, fileList, content)
  page.appendChild(grid)

  // Group entries by top-level subdirectory
  const groupLabels = { science_profile: 'Profile', science_trajectory: 'Trajectory' }
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
  Object.entries(groups).forEach(([group, items]) => {
    fileList.appendChild(h('div', { class: 'meta-section-header' }, groupLabels[group] || group))
    items.forEach(([rel, json]) => {
      const filename = rel.split(/[\\/]/).pop()
      const isFirst  = firstEntry === null
      if (isFirst) firstEntry = [rel, json]

      // Copy-from-deployment button (shown on hover)
      const copyBtn = otherDeps.length
        ? h('button', {
            class: 'meta-file-copy',
            title: `Copy ${filename} from another deployment…`,
            onclick: async e => {
              e.stopPropagation()
              await _metaCopyFromPrev(dep, glider, rel, fileList, content)
            }
          }, '↩')
        : null

      // Import-from-disk button (shown on hover)
      const importBtn = h('button', {
        class: 'meta-file-import',
        title: `Replace ${filename} with a file from disk…`,
        onclick: e => {
          e.stopPropagation()
          _metaImportFileReplace(dep, rel, fileList, content)
        }
      }, '↑')

      const item = h('div', {
        class: 'meta-file-item' + (isFirst ? ' active' : ''),
        'data-rel': rel,
        onclick: () => selectMetaFile(rel, json, fileList, content, dep),
      }, h('span', { class: 'meta-file-name' }, filename), importBtn, copyBtn)
      fileList.appendChild(item)
    })
  })
  // Add-file bar at the bottom of the file list
  fileList.appendChild(_metaAddBar(dep))

  if (firstEntry) selectMetaFile(firstEntry[0], firstEntry[1], fileList, content, dep)
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
      // Refresh content pane with new data
      selectMetaFile(rel, parsed, fileList, content, dep)
      // Flash the row green
      const row = fileList.querySelector(`.meta-file-item.active`)
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
            '⧉  Copy all from deployment')
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
          onclick: () => _metaCopyAllFromDep(dep, glider) }, '⧉ Copy all from dep')
      : null,
    h('button', { class: 'meta-add-action', title: 'Import JSON files from disk',
      onclick: () => _metaImportJsonFiles(dep) }, '↑ Import files'),
    h('button', { class: 'meta-add-action', title: 'Create a new blank config file',
      onclick: () => _metaNewFileModal(dep) }, '+ New file'))
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

/* Import JSON files from disk paths ─────────────────────────────────────────*/
function _metaImportJsonFiles (dep) {
  const SUBDIRS   = ['science_profile', 'science_trajectory']
  const fileItems = []   // {name, path, size, subdir, status, msg?}

  const errorEl    = h('div', { class: 'import-error' })
  const countEl    = h('div', { class: 'import-count' })
  const fileListEl = h('div', { class: 'import-file-list' })

  const defaultSubdir = h('select', { class: 'form-input',
    onchange: e => {
      fileItems.filter(i => i.status === 'queued').forEach(i => { i.subdir = e.target.value })
      renderList()
    }},
    ...SUBDIRS.map(s => h('option', { value: s }, s)))

  function renderList () {
    fileListEl.innerHTML = ''
    fileItems.forEach(item => {
      const icon = { queued: '…', copying: '…', done: '✓', error: '✗' }[item.status] || '?'
      const sel = h('select', { class: 'import-subdir-sel',
        onchange: e => { item.subdir = e.target.value }},
        ...SUBDIRS.map(s => {
          const o = h('option', { value: s }, s)
          if (s === item.subdir) o.selected = true
          return o
        }))
      if (item.status !== 'queued') sel.disabled = true
      fileListEl.appendChild(h('div', { class: 'import-file-item' },
        h('span', { class: `import-status import-status-${item.status}` }, icon),
        h('span', { class: 'import-fname' }, item.name),
        sel,
        item.msg ? h('span', { class: 'import-item-msg' }, item.msg) : null))
    })
    const done  = fileItems.filter(i => i.status === 'done').length
    const total = fileItems.length
    countEl.textContent = total ? `${done} / ${total} imported` : ''
  }

  function handleFiles (files) {
    errorEl.textContent = ''
    const valid   = files.filter(f => f.name.toLowerCase().endsWith('.json'))
    const skipped = files.length - valid.length
    if (skipped) errorEl.textContent = `${skipped} file(s) skipped — only .json files accepted`
    valid.forEach(f => {
      if (!fileItems.find(i => i.name === f.name))
        fileItems.push({ name: f.name, path: f.path || '', size: f.size,
          subdir: defaultSubdir.value, status: 'queued' })
    })
    renderList()
  }

  const fileInput = h('input', { type: 'file', multiple: true, accept: '.json', style: 'display:none',
    onchange: e => { handleFiles(Array.from(e.target.files)); e.target.value = '' }})
  const dropzone = h('div', { class: 'import-dropzone' },
    h('div', { class: 'import-drop-icon' }, '↑'),
    h('div', { class: 'import-drop-text' }, 'Drop JSON config files here'),
    h('div', { class: 'import-drop-ext' }, '.json'),
    h('div', { class: 'import-drop-or' }, 'or'),
    h('label', { class: 'btn-secondary import-browse-btn' }, 'Browse…', fileInput))
  dropzone.addEventListener('dragover',  e => { e.preventDefault(); dropzone.classList.add('drag-over') })
  dropzone.addEventListener('dragleave', e => { if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('drag-over') })
  dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('drag-over'); handleFiles(Array.from(e.dataTransfer.files)) })

  const statusEl  = h('div', { class: 'modal-status' })
  const importBtn = h('button', { class: 'btn-success' }, 'Import')

  importBtn.onclick = async () => {
    const queued = fileItems.filter(i => i.status === 'queued')
    if (!queued.length) { statusEl.textContent = 'No files to import.'; statusEl.style.color = '#e74c3c'; return }

    const paths = queued.map(i => i.path).filter(Boolean)
    if (!paths.length) {
      queued.forEach(i => { i.status = 'error'; i.msg = 'Open in Electron app to import files' })
      renderList(); return
    }

    queued.forEach(i => { i.status = 'copying' })
    renderList(); importBtn.disabled = true

    try {
      const r = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/metadata/import-files`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ files: queued.map(i => ({ path: i.path, subdir: i.subdir })) }),
      })
      r.results.forEach(res => {
        const item = fileItems.find(i => i.path === res.path)
        if (item) { item.status = res.ok ? 'done' : 'error'; if (!res.ok) item.msg = res.error }
      })
      renderList()
      const doneCount = fileItems.filter(i => i.status === 'done').length
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
      renderList(); importBtn.disabled = false
    }
  }

  const overlay = h('div', { class: 'modal-overlay', id: 'modal-overlay',
    onclick: e => { if (e.target === overlay) closeModal() }},
    h('div', { class: 'modal' },
      h('div', { class: 'modal-header' },
        h('h2', {}, 'Import JSON Config Files'),
        h('button', { class: 'modal-close', onclick: closeModal }, '×')),
      h('div', { class: 'modal-body' },
        h('div', { class: 'form-group' },
          h('label', { class: 'form-label' }, 'Import files to folder'),
          defaultSubdir),
        dropzone, errorEl, countEl, fileListEl),
      h('div', { class: 'modal-footer' },
        statusEl,
        h('button', { class: 'btn-secondary', onclick: closeModal }, 'Cancel'),
        importBtn)))
  document.body.appendChild(overlay)
}

/* Create a new blank JSON config file ───────────────────────────────────────*/
function _metaNewFileModal (dep) {
  const SUBDIRS = ['science_profile', 'science_trajectory']

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

    // Refresh the view with the new content
    selectMetaFile(rel, srcJson, fileList, content, dep)
    // Flash the active item
    const activeItem = fileList.querySelector(`.meta-file-item.active`)
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
  const text = JSON.stringify(jsonData, null, 2) || '{}'
  content.innerHTML = ''

  // sensor_defs.json gets a variable table instead of raw JSON
  if (rel.endsWith('sensor_defs.json') && jsonData && typeof jsonData === 'object') {
    renderSensorDefsTable(rel, jsonData, text, content, dep)
    return
  }

  const editBtn = h('button', { class: 'btn-primary', style: 'margin-bottom:10px',
    onclick: () => renderMetaEdit(rel, text, content, dep) }, 'Edit')
  content.appendChild(editBtn)
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
  const datSet     = datData ? new Set(datData.sensors) : null  // sensors in actual DAT files
  const datCount   = datSet ? datData.files_scanned : 0

  // Mutable working copies
  const selected = {
    profile:    new Set(data.profile),
    trajectory: new Set(data.trajectory),
  }

  let activeTab   = 'profile'
  let filterToDat = false  // toggle: show only sensors present in DAT files

  function buildPane (tabKey) {
    const sel  = selected[tabKey]
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

  const tabs = ['profile', 'trajectory'].map(key => {
    const btn = h('button', {
      class: 'ss-tab' + (key === activeTab ? ' active' : ''),
      onclick: () => {
        activeTab = key
        document.querySelectorAll('.ss-tab').forEach(b => b.classList.remove('active'))
        btn.classList.add('active')
        paneContainer.innerHTML = ''
        paneContainer.appendChild(buildPane(key))
      }
    }, key === 'profile' ? 'Profile' : 'Trajectory')
    return btn
  })

  // DAT status line
  const datStatus = datSet
    ? h('span', { class: 'ss-dat-status' },
        `${datCount} DAT file${datCount !== 1 ? 's' : ''} scanned — ${datSet.size} sensors in your data`)
    : h('span', { class: 'ss-dat-status ss-dat-status-none' }, 'No DAT files found for this deployment')

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
            profile:    [...selected.profile].sort(),
            trajectory: [...selected.trajectory].sort(),
          })
        })
        saveBtn.disabled = false; saveBtn.textContent = 'Save'
        statusEl.textContent = `Saved — ${r.profile_count} profile, ${r.traj_count} trajectory sensors. Run "Build Sensor Defs" to apply.`
        statusEl.style.color = '#7ec8a0'
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

function renderMetaEdit (rel, originalText, content, dep) {
  content.innerHTML = ''

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
      statusMsg.textContent = 'Saved.'
      statusMsg.style.color = '#27ae60'
      saveBtn.disabled = false
      saveBtn.textContent = 'Save'
      // Refresh view with updated data
      renderMetaView(rel, JSON.parse(raw), content, dep)
    } catch (e) {
      statusMsg.textContent = e.message
      statusMsg.style.color = '#e74c3c'
      saveBtn.disabled = false
      saveBtn.textContent = 'Save'
    }
  }}, 'Save')

  const cancelBtn = h('button', { class: 'btn-secondary', onclick: () => {
    renderMetaView(rel, JSON.parse(originalText), content, dep)
  }}, 'Cancel')

  const btnRow = h('div', { class: 'btn-row', style: 'margin-bottom:8px' },
    saveBtn, cancelBtn, statusMsg)

  content.appendChild(btnRow)
  content.appendChild(textarea)
  textarea.focus()
}

/* ── DAT Browser page ────────────────────────────────────────────────────────*/
async function renderDatBrowser () {
  const dep  = state.deployment
  const page = document.getElementById('page-dat-browser')
  page.innerHTML = ''

  page.appendChild(h('div', { class: 'page-header' },
    h('h1', {}, 'DAT Browser'),
    h('p',  {}, dep ? `Merged DAT files for ${dep}` : 'Select a deployment.')
  ))

  if (!dep) return

  let dd
  try { dd = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-files`) }
  catch { page.appendChild(h('div', { class: 'empty-state' }, 'Could not load DAT files.')); return }

  if (!dd.files.length) {
    page.appendChild(h('div', { class: 'empty-state' }, `No DAT files found in:\n${dd.dir}`))
    return
  }

  const fileList = h('div', { class: 'dat-file-list' })
  const preview  = h('div', { class: 'dat-preview' }, 'Select a file to preview its header.')
  const layout   = h('div', { class: 'dat-layout' }, fileList, preview)
  page.appendChild(layout)

  dd.files.forEach((name, i) => {
    const item = h('div', { class: 'dat-file-item', onclick: () => previewDat(dd.dir, name, item, fileList, preview) }, name)
    fileList.appendChild(item)
  })
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
    // Show first 80 lines
    preview.textContent = text.split('\n').slice(0, 80).join('\n')
  } catch (e) {
    preview.textContent = 'Error loading file: ' + e.message
  }
}

/* ── Navigation ──────────────────────────────────────────────────────────────*/
const pageRenderers = {
  dashboard:   renderDashboard,
  pipeline:    renderPipeline,
  reports:     renderReports,
  metadata:    renderMetadata,
  'dat-browser': renderDatBrowser,
}

async function navigate (page) {
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
  const { deployments } = await loadAppState()

  const sel = document.getElementById('dep-select')
  if (!deployments.length) {
    sel.appendChild(h('option', { value: '' }, 'No deployments'))
  } else {
    deployments.forEach(dep => sel.appendChild(h('option', { value: dep }, dep)))
    state.deployment = deployments[0]
    sel.value = state.deployment
  }

  sel.addEventListener('change', e => {
    state.deployment = e.target.value
    navigate(state.page)
  })

  document.querySelectorAll('.nav-item').forEach(li =>
    li.addEventListener('click', () => navigate(li.dataset.page))
  )

  await navigate('dashboard')
}

init().catch(err => {
  document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif;color:#c00">
    <h2>Failed to connect to Python backend</h2>
    <p>${err.message}</p>
    <p style="margin-top:12px;color:#888">Make sure Flask is installed: <code>pip install flask</code></p>
  </div>`
})
