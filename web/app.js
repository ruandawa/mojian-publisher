'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pageNames = {console:'发布控制台',workspace:'文章与编辑',ai:'AI 写作',queue:'全部任务',logs:'运行记录',settings:'账号与设置'};
const statusNames = {queued:'待执行',running:'执行中',waiting_user:'等待处理',needs_review:'结果待核对',review_pending:'已提交，微信审核中',drafted:'微信草稿已保存',published:'微信已发表',cancelled:'已结束',failed:'失败'};
const stepNames = {account:'连接公众号',editor:'打开微信编辑器',body:'填写标题与正文',images:'上传图片与封面',saving:'保存微信草稿',saved:'草稿已保存',creation_source:'确认创作来源',publishing:'提交发表',verification:'管理员扫码验证',receipt:'等待发表回执',review:'微信审核',done:'完成'};
let csrf='', state=null, current=null, dirty=false, theme='jade', cover='', source='manual';
let previewHTML='', previewTimer=0, toastTimer=0, highlightedJob='', resolving='', resumingEditor='', currentFilter='all';
let detectedAccount='', previousLogin=null, clearKey=false, wechatFrameId='', wechatTimer=0, currentDetail='';
let preparationBusy=false, preparationReport=null, latestPreview=null, previewSequence=0;
let sourceConfirmationJob='', sourceConfirmationBusy=false;
let verificationPanelJob='', verificationChecking=false, verificationRefreshing=false, verificationFrameVisible=null;
const verificationPanelsShown=new Set();

function date(value){return value?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value)):''}
function toast(message,error){clearTimeout(toastTimer);$('toast').textContent=message||'';$('toast').classList.toggle('error',!!error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?9000:4200)}
async function api(path,method,data,raw){const headers={'X-Mojian-Token':csrf};if(data!==undefined&&!(data instanceof FormData))headers['Content-Type']='application/json';const response=await fetch('/api'+path,{method:method||'GET',headers:headers,body:data===undefined?undefined:data instanceof FormData?data:JSON.stringify(data)});if(!response.ok){let info={};try{info=await response.json()}catch{}let message=info.error||(Array.isArray(info.detail)?info.detail.map(x=>x.msg).join('；'):typeof info.detail==='string'?info.detail:'')||'操作未完成，请重试。';const error=Error(message);error.code=info.code||'';throw error}return raw?response:response.json()}
function bind(id,event,handler){const element=$(id);if(element)element.addEventListener(event,async eventObject=>{try{await handler(eventObject)}catch(error){toast(error.message,true)}})}
function nav(page){if(!pageNames[page])page='console';document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==page);document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===page));$('page-title').textContent=pageNames[page];if(location.hash!=='#'+page)history.replaceState(null,'','#'+page);if(page==='settings')populateSettings();if(page==='console')renderConsole()}
document.querySelectorAll('[data-nav]').forEach(el=>el.addEventListener('click',()=>nav(el.dataset.nav)));
window.addEventListener('hashchange',()=>nav(location.hash.slice(1)));

