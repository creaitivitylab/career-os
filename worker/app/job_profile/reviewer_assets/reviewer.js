/* Plain browser UI plus testable import/export primitives. No network calls. */
(function (root) {
'use strict';
const clone = value => JSON.parse(JSON.stringify(value));
const STATES = ['unlabeled','known','unknown','not_mentioned','ambiguous','conflict'];
const LEVELS = ['A1','A2','B1','B2','C1','C2'];
const COMPARATORS = ['exact','at_least','at_most','range','unknown'];
const STRING_FIELDS = new Set(['technologies','required_technologies','required_skills','preferred_skills','city','country']);
const BOOLEAN_FIELDS = new Set(['is_normal_vacancy','people_management','base_compensation_present']);
function fresh(data) {
  return {tool:'career-os-reviewer-workspace-v1',cohort_hash:data.cohort_hash,reviewer_id:'',current_job_id:data.jobs[0].input.job_id,
    reviews:Object.fromEntries(data.jobs.map(j=>[j.input.job_id,clone(j.blank_review)]))};
}
function status(record) {
  if(record.review_complete) return 'completed';
  return record.reviewed_at || record.reviewer_notes || Object.values(record.labels).some(l=>l.state!=='unlabeled'||l.notes||l.evidence_text) ? 'partial':'untouched';
}
function summary(workspace) {
  const out={completed:0,partial:0,untouched:0,total:Object.keys(workspace.reviews).length};
  Object.values(workspace.reviews).forEach(r=>out[status(r)]++);return out;
}
function validateOffer(offer) {
  if(!offer || typeof offer!=='object'||Array.isArray(offer)) throw Error('Offer must be a separate object');
  const keys=['min_amount','max_amount','currency','period','gross_net_status','component','applicable_locations'];
  if(Object.keys(offer).some(k=>!keys.includes(k))) throw Error('Unexpected offer property');
  const lo=offer.min_amount,hi=offer.max_amount;
  if(lo==null && hi==null) throw Error('Offer needs a minimum or maximum amount');
  for(const x of [lo,hi]) if(x!=null && (typeof x==='boolean'||!Number.isFinite(Number(x))||Number(x)<=0)) throw Error('Amounts must be positive numbers');
  if(lo!=null && hi!=null && Number(hi)<Number(lo)) throw Error('Offer maximum is below minimum');
  if(offer.currency!=null && !/^[A-Z]{3}$/.test(offer.currency)) throw Error('Currency must be three uppercase letters');
  if(!['hour','day','month','year','task','one_time','unknown'].includes(offer.period)) throw Error('Invalid pay period');
  if(!['gross','net','unknown'].includes(offer.gross_net_status)) throw Error('Invalid gross/net state');
  if(!['base','bonus','equity','task_reward','other','unknown'].includes(offer.component)) throw Error('Invalid pay component');
  if(offer.applicable_locations!=null && (!Array.isArray(offer.applicable_locations)||offer.applicable_locations.some(x=>typeof x!=='string'||!x.trim()))) throw Error('Invalid offer locations');
}
function validateLabel(key,label,data) {
  if(!label || !STATES.includes(label.state)) throw Error(`${key}: invalid state`);
  if(Object.keys(label).some(k=>!['state','value','notes','evidence_text','item_details'].includes(k))) throw Error(`${key}: unexpected label property`);
  for(const prop of ['notes','evidence_text'])if(label[prop]!=null&&typeof label[prop]!=='string')throw Error(`${key}: notes/evidence must be text`);
  if(label.item_details!=null&&(!Array.isArray(label.item_details)||label.item_details.some(x=>!x||typeof x!=='object'||typeof x.normalized_value!=='string'||(x.original_wording!=null&&typeof x.original_wording!=='string')||!['required','preferred','unknown'].includes(x.requirement))))throw Error(`${key}: invalid item evidence`);
  if(label.state!=='known') {if(label.value!=null) throw Error(`${key}: unavailable state must have no value`);return;}
  const v=label.value;
  if(v==null || v==='' || (Array.isArray(v)&&!v.length)) throw Error(`${key}: Known needs a value`);
  const config=data.fields.find(f=>f.key===key);
  if(BOOLEAN_FIELDS.has(key)) {if(typeof v!=='boolean')throw Error(`${key}: choose Yes or No`);}
  else if(STRING_FIELDS.has(key)) {
    if(!Array.isArray(v)||v.some(x=>typeof x!=='string'||!x.trim()))throw Error(`${key}: enter nonempty items`);
    if(key==='country'&&v.some(x=>!/^[A-Z]{2}$/.test(x)))throw Error('Country: use ISO codes such as CZ');
  } else if(config?.type==='enum') {if(!config.options.includes(v))throw Error(`${key}: choose a valid value`);}
  else if(['compensation','base_compensation','other_compensation_benefits'].includes(key)) {
    if(!Array.isArray(v))throw Error(`${key}: needs separate items`);
    for(const x of v) {if(typeof x==='string'&&key==='other_compensation_benefits'){if(!x.trim())throw Error('Enter benefit wording');} else validateOffer(x);}
  } else if(key==='languages') {
    if(!Array.isArray(v))throw Error('Languages need separate items');
    for(const x of v) {
      if(!/^[a-z]{2}$/.test(x.language))throw Error('Language: use a two-letter code');
      if(!['required','preferred','unknown'].includes(x.requirement))throw Error('Invalid language requirement');
      if(x.cefr!=null&&!LEVELS.includes(x.cefr))throw Error('Invalid CEFR level');
      if(x.cefr_comparator!=null&&!COMPARATORS.includes(x.cefr_comparator))throw Error('Invalid CEFR comparator');
      if(['at_least','at_most','exact','range'].includes(x.cefr_comparator)&&!x.cefr)throw Error('CEFR comparator needs an explicit level');
      if(x.cefr_comparator==='range'&&(!x.cefr_max||LEVELS.indexOf(x.cefr_max)<LEVELS.indexOf(x.cefr)))throw Error('CEFR range needs an ordered upper level');
      if(x.cefr_comparator!=='range'&&x.cefr_max!=null)throw Error('Upper level is only for CEFR range');
      if(x.cefr==null&&x.cefr_state==='known')throw Error('Known CEFR needs a level');
      if(x.cefr!=null&&['ambiguous','not_mentioned'].includes(x.cefr_state))throw Error('Unavailable CEFR cannot have a level');
      if(x.cefr_or_higher===true&&x.cefr_comparator&&x.cefr_comparator!=='at_least')throw Error('Contradictory CEFR bound');
      if(x.cefr_or_higher===false&&x.cefr_comparator==='at_least')throw Error('Contradictory CEFR bound');
    }
  } else if(key==='experience') {
    if(!Array.isArray(v))throw Error('Experience needs separate constraints');
    for(const x of v) {
      if(x.min_years==null||!Number.isFinite(Number(x.min_years))||Number(x.min_years)<0||Number(x.min_years)>60)throw Error('Experience needs explicit minimum years, 0–60');
      if(x.max_years!=null&&(!Number.isFinite(Number(x.max_years))||Number(x.max_years)<Number(x.min_years)||Number(x.max_years)>60))throw Error('Invalid experience maximum');
    }
  } else if(typeof v!=='string')throw Error(`${key}: enter text`);
}
function validateRecord(record,data) {
  const job=data.jobs.find(j=>j.input.job_id===record.job_id);
  if(!job||record.profile_input_hash!==job.blank_review.profile_input_hash||record.sample_snapshot!==job.blank_review.sample_snapshot)throw Error('Job/input does not match this frozen cohort');
  if(record.cohort_hash && record.cohort_hash!==data.cohort_hash)throw Error('Different frozen cohort');
  if(record.frozen_versions&&Object.keys(record.frozen_versions).length&&JSON.stringify(Object.entries(record.frozen_versions).sort())!==JSON.stringify(Object.entries(data.manifest.versions).sort()))throw Error('Frozen versions mismatch');
  if(typeof record.review_complete!=='boolean')throw Error('Completion must be explicit true/false');
  if(Object.keys(record.labels).length!==data.label_fields.length||data.label_fields.some(k=>!Object.hasOwn(record.labels,k)))throw Error('Label fields do not match contract');
  for(const key of data.label_fields)validateLabel(key,record.labels[key],data);
  if(record.review_complete||Object.values(record.labels).some(l=>l.state!=='unlabeled')) {
    if(!record.reviewer_id?.trim()||!record.reviewed_at||Number.isNaN(Date.parse(record.reviewed_at)))throw Error('Reviewed labels need reviewer name and timestamp');
  }
  return record;
}
function normalizeImport(row,data,draft=false) {
  const job=data.jobs.find(j=>j.input.job_id===row.job_id);
  if(!job||row.profile_input_hash!==job.blank_review.profile_input_hash||row.sample_snapshot!==job.blank_review.sample_snapshot)throw Error('Import rejected: unknown job or changed frozen input');
  if(!['job-profile-human-v1.0','job-profile-human-v1.1','job-profile-human-v1.2'].includes(row.label_schema_version))throw Error('Unsupported human-label version');
  const metadata=['label_schema_version','job_id','profile_input_hash','sample_snapshot','reviewer_id','reviewed_at','reviewer_notes','review_complete','frozen_versions','cohort_hash','labels'];
  if(Object.keys(row).some(k=>!metadata.includes(k)))throw Error('Unexpected import data; predictions are not human labels');
  if(!row.labels||Object.keys(row.labels).some(k=>!data.label_fields.includes(k)))throw Error('Unexpected human label fields');
  const r=clone(job.blank_review);
  for(const key of metadata)if(key!=='labels'&&Object.hasOwn(row,key))r[key]=clone(row[key]);
  r.label_schema_version='job-profile-human-v1.2';
  for(const [key,label] of Object.entries(row.labels))r.labels[key]={...r.labels[key],...clone(label)};
  if(r.labels.languages.state==='known'&&Array.isArray(r.labels.languages.value))r.labels.languages.value=r.labels.languages.value.map(x=>({...{requirement:'unknown',cefr:null,cefr_or_higher:null,cefr_max:null,original_wording:null},...x,cefr_comparator:x.cefr_comparator||(x.cefr_or_higher?'at_least':x.cefr?'unknown':null),cefr_state:x.cefr?'known':x.cefr_state||'unknown'}));
  for(const key of ['compensation','base_compensation','other_compensation_benefits'])if(r.labels[key].state==='known'&&Array.isArray(r.labels[key].value))r.labels[key].value=r.labels[key].value.map(x=>typeof x==='string'?x:{...newOffer('unknown'),...x});
  if(r.cohort_hash&&r.cohort_hash!==data.cohort_hash)throw Error('Cohort hash mismatch');
  if(draft) {
    if(typeof r.review_complete!=='boolean'||Object.values(r.labels).some(l=>!STATES.includes(l.state)))throw Error('Invalid draft state');
    if(JSON.stringify(Object.entries(r.frozen_versions||{}).sort())!==JSON.stringify(Object.entries(data.manifest.versions).sort()))throw Error('Draft versions mismatch');
  } else validateRecord(r,data);
  r.frozen_versions=clone(data.manifest.versions);r.cohort_hash=data.cohort_hash;return r;
}
function parseCSV(text) {
  const rows=[];let row=[],cell='',quoted=false;
  for(let i=0;i<text.length;i++) {
    const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}
    else if(c===','&&!quoted){row.push(cell);cell='';}
    else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cell);if(row.some(x=>x))rows.push(row);row=[];cell='';}
    else cell+=c;
  }
  if(quoted)throw Error('Unclosed CSV quote');
  if(cell||row.length){row.push(cell);rows.push(row);}
  const headers=rows.shift()||[];
  if(new Set(headers).size!==headers.length)throw Error('Duplicate CSV headers');
  return rows.map(values=>{if(values.length!==headers.length)throw Error('CSV row width mismatch');return Object.fromEntries(headers.map((h,i)=>[h,values[i]]));});
}
function csvReviews(text,data) {
  return parseCSV(text).map(row=> {
    const job=data.jobs.find(j=>j.input.job_id===row.job_id);if(!job)throw Error('Unknown CSV job');
    const r=clone(job.blank_review);
    for(const k of ['job_id','profile_input_hash','sample_snapshot','label_schema_version','reviewer_id','reviewed_at','reviewer_notes','cohort_hash'])if(Object.hasOwn(row,k))r[k]=row[k]||null;
    const complete=row.review_complete||'false';if(!['true','false'].includes(complete.toLowerCase()))throw Error('Invalid CSV completion');r.review_complete=complete.toLowerCase()==='true';
    if(row.frozen_versions)r.frozen_versions=JSON.parse(row.frozen_versions);
    for(const k of data.label_fields) {
      const value=row[k+'_value']||'';
      r.labels[k]={state:row[k+'_state']||'unlabeled',value:value?(STRING_FIELDS.has(k)||BOOLEAN_FIELDS.has(k)||['compensation','base_compensation','other_compensation_benefits','languages','experience'].includes(k)?JSON.parse(value):value):null,
        notes:row[k+'_notes']||null,evidence_text:row[k+'_evidence']||null,item_details:JSON.parse(row[k+'_items']||'[]')};
    }
    return r;
  });
}
function importText(text,data,workspace) {
  text=text.trim();let parsed;
  if(text.startsWith('{')||text.startsWith('[')) {
    try{parsed=JSON.parse(text);}catch(_){parsed=text.split(/\r?\n/).filter(Boolean).map(line=>JSON.parse(line));}
  } else parsed=csvReviews(text,data);
  const draft=parsed?.tool==='career-os-reviewer-workspace-v1';
  if(draft&&parsed.cohort_hash!==data.cohort_hash)throw Error('Draft belongs to another cohort');
  const rows=draft?Object.values(parsed.reviews):Array.isArray(parsed)?parsed:[parsed];
  if(!rows.length)throw Error('Import contains no reviews');
  if(new Set(rows.map(r=>r.job_id)).size!==rows.length)throw Error('Duplicate reviews; adjudicate before importing');
  const validated=rows.map(row=>normalizeImport(row,data,draft));
  const next=clone(workspace);validated.forEach(r=>next.reviews[r.job_id]=r);
  if(draft) {next.reviewer_id=parsed.reviewer_id||'';if(data.jobs.some(j=>j.input.job_id===parsed.current_job_id))next.current_job_id=parsed.current_job_id;}
  return next; // Atomic: caller swaps only after every imported row validates.
}
function exportRows(workspace,data,completedOnly=false) {
  return data.jobs.map(j=>workspace.reviews[j.input.job_id]).filter(r=>!completedOnly||r.review_complete).map(r=>clone(validateRecord(r,data)));
}
function exportJSONL(workspace,data,completedOnly=false) {return exportRows(workspace,data,completedOnly).map(r=>JSON.stringify(r)).join('\n')+'\n';}
function exportCSV(workspace,data) {
  const columns=['job_id','profile_input_hash','sample_snapshot','label_schema_version','reviewer_id','reviewed_at','reviewer_notes','review_complete','cohort_hash','frozen_versions',...data.label_fields.flatMap(k=>[k+'_state',k+'_value',k+'_notes',k+'_evidence',k+'_items'])];
  const escape=x=>'"'+String(x??'').replaceAll('"','""')+'"';
  const rows=exportRows(workspace,data).map(r=> {
    const x={...r,frozen_versions:JSON.stringify(r.frozen_versions),review_complete:String(r.review_complete)};
    for(const k of data.label_fields){const l=r.labels[k];x[k+'_state']=l.state;x[k+'_value']=l.value==null?'':typeof l.value==='string'?l.value:JSON.stringify(l.value);x[k+'_notes']=l.notes;x[k+'_evidence']=l.evidence_text;x[k+'_items']=JSON.stringify(l.item_details||[]);}
    return columns.map(c=>escape(x[c])).join(',');
  });return [columns.map(escape).join(','),...rows].join('\r\n')+'\r\n';
}
const API={fresh,status,summary,validateRecord,normalizeImport,importText,parseCSV,exportRows,exportJSONL,exportCSV};
if(typeof module!=='undefined'&&module.exports)module.exports=API;
root.CareerReviewer=API;
if(typeof document==='undefined')return;

