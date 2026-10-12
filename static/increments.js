const $ = id => document.getElementById(id);
const numbers = value => Number(value || 0).toLocaleString('zh-CN');
const timeText = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '尚无记录';
function bytes(value) { if (value === null) return '未记录'; let amount = Number(value || 0), unit = 0; const units = ['B','KB','MB','GB','TB','PB']; while (amount >= 1024 && unit < 5) { amount /= 1024; unit++; } return `${amount.toLocaleString('zh-CN', {maximumFractionDigits:1})} ${units[unit]}`; }
const statusLabels = {SUCCESS:'已完成', PARTIAL:'部分未确认', FAILED:'失败', RUNNING:'正在上传', INTERRUPTED:'已中断，结果未确认'};
const originLabels = {NEW:'新增', CHANGED:'变化', UPLOAD_FAILED:'重试'};
let selectedId = null, selectedBatch = null, directoryPage = 1, directoryPages = 1, filePage = 1, filePages = 1;
let trend = [], refreshing = false, detailSequence = 0, directorySignature = '', currentDb = null;

async function api(url) { const response = await fetch(url); const body = await response.json(); if (!response.ok) throw new Error(body.error || '读取记录失败'); return body; }
function errorMessage(error) { $('history-error').textContent = error.message; $('history-error').hidden = false; }
function badge(element, text, state) { element.textContent = text; element.className = 'badge ' + (['FAILED','PARTIAL','INTERRUPTED'].includes(state) ? 'error' : ['RUNNING','PENDING'].includes(state) ? 'pending' : ''); }
function emptyRow(text) { const row = document.createElement('tr'), cell = document.createElement('td'); cell.colSpan = 5; cell.className = 'empty'; cell.textContent = text; row.append(cell); $('increment-body').replaceChildren(row); }

function drawTrend() {
  const svg = $('trend-svg'), chart = $('trend-chart');
  const known = trend.filter(point => point.size_known);
  if (!known.length) {
    svg.toggleAttribute('hidden', true); $('trend-empty').hidden = false;
    $('trend-empty').textContent = trend.length ? '已有批次尚未记录可用的增量大小，完成一次上传后即可查看趋势。' : '暂无上传记录。点击首页的「开始上传」后，这里会记录每次进入上传阶段的增量。';
    return;
  }
  svg.toggleAttribute('hidden', false); $('trend-empty').hidden = true;
  const width = Math.max(280, chart.clientWidth), height = 252, left = 65, right = 24, top = 24, bottom = 44;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const maximum = known.reduce((value, point) => Math.max(value, point.total_bytes), 1);
  const unitIndex = Math.min(5, Math.max(0, Math.floor(Math.log(maximum) / Math.log(1024))));
  const unit = ['B','KB','MB','GB','TB','PB'][unitIndex], divisor = 1024 ** unitIndex;
  const rough = maximum / divisor;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const ceiling = Math.ceil(rough / magnitude * 2) / 2 * magnitude;
  const y = value => top + plotHeight * (1 - value / divisor / ceiling);
  const x = index => left + (trend.length === 1 ? plotWidth / 2 : plotWidth * index / (trend.length - 1));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.replaceChildren();
  svg.setAttribute('aria-label', `已记录 ${trend.length} 次上传，折线显示各次选中增量文件大小，纵轴单位 ${unit}`);
  $('trend-unit').textContent = `纵轴单位：${unit}`;
  const ns = 'http://www.w3.org/2000/svg';
  function node(tag, attributes, text) { const element = document.createElementNS(ns,tag); Object.entries(attributes).forEach(([key,value]) => element.setAttribute(key,value)); if (text !== undefined) element.textContent = text; svg.append(element); return element; }
  for (let tick = 0; tick <= 4; tick++) {
    const value = ceiling * tick / 4, position = y(value * divisor);
    node('line',{x1:left,y1:position,x2:width-right,y2:position,class:'chart-grid'});
    node('text',{x:left-10,y:position+4,'text-anchor':'end',class:'chart-label'},value.toLocaleString('zh-CN',{maximumFractionDigits:2}));
  }
  const ticks = width < 500 ? 3 : 6;
  const indexes = [...new Set(Array.from({length:Math.min(ticks,trend.length)},(_,index) => Math.round(index * (trend.length - 1) / Math.max(1,Math.min(ticks,trend.length)-1))))];
  indexes.forEach(index => node('text',{x:x(index),y:height-15,'text-anchor':index===0&&trend.length>1?'start':index===trend.length-1&&trend.length>1?'end':'middle',class:'chart-label'},`第 ${trend[index].number} 次`));
  let segment = [];
  function line() { if (segment.length > 1) node('polyline',{points:segment.join(' '),class:'chart-line'}); segment=[]; }
  trend.forEach((point,index) => { if (point.size_known) segment.push(`${x(index)},${y(point.total_bytes)}`); else line(); }); line();
  trend.forEach((point,index) => {
    if (!point.size_known) return;
    const pointX=x(index), pointY=y(point.total_bytes), selected=point.id===selectedId;
    if (selected) node('line',{x1:pointX,y1:top,x2:pointX,y2:top+plotHeight,class:'chart-selection'});
    node('circle',{cx:pointX,cy:pointY,r:selected?5:4,class:'chart-point'});
    const hit=node('circle',{cx:pointX,cy:pointY,r:18,class:'chart-hit',tabindex:0,role:'button','aria-label':`查看第 ${point.number} 次上传，增量 ${bytes(point.total_bytes)}`});
    const title=document.createElementNS(ns,'title'); title.textContent=`第 ${point.number} 次 · ${timeText(point.started_at)} · ${bytes(point.total_bytes)}`; hit.append(title);
    const describe=() => { $('trend-detail').textContent=title.textContent; };
    hit.addEventListener('mouseenter',describe); hit.addEventListener('focus',describe);
    hit.addEventListener('click',() => selectBatch(point.id));
    hit.addEventListener('keydown',event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); selectBatch(point.id); } });
  });
}