function renderConnection(info){
 info=info||{};const status=info.state||'unknown',actual=info.account_name||'';detectedAccount=actual;
 if($('login-indicator')){$('login-indicator').dataset.state=status;$('login-indicator-text').textContent=info.logged_in?'已连接 · '+actual:(info.label||'待连接');$('login-indicator').title=info.message||''}
 if($('browser-state')){const attention=!!info.needs_user||['waiting_scan','expired','unverified','mismatch'].includes(status);$('browser-state').textContent=attention?'需要本人处理':info.open?'后台执行中':'执行器待命';$('browser-state').classList.toggle('attention',attention);$('browser-state').classList.toggle('on',!!info.open&&!attention)}
 if($('login-card')){$('login-card').dataset.state=status;$('login-status-title').textContent=info.label||'待连接';$('login-status-message').textContent=info.message||'';$('login-account-row').hidden=!actual;$('login-account-name').textContent=actual;$('use-detected-account').hidden=!actual||info.account_matches===true;$('login-checked-at').textContent=info.checked_at?'最近检测 '+date(info.checked_at):'自动检测'}
 if($('connection-text')){$('connection-text').textContent=info.logged_in?actual:(info.label||'待连接');$('connection-help').textContent=info.message||'首次使用请在软件内扫码'}
 if(previousLogin&&previousLogin!=='logged_in'&&status==='logged_in')toast('公众号连接成功：'+actual);previousLogin=status
}
function renderDelivery(){const jobs=(state&&state.jobs)||[],active=jobs.filter(j=>['queued','running','waiting_user','needs_review','review_pending'].includes(j.status));const job=active.find(j=>j.id===currentDetail)||active.find(j=>j.article_id===current?.id)||active[0];const banner=$('delivery-status');if(!banner)return;banner.hidden=!job;if(!job)return;$('delivery-title').textContent=jobStatusName(job)+' · '+job.snapshot.title;$('delivery-message').textContent=job.message||'等待执行。';$('delivery-local').textContent=job.status==='published'?'已收到微信发表成功回执。':job.status==='drafted'?'已收到微信草稿保存回执，尚未发表。':publicationReviewPending(job)?'微信已接收，正在审核；尚未收到发表成功回执，可核对结果，不会重复发送。':verificationRequired(job)?'用公众号管理员微信扫码并在手机上确认；软件会核对实际发表结果，不会再次提交。':'文章内容保留在本机，任务不会盲目重复提交。';$('delivery-details').dataset.job=job.id;banner.dataset.state=['waiting_user','needs_review'].includes(job.status)?'attention':'working'}
function stepsFor(job){
 const order=['account','editor','body','images','saving','saved',...(job.step==='creation_source'||job.stage==='source_required'?['creation_source']:[]),...(job.action==='publish'?['publishing',...(job.step==='verification'?['verification']:[]),'receipt',...(job.step==='review'||job.stage==='platform_rejected'?['review']:[])]:[]),'done'];
 const key=job.stage==='platform_rejected'?'review':job.step||({draft_saved:'saved',editing:'body',saving:'saving',publishing:'publishing'}[job.stage]||'account');
 const finished=['drafted','published'].includes(job.status);
 const reached=finished?order.length:order.indexOf(key);
 return order.map((key,index)=>({key,label:stepNames[key]||key,state:
  job.status==='cancelled'||job.status==='queued'?'pending':
  index<reached?'done':index===reached?(['waiting_user','needs_review','failed'].includes(job.status)?'attention':'current'):'pending'}));
}
function renderStepList(target,job){if(!target)return;target.innerHTML=stepsFor(job).map(step=>'<li class="'+step.state+'"><i></i><span>'+esc(step.label)+'</span></li>').join('')}
function sourceConfirmationRequired(job){return !!job&&job.status==='waiting_user'&&job.stage==='source_required'&&job.step==='creation_source'}
function verificationRequired(job){return !!job&&job.action==='publish'&&job.status==='needs_review'&&job.stage==='publishing'&&job.step==='verification'}
function publicationReviewPending(job){return !!job&&job.action==='publish'&&job.status==='review_pending'&&job.stage==='publishing'}
function platformRejected(job){return !!job&&job.status==='failed'&&job.stage==='platform_rejected'}
function jobStatusName(job){return platformRejected(job)?'微信审核未通过':verificationRequired(job)?'等待扫码验证':statusNames[job.status]||job.status}
function publicArticleUrl(value){
 try{
  const url=new URL(value);
  if(url.protocol!=='https:'||url.hostname!=='mp.weixin.qq.com'||url.port||url.username||url.password)return '';
  const privateKeys=['tempkey','key','pass_ticket','token','authkey','sessionid','ticket','draft'];
  if([...url.searchParams.keys()].some(key=>privateKeys.includes(key.toLowerCase())))return '';
  if(/^\/s\/[A-Za-z0-9_-]+\/?$/.test(url.pathname))return url.origin+url.pathname;
  const keys=['__biz','mid','idx','sn'];
  if(url.pathname!=='/s'||!keys.every(key=>url.searchParams.get(key)))return '';
  const query=new URLSearchParams();keys.forEach(key=>query.set(key,url.searchParams.get(key)));
  return url.origin+'/s?'+query.toString();
 }catch{return '';}
}
function jobCard(job,compact){
 const pending=['waiting_user','needs_review'].includes(job.status),sourceRequired=sourceConfirmationRequired(job),verification=verificationRequired(job),review=publicationReviewPending(job),rejected=platformRejected(job),actions=[];
 const button=(label,action,kind='btn')=>'<button class="'+kind+'" data-job="'+esc(job.id)+'" data-action="'+action+'">'+label+'</button>';
 if(job.status==='queued')actions.push(button('取消','cancel','text-btn'));
 if(job.status==='waiting_user'&&!sourceRequired)actions.push(button('继续任务','resume'));
 if(job.status==='needs_review'&&['editing','resume_editor'].includes(job.stage))actions.push(button('继续填写','continue-editing'));
 if(pending&&!sourceRequired&&!verification)actions.push(button('人工核对','resolve','text-btn'));
 if(job.status==='needs_review'&&job.stage==='publishing')actions.push(button(verification?'核对发表结果':'核对回执','verify','text-btn'));
 if(review)actions.push(button('核对结果','verify'));
 const articleUrl=job.status==='published'?publicArticleUrl(job.article_url):'';
 if(articleUrl)actions.push('<a class="btn" href="'+esc(articleUrl)+'" target="_blank" rel="noopener noreferrer">查看文章</a>');
 if(job.evidence)actions.push('<button class="text-btn" data-evidence="'+esc(job.evidence.split('/').pop())+'">现场截图</button>');
 if(pending)actions.unshift(button(sourceRequired?'确认创作来源':verification?'扫码验证':'打开处理面板',sourceRequired?'creation-source-panel':verification?'scan-verification':'panel'));
 if(job.status==='queued')actions.unshift(button('立即执行','run'));
 const step=rejected?stepNames.review:stepNames[job.step]||stepNames[job.stage]||'准备中';
 return '<article class="job-card '+(highlightedJob===job.id?'job-highlight':'')+'" id="'+(compact?'recent-card-':'job-card-')+esc(job.id)+'" tabindex="-1"><div class="job-main"><div class="job-top"><b>'+esc(job.snapshot.title)+'</b><span class="badge '+(pending?'warning-badge':review?'review-badge':rejected?'rejected-badge':'')+'">'+esc(job.stage==='user_confirmed'?'人工核对 · '+jobStatusName(job):jobStatusName(job))+'</span></div><div class="job-meta">'+(job.action==='publish'?'发表到公众号':'保存到微信草稿')+' · '+date(job.scheduled_at)+' · '+esc(step)+'</div><p class="job-message">'+esc(job.message||'等待排期执行。')+'</p>'+(!compact?'<ol class="task-steps mini">'+stepsFor(job).map(s=>'<li class="'+s.state+'"><i></i><span>'+esc(s.label)+'</span></li>').join('')+'</ol>':'')+'</div><div class="job-actions">'+actions.join('')+'</div></article>';
}
function activeJobs(){return (state?.jobs||[]).filter(j=>['queued','running','waiting_user','needs_review','review_pending'].includes(j.status))}
function renderConsole(){if(!state)return;const jobs=state.jobs||[],active=activeJobs(),attention=jobs.filter(j=>['waiting_user','needs_review'].includes(j.status));$('recent-jobs').innerHTML=jobs.slice(0,4).map(j=>jobCard(j,true)).join('')||'<div class="empty">还没有任务。先写好文章，再选择保存草稿或发表。</div>';const job=active.find(j=>j.status==='running')||active.find(j=>['waiting_user','needs_review'].includes(j.status))||active[0];$('current-task-title').textContent=job?job.snapshot.title:'还没有发布任务';$('current-task-message').textContent=job?(job.message||'正在后台处理。'):'在文章编辑页选择“立即发表”，或安排定时任务。';$('current-task-state').textContent=job?jobStatusName(job):'待命';$('current-task-state').classList.toggle('warning-badge',!!job&&['waiting_user','needs_review'].includes(job.status));renderStepList($('current-task-steps'),job||{step:'account',status:'queued',action:'publish'});$('current-task-time').textContent=job?'最近更新 '+date(job.updated_at):'';$('current-task-details').hidden=!job;$('current-task-details').dataset.job=job?.id||'';$('attention-count').textContent=attention.length;$('attention-message').textContent=attention.length?'有 '+attention.length+' 个任务需要你处理，队列已暂停。':'暂无需要你处理的任务。';$('attention-open').hidden=!attention.length;$('auto-state').textContent=state.settings.automation_enabled?'已开启':'已暂停';$('auto-help').textContent=state.settings.automation_enabled?'到期任务会自动执行。':'立即发送不受此开关影响。';$('auto-toggle').textContent=state.settings.automation_enabled?'暂停定时队列':'开启定时队列'}
function showJob(jobId){const job=state?.jobs.find(j=>j.id===jobId);if(!job){toast('任务不存在或已刷新。',true);return}currentDetail=jobId;highlightedJob=jobId;currentFilter='all';document.querySelectorAll('[data-filter]').forEach(el=>el.classList.toggle('active',el.dataset.filter==='all'));nav('queue');renderJobs();requestAnimationFrame(()=>document.getElementById('job-card-'+jobId)?.scrollIntoView({behavior:'smooth',block:'center'}));if(sourceConfirmationRequired(job))openCreationSourcePanel(job)}
function renderCreationSourcePanel(){
 if(!$('task-dialog').open||!sourceConfirmationJob)return;
 const job=state?.jobs.find(j=>j.id===sourceConfirmationJob),required=sourceConfirmationRequired(job);
 $('task-creation-source').hidden=!required;
 if(job){$('task-detail-badge').textContent=jobStatusName(job);$('task-detail-message').textContent=job.message||'微信要求确认这篇文章的创作来源。';renderStepList($('task-detail-steps'),job);}
 ['task-source-ai','task-source-non-ai'].forEach(id=>$(id).disabled=sourceConfirmationBusy||!required);
 $('task-source-progress').hidden=!sourceConfirmationBusy;
}
function openCreationSourcePanel(job){
 if(!sourceConfirmationRequired(job))return;
 sourceConfirmationJob=job.id;$('task-detail-title').textContent=job.snapshot.title;$('task-detail-meta').textContent='按本次任务中的文章版本继续发表';$('task-detail-actions').innerHTML='';
 $('task-detail-events').innerHTML=(state.events||[]).filter(event=>event.job_id===job.id).map(event=>'<p>'+esc(date(event.created_at))+' · '+esc(event.message)+'</p>').join('')||'<p>暂无更多记录。</p>';
 if(!$('task-dialog').open)$('task-dialog').showModal();renderCreationSourcePanel();
}
async function confirmCreationSource(value){
 if(sourceConfirmationBusy||!['ai','non_ai'].includes(value))return;
 const job=state?.jobs.find(j=>j.id===sourceConfirmationJob);if(!sourceConfirmationRequired(job))throw Error('任务状态已变化，请刷新后处理。');
 const jobId=job.id;sourceConfirmationBusy=true;renderCreationSourcePanel();
 try{await api('/jobs/'+jobId+'/creation-source','POST',{creation_source:value});$('task-dialog').close();sourceConfirmationJob='';await refresh();toast('创作来源已确认，正在继续原任务。');}
 finally{sourceConfirmationBusy=false;renderCreationSourcePanel();}
}
function renderJobs(){const filter=currentFilter;let jobs=state?.jobs||[];if(filter==='active')jobs=jobs.filter(j=>['queued','running','review_pending'].includes(j.status));if(filter==='attention')jobs=jobs.filter(j=>['waiting_user','needs_review'].includes(j.status)||platformRejected(j));if(filter==='done')jobs=jobs.filter(j=>['drafted','published','cancelled'].includes(j.status));$('jobs-list').innerHTML=jobs.map(j=>jobCard(j,false)).join('')||'<div class="empty empty-queue">没有符合条件的任务。</div>'}