const data=JSON.parse(document.getElementById('review-data').textContent),storageKey='career-os-human-review:'+data.cohort_hash;
let workspace=fresh(data),storageAvailable=true;
const $=id=>document.getElementById(id);
function save() {
  try{localStorage.setItem(storageKey,JSON.stringify(workspace));storageAvailable=true;$('save-status').textContent='Saved in this browser · export a durable copy regularly.';}
  catch(_){storageAvailable=false;$('save-status').textContent='Browser storage unavailable/full. Export a draft backup before closing this page.';}
  progress();
}
try{const saved=localStorage.getItem(storageKey);if(saved)workspace=importText(saved,data,workspace);}catch(error){storageAvailable=false;$('save-status').textContent='Local resume failed: '+error.message+'. Import an exported backup.';}
function current(){return workspace.reviews[workspace.current_job_id];}
function touch(){const r=current();r.review_complete=false;r.reviewer_id=$('reviewer').value.trim()||null;r.reviewed_at=new Date().toISOString();save();}
function visible(){const q=$('search').value.trim().toLowerCase();return data.jobs.filter(j=>(!$('incomplete').checked||!workspace.reviews[j.input.job_id].review_complete)&&[j.input.title,j.input.company,...j.sources].join(' ').toLowerCase().includes(q));}
function progress() {
  const s=summary(workspace);$('summary').textContent=`${s.completed} completed · ${s.partial} partial · ${s.untouched} untouched · ${s.total} total`;
  for(const option of $('jump').options){const j=data.jobs.find(j=>j.input.job_id===option.value);const state=status(workspace.reviews[option.value]);option.textContent=(state==='completed'?'✓ ':state==='partial'?'◐ ':'○ ')+(data.jobs.indexOf(j)+1)+'. '+j.input.company+' — '+j.input.title;}
  if(current()){$('review-status').textContent=status(current());$('review-status').className='badge '+status(current());}
  const rows=visible(),index=rows.findIndex(j=>j.input.job_id===workspace.current_job_id);
  $('previous').disabled=index<=0;$('next').disabled=index<0||index>=rows.length-1;
}
function element(tag,props={}){const e=document.createElement(tag);for(const [k,v] of Object.entries(props)){if(k==='text')e.textContent=v;else if(k==='class')e.className=v;else e[k]=v;}return e;}
function select(options,value,onchange,blank=false){const s=element('select');if(blank)s.append(element('option',{value:'',text:'Choose…'}));for(const v of options)s.append(element('option',{value:v,text:v.replaceAll('_',' ')}));s.value=value??'';s.addEventListener('change',()=>onchange(s.value));return s;}
function input(value,onchange,type='text',placeholder=''){const e=element('input',{type,value:value??'',placeholder});e.addEventListener('input',()=>onchange(e.value));return e;}
function labeled(parent,text,control){const l=element('label',{text});l.append(control);parent.append(l);return control;}
function numeric(value,onchange){const n=input(value,v=>onchange(v===''?null:v),'number');n.step='any';return n;}
function deleteItem(key,index,container){const l=current().labels[key];l.value.splice(index,1);if(l.item_details?.length)l.item_details.splice(index,1);touch();renderValue(key,container);}
function offerEditor(key,offer,index,container,item){
 const grid=element('div',{class:'item-grid'});item.append(grid);
 for(const [k,t] of [['min_amount','Minimum'],['max_amount','Maximum']])labeled(grid,t,numeric(offer[k],v=>{offer[k]=v;touch();}));
 labeled(grid,'Currency',input(offer.currency,v=>{offer.currency=v.trim().toUpperCase()||null;touch();},'text','CZK / EUR / USD'));
 labeled(grid,'Period / unit',select(['unknown','hour','day','month','year','task','one_time'],offer.period,v=>{offer.period=v;touch();}));
 labeled(grid,'Gross / net',select(['unknown','gross','net'],offer.gross_net_status,v=>{offer.gross_net_status=v;touch();}));
 labeled(grid,'Component',select(key==='base_compensation'?['base','unknown']:['unknown','bonus','equity','task_reward','other'],offer.component,v=>{offer.component=v;touch();}));
 const scope=element('label',{text:'Applicable locations — only if explicitly linked',class:'wide'});scope.append(input((offer.applicable_locations||[]).join(', '),v=>{offer.applicable_locations=v.split(',').map(x=>x.trim()).filter(Boolean);if(!offer.applicable_locations.length)offer.applicable_locations=null;touch();}));grid.append(scope);
}
function renderValue(key,container){
 container.replaceChildren();const l=current().labels[key],f=data.fields.find(x=>x.key===key);
 if(l.state!=='known'){container.append(element('p',{class:'help',text:l.state==='unlabeled'?'Not reviewed yet. No value has been assumed.':'No factual value is asserted in this state.'}));return;}
 if(f.type==='text'){container.append(input(l.value,v=>{l.value=v;touch();}));return;}
 if(f.type==='enum'){container.append(select(f.options,l.value,v=>{l.value=v||null;touch();},true));return;}
 if(f.type==='boolean'){container.append(select(['true','false'],l.value==null?'':String(l.value),v=>{l.value=v===''?null:v==='true';touch();},true));return;}
 if(!Array.isArray(l.value))l.value=[];
 l.value.forEach((value,index)=> {
   const item=element('div',{class:'item'});container.append(item);
   if(f.type==='strings'){
     const grid=element('div',{class:'item-grid'});item.append(grid);
     const detail=l.item_details[index]||(l.item_details[index]={normalized_value:value,original_wording:null,requirement:'unknown'});
     labeled(grid,'Normalized value',input(value,v=>{l.value[index]=key==='country'?v.toUpperCase():v;detail.normalized_value=l.value[index];touch();}));
     labeled(grid,'Original wording (optional)',input(detail.original_wording,v=>{detail.original_wording=v||null;touch();}));
     if(!['city','country'].includes(key))labeled(grid,'Required / preferred?',select(['unknown','required','preferred'],detail.requirement,v=>{detail.requirement=v;touch();}));
   }else if(f.type==='offers'||(f.type==='benefits'&&typeof value==='object'))offerEditor(key,value,index,container,item);
   else if(f.type==='benefits')item.append(input(value,v=>{l.value[index]=v;touch();},'text','Original benefit wording; do not call this base salary'));
   else if(f.type==='languages'){
     const grid=element('div',{class:'item-grid'});item.append(grid);
     labeled(grid,'Language code',input(value.language,v=>{value.language=v.toLowerCase().trim();touch();},'text','en / cs / de'));
     labeled(grid,'Requirement',select(['unknown','required','preferred'],value.requirement,v=>{value.requirement=v;touch();}));
     labeled(grid,'Explicit CEFR',select(LEVELS,value.cefr,v=>{value.cefr=v||null;value.cefr_state=v?'known':'unknown';if(!v){value.cefr_comparator=null;value.cefr_max=null;}value.cefr_or_higher=v?(value.cefr_comparator==='at_least'):null;touch();renderValue(key,container);},true));
     labeled(grid,'CEFR comparator',select(COMPARATORS,value.cefr_comparator||'unknown',v=>{value.cefr_comparator=v;value.cefr_or_higher=value.cefr?(v==='at_least'):null;if(v!=='range')value.cefr_max=null;touch();renderValue(key,container);}));
     if(value.cefr_comparator==='range')labeled(grid,'Upper CEFR',select(LEVELS,value.cefr_max,v=>{value.cefr_max=v||null;touch();},true));
     if(!value.cefr)labeled(grid,'CEFR evidence state',select(['unknown','not_mentioned','ambiguous'],value.cefr_state||'unknown',v=>{value.cefr_state=v;touch();}));
     labeled(grid,'Original proficiency wording',input(value.original_wording,v=>{value.original_wording=v||null;touch();},'text','Fluent / B2 or above / original wording'));
   }else if(f.type==='experience'){
     const grid=element('div',{class:'item-grid'});item.append(grid);
     labeled(grid,'Minimum explicit years',numeric(value.min_years,v=>{value.min_years=v;touch();}));
     labeled(grid,'Maximum explicit years (optional)',numeric(value.max_years,v=>{value.max_years=v;touch();}));
   }
   const remove=element('button',{text:'Remove item',type:'button'});remove.onclick=()=>deleteItem(key,index,container);item.append(remove);
 });
 const add=element('button',{text:f.type==='offers'?'Add separate offer':f.type==='benefits'?'Add benefit statement':'Add item',type:'button'});
 add.onclick=()=>{let value='';if(f.type==='offers')value=newOffer('base');if(f.type==='languages')value={language:'',requirement:'unknown',cefr:null,cefr_comparator:null,cefr_max:null,cefr_or_higher:null,original_wording:null,cefr_state:'unknown'};if(f.type==='experience')value={min_years:null,max_years:null};l.value.push(value);touch();renderValue(key,container);};container.append(add);
 if(f.type==='benefits'){const offer=element('button',{text:'Add non-base monetary offer',type:'button'});offer.onclick=()=>{l.value.push(newOffer('other'));touch();renderValue(key,container);};container.append(offer);}
}
function newOffer(component){return {min_amount:null,max_amount:null,currency:null,period:'unknown',gross_net_status:'unknown',component,applicable_locations:null};}
function renderFields(){
 $('fields').replaceChildren();let group=null,fieldset=null;
 for(const f of data.fields){
   if(f.group!==group){group=f.group;fieldset=element('fieldset',{class:'group'});fieldset.append(element('legend',{text:group}));$('fields').append(fieldset);}
   const l=current().labels[f.key],field=element('div',{class:'field '+l.state});field.dataset.field=f.key;fieldset.append(field);
   const head=element('div',{class:'field-header'});head.append(element('label',{text:f.label}));field.append(head);
   const content=element('div',{class:'field-value'});
   const state=select(['unlabeled','known','unknown','not_mentioned','ambiguous'],l.state==='conflict'?'ambiguous':l.state,v=>{l.state=v;l.value=v==='known'?(f.type==='text'?'':f.type==='enum'||f.type==='boolean'?null:[]):null;field.className='field '+v;touch();renderValue(f.key,content);});state.setAttribute('aria-label',f.label+' state');head.append(state);field.append(content);renderValue(f.key,content);
   const notes=element('details'),heading=element('summary',{text:'Optional evidence / notes'});notes.append(heading);const grid=element('div',{class:'evidence-grid'});notes.append(grid);field.append(notes);
   for(const [prop,label] of [['evidence_text','Short employer evidence'],['notes','Reviewer interpretation / notes']]){const box=element('textarea',{rows:2,value:l[prop]||''});box.oninput=()=>{l[prop]=box.value||null;touch();};labeled(grid,label,box);}
 }
}
function render(){
 const rows=visible();if(!rows.some(j=>j.input.job_id===workspace.current_job_id))workspace.current_job_id=rows[0]?.input.job_id||data.jobs[0].input.job_id;
 $('jump').replaceChildren();rows.forEach(j=>$('jump').append(element('option',{value:j.input.job_id,text:(data.jobs.indexOf(j)+1)+'. '+j.input.company+' — '+j.input.title})));
 $('job').hidden=!rows.length;$('position').textContent=rows.length?`${data.jobs.findIndex(j=>j.input.job_id===workspace.current_job_id)+1} / 60 · ${rows.length} in view`:'No matching jobs';
 if(!rows.length){progress();return;}
 const job=rows.find(j=>j.input.job_id===workspace.current_job_id),r=current();$('jump').value=job.input.job_id;
 $('title').textContent=job.input.company+' — '+job.input.title;$('identity').textContent='Canonical ID '+job.input.job_id+' · frozen input '+r.profile_input_hash.slice(0,12)+' · '+data.manifest.snapshot;
 $('sources').textContent='Sources: '+job.sources.join(', ')+' · description: '+(job.input.description_source||'none')+' · input quality: '+job.prediction.content.description_quality;
 $('description').textContent=job.input.description||'No usable employer description — abstention is appropriate.';
 $('raw-content').textContent=JSON.stringify(job.input.structured_source_inputs,null,2);
 $('prediction-content').textContent=JSON.stringify(job.prediction,null,2);$('suggestions-content').textContent=JSON.stringify(job.provider_suggestions,null,2);
 $('prediction').open=false;$('suggestions').open=false;$('raw').open=false;$('validation').textContent='';$('reviewer-notes').value=r.reviewer_notes||'';renderFields();progress();
}
function navigate(delta){const rows=visible(),i=rows.findIndex(j=>j.input.job_id===workspace.current_job_id);if(rows[i+delta]){workspace.current_job_id=rows[i+delta].input.job_id;render();save();$('title').focus();}}
function download(text,name,type='application/x-ndjson'){const blob=new Blob([text],{type}),url=URL.createObjectURL(blob),a=element('a',{href:url,download:name});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function runExport(fn){try{fn();$('validation').textContent='';}catch(error){$('validation').textContent=error.message+' · Fix the field/state, or export Draft backup to preserve unfinished work.';}}
$('reviewer').value=workspace.reviewer_id||'';$('reviewer').oninput=()=>{workspace.reviewer_id=$('reviewer').value;save();};
$('reviewer-notes').oninput=()=>{current().reviewer_notes=$('reviewer-notes').value||null;touch();};
$('previous').onclick=()=>navigate(-1);$('next').onclick=()=>navigate(1);
$('jump').onchange=()=>{workspace.current_job_id=$('jump').value;render();save();};$('search').oninput=render;$('incomplete').onchange=render;
$('complete').onclick=()=>{try{const r=current();r.reviewer_id=$('reviewer').value.trim()||null;r.reviewed_at=new Date().toISOString();validateRecord(r,data);if(!r.reviewer_id)throw Error('Enter your reviewer name');const unreviewed=data.fields.filter(f=>r.labels[f.key].state==='unlabeled').length;if(unreviewed&&!confirm(`${unreviewed} fields remain unlabeled and will not be scored. Explicitly mark this review complete?`))return;r.review_complete=true;save();if($('incomplete').checked)render();$('validation').textContent='Review explicitly marked complete. Export a durable copy.';}catch(error){$('validation').textContent=error.message;}};
$('reopen').onclick=()=>{current().review_complete=false;save();};
$('export-complete').onclick=()=>runExport(()=>{if(!summary(workspace).completed)throw Error('No completed reviews yet');download(exportJSONL(workspace,data,true),'career-os-completed-labels.jsonl');});
$('export-all').onclick=()=>runExport(()=>download(exportJSONL(workspace,data),'career-os-human-progress.jsonl'));
$('export-csv').onclick=()=>runExport(()=>download(exportCSV(workspace,data),'career-os-human-progress.csv','text/csv'));
$('backup').onclick=()=>download(JSON.stringify(workspace,null,2),'career-os-reviewer-draft.json','application/json');
$('import').onclick=()=>$('file').click();$('file').onchange=async()=>{const file=$('file').files[0];if(!file)return;try{const text=await file.text();const next=importText(text,data,workspace);if(!confirm('Import validated. Replace only the jobs present in this file?'))return;workspace=next;$('reviewer').value=workspace.reviewer_id||'';render();save();}catch(error){$('validation').textContent=error.message;}finally{$('file').value='';}};
document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.altKey&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();navigate(event.key==='ArrowRight'?1:-1);}});
render();if(storageAvailable)save();
})(typeof globalThis!=='undefined'?globalThis:this);