function displayDirectory(value) {
  const trendChanged=JSON.stringify(trend)!==JSON.stringify(value.trend);
  directoryPage=value.page; directoryPages=Math.max(1,Math.ceil(value.total/value.page_size)); trend=value.trend;
  $('history-total').textContent=`已记录 ${numbers(value.total)} 次上传`; $('batch-count').textContent=numbers(value.total);
  const signature=JSON.stringify(value.batches);
  if (signature!==directorySignature) {
    directorySignature=signature; $('batch-list').replaceChildren();
    if (!value.batches.length) { const empty=document.createElement('div'); empty.className='empty'; empty.textContent='暂无上传批次'; $('batch-list').append(empty); }
    value.batches.forEach(batch => {
      const button=document.createElement('button'); button.type='button'; button.className='batch-button'; button.dataset.id=batch.id;
      const head=document.createElement('span'); head.className='batch-button-head';
      const title=document.createElement('strong'); title.textContent=`第 ${batch.number} 次上传`;
      const status=document.createElement('span'); badge(status,statusLabels[batch.status]||batch.status,batch.status); head.append(title,status);
      const time=document.createElement('small'); time.textContent=timeText(batch.started_at);
      const amount=document.createElement('span'); amount.className='batch-button-amount'; amount.textContent=batch.size_known?bytes(batch.total_bytes):'大小未确认';
      const count=document.createElement('small'); count.textContent=batch.file_count===null?'旧记录或文件清单未完成':`${numbers(batch.file_count)} 个文件 · 新增 ${numbers(batch.new_files)}`;
      button.append(head,time,amount,count); button.addEventListener('click',() => selectBatch(batch.id)); $('batch-list').append(button);
    });
  }
  $('batch-page').textContent=`${directoryPage} / ${directoryPages}`;
  $('batch-previous').disabled=directoryPage<=1; $('batch-next').disabled=directoryPage>=directoryPages;
  highlightSelection(); if (trendChanged) drawTrend();
}
function highlightSelection() { document.querySelectorAll('.batch-button').forEach(button => { const active=Number(button.dataset.id)===selectedId; button.classList.toggle('is-selected',active); button.setAttribute('aria-pressed',active); }); }
function clearDetail() {
  detailSequence++; $('batch-title').textContent='选择一个上传批次';
  $('batch-time').textContent='左侧按第 1 次、第 2 次、第 N 次上传保存记录。';
  badge($('batch-status'),'暂无批次','PENDING');
  $('batch-metrics').hidden=true; $('batch-destination').hidden=true; $('batch-notice').hidden=true;
  $('increment-search').value=''; $('increment-origin').value='';
  $('increment-search').disabled=true; $('increment-origin').disabled=true;
  $('increment-previous').disabled=true; $('increment-next').disabled=true;
  $('increment-page').textContent='1 / 1'; $('increment-caption').textContent='每页 30 条';
  emptyRow('选择批次后查看文件清单。');
}

