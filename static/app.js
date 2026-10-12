const $ = id => document.getElementById(id);
const settingsPage = !!$('settings-form');
let consoleView = 'operations';
const labels = {DISK_BACKED:'硬盘已备份', NEW:'新增待上传', CHANGED:'变化待上传', CLOUD_UPLOADED:'云端已上传', UPLOAD_FAILED:'上传待重试', SOURCE_DELETED:'源端已删除', ERROR:'不支持的文件'};
const taskLabels = {running:'正在运行', success:'已完成', failed:'失败', cancelled:'已停止', interrupted:'已中断'};
const number = value => Number(value || 0).toLocaleString('zh-CN');
const date = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '尚无记录';
function bytes(value) { let size = Number(value || 0), unit = 0; const units = ['B','KB','MB','GB','TB','PB']; while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; } return `${size.toLocaleString('zh-CN', {maximumFractionDigits:1})} ${units[unit]}`; }
let settings = null, summary = null, page = 1, totalPages = 1, chosenAction = null, chosenPaths = [];
const selectedUploads = new Map();
let pendingPage = 1, pendingPages = 1, pendingItems = [], pendingSequence = 0, lastPendingRefresh = 0;
let configurationSaved = false, settingsDirty = false;
let lockedPaths = {source:false,disk:false}, passwordConfigured = false, savingSettings = false;
let logCursor = 0, logTaskId = null, polling = false, fileSequence = 0, lastFileRefresh = 0, pollError = false;

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content,...options.headers}});
  const content = await response.json().catch(() => ({error:'服务返回了无法读取的响应'}));
  if (!response.ok) throw new Error(content.error || `请求失败（${response.status}）`);
  return content;
}
function message(text, error = false) { const node = $(error ? 'page-error' : 'page-message'); node.textContent = text; node.hidden = !text; if (error) pollError = false; if (!error) setTimeout(() => { node.hidden = true; }, 5000); }
function statusClass(status) { return ['ERROR','UPLOAD_FAILED','failed','interrupted'].includes(status) ? 'error' : ['NEW','CHANGED','running'].includes(status) ? 'pending' : ['SOURCE_DELETED','cancelled'].includes(status) ? 'neutral' : ''; }
function setBadge(node, text, status) { node.textContent = text; node.className = `badge ${statusClass(status)}`; }
function displaySettings() {
  if (!settingsPage) {
    $('source-summary').textContent = settings.source;
    $('disk-summary').textContent = settings.disk;
    $('cloud-summary').textContent = `${settings.cloud_host}:${settings.cloud_path}`;
    return;
  }
  for (const [key,value] of Object.entries(settings)) { const field = $('settings-form').elements.namedItem(key); if (field) field.value = value; }
  for (const key of ['source','disk']) {
    $('settings-form').elements.namedItem(key).readOnly = !!lockedPaths[key];
    $(`${key}-hint`).textContent = lockedPaths[key] ? '由 Docker Compose 配置，只读展示。' : '填写这台服务器上的实际路径。';
  }
  $('cloud-password').placeholder = passwordConfigured ? '本次服务会话已保存；留空保留原密码' : '输入 SSH 登录密码';
  $('password-hint').textContent = passwordConfigured ? '密码已在本次服务会话中保存，刷新或切换页面可继续使用；服务重启后需重新输入。' : '请填写并保存后再上传。密码不写入文件，服务重启后需重新输入。';
}
function selectConsoleView(view, record = true) {
  if (!['operations', 'files', 'activity'].includes(view)) view = 'operations';
  consoleView = view;
  document.body.classList.toggle('operations-scroll', view === 'operations');
  document.querySelectorAll('.console-view').forEach(panel => { panel.hidden = panel.id !== view; });
  document.querySelectorAll('.console-nav [role="tab"]').forEach(tab => {
    const selected = tab.dataset.view === view;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  if (record && location.hash !== `#${view}`) history.pushState(null, '', `#${view}`);
  if (settings && view === 'files') refreshFiles().catch(error => message(error.message, true));
  if (settings && view === 'operations') refreshPending().catch(error => message(error.message, true));
}

function updateControls() {
  const running = summary?.task?.status === 'running';
  document.querySelectorAll('.task-action').forEach(button => {
    button.disabled = !settings || !configurationSaved || settingsDirty || running || savingSettings || (button.classList.contains('needs-baseline') && !summary?.baseline_at) || (button.dataset.action === 'upload-selected' && !selectedUploads.size);
  });
  if (settingsPage) {
    $('save-settings').disabled = !settings || running || savingSettings;
    $('test-cloud').disabled = !settings || running || savingSettings;
    $('settings-form').querySelectorAll('input').forEach(input => { input.disabled = running || savingSettings; });
  }
  if (!settingsPage) displaySelection();
  $('config-state').textContent = running ? '任务正在运行，完成或停止后可以修改配置。' : settingsDirty ? '配置有修改，请保存后再开始任务。' : configurationSaved ? (settingsPage ? '配置已保存，可返回控制台开始任务。' : '配置已保存，可选择下方操作开始任务。') : '请先在全局配置中确认并保存配置，再开始任务。';
}
function displaySummary(value) {
  const previousTask = summary?.task;
  if (value.task && value.task.status !== 'running' && ['upload', 'upload-selected'].includes(value.task.action) && (previousTask?.id !== value.task.id || previousTask?.status === 'running')) selectedUploads.clear();
  summary = value;
  if (settingsPage) { updateControls(); return false; }
  const counts = value.counts, pending = (counts.NEW || 0) + (counts.CHANGED || 0) + (counts.UPLOAD_FAILED || 0);
  $('metric-baseline').textContent = number(value.baseline_count);
  $('metric-pending').textContent = number(pending);
  $('metric-uploaded').textContent = number(counts.CLOUD_UPLOADED);
  $('metric-bytes').textContent = bytes(value.total_bytes);
  $('baseline-time').textContent = value.baseline_at ? `记录于 ${date(value.baseline_at)}` : '还没有备份基准';
  setBadge($('baseline-badge'), value.baseline_at ? '已保存基准' : '尚无基准', value.baseline_at ? 'DISK_BACKED' : 'SOURCE_DELETED');
  $('scan-caption').textContent = value.baseline_at ? '已有记录，硬盘可拔掉' : '首次对比磁盘与服务器';
  $('pending-caption').textContent = value.baseline_at ? `${number(pending)} 个条目等待上传` : '等待扫描';
  const task = value.task, running = task?.status === 'running';
  updateControls();
  $('cancel-task').disabled = !running;
  setBadge($('task-badge'), task ? taskLabels[task.status] : '暂无任务', task?.status || 'SOURCE_DELETED');
  $('task-description').textContent = task ? `${task.label} · ${date(task.started_at)}${task.error ? ' · ' + task.error : ''}` : '开始一个任务后，这里会显示执行进度。';
  $('log-title').textContent = task ? task.label : '等待任务';
  $('task-time').textContent = task ? (task.finished_at ? `结束于 ${date(task.finished_at)}` : `开始于 ${date(task.started_at)}`) : '任务日志会保存在本机';
  return previousTask?.id !== task?.id || previousTask?.status !== task?.status;
}
function appendFileCells(row, item) {
  const pathCell = row.insertCell(), path = document.createElement('code'); path.className = 'file-path'; path.textContent = item.relative_path; pathCell.append(path);
  if (item.last_upload_error) { const error = document.createElement('small'); error.className = 'file-error'; error.textContent = item.last_upload_error; pathCell.append(error); }
  row.insertCell().textContent = {file:'文件',directory:'目录',symlink:'链接',other:'特殊文件'}[item.entry_type] || item.entry_type;
  const sizeCell = row.insertCell(); sizeCell.className = 'size'; sizeCell.textContent = item.entry_type === 'file' ? bytes(item.size) : '—';
  const dateCell = row.insertCell(); dateCell.className = 'date'; dateCell.textContent = date(item.mtime_ns / 1e6);
  const statusCell = row.insertCell(), badge = document.createElement('span'); setBadge(badge, labels[item.status] || item.status, item.status); statusCell.append(badge);
}
function displaySelection() {
  const running = summary?.task?.status === 'running';
  const blocked = running || !settings || !configurationSaved;
  const size = [...selectedUploads.values()].reduce((total, item) => total + (item.entry_type === 'file' ? item.size : 0), 0);
  $('selection-caption').textContent = selectedUploads.size ? `已选 ${number(selectedUploads.size)} 项 · 普通文件 ${bytes(size)}` : '没有选择部分上传的文件';
  $('clear-selection').disabled = !selectedUploads.size || running;
  const count = pendingItems.filter(item => selectedUploads.has(item.relative_path)).length;
  $('select-pending-page').checked = pendingItems.length > 0 && count === pendingItems.length;
  $('select-pending-page').indeterminate = count > 0 && count < pendingItems.length;
  $('select-pending-page').disabled = blocked || !pendingItems.length;
  document.querySelectorAll('#pending-body input[type="checkbox"]').forEach(input => {
    input.checked = selectedUploads.has(input.dataset.path);
    input.disabled = blocked;
    input.closest('tr').classList.toggle('is-selected', input.checked);
  });
}
async function refreshPending() {
  const sequence = ++pendingSequence;
  const parameters = new URLSearchParams({pending:'1', page:pendingPage, q:$('pending-search').value});
  document.querySelectorAll('.pending-status-filters input:checked').forEach(input => parameters.append('status', input.value));
  const value = await api(`/api/files?${parameters}`);
  if (sequence !== pendingSequence) return;
  pendingPage = value.page; pendingPages = Math.max(1, Math.ceil(value.total / value.page_size)); pendingItems = value.items;
  $('pending-total').textContent = number(value.total);
  const body = $('pending-body'); body.replaceChildren();
  if (!pendingItems.length) {
    const cell = body.insertRow().insertCell(); cell.colSpan = 6; cell.className = 'empty';
    cell.textContent = summary?.baseline_at ? '没有符合筛选条件的待上传文件。' : '先开始扫描，待上传的新增和变化条目会显示在这里。';
  }
  pendingItems.forEach(item => {
    if (selectedUploads.has(item.relative_path)) selectedUploads.set(item.relative_path, item);
    const row = body.insertRow(), cell = row.insertCell(), checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.dataset.path = item.relative_path;
    checkbox.setAttribute('aria-label', `选择 ${item.relative_path}`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selectedUploads.set(item.relative_path, item); else selectedUploads.delete(item.relative_path);
      updateControls();
    });
    cell.append(checkbox); appendFileCells(row, item);
  });
  $('pending-page-caption').textContent = `共 ${number(value.total)} 条 · 每页 ${value.page_size} 条`;
  $('pending-page-number').textContent = `${pendingPage} / ${pendingPages}`;
  $('pending-previous').disabled = pendingPage <= 1; $('pending-next').disabled = pendingPage >= pendingPages;
  lastPendingRefresh = Date.now(); updateControls();
}
async function refreshFiles() {
  const sequence = ++fileSequence;
  const parameters = new URLSearchParams({page, status:$('status-filter').value, q:$('file-search').value});
  const value = await api(`/api/files?${parameters}`);
  if (sequence !== fileSequence) return;
  page = value.page; totalPages = Math.max(1, Math.ceil(value.total / value.page_size));
  $('file-total').textContent = number(value.total);
  $('file-origin').textContent = value.origin === 'baseline' ? '当前显示硬盘备份清单；扫描后会显示服务器当前状态。' : '查看每个文件当前的迁移状态。';
  const body = $('file-body'); body.replaceChildren();
  if (!value.items.length) {
    const row = body.insertRow(), cell = row.insertCell(); cell.colSpan = 5; cell.className = 'empty';
    cell.textContent = $('file-search').value || $('status-filter').value ? '没有符合条件的文件。' : '还没有文件记录。已有磁盘备份时，直接点击「开始扫描」对比并建立记录。';
  }
  for (const item of value.items) appendFileCells(body.insertRow(), item);
  $('page-caption').textContent = `共 ${number(value.total)} 条 · 每页 ${value.page_size} 条`;
  $('page-number').textContent = `${page} / ${totalPages}`;
  $('previous-page').disabled = page <= 1; $('next-page').disabled = page >= totalPages;
  lastFileRefresh = Date.now();
}
async function refreshLogs() {
  const value = await api(`/api/logs?${new URLSearchParams({cursor:logCursor,task_id:logTaskId || ''})}`);
  if (!value.task_id) return;
  if (logTaskId !== value.task_id) { $('log-output').textContent = ''; logCursor = 0; logTaskId = value.task_id; }
  logCursor = value.cursor;
  if (value.text) {
    const output = $('log-output'); output.textContent = (output.textContent + value.text).slice(-160000);
    if ($('follow-logs').checked) output.scrollTop = output.scrollHeight;
  }
}
async function refresh(forceFiles = false) {
  if (polling) return;
  polling = true;
  try {
    // 账本写入时可能短暂无法读取，日志仍然独立刷新。
    const [summaryResult, logResult] = await Promise.allSettled([api('/api/summary'), settingsPage ? Promise.resolve() : refreshLogs()]);
    if (logResult.status === 'rejected') throw logResult.reason;
    if (summaryResult.status === 'rejected') throw summaryResult.reason;
    const changed = displaySummary(summaryResult.value);
    if (!settingsPage && consoleView === 'files' && (forceFiles || changed || Date.now() - lastFileRefresh > 10000)) await refreshFiles();
    if (!settingsPage && consoleView === 'operations' && (forceFiles || changed || Date.now() - lastPendingRefresh > 10000)) await refreshPending();
    $('connection-status').textContent = '服务已连接'; if (pollError) { $('page-error').hidden = true; pollError = false; }
  } catch (error) { $('connection-status').textContent = '连接或读取失败'; message(error.message, true); pollError = true; }
  finally { polling = false; }
}
function openAction(action) {
  if (!configurationSaved || settingsDirty) { message('请先在全局配置中保存配置，再开始任务。', true); return; }
  if (action === 'upload-selected' && !selectedUploads.size) { message('没有选择部分上传的文件，请先勾选待上传条目。', true); return; }
  chosenAction = action;
  chosenPaths = action === 'upload-selected' ? [...selectedUploads.keys()] : [];
  const descriptions = {
    backup:['备份到硬盘','把源目录文件复制到硬盘，成功后更新备份清单。请确认这个硬盘目录已准备好。'],
    preview:['预览备份差异','只查看准备复制哪些文件，不复制文件，也不更新账本。'],
    scan:['扫描文件变化',summary?.baseline_at?'扫描服务器文件，对照数据库记录识别新增和变化，不需要读取硬盘。':'先扫描已有磁盘备份，再扫描服务器目录。相同的记为已备份，磁盘缺少的记为新增，不同的记为变化。只建立记录，不复制或上传文件。'],
    upload:['全部上传增量文件','先扫描服务器文件，再上传全部新增、变化和需要重试的条目，不受勾选或筛选限制。'],
    'upload-selected':['部分上传',`只上传勾选的 ${number(chosenPaths.length)} 个条目。先重新扫描，跳过已不再待上传的路径；目录不会递归上传未勾选文件。`]
  };
  $('action-title').textContent = descriptions[action][0]; $('action-description').textContent = descriptions[action][1];
  const paths = [['源目录',settings.source]];
  if (['backup','preview'].includes(action) || (action === 'scan' && !summary?.baseline_at)) paths.splice(1,0,['硬盘目录',settings.disk]);
  if (['upload','upload-selected'].includes(action)) paths.splice(1,0,['云端目录',`${settings.cloud_user}@${settings.cloud_host}:${settings.cloud_path}`]);
  if (action === 'upload-selected') chosenPaths.forEach(path => paths.push(['选中路径', path]));
  $('action-paths').replaceChildren();
  paths.forEach(([label,value]) => { const row = document.createElement('div'), title = document.createElement('span'), path = document.createElement('code'); title.textContent = label; path.textContent = value; row.append(title,path); $('action-paths').append(row); });
  $('action-dialog').showModal();
}
if (!settingsPage) {
document.querySelectorAll('.task-action').forEach(button => button.addEventListener('click', () => openAction(button.dataset.action)));
document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
$('confirm-action').addEventListener('click', async () => {
  $('confirm-action').disabled = true;
  try {
    const payload = {action:chosenAction};
    if (chosenAction === 'upload-selected') payload.selected_paths = chosenPaths;
    await api('/api/tasks', {method:'POST',body:JSON.stringify(payload)});
    $('action-dialog').close(); logCursor = 0; logTaskId = null;
    selectConsoleView('activity');
    await refresh(true);
  } catch (error) { $('action-dialog').close(); message(error.message,true); }
  finally { $('confirm-action').disabled = false; }
});
}
if (settingsPage) $('settings-form').addEventListener('input', () => { settingsDirty = true; $('settings-error').hidden = true; updateControls(); });
async function saveSettings(showMessage = true) {
  if (!$('settings-form').reportValidity()) throw new Error('请填写完整的连接配置。');
  savingSettings = true; updateControls();
  const updated = {...settings};
  for (const key of Object.keys(updated)) { const input = $('settings-form').elements.namedItem(key); if (input) updated[key] = typeof settings[key] === 'number' ? Number(input.value) : input.value; }
  updated.cloud_password = $('cloud-password').value;
  try {
    const value = await api('/api/config',{method:'POST',body:JSON.stringify(updated)});
    settings = value.settings; passwordConfigured = value.password_configured; $('cloud-password').value = '';
    configurationSaved = true; settingsDirty = false;
    displaySettings(); page = 1; if (showMessage) message('配置已保存，可返回控制台开始任务。'); await refresh(true);
  } finally { savingSettings = false; updateControls(); }
}
if (settingsPage) {
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await saveSettings(); }
  catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; }
});
$('test-cloud').addEventListener('click', async () => {
  try {
    await saveSettings(false); savingSettings = true; updateControls();
    const value = await api('/api/cloud-test',{method:'POST',body:'{}'});
    message(value.message); $('settings-error').hidden = true;
  } catch (error) { $('settings-error').textContent = error.message; $('settings-error').hidden = false; }
  finally { savingSettings = false; updateControls(); }
});
}
if (!settingsPage) {
const tabs = [...document.querySelectorAll('.console-nav [role="tab"]')];
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectConsoleView(tab.dataset.view));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;
    event.preventDefault(); tabs[next].focus(); tabs[next].click();
  });
});
window.addEventListener('popstate', () => selectConsoleView(location.hash.slice(1), false));
window.addEventListener('hashchange', () => selectConsoleView(location.hash.slice(1), false));
selectConsoleView(location.hash.slice(1), false);
$('cancel-task').addEventListener('click', async () => { $('cancel-task').disabled = true; try { await api('/api/tasks/cancel',{method:'POST',body:'{}'}); await refresh(true); } catch (error) { message(error.message,true); } });
$('refresh').addEventListener('click', () => refresh(true));
$('status-filter').addEventListener('change', () => { page = 1; refreshFiles().catch(error => message(error.message,true)); });
let searchTimer;
$('file-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { page = 1; refreshFiles().catch(error => message(error.message,true)); },250); });
$('clear-selection').addEventListener('click', () => { selectedUploads.clear(); updateControls(); });
$('select-pending-page').addEventListener('change', event => {
  pendingItems.forEach(item => { if (event.target.checked) selectedUploads.set(item.relative_path, item); else selectedUploads.delete(item.relative_path); });
  updateControls();
});
document.querySelectorAll('.pending-status-filters input').forEach(input => input.addEventListener('change', () => { pendingPage = 1; refreshPending().catch(error => message(error.message,true)); }));
let pendingSearchTimer;
$('pending-search').addEventListener('input', () => { clearTimeout(pendingSearchTimer); pendingSearchTimer = setTimeout(() => { pendingPage = 1; refreshPending().catch(error => message(error.message,true)); },250); });
$('pending-previous').addEventListener('click', () => { pendingPage--; refreshPending().catch(error => message(error.message,true)); });
$('pending-next').addEventListener('click', () => { pendingPage++; refreshPending().catch(error => message(error.message,true)); });
$('previous-page').addEventListener('click', () => { page--; refreshFiles().catch(error => message(error.message,true)); });
$('next-page').addEventListener('click', () => { page++; refreshFiles().catch(error => message(error.message,true)); });
}
async function initialize() {
  try { const config = await api('/api/config'); settings = config.settings; configurationSaved = config.configured; lockedPaths = config.locked_paths; passwordConfigured = config.password_configured; displaySettings(); updateControls(); await refresh(true); }
  catch (error) { message(error.message,true); }
  setInterval(() => { if (settings && !document.hidden) refresh(); },2000);
}
initialize();
