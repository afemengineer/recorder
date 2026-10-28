const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = { projects: [], project: null, sectionId: null, tab: innerWidth < 740 ? 'record' : 'review', recording: null, asr: false, audio: null, startedAt: 0, timer: null, uploadBusy: false };
const fmt = seconds => { const n = Math.max(0, Math.floor(Number(seconds) || 0)); return `${Math.floor(n / 60).toString().padStart(2,'0')}:${(n%60).toString().padStart(2,'0')}`; };
function toast(message, bad=false) { const el=$('#toast'); el.textContent=message; el.className=bad?'show error':'show'; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.className='',4300); }
async function api(path, opts={}) {
  const r=await fetch('/api'+path, { credentials:'same-origin', ...opts });
  if (!r.ok) { let detail='Request failed'; try { const data=await r.json(); detail= typeof data.detail==='string'?data.detail:JSON.stringify(data.detail); } catch {} throw new Error(`${detail} (${r.status})`); }
  if (r.status===204) return null;
  return r.json();
}
function json(method, path, body) { return api(path, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); }
async function init() {
  try {
    const auth=await api('/auth/status'); S.asr=auth.transcription_available;
    if (auth.enabled && !auth.authenticated) { showLogin(); return; }
    await loadProjects();
    await recoverUploads();
  } catch(e) { toast(e.message,true); }
}
function showLogin() {
  $('#modalRoot').innerHTML=`<div class="overlay"><form class="modal small" id="loginForm"><div class="modal-header"><span class="eyebrow">LOCAL ACCESS</span><h2>Unlock your studio</h2><p>Enter the password set on the recorder server.</p></div><label>Server password<input name="password" type="password" required autofocus autocomplete="current-password"></label><button class="button primary wide" type="submit">Unlock studio →</button></form></div>`;
  $('#loginForm').addEventListener('submit',async ev=>{ev.preventDefault();try{await json('POST','/auth/login',{password:ev.target.password.value});$('#modalRoot').innerHTML='';await init();}catch(e){toast(e.message,true);}});
}
async function loadProjects(preferred) {
  S.projects=await api('/projects');
  renderProjectList();
  const id=preferred || S.project?.id || S.projects[0]?.id;
  if(id) await loadProject(id); else renderWelcome();
}
async function loadProject(id, preferSection) {
  S.project=await api(`/projects/${id}`);
  S.sectionId=preferSection && S.project.sections.some(x=>x.id===preferSection)?preferSection:
    S.project.sections.some(x=>x.id===S.sectionId)?S.sectionId:S.project.sections[0]?.id;
  renderProjectList(); render();
}
function renderProjectList() {
  $('#projectList').innerHTML=S.projects.map(p=>`<button class="project-link ${S.project?.id===p.id?'active':''}" data-action="open-project" data-id="${p.id}"><span class="project-glyph">▤</span><span class="project-name">${esc(p.title)}</span></button>`).join('')||'<p class="muted sidebar-empty">No projects yet</p>';
}
function renderWelcome() {
  S.project=null;$('#projectTitle').textContent='Narration Studio'; $('#topSection').textContent='WELCOME';
  $('#content').innerHTML=`<section class="welcome"><div class="welcome-symbol">◉</div><span class="eyebrow">SCRIPT → RECORD → REVIEW</span><h2>Your voice, on your terms.</h2><p>Import a script, record paragraphs with unlimited retakes, and assemble a first edit entirely on your own network.</p><button class="button primary" data-action="new-project">Create your first project →</button></section>`;
}
function selectedSection() { return S.project?.sections.find(s=>s.id===S.sectionId); }
function selectedTake(s) { return s?.takes.find(t=>t.selected && t.state==='ready'); }
function render() {
  if(!S.project) return renderWelcome();
  const section=selectedSection();const index=S.project.sections.findIndex(s=>s.id===S.sectionId);
  $('#projectTitle').textContent=S.project.title;$('#topSection').textContent=S.tab==='record'?'RECORDING':'REVIEW';
  const completed=S.project.sections.filter(s=>s.takes.some(t=>t.selected && t.state==='ready')).length;
  const progress=100*completed/Math.max(S.project.sections.length,1);
  const tabs=`<div class="tab-row"><button data-action="tab" data-tab="record" class="tab ${S.tab==='record'?'active':''}">◉ &nbsp; Record</button><button data-action="tab" data-tab="review" class="tab ${S.tab==='review'?'active':''}">☷ &nbsp; Review & export</button></div>`;
  $('#content').innerHTML=`<div class="editor-header"><div><span class="eyebrow">${S.project.sections.length} SECTIONS · ${completed} RECORDED</span><h2>${S.tab==='record'?'Recording session':'Review your takes'}</h2></div><div class="export-buttons"><a class="button secondary small" href="/api/projects/${S.project.id}/mix.wav">↓ Rough cut</a><a class="button secondary small" href="/api/projects/${S.project.id}/export.zip">↓ Project ZIP</a></div></div>
    <div class="progress"><div style="width:${progress}%"></div></div>${tabs}
    <div class="studio-grid"><div class="stage">${section?(S.tab==='record'?recordPanel(section,index):reviewPanel(section,index)):'No sections yet'}</div>
    <aside class="section-panel"><div class="panel-heading"><span class="eyebrow">SCRIPT OUTLINE</span><span class="muted">${completed}/${S.project.sections.length}</span></div>
      <div class="sections">${S.project.sections.map((s,i)=>`<button class="section-link ${s.id===S.sectionId?'active':''}" data-action="open-section" data-id="${s.id}"><span class="section-number">${(i+1).toString().padStart(2,'0')}</span><span class="section-snippet">${esc(s.text)}</span><span class="section-state">${s.takes.some(t=>t.selected&&t.state==='ready')?'●':'○'}</span></button>`).join('')}</div>
      <button class="button dashed wide" data-action="add-section">+ Add section</button>
    </aside></div>`;
  if(S.tab==='review'&&section) { drawWave(selectedTake(section)); }
  if(S.recording) updateRecordUI();
}
function controls(index) {
  return `<div class="section-navigation"><button class="button subtle" data-action="prev" ${index===0?'disabled':''}>← Previous</button><span>SECTION ${index+1} OF ${S.project.sections.length}</span><button class="button subtle" data-action="next" ${index===S.project.sections.length-1?'disabled':''}>Next →</button></div>`;
}
function recordPanel(section,index) {
  const ready=section.takes.filter(t=>t.state==='ready');const pending=section.takes.filter(t=>t.state==='uploading');
  const active=S.recording?.sectionId===section.id;
  return `${controls(index)}<div class="script-surface"><div class="script-toolbar"><span class="eyebrow">TELEPROMPTER</span><div><button class="text-button" data-action="smaller" title="Smaller text">A−</button><button class="text-button" data-action="larger" title="Larger text">A+</button><button class="text-button" data-action="edit-script">Edit script</button></div></div><p class="script-text" id="scriptText" style="font-size:${localStorage.getItem('fontSize')||26}px">${esc(section.text)}</p></div>
  <div class="recorder-card ${active?'is-recording':''}"><div class="record-head"><div><span id="recordBadge" class="record-badge">${active?'● RECORDING':'● READY TO RECORD'}</span><div class="record-hint">${active?'Speak naturally. Restart any paragraph with Retake.':'Your microphone will only activate when you press Record.'}</div></div><div class="timer" id="recordTimer">${active?fmt((Date.now()-S.startedAt)/1000):'00:00'}</div></div>
    <div class="meter" id="meter"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
    <div class="record-buttons"><button class="button primary large" id="recordButton" data-action="${active?'stop':'record'}">${active?'■ Stop recording':'● Start recording'}</button><button class="button secondary large" data-action="retake" ${!active?'disabled':''}>↺ Retake</button></div>
    <p id="captureStatus" class="fineprint">${active?'Recording locally and transferring audio in the background.':'WAV · Mono PCM16 · Your recordings never leave this network.'}</p>
  </div><div class="take-header"><h3>Recorded takes <span class="count">${ready.length}</span></h3><span class="muted">Latest completed take is selected automatically</span></div>${takeList(section, false)}
  ${pending.length?`<p class="notice">${pending.length} unfinished upload(s). Use the recovery button if needed.</p><button class="button secondary" data-action="recover">Retry uploads</button>`:''}`;
}
function takeList(section, detailed) {
  const takes=section.takes.filter(t=>t.state==='ready');
  if(!takes.length) return '<div class="empty-takes">No completed takes yet. Record your first attempt above.</div>';
  return `<div class="take-list">${takes.map((t,i)=>`<div class="take-card ${t.selected?'chosen':''}"><div class="take-info"><div class="take-title"><span class="take-number">TAKE ${i+1}</span>${t.selected?'<span class="pill">SELECTED</span>':''}</div><div class="muted">${fmt(t.duration)} · ${t.sample_rate} Hz · Peak ${t.peak_dbfs??'—'} dBFS ${t.clipping_pct>0.1?' · ⚠ CLIPPING':''}</div></div><button class="button ${t.selected?'subtle':'secondary'} small" data-action="select-take" data-id="${t.id}" ${t.selected?'disabled':''}>${t.selected?'✓ Using':'Use take'}</button><audio controls preload="none" src="/api/takes/${t.id}/audio"></audio>${detailed?`<button class="text-button" data-action="edit-take" data-id="${t.id}">Edit transcript & trim →</button>`:''}</div>`).join('')}</div>`;
}
function reviewPanel(section,index) {
  const t=selectedTake(section);return `${controls(index)}<div class="review-surface"><div class="eyebrow">REFERENCE SCRIPT</div><p>${esc(section.text)}</p><button class="text-button" data-action="edit-script">Edit reference</button></div>
  <div class="wave-panel"><div class="panel-heading"><h3>Selected take</h3><span class="muted">${t?fmt(t.duration):'No take selected'}</span></div>
  ${t?`<canvas id="waveCanvas" height="100" aria-label="Audio waveform"></canvas><audio id="reviewAudio" controls src="/api/takes/${t.id}/audio" preload="metadata"></audio>
   <div class="trim-row"><label>Start (s)<input id="trimStart" type="number" min="0" step="0.01" value="${t.trim_start}"></label><label>End (s; blank = end)<input id="trimEnd" type="number" min="0" step="0.01" placeholder="${t.duration.toFixed(2)}" value="${t.trim_end??''}"></label><button class="button secondary small" data-action="save-trim" data-id="${t.id}">Save trim</button></div><div class="fineprint">Non-destructive trims are applied to the rough cut, not to the original take files.</div>`:'<div class="empty-takes">Record and select a take to start reviewing.</div>'}</div>
  <div class="take-header"><h3>All takes</h3><span class="muted">Switch between alternatives</span></div>${takeList(section,true)}
  ${t?`<div class="transcript-panel"><div class="panel-heading"><h3>Spoken transcript</h3><span class="muted">${t.transcript_reviewed?'✓ Human verified':'Unverified'}</span></div><textarea id="spokenTranscript" rows="4" placeholder="What you actually said (including improvised words)">${esc(t.transcript)}</textarea><label class="checkbox"><input type="checkbox" id="verifiedTranscript" ${t.transcript_reviewed?'checked':''}> I checked this against the recording</label><div class="action-row"><button class="button secondary" data-action="save-transcript" data-id="${t.id}">Save transcript</button>${S.asr?`<button class="button subtle" data-action="transcribe" data-id="${t.id}" ${t.analysis_status==='processing'?'disabled':''}>${t.analysis_status==='processing'?'Transcribing…':'Transcribe locally'}</button>`:''}</div>${t.analysis_status==='error'?`<p class="notice">${esc(t.analysis_error)}</p>`:''}<p class="fineprint">The script is only a reference. TTS training data should use human-verified transcripts of the real audio.</p></div>`:''}`;
}
function updateRecordUI() {
  const b=$('#recordButton');if(b){b.textContent='■ Stop recording';b.dataset.action='stop';}
  $('#recordTimer') && ($('#recordTimer').textContent=fmt((Date.now()-S.startedAt)/1000));
}
function newProject() {
  if(S.recording) return toast('Stop recording first',true);
  $('#modalRoot').innerHTML=`<div class="overlay"><form id="newProjectForm" class="modal"><div class="modal-header"><button type="button" class="modal-close" data-action="close-modal">✕</button><span class="eyebrow">NEW NARRATION PROJECT</span><h2>Start with a script.</h2><p>Separate paragraphs with a blank line. Each paragraph becomes a recording section with unlimited takes.</p></div><label>Project title<input name="title" placeholder="e.g. Video #8 — MoE architectures" maxlength="160" required autofocus></label><label>Script<textarea name="script" rows="10" required placeholder="Paste your script here…\n\nEach paragraph becomes its own section."></textarea></label><button class="button primary wide" type="submit">Create project →</button></form></div>`;
  $('#newProjectForm').addEventListener('submit',async e=>{e.preventDefault();try{const p=await json('POST','/projects',{title:e.target.elements.namedItem('title').value,script:e.target.elements.namedItem('script').value});$('#modalRoot').innerHTML='';S.tab='record';await loadProjects(p.id);toast(`${p.sections.length} sections ready to record`);}catch(err){toast(err.message,true);}});
}
function editScript() {
  const s=selectedSection();if(!s)return;
  $('#modalRoot').innerHTML=`<div class="overlay"><form id="editForm" class="modal"><div class="modal-header"><button type="button" class="modal-close" data-action="close-modal">✕</button><span class="eyebrow">EDIT REFERENCE</span><h2>Section script</h2><p>Edits will not change recordings or existing transcripts.</p></div><label>Text<textarea name="text" rows="10" required>${esc(s.text)}</textarea></label><button class="button primary wide" type="submit">Save section</button></form></div>`;
  $('#editForm').addEventListener('submit',async e=>{e.preventDefault();try{await json('PATCH',`/sections/${s.id}`,{text:e.target.elements.namedItem('text').value});$('#modalRoot').innerHTML='';await loadProject(S.project.id,s.id);toast('Script updated');}catch(err){toast(err.message,true);}});
}
async function addSection() {
  const value=prompt('New section text:');if(!value?.trim())return;
  try{const result=await json('POST',`/projects/${S.project.id}/sections`,{text:value});await loadProject(S.project.id,result.id);}catch(e){toast(e.message,true);}
}
const openDb=()=>new Promise((resolve,reject)=>{const r=indexedDB.open('recorder-offline-v1',1);r.onupgradeneeded=()=>{const db=r.result;db.createObjectStore('chunks',{keyPath:'key'});db.createObjectStore('pending',{keyPath:'id'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
async function store(dbName, mode, fn) { const db=await openDb();return new Promise((resolve,reject)=>{const tx=db.transaction(dbName,mode);const request=fn(tx.objectStore(dbName));let result;request.onsuccess=()=>{result=request.result;};tx.oncomplete=()=>{db.close();resolve(result);};tx.onerror=()=>{db.close();reject(tx.error);};}); }
const putLocal=(table,item)=>store(table,'readwrite',s=>s.put(item));
const deleteLocal=(table,key)=>store(table,'readwrite',s=>s.delete(key));
const getLocal=(table,key)=>store(table,'readonly',s=>s.get(key));
const allLocal=table=>store(table,'readonly',s=>s.getAll());
function encodePCM(floatArray) { const bytes=new ArrayBuffer(floatArray.length*2);const view=new DataView(bytes);for(let i=0;i<floatArray.length;i++){const v=Math.max(-1,Math.min(1,floatArray[i]));view.setInt16(i*2,v<0?v*32768:v*32767,true);}return bytes; }
async function record() {
  if(S.recording)return;
  if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) return toast('Microphone recording requires HTTPS (or localhost) and AudioWorklet support.',true);
  const s=selectedSection(); if(!s)return;
  let stream,ctx;
  try {
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});
    ctx=new AudioContext({sampleRate:48000});await ctx.audioWorklet.addModule('/static/pcm-worklet.js');await ctx.resume();
    const started=await json('POST',`/sections/${s.id}/takes`,{sample_rate:ctx.sampleRate});
    const active={id:started.id,sectionId:s.id,stream,ctx,seq:0,stores:[],flushed:null,stopRequested:false};
    await putLocal('pending',{id:active.id,total:null,sectionId:s.id,projectId:S.project.id});
    const source=ctx.createMediaStreamSource(stream);const node=new AudioWorkletNode(ctx,'pcm-recorder',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1]});
    active.node=node;active.source=source;
    node.port.onmessage=e=>{
      if(e.data?.type==='audio') {
        const idx=active.seq++;const job=putLocal('chunks',{key:`${active.id}:${String(idx).padStart(8,'0')}`,takeId:active.id,idx,data:encodePCM(e.data.data)}).then(()=>drainUploads(active.id)).catch(err=>toast('Audio storage error: '+err.message,true));
        active.stores.push(job);
      } else if(e.data?.type==='flushed' && active.flushed) active.flushed();
    };
    source.connect(node);node.connect(ctx.destination);
    S.recording=active;S.startedAt=Date.now();S.timer=setInterval(()=>{updateRecordUI();const meter=$('#meter');if(meter)meter.classList.toggle('pulse');},1000);
    render(); toast('Recording started');
  } catch(e) {stream?.getTracks().forEach(t=>t.stop());await ctx?.close().catch(()=>{});toast('Could not start recording: '+e.message,true);}
}
async function stop() {
  const active=S.recording;if(!active||active.stopRequested)return;
  active.stopRequested=true;clearInterval(S.timer);
  try{
    active.stream.getTracks().forEach(t=>t.stop());active.source.disconnect();
    await new Promise(resolve=>{active.flushed=resolve;active.node.port.postMessage({type:'flush'});setTimeout(resolve,1500);});
    active.node.disconnect();await active.ctx.close();
    await Promise.all(active.stores);
    if(active.seq<1)throw new Error('No microphone samples were received');
    const meta=await getLocal('pending',active.id);await putLocal('pending',{...meta,total:active.seq});
    await drainUploads(active.id);
    const out=await finalizeLocal(active.id,active.seq);
    if(out)toast('Take saved and selected');else toast('Recorded safely on phone; upload pending. Use Retry uploads.',true);
  } catch(e){toast('Could not finalize: '+e.message,true);}
  finally{S.recording=null;await loadProject(S.project.id,active.sectionId).catch(()=>{});}
}
async function retake() {const sid=S.sectionId;if(S.recording)await stop();if(S.sectionId===sid)await record();}
async function drainUploads(takeId) {
  if(!navigator.onLine)return false;
  // Each call has its own pending list; HTTP PUT is idempotent for repeat chunks.
  const candidates=(await allLocal('chunks')).filter(c=>c.takeId===takeId).sort((a,b)=>a.idx-b.idx);
  for(const chunk of candidates){
    try {await api(`/takes/${takeId}/chunks/${chunk.idx}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:chunk.data});await deleteLocal('chunks',chunk.key);}
    catch {return false;}
  }
  return true;
}
async function finalizeLocal(id,total) {
  if((await allLocal('chunks')).some(c=>c.takeId===id))return false;
  try{await json('POST',`/takes/${id}/finish`,{expected_chunks:total});await deleteLocal('pending',id);return true;}
  catch(e){console.warn('Finalize pending:',e);return false;}
}
async function recoverUploads() {
  const pending=await allLocal('pending');let recovered=0;
  for(const entry of pending){
    const chunks=(await allLocal('chunks')).filter(c=>c.takeId===entry.id);
    const total=entry.total??(chunks.length?Math.max(...chunks.map(c=>c.idx))+1:null);
    if(total==null)continue;
    if(entry.total==null)await putLocal('pending',{...entry,total});
    if(await drainUploads(entry.id) && await finalizeLocal(entry.id,total))recovered++;
  }
  if(recovered){toast(`${recovered} recording(s) recovered`);if(S.project)await loadProject(S.project.id);}
}
async function updateTake(id,patch) {try{await json('PATCH',`/takes/${id}`,patch);await loadProject(S.project.id,S.sectionId);}catch(e){toast(e.message,true);}}
async function drawWave(take) {
  const canvas=$('#waveCanvas');if(!take||!canvas)return;
  try {
    const r=await fetch(`/api/takes/${take.id}/audio`);if(!r.ok)return;
    const bytes=await r.arrayBuffer();const ctx=new AudioContext();const audio=await ctx.decodeAudioData(bytes);const channel=audio.getChannelData(0);
    const dpr=devicePixelRatio||1;canvas.width=canvas.clientWidth*dpr;canvas.height=100*dpr;const g=canvas.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,canvas.clientWidth,100);g.fillStyle='#9ba8ff';const w=canvas.clientWidth;const step=Math.max(1,Math.floor(channel.length/w));for(let x=0;x<w;x++){let peak=0;for(let j=x*step;j<Math.min((x+1)*step,channel.length);j++)peak=Math.max(peak,Math.abs(channel[j]));const h=Math.max(2,peak*94);g.fillRect(x,(100-h)/2,1,h);}await ctx.close();
  } catch(e) {console.debug('Waveform unavailable:',e);}
}
function editTake(id) {const t=selectedSection()?.takes.find(x=>x.id===id);if(!t)return;S.tab='review';if(!t.selected)updateTake(id,{selected:true});else render();}
async function act(target) {
  const action=target.dataset.action;
  if(S.recording && !['stop','retake','smaller','larger'].includes(action))return toast('Stop recording before switching sections',true);
  try {
    switch(action){
      case 'new-project':newProject();break;case 'close-modal':$('#modalRoot').innerHTML='';break;
      case 'open-project':$('#sidebar').classList.remove('mobile-open');await loadProject(target.dataset.id);break;
      case 'open-section':S.sectionId=target.dataset.id;render();break;
      case 'tab':S.tab=target.dataset.tab;render();break;
      case 'prev':case 'next':{const i=S.project.sections.findIndex(s=>s.id===S.sectionId)+(action==='next'?1:-1);if(S.project.sections[i]){S.sectionId=S.project.sections[i].id;render();}break;}
      case 'record':await record();break;case 'stop':await stop();break;case 'retake':await retake();break;
      case 'recover':await recoverUploads();await loadProject(S.project.id);break;
      case 'edit-script':editScript();break;case 'add-section':await addSection();break;
      case 'select-take':await updateTake(target.dataset.id,{selected:true});break;
      case 'edit-take':editTake(target.dataset.id);break;
      case 'save-trim':{const start=Number($('#trimStart').value);const end=$('#trimEnd').value.trim();await updateTake(target.dataset.id,{trim_start:start,trim_end:end===''?null:Number(end)});toast('Trim saved');break;}
      case 'save-transcript':await updateTake(target.dataset.id,{transcript:$('#spokenTranscript').value,transcript_reviewed:$('#verifiedTranscript').checked});toast('Transcript saved');break;
      case 'transcribe':await json('POST',`/takes/${target.dataset.id}/transcribe`,{});toast('Transcription requested. Refresh to see its result.');await loadProject(S.project.id);break;
      case 'smaller':case 'larger':{const size=Math.max(17,Math.min(48,Number(localStorage.getItem('fontSize')||26)+(action==='larger'?2:-2)));localStorage.setItem('fontSize',size);$('#scriptText').style.fontSize=size+'px';break;}
    }
  }catch(e){toast(e.message,true);}
}
document.addEventListener('click',e=>{const target=e.target.closest('[data-action]');if(target)act(target);});
$('#newProjectBtn').addEventListener('click',newProject);$('#newTopBtn').addEventListener('click',newProject);
$('#mobileProjects').addEventListener('click',()=>$('#sidebar').classList.toggle('mobile-open'));
window.addEventListener('online',()=>recoverUploads().catch(console.error));
window.addEventListener('beforeunload',e=>{if(S.recording){e.preventDefault();e.returnValue='Recording is still in progress';}});
init();