async function loadDetail() {
  if (selectedId===null) return;
  const sequence=++detailSequence;
  const parameters=new URLSearchParams({page:filePage,q:$('increment-search').value,origin:$('increment-origin').value});
  const value=await api(`/api/increments/${selectedId}?${parameters}`);
  if (sequence!==detailSequence) return;
  const batch=value.batch; selectedBatch=batch;
  $('batch-title').textContent=`第 ${batch.number} 次上传`;
  $('batch-time').textContent=`开始于 ${timeText(batch.started_at)}${batch.finished_at?' · 结束于 '+timeText(batch.finished_at):''}`;
  badge($('batch-status'),statusLabels[batch.status]||batch.status,batch.status);
  $('batch-destination').hidden=!batch.destination; $('batch-destination').textContent=batch.destination?`当次上传目标：${batch.destination}`:'';
  $('batch-metrics').hidden=false;
  $('batch-files').textContent=batch.file_count===null?'未记录':numbers(batch.file_count);
  $('batch-types').textContent=batch.has_details?`新增 ${numbers(batch.new_files)} · 变化 ${numbers(batch.changed_files)} · 重试 ${numbers(batch.retry_files)}`:'此批次没有保存文件明细';
  $('batch-bytes').textContent=batch.size_known?bytes(batch.total_bytes):'未确认';
  $('batch-uploaded').textContent=bytes(batch.uploaded_bytes);
  $('batch-results').textContent=batch.uploaded_files===null?'依据旧上传任务的汇总':`已确认 ${numbers(batch.uploaded_files)} 个文件 · 未确认 ${numbers((batch.failed_items||0)+(batch.pending_items||0))} 个条目`;
  const notice=$('batch-notice');
  notice.textContent=!batch.has_details?(batch.status==='RUNNING'?'正在准备本次清单，稍后会自动刷新。':batch.destination?'本次任务在文件清单保存完成前结束，明细未确认。':'此批次未保存文件清单。升级前的记录只能查看已有汇总，后续上传会保存完整明细。'):batch.error||(batch.status==='INTERRUPTED'?'任务已中断，结果未确认的文件不计为上传成功。':'');
  notice.hidden=!notice.textContent;
  $('increment-search').disabled=!batch.has_details; $('increment-origin').disabled=!batch.has_details;
  const body=$('increment-body'); body.replaceChildren();
  if (!value.items.length) emptyRow(!batch.has_details?'此批次没有可查看的文件明细。':value.total===0&&batch.entry_count===0?'本次没有新增或变化的文件，增量为 0 B。':'没有符合条件的文件。');
  value.items.forEach(item => {
    const row=body.insertRow(), pathCell=row.insertCell(), path=document.createElement('code'); path.className='file-path'; path.textContent=item.relative_path; pathCell.append(path);
    if (item.error) { const text=document.createElement('small'); text.className='file-error'; text.textContent=item.error; pathCell.append(text); }
    const type=row.insertCell(); type.textContent=(originLabels[item.original_status]||item.original_status)+(item.entry_type==='directory'?' · 目录':item.entry_type==='symlink'?' · 链接':'');
    const size=row.insertCell(); size.className='size'; size.textContent=item.entry_type==='file'?bytes(item.size):'不计入大小';
    const modified=row.insertCell(); modified.className='date'; modified.textContent=timeText(item.mtime_ns/1e6);
    const result=row.insertCell(), state=document.createElement('span');
    badge(state,item.result==='UPLOADED'?'上传成功':item.result==='FAILED'?'未确认成功':batch.status==='RUNNING'?'等待确认':'结果未确认',item.result==='FAILED'?'FAILED':item.result); result.append(state);
  });
  filePage=value.page; filePages=Math.max(1,Math.ceil(value.total/value.page_size));
  $('increment-caption').textContent=`共 ${numbers(value.total)} 个条目 · 每页 ${value.page_size} 条`;
  $('increment-page').textContent=`${filePage} / ${filePages}`;
  $('increment-previous').disabled=filePage<=1; $('increment-next').disabled=filePage>=filePages;
}
async function selectBatch(identifier) {
  selectedId=identifier; selectedBatch=null; filePage=1; $('increment-search').value=''; $('increment-origin').value='';
  highlightSelection(); drawTrend(); emptyRow('正在读取本批次文件…');
  try { await loadDetail(); } catch (error) { errorMessage(error); }
}
async function refreshHistory(force=false) {
  if (refreshing) return; refreshing=true;
  try {
    const value=await api(`/api/increments?page=${directoryPage}`);
    if (currentDb!==value.db) { selectedId=null; selectedBatch=null; directorySignature=''; currentDb=value.db; clearDetail(); }
    displayDirectory(value);
    if (selectedId===null&&value.batches.length) await selectBatch(value.batches[0].id);
    else if (selectedId!==null&&(force||selectedBatch?.status==='RUNNING'||value.batches.some(batch=>batch.id===selectedId&&batch.status!==selectedBatch?.status))) await loadDetail();
    $('history-error').hidden=true;
  } catch (error) { errorMessage(error); }
  finally { refreshing=false; }
}
$('history-refresh').addEventListener('click',()=>refreshHistory(true));
$('batch-previous').addEventListener('click',()=>{ directoryPage--; refreshHistory(); });
$('batch-next').addEventListener('click',()=>{ directoryPage++; refreshHistory(); });
$('increment-previous').addEventListener('click',()=>{ filePage--; loadDetail().catch(errorMessage); });
$('increment-next').addEventListener('click',()=>{ filePage++; loadDetail().catch(errorMessage); });
$('increment-origin').addEventListener('change',()=>{ filePage=1; loadDetail().catch(errorMessage); });
let searchTimer;
$('increment-search').addEventListener('input',()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>{ filePage=1; loadDetail().catch(errorMessage); },250); });
new ResizeObserver(()=>drawTrend()).observe($('trend-chart'));
refreshHistory();
setInterval(()=>{ if (!document.hidden) refreshHistory(); },5000);
