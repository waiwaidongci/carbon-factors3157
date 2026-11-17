import {heading} from '../components/common/header.js';
import {esc,fmt} from '../utils/format.js';
import {table} from '../components/common/table.js';
import {SAMPLING_STATUSES} from '../constants/enums.js';

const sStatus=s=>SAMPLING_STATUSES[s]||s;
const factorActivities=p=>Object.keys(p.data.factors);
let chosenPolicyId=null;

export function uncertainty(c,d,selected,render){
 const current=d.policies.find(p=>p.id===chosenPolicyId)||d.policies[0];
 if(current)chosenPolicyId=current.id;
 const job=d.sampling.find(j=>j.id===selected)||d.sampling[0];
 return heading('UNCERTAINTY DESK / 05','因子联合抽样与排放不确定区间','按因子均值、标准差与对数相关矩阵生成联合对数正态样本；同次抽样各部门共用因子，先合并排放再取全体与部门的精确95%区间。非法相关矩阵在排队时拒绝。')
 +(d.policies.length?form(d,current):'<p class="empty">请先在口径版本页登记口径。</p>')
 +jobList(d,job)
 +(job?jobDetail(job):'');
}

function form(d,current){
 const acts=factorActivities(current);
 const policies=d.policies.map(p=>`<option value="${p.id}" ${p.id===current.id?'selected':''}>${esc(p.version)}</option>`).join('');
 const rows=acts.map((a,i)=>{
  const f=current.data.factors[a];
  return `<div class="policy-row" data-idx="${i}" data-activity="${esc(a)}">
   <label>活动<input name="activity_name" value="${esc(a)}" readonly></label>
   <label>因子均值 kg/${esc(f.unit)}<input name="mean" type="number" step="any" min="0" value="${esc(f.kg_per_unit)}" required></label>
   <label>因子标准差<input name="std" type="number" step="any" min="0" value="${esc((f.kg_per_unit*0.1).toFixed(4))}" required></label>
  </div>`;
 }).join('');
 const matrix=acts.map((a,i)=>`<tr><th>${esc(a)}</th>`+acts.map((b,j)=>{
  if(i===j)return '<td>1</td>';
  if(i>j)return '<td class="mirror"></td>';
  return `<td><input name="corr" data-i="${i}" data-j="${j}" type="number" step="any" min="-1" max="1" value="0" required></td>`;
 }).join('')+'</tr>').join('');
 return `<form id="sampling-form" class="sampling-form">
  <div class="inline-form">
   <label>作业名称<input name="name" value="${esc(current.version)}不确定性抽样" required minlength="2"></label>
   <label>口径版本<select name="policy_id">${policies}</select></label>
   <label>期间<select name="period"><option>全部</option>${d.periods.map(p=>`<option>${esc(p.name)}</option>`).join('')}</select></label>
  </div>
  <h3>因子分布参数（均值 μ 与标准差 σ，内部换算为对数正态 μ_ln、σ_ln）</h3>
  <div id="sampling-rows">${rows}</div>
  <h3>对数相关矩阵（对角线固定为 1，上三角编辑、下三角自动镜像；需为对称正定矩阵）</h3>
  <div class="table-wrap"><table class="corr-table"><thead><tr><th></th>${acts.map(a=>`<th>${esc(a)}</th>`).join('')}</tr></thead><tbody>${matrix}</tbody></table></div>
  <div class="inline-form">
   <label>抽样次数（上限100万）<input name="sample_count" type="number" min="100" max="1000000" step="100" value="20000" required></label>
   <label>分块大小（内存只随块大小增长）<input name="chunk_size" type="number" min="100" max="100000" step="100" value="10000" required></label>
   <label>随机种子<input name="seed" type="number" min="0" max="2147483647" value="42" required></label>
  </div>
  <button>排队抽样</button>
 </form>`;
}

function jobList(d,current){
 if(!d.sampling.length)return '<h2>抽样作业</h2><p class="empty">尚无抽样作业。填写参数后排队，分块执行、中断可恢复。</p>';
 return '<h2>抽样作业</h2><div class="run-list sampling-list">'+d.sampling.map(j=>{
  const pct=j.sample_count?Math.round(100*j.progress/j.sample_count):0;
  return `<button class="select-sampling ${current&&j.id===current.id?'active':''}" data-id="${j.id}">
   ${esc(j.name)} <span class="status">${sStatus(j.status)}</span>
   <span class="progress"><i style="width:${pct}%"></i></span><small>${j.progress}/${j.sample_count} · ${pct}%</small>
  </button>`;
 }).join('')+'</div>';
}

function jobDetail(j){
 const pct=j.sample_count?Math.round(100*j.progress/j.sample_count):0;
 return `<section class="report sampling-detail">
  <div class="report-top"><h2>${esc(j.name)}</h2><span class="status">${sStatus(j.status)}</span></div>
  <p>口径：${esc(j.snapshot.policy.version)} · 来源 ${j.snapshot.sources.length} 行 · 期间 ${esc(j.snapshot.period)} · 种子 ${j.seed}</p>
  <p class="checksum">SHA256 ${esc(j.checksum)}</p>
  <div class="progress progress-lg"><i style="width:${pct}%"></i></div><p>已计样本 ${j.progress}/${j.sample_count}（分块 ${j.chunk_size}）</p>
  ${j.error?`<p class="error">${esc(j.error)}</p>`:''}
  ${j.result?`
   <div class="metrics">${Object.entries(j.result.summary).map(([k,v])=>`<div><small>${esc(k)}</small><strong>${fmt(v)}</strong></div>`).join('')}</div>
   <h3>全体与部门精确95%区间（kgCO₂e，最近秩 P2.5/P50/P97.5）</h3>
   ${table(j.result.intervals)}
   <h3>因子对数正态依据</h3>${table(j.result.factors)}
   <p class="method">${esc(j.result.method)}</p>
   <p class="warning">${j.result.warnings.map(esc).join('；')}</p>`:''}
  <details><summary>冻结底稿、口径与抽样依据</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px">${esc(JSON.stringify(j.snapshot,null,2))}</pre></details>
 </section>`;
}

export function bindUncertainty(c,d,render){
 const f=document.querySelector('#sampling-form');
 if(!f)return;
 f.policy_id.onchange=()=>{chosenPolicyId=Number(f.policy_id.value);render();};
 f.querySelectorAll('input[name=corr]').forEach(inp=>inp.oninput=()=>{
  const i=Number(inp.dataset.i),j=Number(inp.dataset.j);
  const rows=f.querySelectorAll('.corr-table tbody tr');
  rows[j].querySelectorAll('td')[i].textContent=inp.value;
 });
 document.querySelectorAll('.select-sampling').forEach(b=>b.onclick=()=>{window.__selectSampling=Number(b.dataset.id);render();});
}
