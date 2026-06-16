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

  // Free-text deployment name field — user types the full name directly.
  // Datalist suggests known glider prefixes as a starting point.
  const nameInput   = h('input', { class: 'form-input', type: 'text', id: 'nd-name',
    placeholder: gliders.length ? gliders[0] + '-' : 'e.g. uvi_01-20250318T2008',
    list: 'nd-name-hints',
    oninput: () => { _ndUpdateFolders(); _ndAutoTemplate() } })
  const nameHints   = h('datalist', { id: 'nd-name-hints' },
    ...gliders.map(g => h('option', { value: g + '-' })))
  const nameHint    = h('div', { class: 'nd-name-hint' }, 'Format: glider-YYYYMMDDThhmm')

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

  const deps = Array.from(document.getElementById('dep-select').options).map(o => o.value).filter(v => v)
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

    const btnRun   = h('button', { class: 'btn-success', onclick: () => doRun(dep, sk, badge, btnRun, btnAbort, log, stageOpts ? stageOpts() : {}) }, '▶  Run Stage')
    const btnAbort = h('button', { class: 'btn-danger hidden', onclick: () => doAbort(dep, sk, badge, btnRun, btnAbort) }, '■  Abort')
    const btnClear = h('button', { class: 'btn-secondary', onclick: () => { log.innerHTML = ''; log.classList.add('hidden'); stageLogs[sk] = [] } }, 'Clear log')
    const btnRow   = h('div', { class: 'btn-row' }, btnRun, btnAbort, btnClear)

    // Restore state if this stage is currently running or has prior log output
    if (activeRuns[sk]) {
      // This page session started the run — reconnect live DOM refs and replay log
      stageEls[sk] = { log, badge, btnRun, btnAbort }
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

      function showReconnectBtn () {
        note.innerHTML = ''
        const btn = h('button', { class: 'btn-secondary', style: 'margin-left:8px',
          onclick: attemptReconnect }, '⟳  Reconnect to logs')
        note.appendChild(document.createTextNode('⏳ Stage is running in the background.'))
        note.appendChild(btn)
      }

      function applyFinishedState (st) {
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
        stageEls[sk]   = { log, badge, btnRun, btnAbort }
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

function doRun (dep, sk, badge, btnRun, btnAbort, log, opts = {}) {
  // Register live DOM refs — updated by buildStageCard on re-render
  stageEls[sk]  = { log, badge, btnRun, btnAbort }
  stageLogs[sk] = []

  log.classList.remove('hidden')
  log.innerHTML = ''
  btnRun.classList.add('hidden')
  btnAbort.classList.remove('hidden')
  badge.className   = 'status-badge running'
  badge.textContent = 'RUNNING…'

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
      const el = stageEls[sk]
      delete stageEls[sk]
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
    const actionBadge = h('span', { class: `preflight-badge ${actionBadgeClass(info.action)}` },
      info.action.toUpperCase())

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
    const inpLatMin = makeNumInput(rc.proposed_lat[0])
    const inpLatMax = makeNumInput(rc.proposed_lat[1])
    const inpLonMin = makeNumInput(rc.proposed_lon[0])
    const inpLonMax = makeNumInput(rc.proposed_lon[1])

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

  btnSkip.addEventListener('click', () => {
    // If nothing needs changing, mark stage done; otherwise leave as-is
    if (!needsApply) {
      apiFetch(`/api/deployment/${encodeURIComponent(dep)}/set-status/preflight`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'done' }) })
      badge.className   = 'status-badge done'
      badge.textContent = statusLabel('done')
    }
    panel.remove()
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

async function doAbort (dep, sk, badge, btnRun, btnAbort) {
  await abortRun(dep, sk)
  const el = stageEls[sk] || { badge, btnRun, btnAbort }
  el.btnRun.classList.remove('hidden')
  el.btnAbort.classList.add('hidden')
  el.badge.className   = 'status-badge failed'
  el.badge.textContent = '✖ FAILED'
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

  const tabBar = h('div', { class: 'tab-bar' },
    h('button', { class: 'tab-btn active', 'data-tab': 'report',    onclick: switchTab     }, 'QC Report'),
    h('button', { class: 'tab-btn',        'data-tab': 'plots',     onclick: switchTab     }, 'Plots'),
    h('button', { class: 'tab-btn',        'data-tab': 'variables', onclick: switchToVars  }, 'Variables'),
    h('button', { class: 'tab-btn',        'data-tab': 'map',       onclick: switchToMap   }, 'Map'),
    h('button', { class: 'tab-btn',        'data-tab': 'flight',    onclick: switchToFlight}, 'Flight Stats'),
  )
  page.appendChild(tabBar)

  // Report tab
  const frame = h('iframe', { id: 'report-frame', src: dep ? `${API}/api/deployment/${encodeURIComponent(dep)}/report` : 'about:blank' })
  const reportTab = h('div', { class: 'tab-panel active', id: 'tab-report' }, frame)

  // Plots tab
  const plotGrid = h('div', { class: 'plot-grid' })
  const plotsTab = h('div', { class: 'tab-panel', id: 'tab-plots' }, plotGrid)

  // Variables tab
  const varsTab   = h('div', { class: 'tab-panel', id: 'tab-variables' })

  // Map tab
  const mapTab    = h('div', { class: 'tab-panel', id: 'tab-map'    })

  // Flight Stats tab
  const flightTab = h('div', { class: 'tab-panel', id: 'tab-flight' })

  page.appendChild(reportTab)
  page.appendChild(plotsTab)
  page.appendChild(varsTab)
  page.appendChild(mapTab)
  page.appendChild(flightTab)

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
  catch (e) { container.innerHTML = `<div class="empty-state">Could not load variables: ${e.message}</div>`; return }

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
    const stagePill = h('span', { class: 'nc-section-stage' }, `Stage ${stage}`)
    card.appendChild(h('div', { class: 'vars-card-header' },
      h('span', { class: 'vars-card-title' }, label),
      stagePill
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
    '⏳ Loading trajectory data from ' + dep + '…',
    h('br', {}),
    h('span', { style: 'font-size:11px;color:#546e7a' },
      'First load reads all trajectory NC files and may take 1–3 minutes. Subsequent loads are instant.')
  ))

  let trajData
  try {
    trajData = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/trajectory-report`)
  } catch (e) {
    container.innerHTML = ''
    container.appendChild(h('div', { class: 'empty-state' }, 'Could not load trajectory data: ' + e.message))
    return null
  }

  container.innerHTML = ''

  const { surfacings, stats } = trajData
  const lats  = surfacings.lats  || []
  const lons  = surfacings.lons  || []
  const times = surfacings.times || []

  // ── Header bar ─────────────────────────────────────────────────────────────
  const kmzBtn = h('button', { class: 'btn-secondary',
    onclick: () => {
      const a = document.createElement('a')
      a.href     = `${API}/api/deployment/${encodeURIComponent(dep)}/kmz`
      a.download = `${dep}_glider_track.kmz`
      document.body.appendChild(a); a.click(); a.remove()
    }
  }, '⬇ Download KMZ')

  const headerBar = h('div', { class: 'traj-header-bar' },
    h('span', { class: 'traj-stat-chip' }, `${stats.n_surfacings} GPS surfacings`),
    h('span', { class: 'traj-stat-chip' }, `${stats.n_records.toLocaleString()} records`),
    h('span', { class: 'traj-stat-chip' }, `${stats.n_files} traj files`),
    kmzBtn,
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
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 18,
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
      const popupHtml = `<b>Surfacing ${i + 1} of ${lats.length}</b><br>${times[i] || ''}`
      L.marker([lat, lons[i]], { icon }).bindPopup(popupHtml).addTo(leafletMap)
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
    _statCard('GPS Surfacings',   stats.n_surfacings),
    _statCard('Records',          stats.n_records.toLocaleString()),
    _statCard('Battery Min',      fmtV(stats.battery_v.min, 2) + ' V'),
    _statCard('Battery Max',      fmtV(stats.battery_v.max, 2) + ' V'),
    _statCard('Amp-Hours Used',   stats.total_ah_used != null ? fmtV(stats.total_ah_used, 1) + ' Ah' : 'N/A'),
    _statCard('Mean Pitch',       fmtV(stats.pitch_deg.mean, 1) + '°'),
    _statCard('Mean Roll',        fmtV(stats.roll_deg.mean,  1) + '°'),
    _statCard('Mean Vacuum',      fmtV(stats.vacuum_inhg.mean, 2) + ' inHg'),
  ))

  const chartsWrap = h('div', { class: 'flight-charts' })
  container.appendChild(chartsWrap)

  // ── Plotly charts ──────────────────────────────────────────────────────────
  const DARK = {
    paper_bgcolor: '#131c27',
    plot_bgcolor:  '#0f1720',
    font:          { color: '#9ab4cc', size: 11 },
    margin:        { l: 56, r: 16, t: 10, b: 48 },
    xaxis:         { type: 'date', gridcolor: '#1e2d40', zerolinecolor: '#1e2d40' },
    showlegend:    true,
    legend:        { orientation: 'h', y: 1.08, font: { size: 11 } },
  }

  function addChart (title, traces) {
    const div = h('div', { class: 'flight-chart' })
    chartsWrap.appendChild(h('div', { class: 'flight-chart-wrap' },
      h('div', { class: 'flight-chart-title' }, title),
      div))
    return div
  }

  try {
    const Plotly = await loadPlotlyLib()

    const cfg = { responsive: true, displayModeBar: false }

    // Pitch & Roll
    const prDiv = addChart('Pitch & Roll (degrees)')
    Plotly.newPlot(prDiv, [
      { x: times, y: timeseries.pitch, name: 'Pitch', type: 'scattergl', mode: 'lines',
        line: { color: '#60a5fa', width: 1 } },
      { x: times, y: timeseries.roll,  name: 'Roll',  type: 'scattergl', mode: 'lines',
        line: { color: '#f97316', width: 1 } },
    ], { ...DARK, yaxis: { ...DARK.xaxis, title: { text: 'deg', standoff: 4 } } }, cfg)

    // Battery Voltage
    const battDiv = addChart('Battery Voltage (V)')
    Plotly.newPlot(battDiv, [
      { x: times, y: timeseries.battery, name: 'Battery', type: 'scattergl', mode: 'lines',
        line: { color: '#4ade80', width: 1 } },
    ], { ...DARK, showlegend: false,
         yaxis: { ...DARK.xaxis, title: { text: 'V', standoff: 4 } } }, cfg)

    // Cumulative Amp-Hours
    const ahDiv = addChart('Cumulative Amp-Hours')
    Plotly.newPlot(ahDiv, [
      { x: times, y: timeseries.coulomb, name: 'Amp-hrs', type: 'scattergl', mode: 'lines',
        line: { color: '#facc15', width: 1 } },
    ], { ...DARK, showlegend: false,
         yaxis: { ...DARK.xaxis, title: { text: 'Ah', standoff: 4 } } }, cfg)

    // Vacuum (hull integrity)
    const vacDiv = addChart('Vacuum (inHg) — Hull Integrity')
    Plotly.newPlot(vacDiv, [
      { x: times, y: timeseries.vacuum, name: 'Vacuum', type: 'scattergl', mode: 'lines',
        line: { color: '#c084fc', width: 1 } },
    ], { ...DARK, showlegend: false,
         yaxis: { ...DARK.xaxis, title: { text: 'inHg', standoff: 4 } } }, cfg)

  } catch (e) {
    chartsWrap.appendChild(h('div', { class: 'empty-state' }, 'Could not render charts: ' + e.message))
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
  const groupLabels = { science_profile: 'Profile', science_trajectory: 'Trajectory (Science)', trajectory_flight: 'Trajectory (Flight)' }
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
    profile:            new Set(data.profile),
    trajectory:         new Set(data.trajectory),
    trajectory_flight:  new Set(data.trajectory_flight),
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
      }
    }, TAB_LABELS[key])
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
            profile:           [...selected.profile].sort(),
            trajectory:        [...selected.trajectory].sort(),
            trajectory_flight: [...selected.trajectory_flight].sort(),
          })
        })
        saveBtn.disabled = false; saveBtn.textContent = 'Save'
        statusEl.textContent = `Saved — ${r.profile_count} profile, ${r.traj_count} traj (sci), ${r.traj_flight_count} traj (flt) sensors. Run "Build Sensor Defs" to apply.`
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
  }

  const tabBar = h('div', { class: 'files-tab-bar' },
    h('button', { class: 'files-tab active', 'data-tab': 'raw',        onclick: () => setTab('raw')        }, 'Raw Files'),
    h('button', { class: 'files-tab',        'data-tab': 'dat',        onclick: () => setTab('dat')        }, 'DAT Files'),
    h('button', { class: 'files-tab',        'data-tab': 'profile-nc', onclick: () => setTab('profile-nc') }, 'Profile NC'),
    h('button', { class: 'files-tab',        'data-tab': 'traj-nc',    onclick: () => setTab('traj-nc')    }, 'Trajectory NC'),
    h('button', { class: 'files-tab',        'data-tab': 'qc-output',  onclick: () => setTab('qc-output')  }, 'QC Output'),
    h('button', { class: 'files-tab',        'data-tab': 'cac',        onclick: () => setTab('cac')        }, 'CAC Files'),
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
  const totalSize  = files.reduce((s, f) => s + f.size, 0)
  const summaryEl  = h('div', { class: 'raw-summary' },
    ...Object.entries(extCounts).sort().map(([ext, n]) =>
      h('span', { class: 'raw-ext-badge' }, `${ext.slice(1).toUpperCase()}: ${n}`)),
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

  container.appendChild(h('div', { class: 'raw-controls' }, summaryEl, dropzone, errorEl, statusEl))
  container.appendChild(listEl)
}


async function renderDatTab (dep, container) {
  container.innerHTML = ''

  let data
  try { data = await apiFetch(`/api/deployment/${encodeURIComponent(dep)}/dat-files`) }
  catch (e) { container.appendChild(h('div', { class: 'empty-state' }, 'Could not load DAT files: ' + e.message)); return }

  // Sub-tabs: merged_data and flight_data
  let activeSub = 'merged_data'
  const subBody = h('div', { class: 'dat-sub-body' })

  function setSubTab (key) {
    activeSub = key
    subBar.querySelectorAll('.dat-sub-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.sub === key))
    buildDatView(key)
  }

  const subBar = h('div', { class: 'dat-sub-bar' },
    h('button', { class: 'dat-sub-tab active', 'data-sub': 'merged_data', onclick: () => setSubTab('merged_data') },
      `merged_data (${data.merged_data.files.length})`),
    h('button', { class: 'dat-sub-tab', 'data-sub': 'flight_data', onclick: () => setSubTab('flight_data') },
      `flight_data (${data.flight_data.files.length})`),
  )

  function buildDatView (subdir) {
    subBody.innerHTML = ''
    const { files, dir } = data[subdir]
    if (!files.length) {
      subBody.appendChild(h('div', { class: 'empty-state' }, `No DAT files in:\n${dir}`))
      return
    }
    const fileList = h('div', { class: 'dat-file-list' })
    const preview  = h('div', { class: 'dat-preview' }, 'Select a file to preview its header.')
    subBody.appendChild(h('div', { class: 'dat-layout' }, fileList, preview))
    files.forEach(f => {
      const item = h('div', { class: 'dat-file-item' },
        h('span', { class: 'dat-file-name' }, f.name),
        h('span', { class: 'dat-file-size' }, _fmtBytes(f.size)))
      item.addEventListener('click', () => previewDat(dir, f.name, item, fileList, preview))
      fileList.appendChild(item)
    })
  }

  container.appendChild(subBar)
  container.appendChild(subBody)
  setSubTab('merged_data')
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


/* ── NC file sections (Stages 2a / 2b / 2c / 4) ─────────────────────────────*/
function renderNcSection (title, stageLabel, data, container) {
  const { files = [], dir = '', exists = false } = data
  const totalSize = files.reduce((s, f) => s + f.size, 0)

  // Section header row
  const canOpen = exists && window.APP && window.APP.shellOpen
  container.appendChild(h('div', { class: 'nc-section-header' },
    h('div', { class: 'nc-section-meta' },
      h('span', { class: 'nc-section-title' }, title),
      h('span', { class: 'nc-section-stage' }, stageLabel),
      files.length
        ? h('span', { class: 'nc-section-count' }, `${files.length} files · ${_fmtBytes(totalSize)}`)
        : h('span', { class: 'nc-section-count nc-section-empty' }, 'no files yet'),
    ),
    h('div', { class: 'nc-section-path' },
      h('code', { class: 'nc-dir-path', title: dir }, dir),
      canOpen
        ? h('button', { class: 'btn-secondary btn-xs',
            onclick: () => window.APP.shellOpen(dir) }, 'Open ↗')
        : null,
    ),
  ))

  if (!files.length) {
    container.appendChild(h('div', { class: 'nc-empty' },
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
  container.appendChild(listEl)
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

  // ── Header ────────────────────────────────────────────────────────────────
  const header = h('div', { class: 'cac-header' },
    h('h2', { class: 'cac-title' }, 'CAC Files'),
    h('p',  { class: 'cac-subtitle' },
      'Slocum glider cache files (.cac / .ccc) used by Stage 1 to decode binary data. ' +
      'Files are stored in master_cac/ and shared across all deployments.'
    )
  )
  container.appendChild(header)

  // ── File list ─────────────────────────────────────────────────────────────
  const listWrap = h('div', { class: 'cac-list-wrap' })
  container.appendChild(listWrap)

  async function refreshList () {
    listWrap.innerHTML = ''
    let data
    try { data = await apiFetch('/api/cac-files') }
    catch (e) {
      listWrap.appendChild(h('div', { class: 'empty-state' }, 'Could not load CAC files: ' + e.message))
      return
    }

    const files = data.files
    const dirEl = h('div', { class: 'cac-dir-path' }, data.dir)
    const countEl = h('span', { class: 'cac-count' }, `${files.length} files`)
    listWrap.appendChild(h('div', { class: 'cac-list-header' }, dirEl, countEl))

    if (!files.length) {
      listWrap.appendChild(h('div', { class: 'empty-state' }, 'No CAC files found. Upload some using the drop zone below.'))
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
    listWrap.appendChild(table)
  }

  await refreshList()

  // ── Drop zone ─────────────────────────────────────────────────────────────
  const statusEl = h('div', { class: 'cac-upload-status hidden' })

  const fileInput = h('input', {
    type: 'file', multiple: true,
    accept: '.cac,.ccc',
    style: 'display:none',
  })

  async function uploadFiles (files) {
    if (!files.length) return
    statusEl.textContent = `Uploading ${files.length} file${files.length > 1 ? 's' : ''}…`
    statusEl.className = 'cac-upload-status uploading'
    const fd = new FormData()
    for (const f of files) fd.append('files', f)
    try {
      const resp = await fetch(`${API}/api/cac-files/upload`, { method: 'POST', body: fd })
      const result = await resp.json()
      if (result.ok) {
        const n = result.saved.length
        statusEl.textContent = `Saved ${n} file${n !== 1 ? 's' : ''}: ${result.saved.join(', ')}`
        statusEl.className = 'cac-upload-status success'
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

  container.appendChild(h('div', { class: 'cac-upload-section' }, dropZone, statusEl))
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

  const header = h('div', { class: 'page-header' },
    h('h1', {}, 'Run Logs'),
    h('p',  {}, 'History of every pipeline stage run — pass and fail.')
  )
  const body = h('div', { class: 'page-body' })
  page.appendChild(header)
  page.appendChild(body)

  let logs
  try {
    const d = await apiFetch('/api/logs')
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

  // Filter bar
  const allDeps = [...new Set(logs.map(l => l.dep))].sort()
  const filterSel = h('select', { class: 'log-filter-sel' })
  filterSel.appendChild(h('option', { value: '' }, 'All deployments'))
  allDeps.forEach(d => filterSel.appendChild(h('option', { value: d }, d)))
  if (state.deployment) filterSel.value = state.deployment

  const filterRow = h('div', { class: 'log-filter-row' },
    h('label', { style: 'font-size:12px;color:#607d8b;margin-right:6px' }, 'Deployment:'),
    filterSel
  )
  body.appendChild(filterRow)

  // Table container (re-rendered on filter change)
  const tableWrap = h('div', {})
  body.appendChild(tableWrap)

  function renderTable () {
    tableWrap.innerHTML = ''
    const dep = filterSel.value
    const filtered = dep ? logs.filter(l => l.dep === dep) : logs

    if (!filtered.length) {
      tableWrap.appendChild(h('p', { style: 'color:#8a9bac;margin-top:12px' }, 'No logs for this deployment yet.'))
      return
    }

    const table = h('table', { class: 'log-table' })
    table.appendChild(h('thead', {},
      h('tr', {},
        h('th', {}, 'Result'),
        h('th', {}, 'Stage'),
        h('th', {}, 'Deployment'),
        h('th', {}, 'Date / Time'),
        h('th', {}, 'Size'),
        h('th', {}, '')
      )
    ))
    const tbody = h('tbody', {})
    filtered.forEach(entry => {
      const badge = h('span', {
        class: `badge ${entry.result === 'pass' ? 'done' : 'failed'}`
      }, entry.result === 'pass' ? '✔ Pass' : '✖ Fail')

      const viewBtn = h('button', { class: 'btn-secondary btn-sm' }, 'View')
      viewBtn.onclick = () => showLogFileViewer(entry)

      tbody.appendChild(h('tr', {},
        h('td', {}, badge),
        h('td', {}, state.stageInfo[entry.stage]?.label || entry.stage),
        h('td', { style: 'font-family:monospace;font-size:11px' }, entry.dep),
        h('td', { style: 'font-family:monospace;font-size:11px' }, entry.timestamp),
        h('td', { style: 'color:#8a9bac;font-size:11px' }, formatBytes(entry.size)),
        h('td', {}, viewBtn)
      ))
    })
    table.appendChild(tbody)
    tableWrap.appendChild(table)
  }

  filterSel.onchange = renderTable
  renderTable()
}

/* ── Navigation ──────────────────────────────────────────────────────────────*/
const pageRenderers = {
  dashboard:     renderDashboard,
  pipeline:      renderPipeline,
  logs:          renderLogs,
  reports:       renderReports,
  metadata:      renderMetadata,
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
    // Blank "choose" option so nothing is selected until the user picks
    sel.appendChild(h('option', { value: '' }, '— select deployment —'))
    deployments.forEach(dep => sel.appendChild(h('option', { value: dep }, dep)))

    // Restore last deployment from localStorage (e.g. after a page refresh)
    const lastDep = localStorage.getItem('lastDeployment')
    if (lastDep && deployments.includes(lastDep)) {
      state.deployment = lastDep
      sel.value = lastDep
    } else {
      // First launch (or stored deployment no longer exists) — stay on dashboard
      state.deployment = null
      sel.value = ''
    }
  }

  sel.addEventListener('change', e => {
    state.deployment = e.target.value || null
    if (state.deployment) localStorage.setItem('lastDeployment', state.deployment)
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