function bodyWordCount(){const value=$('article-body').value;const text=$('article-format').value==='html'?new DOMParser().parseFromString(value,'text/html').body.textContent:value;return text.replace(/\s/g,'').length+' 字'}
function markDirty(){dirty=true;$('dirty-mark').textContent='未保存';$('dirty-mark').classList.add('dirty');clearTimeout(previewTimer);previewTimer=setTimeout(updatePreview,250);$('word-count').textContent=bodyWordCount();if(!preparationBusy&&preparationReport&&!samePreparedBody()){$('preparation-heading').textContent='正文已修改';$('preparation-badge').textContent='待重新检查';$('article-ready').dataset.state='pending';$('preparation-summary').textContent='预览正在更新；发送前会自动重新整理正文与图片。';$('preparation-details').hidden=true}}
function form(){return {title:$('article-title').value.trim(),author:$('article-author').value.trim(),digest:$('article-digest').value.trim(),body:$('article-body').value,format:$('article-format').value,creation_source:$('article-creation-source').value,theme:theme,cover:cover,source:source,revision:current?.revision??null}}
function setTheme(value,changed){theme=value||'jade';document.querySelectorAll('[data-theme]').forEach(el=>el.classList.toggle('active',el.dataset.theme===theme));if(changed)markDirty()}
function setCover(value){cover=value||'';$('cover-preview').hidden=!cover;if(cover)$('cover-preview').src=cover;$('cover-hint').textContent=cover?'封面已就绪':'未设置时自动生成'}
function creationSource(value){return ['ai','non_ai'].includes(value)?value:'unspecified'}
function fillEditor(article){current=article||null;preparationReport=null;latestPreview=null;source=article?.source||'manual';$('article-title').value=article?.title||'';$('article-author').value=article?.author||state?.settings.author||'';$('article-digest').value=article?.digest||'';$('article-body').value=article?.body||'';$('article-format').value=article?.format||'markdown';$('article-creation-source').value=creationSource(article?.creation_source);setTheme(article?.theme||state?.settings.theme||'jade',false);setCover(article?.cover||'');$('editor-state').textContent=article?'编辑文章':'开始一篇新文章';$('dirty-mark').textContent=article?'已保存':'未保存';$('dirty-mark').classList.remove('dirty');$('article-ready').hidden=!preparationBusy;dirty=false;$('word-count').textContent=bodyWordCount();renderLibrary();updatePreview()}
function samePreparedBody(){return preparationReport&&preparationReport.body===$('article-body').value&&preparationReport.format===$('article-format').value}
function imageIssues(value){if(Array.isArray(value))return {count:value.length,messages:value.map(issue=>typeof issue==='string'?issue:issue.message||issue.error||'图片未能准备，请检查来源。')};const count=Math.max(0,Number(value)||0);return {count,messages:count?['有 '+count+' 张图片尚未保存到本机，点击“整理正文与图片”可下载网络图片；本地图片请随文章放入 ZIP 图文包。']:[]}}
function renderPreparation(result){
 if(preparationBusy)return;
 const box=$('article-ready');box.hidden=!$('article-body').value.trim();if(box.hidden)return;
 const report=samePreparedBody()?preparationReport:null;
 const currentIssues=imageIssues(result?.image_warnings),savedIssues=imageIssues(report?.image_warnings);
 const issues=savedIssues.count?savedIssues:currentIssues;
 const total=Number(report?.image_count??result?.image_count??0),local=Number(report?.local_image_count??result?.local_image_count??Math.max(0,total-issues.count));
 const ready=result?.ready!==false&&issues.count===0;
 box.dataset.state=ready?'ready':'attention';box.setAttribute('aria-busy','false');$('preparation-progress').hidden=true;
 $('preparation-heading').textContent=ready?'正文排版与图片已就绪':'有图片尚未准备好';$('preparation-badge').textContent=ready?'本地就绪':'需处理图片';
 $('preparation-summary').textContent=(report?.filename?'已导入“'+report.filename+'”。':'')+(total?'正文含 '+total+' 处图片，'+local+' 个图片文件已保存到本机。':'正文已按当前样式排版。')+(ready?'':' 正文已保留，请处理以下图片问题。');
 const messages=issues.messages.length?issues.messages:(ready?[]:['图片文件不可用，请重新整理正文与图片。']);
 $('preparation-details').hidden=!messages.length;$('preparation-details').open=!!messages.length;$('preparation-details-title').textContent='图片问题（'+Math.max(messages.length,issues.count)+'）';$('preparation-errors').innerHTML=messages.map(message=>'<li>'+esc(message)+'</li>').join('');
}
function showPreparationError(message){$('article-ready').hidden=false;$('article-ready').dataset.state='attention';$('article-ready').setAttribute('aria-busy','false');$('preparation-heading').textContent='本次整理未完成';$('preparation-badge').textContent='请检查';$('preparation-summary').textContent=message;$('preparation-progress').hidden=true;$('preparation-details').hidden=true}
function preparationControls(busy,label){
 preparationBusy=busy;$('article-ready').setAttribute('aria-busy',String(busy));
 const ids=['import-btn','new-btn','prepare-btn','article-title','article-author','article-digest','article-format','article-creation-source','article-body','insert-image','example-btn','cover-generate','cover-upload','save-btn','send-draft-btn','schedule-btn','publish-now-btn','copy-btn','export-btn','full-preview-btn'];
 ids.forEach(id=>$(id).disabled=busy);document.querySelectorAll('[data-theme]').forEach(button=>button.disabled=busy);
 $('import-btn').textContent=busy?'正在准备文章…':'导入文章 / 图文包';$('prepare-btn').textContent=busy?'正在整理…':'整理正文与图片';
 if(busy){clearTimeout(previewTimer);++previewSequence;$('article-ready').hidden=false;$('article-ready').dataset.state='working';$('preparation-heading').textContent=label||'正在整理正文与图片';$('preparation-badge').textContent='处理中';$('preparation-summary').textContent='正在整理文章结构、下载并保存图片，请稍候。';$('preparation-progress').hidden=false;$('preparation-details').hidden=true;}
}
function previewDocument(content,withTitle){const heading=withTitle?'<h1 style="margin:0 0 12px;font-size:23px;line-height:1.5;font-weight:600;overflow-wrap:anywhere">'+esc($('article-title').value||'你的下一篇文章')+'</h1><p style="margin:0 0 26px;color:#7e9183;font-size:12px">'+esc($('article-author').value||'作者')+' · 手机阅读预览</p>':'';return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html{background:white}body{margin:0;color:#333;font-family:"Microsoft YaHei","PingFang SC",sans-serif;overflow-wrap:anywhere}img{max-width:100%}a{pointer-events:none}</style></head><body'+(withTitle?' style="padding:24px 20px 42px"':'')+'>'+heading+content+'</body></html>'}
async function save(){if(current&&!dirty)return current;const data=form();if(!data.title||!data.body.trim())throw Error('请先填写文章标题和正文。');current=await api(current?'/articles/'+current.id:'/articles',current?'PUT':'POST',data);dirty=false;$('dirty-mark').textContent='已保存';$('dirty-mark').classList.remove('dirty');await refresh();return current}
async function updatePreview(){if(preparationBusy)return;const sequence=++previewSequence;try{const result=await api('/preview','POST',{body:$('article-body').value,format:$('article-format').value,theme:theme});if(sequence!==previewSequence||preparationBusy)return;latestPreview=result;previewHTML=result.html;$('preview-title').textContent=$('article-title').value||'你的下一篇文章';$('preview-author').textContent=$('article-author').value||'作者';$('preview-frame').srcdoc=previewDocument($('article-body').value?result.html:'<p style="color:#aab5aa;line-height:2">文章正文会在这里预览。</p>',false);const issues=imageIssues(result.image_warnings);$('image-warning').hidden=!issues.count;$('image-warning').textContent='有 '+issues.count+' 张图片待准备，详情见正文下方。';renderPreparation(result);if($('article-preview-dialog').open)$('full-preview-frame').srcdoc=previewDocument(previewHTML,true)}catch(error){if(sequence===previewSequence&&!preparationBusy){$('image-warning').hidden=false;$('image-warning').textContent='预览未更新：'+error.message;}}}
function renderLibrary(){if(!state)return;const filter=$('search').value.trim().toLowerCase(),articles=state.articles.filter(a=>a.title.toLowerCase().includes(filter));$('library-count').textContent=state.articles.length;$('article-list').innerHTML=articles.length?articles.map(a=>'<button class="article-item '+(current?.id===a.id?'active':'')+'" data-article="'+esc(a.id)+'"><b>'+esc(a.title)+'</b><small>'+({ai:'AI 创作',import:'导入文章'}[a.source]||'手动创作')+' · '+date(a.updated_at)+'</small></button>').join(''):'<div class="empty">还没有文章</div>'}
function populateSettings(){if(!state)return;const s=state.settings;$('setting-account').value=s.account_name||'';$('setting-author').value=s.author||'';$('setting-theme').value=s.theme||'jade';$('setting-base').value=s.ai_base_url||'';$('setting-model').value=s.ai_model||'';$('setting-style').value=s.ai_instructions||'';$('setting-action').value=s.default_action||'draft';$('setting-key').value='';clearKey=false;$('key-status').textContent=s.ai_key_saved?'已加密保存密钥，留空保持不变':'尚未保存密钥';$('data-dir').textContent=state.data_dir}
async function refresh(){state=await api('/state');document.querySelectorAll("[data-app-version]").forEach(el=>el.textContent="v"+state.version);$('article-total').innerHTML=state.articles.length+'<small>篇</small>';$('pending-total').innerHTML=activeJobs().length+'<small>篇</small>';$('published-total').innerHTML=state.jobs.filter(j=>j.status==='published'&&j.stage!=='user_confirmed').length+'<small>篇</small>';$('queue-count').textContent=activeJobs().length;renderConnection(state.browser);renderDelivery();renderConsole();renderLibrary();renderJobs();renderCreationSourcePanel();renderVerificationPanel();maybeOpenVerificationPanel().catch(error=>toast(error.message,true));$('logs-list').innerHTML=state.events.length?state.events.map(e=>'<div class="log-row"><time>'+date(e.created_at)+'</time><span class="log-level '+esc(e.level)+'">'+({info:'记录',warning:'待处理',error:'异常'}[e.level]||esc(e.level))+'</span><span>'+esc(e.message)+'</span></div>').join(''):'<div class="empty">暂无运行记录。</div>'}
let panelBusy=false, panelClosing=false;
function renderVerificationPanel(){
 const job=state?.jobs.find(j=>j.id===verificationPanelJob),verification=verificationRequired(job),review=publicationReviewPending(job),busy=verificationChecking||verificationRefreshing;
 $('wechat-title').textContent=review?'微信审核中':platformRejected(job)?'微信审核未通过':verificationPanelJob?'管理员扫码验证':'公众号连接与验证';
 $('wechat-frame').alt=verificationPanelJob?'微信发表验证页面，请用公众号管理员微信扫码并在手机上确认':'微信连接页面；可扫码或点击画面完成账号选择';
 $('wechat-verify').hidden=!verification&&!review;
 $('wechat-verify').disabled=busy;
 $('wechat-verify').textContent=review?'核对结果':'已扫码，核对发表结果';
 $('wechat-refresh-verification').hidden=!verification;
 $('wechat-refresh-verification').disabled=busy;
 $('wechat-refresh-verification').textContent=verificationRefreshing?'正在刷新验证码…':'二维码过期，刷新验证码';
 $('wechat-close').disabled=verificationRefreshing;
 $('wechat-done').disabled=verificationRefreshing;
 $('wechat-footer-note').textContent=verification?'用管理员微信扫码，在手机确认后核对发表结果；不会再次提交文章。':review?'已提交微信审核，尚未发表成功。可只读核对结果，不会重复发送。':platformRejected(job)?'微信审核未通过。任务保留现场证据，不会自动重发。':job?.status==='published'?'已收到微信发表成功回执，可以返回发布控制台。':'关闭面板后，登录等待任务会自动继续；其他中断请在任务卡片处理。';
 if((verification||review||platformRejected(job))&&$('wechat-dialog').open)$('wechat-message').textContent=verificationRefreshing?'正在核对发表结果，并刷新过期验证码…':verification&&verificationFrameVisible===false?'二维码已过期或验证窗口已关闭，点击刷新验证码恢复；若已在手机确认，请先核对结果。':job.message||(review?'微信已接收文章，正在审核，尚未发表成功。':'请用公众号管理员微信扫描页面内二维码，并在手机上确认发表。');
 if(job?.status==='published'&&$('wechat-dialog').open)$('wechat-message').textContent='已收到微信发表成功回执：'+job.snapshot.title;
}
async function maybeOpenVerificationPanel(){
 const job=(state?.jobs||[]).find(j=>verificationRequired(j)&&!verificationPanelsShown.has(j.id));
 if(!job||dirty||preparationBusy||panelBusy||panelClosing||document.querySelector('dialog[open]'))return;
 verificationPanelsShown.add(job.id);
 await openWechatPanel(job.id);
}
async function verifyPublication(jobId){
 if(verificationChecking||verificationRefreshing)return;
 const job=state?.jobs.find(j=>j.id===jobId);
 if(!job||!['needs_review','review_pending'].includes(job.status)||job.stage!=='publishing')throw Error('任务状态已变化，请刷新后查看。');
 verificationChecking=true;renderVerificationPanel();
 try{
  const result=await api('/jobs/'+jobId+'/verify','POST');
  await refresh();
  toast(result.message||'已核对当前发表结果。');
 }finally{verificationChecking=false;renderVerificationPanel();}
}
async function refreshVerification(){
 if(verificationRefreshing||verificationChecking||panelClosing)return;
 const job=state?.jobs.find(j=>j.id===verificationPanelJob);
 if(!verificationRequired(job))throw Error('当前任务不处于管理员扫码验证状态，请先核对发表结果。');
 verificationRefreshing=true;clearTimeout(wechatTimer);renderVerificationPanel();
 try{
  while(panelBusy)await new Promise(resolve=>setTimeout(resolve,100));
  panelBusy=true;wechatFrameId='';
  let result;
  try{result=await api('/jobs/'+job.id+'/refresh-verification','POST');}
  catch(error){
   if(error.code!=='verification_recovery_required')throw error;
   const message='微信验证会话已中断。\n\n只有确认上次没有在手机完成发表，才可恢复原文章的二维码。\n\n如果已在手机确认发表，或不确定是否完成，请选择“取消”，再点“核对发表结果”，避免重复发表。\n\n确定上次没有在手机完成发表，并恢复二维码？';
   if(!window.confirm(message)){await refresh();toast('未恢复二维码。请点“核对发表结果”查看上次提交状态。');return;}
   result=await api('/jobs/'+job.id+'/refresh-verification','POST',{previous_not_confirmed:true});
  }
  await refresh();
  toast(result.message||'验证码已刷新，请用公众号管理员微信扫码。');
 }finally{
  panelBusy=false;
  await refreshWechatFrame();
  verificationRefreshing=false;renderVerificationPanel();
 }
}
async function openWechatPanel(jobId=''){
 if(panelBusy)return;
 panelBusy=true;
 verificationPanelJob=typeof jobId==='string'?jobId:'';
 verificationFrameVisible=null;
 if(verificationPanelJob)verificationPanelsShown.add(verificationPanelJob);
 const dialog=$('wechat-dialog');
 if(!dialog.open)dialog.showModal();
 $('wechat-loading').hidden=false;
 $('wechat-loading').textContent='正在连接，请稍候…';
 $('wechat-message').textContent='正在启动后台连接…';
 renderVerificationPanel();
 try{
  const result=await api('/browser/show','POST');
  if(!dialog.open){await api('/browser/hide','POST');return;}
  $('wechat-message').textContent=result.message||'已建立软件内连接。';
 }catch(error){
  $('wechat-loading').textContent=error.message;
  throw error;
 }finally{panelBusy=false;}
 await refreshWechatFrame();
 await refresh();
}
async function refreshWechatFrame(){
 if(!$('wechat-dialog').open||panelBusy||panelClosing)return;
 panelBusy=true;
 clearTimeout(wechatTimer);
 const image=$('wechat-frame');
 try{
  const result=await api('/browser/frame');
  if(!$('wechat-dialog').open)return;
  wechatFrameId=result.frame_id;
  verificationFrameVisible=typeof result.verification_visible==='boolean'?result.verification_visible:null;
  image.src=result.image;
  image.hidden=false;
  $('wechat-loading').hidden=true;
  $('wechat-message').textContent=state?.browser?.message||'请扫码或点击画面完成账号选择。';
  renderVerificationPanel();
 }catch(error){
  wechatFrameId='';image.hidden=true;
  $('wechat-loading').textContent=error.message;
  $('wechat-loading').hidden=false;
 }finally{
  panelBusy=false;
  if($('wechat-dialog').open&&!panelClosing)wechatTimer=setTimeout(refreshWechatFrame,4500);
 }
}
async function panelAction(payload){
 if(panelBusy||panelClosing)return;
 if(!wechatFrameId)throw Error('请先刷新画面。');
 panelBusy=true;clearTimeout(wechatTimer);
 const frameId=wechatFrameId;wechatFrameId='';
 try{
  await api('/browser/interact','POST',Object.assign({frame_id:frameId},payload));
  await new Promise(resolve=>setTimeout(resolve,450));
 }finally{
  panelBusy=false;
  await refreshWechatFrame();
  await refresh();
 }
}
async function closeWechatPanel(){
 if(panelClosing||verificationRefreshing)return;
 panelClosing=true;clearTimeout(wechatTimer);
 try{
  while(panelBusy)await new Promise(resolve=>setTimeout(resolve,100));
  const result=await api('/browser/hide','POST');
  wechatFrameId='';$('wechat-frame').hidden=true;
  $('wechat-dialog').close();
  verificationPanelJob='';
  verificationFrameVisible=null;
  await refresh();
  if(result.resumed)toast('登录已确认，正在自动继续任务。');
 }finally{panelClosing=false;}
}
async function queueArticle(action){const article=await save();const existing=state.jobs.find(j=>j.article_id===article.id&&['queued','running','waiting_user','needs_review','review_pending'].includes(j.status));if(existing){showJob(existing.id);return}const job=await api('/jobs','POST',{article_id:article.id,action:action,run_now:true});await refresh();showJob(job.id);toast(action==='publish'?'已开始自动发表，进度会在控制台显示。':'已开始保存微信草稿。')}
bind('login-top','click',openWechatPanel);bind('login-settings','click',openWechatPanel);bind('login-indicator','click',()=>nav('settings'));bind('login-refresh','click',async()=>{const result=await api('/browser/status?refresh=true');state.browser=result;renderConnection(result);toast(result.message,result.state!=='logged_in')});bind('use-detected-account','click',()=>{if(detectedAccount){$('setting-account').value=detectedAccount;toast('公众号名称已填入，请保存设置。')}});
bind('wechat-dialog','cancel',event=>{event.preventDefault();return closeWechatPanel()});
bind('wechat-close','click',closeWechatPanel);
bind('wechat-refresh','click',refreshWechatFrame);bind('wechat-done','click',closeWechatPanel);bind('wechat-frame','click',async eventObject=>{const rect=$('wechat-frame').getBoundingClientRect();await panelAction({kind:'click',x:(eventObject.clientX-rect.left)/rect.width,y:(eventObject.clientY-rect.top)/rect.height})});bind('wechat-up','click',()=>panelAction({kind:'scroll',delta:-570}));bind('wechat-down','click',()=>panelAction({kind:'scroll',delta:570}));bind('wechat-send-text','click',()=>{const value=$('wechat-text').value;if(!value)return;return panelAction({kind:'text',text:value}).then(()=>{$('wechat-text').value=''})});bind('wechat-enter','click',()=>panelAction({kind:'key',key:'Enter'}));bind('wechat-tab','click',()=>panelAction({kind:'key',key:'Tab'}));
bind('wechat-verify','click',()=>verifyPublication(verificationPanelJob));
bind('wechat-refresh-verification','click',refreshVerification);
bind('delivery-details','click',()=>showJob($('delivery-details').dataset.job));bind('current-task-details','click',()=>showJob($('current-task-details').dataset.job));bind('attention-open','click',()=>showJob(activeJobs().find(j=>['waiting_user','needs_review'].includes(j.status))?.id));
bind('auto-toggle','click',async()=>{await api('/automation/'+(state.settings.automation_enabled?'pause':'start'),'POST');await refresh()});bind('queue-refresh','click',refresh);
bind('send-draft-btn','click',()=>queueArticle('draft'));bind('publish-now-btn','click',()=>queueArticle('publish'));bind('save-btn','click',async()=>{await save();toast('文章已保存到本机。')});bind('schedule-btn','click',async()=>{const article=await save();$('schedule-title').textContent=article.title;$('schedule-action').value=state.settings.default_action;$('schedule-time').value='';$('schedule-dialog').showModal()});
bind('schedule-confirm','click',async()=>{const article=await save();await api('/jobs','POST',{article_id:article.id,action:$('schedule-action').value,scheduled_at:$('schedule-time').value?$('schedule-time').value+':00+08:00':'',run_now:!$('schedule-time').value});$('schedule-dialog').close();await refresh();nav('queue');toast('已加入发布队列。')});
bind('article-title','input',markDirty);bind('article-author','input',markDirty);bind('article-digest','input',markDirty);bind('article-body','input',markDirty);bind('article-format','change',markDirty);bind('article-creation-source','change',markDirty);bind('search','input',renderLibrary);
bind('article-list','click',eventObject=>{if(preparationBusy){toast('文章正在准备，请稍候。');return}const button=eventObject.target.closest('[data-article]');if(button)fillEditor(state.articles.find(a=>a.id===button.dataset.article))});bind('new-btn','click',()=>fillEditor(null));document.querySelectorAll('[data-theme]').forEach(button=>button.addEventListener('click',()=>setTheme(button.dataset.theme,true)));
bind('example-btn','click',()=>{fillEditor(null);$('article-title').value='给忙碌生活，留一点记录的空间';$('article-digest').value='从一个小小的习惯开始，让每天的想法有处安放。';$('article-body').value='## 从三行文字开始\n\n记录不是为了完美，而是为了看得更清楚。\n\n**把今天最想保留的一件事写下来。**';markDirty()});
bind('import-btn','click',()=>$('import-file').click());
bind('import-file','change',async eventObject=>{
 const file=eventObject.target.files[0];if(!file||preparationBusy)return;
 preparationControls(true,'正在导入“'+file.name+'”');let imported=false;
 try{
  const data=new FormData();data.append('file',file);const parsed=await api('/import','POST',data);
  fillEditor(null);$('article-title').value=(parsed.title||file.name.replace(/\.[^.]+$/,'')).slice(0,64);$('article-author').value=parsed.author||state?.settings.author||'';$('article-digest').value=parsed.digest||'';$('article-body').value=parsed.body||'';$('article-format').value=parsed.format==='html'?'html':'markdown';$('article-creation-source').value=creationSource(parsed.creation_source);source=parsed.source||'import';setTheme(parsed.theme||state?.settings.theme||'jade',false);setCover(parsed.cover||'');markDirty();
  preparationReport=Object.assign({},parsed,{body:$('article-body').value,format:$('article-format').value,filename:file.name});imported=true;nav('workspace');
  const issues=imageIssues(parsed.image_warnings);toast(issues.count?'正文已导入，'+issues.count+' 张图片需要处理。详情保留在正文下方。':'已导入、整理排版并保存图片。',!!issues.count);
 }catch(error){showPreparationError('导入未完成：'+error.message);throw error;}
 finally{eventObject.target.value='';preparationControls(false);if(imported)await updatePreview();}
});
bind('prepare-btn','click',async()=>{
 if(preparationBusy)return;if(!$('article-body').value.trim())throw Error('请先导入文章或填写正文。');
 preparationControls(true,'正在整理正文与图片');let prepared=false;
 try{
  const result=await api('/prepare','POST',{body:$('article-body').value,format:$('article-format').value,theme,cover});
  $('article-body').value=result.body;$('article-format').value=result.format||'html';if(Object.prototype.hasOwnProperty.call(result,'cover'))setCover(result.cover);markDirty();preparationReport=Object.assign({},result,{body:$('article-body').value,format:$('article-format').value});prepared=true;
  const issues=imageIssues(result.image_warnings);toast(issues.count?'正文已整理，'+issues.count+' 张图片需要处理。':'正文与图片已在本机准备好。',!!issues.count);
 }catch(error){showPreparationError(error.message);throw error;}
 finally{preparationControls(false);if(prepared)await updatePreview();}
});
bind('full-preview-btn','click',async()=>{await updatePreview();$('full-preview-frame').srcdoc=previewDocument(previewHTML||'<p>文章正文会在这里预览。</p>',true);$('article-preview-dialog').showModal()});
bind('cover-generate','click',async()=>{const result=await api('/cover','POST',form());setCover(result.url);markDirty()});bind('cover-upload','click',()=>{window.imagePurpose='cover';$('image-file').click()});bind('insert-image','click',()=>{window.imagePurpose='body';$('image-file').click()});bind('image-file','change',async eventObject=>{const file=eventObject.target.files[0];if(!file)return;try{const data=new FormData();data.append('file',file);const result=await api('/upload','POST',data);if(window.imagePurpose==='cover')setCover(result.url);else{const insertion=$('article-format').value==='html'?'\n<p><img src="'+esc(result.url)+'" alt="图片"></p>\n':'\n\n![图片]('+result.url+')\n\n';$('article-body').setRangeText(insertion,$('article-body').selectionStart,$('article-body').selectionEnd,'end');}markDirty();}finally{eventObject.target.value=''}});
bind('copy-btn','click',async()=>{await updatePreview();const box=document.createElement('div');box.innerHTML=previewHTML;await navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([previewHTML],{type:'text/html'}),'text/plain':new Blob([box.textContent],{type:'text/plain'})})]);toast('排版已复制。')});bind('export-btn','click',async()=>{const article=await save();const response=await api('/articles/'+article.id+'/export','GET',undefined,true);const link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());link.download=article.title+'.html';link.click()});
async function handleJobAction(eventObject){
 const evidence=eventObject.target.closest('[data-evidence]');
 if(evidence){
  const response=await api('/evidence/'+evidence.dataset.evidence,'GET',undefined,true);
  $('evidence-image').src=URL.createObjectURL(await response.blob());
  $('evidence-dialog').showModal();
  return;
 }
 const button=eventObject.target.closest('[data-job]');
 if(!button)return;
 const action=button.dataset.action;
 if(action==='creation-source-panel'){showJob(button.dataset.job);return;}
 if(action==='scan-verification'){await openWechatPanel(button.dataset.job);return;}
 if(action==='panel'){await openWechatPanel();return;}
 if(action==='verify'){await verifyPublication(button.dataset.job);return;}
 if(action==='resolve'){
  resolving=button.dataset.job;
  $('resolve-check').checked=false;
  $('resolve-url').value='';
  $('resolve-dialog').showModal();
  return;
 }
 if(action==='continue-editing'){
  resumingEditor=button.dataset.job;
  $('resume-editor-dialog').showModal();
  return;
 }
 button.disabled=true;
 try{
  const result=await api('/jobs/'+button.dataset.job+'/'+action,'POST');
  await refresh();
  toast(result.message||'任务状态已更新。');
 }finally{
  button.disabled=false;
 }
}
bind('jobs-list','click',handleJobAction);
bind('recent-jobs','click',handleJobAction);
bind('task-source-ai','click',()=>confirmCreationSource('ai'));bind('task-source-non-ai','click',()=>confirmCreationSource('non_ai'));
document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{currentFilter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===button));renderJobs()}));
bind('resolve-confirm','click',async()=>{await api('/jobs/'+resolving+'/resolve','PUT',{status:$('resolve-status').value,confirmed:$('resolve-check').checked,article_url:$('resolve-url').value.trim()});$('resolve-dialog').close();await refresh()});bind('resume-editor-confirm','click',async()=>{await api('/jobs/'+resumingEditor+'/continue-editing','POST');$('resume-editor-dialog').close();await refresh()});
bind('settings-save','click',async()=>{const values={account_name:$('setting-account').value.trim(),author:$('setting-author').value.trim(),theme:$('setting-theme').value,ai_base_url:$('setting-base').value.trim(),ai_model:$('setting-model').value.trim(),ai_instructions:$('setting-style').value,ai_api_key:clearKey?null:$('setting-key').value.trim(),automation_enabled:state.settings.automation_enabled,default_action:$('setting-action').value,browser_mode:'background'};await api('/settings','PUT',values);await refresh();populateSettings();toast('设置已保存。')});bind('clear-key','click',()=>{clearKey=true;$('setting-key').value='';$('key-status').textContent='保存后移除密钥'});
bind('ai-next','change',()=>{
 $('ai-time-label').hidden=$('ai-next').value==='edit';
});
bind('generate-btn','click',async()=>{
 const topic=$('ai-topic').value.trim();
 if(!topic)throw Error('请填写文章主题。');
 $('generate-btn').disabled=true;$('ai-progress').textContent='正在生成文章，请稍候…';
 try{
  const article=await api('/generate','POST',{topic:topic,notes:$('ai-notes').value,length:Number($('ai-length').value)});
  fillEditor(article);
  if($('ai-next').value!=='edit'){
   await refresh();await api('/jobs','POST',{article_id:article.id,action:$('ai-next').value,scheduled_at:$('ai-time').value?$('ai-time').value+':00+08:00':'',run_now:!$('ai-time').value});
   nav('queue');
  }else{
   nav('workspace');
  }
  await refresh();
  $('ai-progress').textContent='文章已生成并保存。';toast('文章已生成。');
 }catch(error){$('ai-progress').textContent=error.message;throw error;}finally{
  $('generate-btn').disabled=false;
 }
});
bind('exit-app','click',()=>$('exit-dialog').showModal());bind('exit-confirm','click',async()=>{await api('/system/exit','POST',{confirmed:true});$('exit-dialog').close()});
(async()=>{try{csrf=(await fetch('/api/bootstrap').then(r=>r.json())).token;await refresh();fillEditor(state.articles[0]||null);nav(location.hash.slice(1)||'console');setInterval(()=>refresh().catch(()=>{}),state?.jobs.some(j=>j.status==='running')?2000:4000)}catch(error){toast('无法连接本机服务：'+error.message,true)}})();
