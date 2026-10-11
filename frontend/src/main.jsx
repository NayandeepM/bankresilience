import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Activity, Bot, Clock3, Network, Play, RefreshCw, ShieldCheck, Zap} from 'lucide-react';
import {experimentView, diagnosisFor} from './view.js';
import {Lifecycle, DiagnosisReport, SafetyPolicy} from './reports.jsx';
import {SuitePanel, ToleranceReport} from './suite-panel.jsx';
import {FindingsPanel} from './findings-panel.jsx';
import './style.css';
const API=import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';
async function api(path,body) {
  const response=await fetch(API+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok) throw new Error(data.detail || 'Request failed');
  return data;
}
const metric=(n,unit='')=>n==null?'—':`${Number(n).toFixed(1)}${unit}`;
function App(){
  const [data,setData]=useState({services:[],events:[],recent_telemetry:[]});
  const [health,setHealth]=useState({}); const [history,setHistory]=useState([]);
  const [catalogue,setCatalogue]=useState([]); const [selected,setSelected]=useState(null);
  const [systemAnalysis,setSystemAnalysis]=useState({weaknesses:[]});
  const [busy,setBusy]=useState(false); const [message,setMessage]=useState('Loading the local control plane…');
  const [suites,setSuites]=useState([]); const [selectedSuite,setSelectedSuite]=useState(null);
  const [protection,setProtection]=useState(true); const [payments,setPayments]=useState(false);
  const [paymentBudget,setPaymentBudget]=useState(15);
  const [confirmation,setConfirmation]=useState(null);
  const polling=useRef(false);
  async function refresh(){
    if(polling.current) return;
    polling.current=true;
    try{
      const [state,h,rows,c,ss,analysis]=await Promise.all([api('/api/state'),api('/api/health'),api('/api/history'),api('/api/experiments'),api('/api/suites'),api('/api/analysis')]);
      setSystemAnalysis(analysis);
      setData(state);setHealth(h);setHistory(rows);setCatalogue(c);setSuites(ss);setMessage(previous=>previous==='Loading the local control plane…'?'Connected. Choose an experiment or inspect saved evidence.':previous);
    }catch(e){setHealth({docker_connected:false});setMessage(e.message);}
    finally{polling.current=false;}
  }
  useEffect(()=>{refresh();const timer=setInterval(refresh,1000);return()=>clearInterval(timer);},[]);
  async function action(path,body,label){
    setBusy(true);setMessage(label);
    try{const r=await api(path,body);setMessage(r.id?`${path==='/api/suites'?'Suite':'Experiment'} ${r.id} started.`:`${label} completed.`);await refresh();}
    catch(e){setMessage(e.message);}finally{setBusy(false);}
  }
  function run(spec){
    setConfirmation({type:'experiment',spec});
  }
  useEffect(()=>{if(data.active)setSelected(null);},[data.active,data.experiment?.id]);
  const view=experimentView(data,history,selected); const diagnosis=diagnosisFor(view);
  const points=view?.telemetry ?? data.recent_telemetry ?? [];
  const events=view?.events ?? [];
  const baseline=view?.baseline ?? data.baseline;
  const target=view?.target ?? 'ledgerwriter';
  const metricCharts=[
    {title:`${target} latency (includes failed request duration)`,unit:'ms',value:p=>p.target_probe?.latency_ms??null,baseline:baseline?.[target]?.latency_ms?.median,threshold:view?.safety_policy?.latency_ms?.critical},
    {title:'Target availability',unit:'%',value:p=>p.target_probe?.ok==null?null:p.target_probe.ok?100:0,max:100,threshold:view?.safety_policy?.availability_percent?.critical},
    {title:'Probe errors',unit:'%',value:p=>p.target_probe?.ok==null?null:p.target_probe.ok?0:100,max:100,threshold:view?.safety_policy?.error_rate_percent?.critical},
    {title:'Target CPU',unit:'%',value:p=>p.resources?.cpu_percent??null,threshold:view?.safety_policy?.cpu_percent?.critical},
    {title:'Target memory',unit:'MiB',value:p=>p.resources?.memory_bytes==null?null:p.resources.memory_bytes/1048576,threshold:view?.safety_policy?.memory_bytes?.critical==null?null:view.safety_policy.memory_bytes.critical/1048576},
    {title:'Observed affected services',unit:'services',value:p=>p.affected_services?.length??null,threshold:view?.safety_policy?.blast_radius?.max_affected_services},
  ];
  if(view?.experiment_type==='cpu-stress')metricCharts.unshift(metricCharts.splice(3,1)[0]);
  else if(view?.experiment_type==='packet-loss')metricCharts.unshift(metricCharts.splice(2,1)[0]);
  else if(view?.experiment_type && view.experiment_type!=='network-slowdown')metricCharts.unshift(metricCharts.splice(1,1)[0]);

  const weaknesses=systemAnalysis.weaknesses??[];
  const disabled=busy||data.active||data.suite_running||data.baseline_collecting||!health.docker_connected||data.safety_state==='manual-escalation';
  const suiteView=data.suite_running?data.suite:selectedSuite?suites.find(s=>s.id===selectedSuite):data.suite??suites[0];
  function runSuite(combinations=false){setConfirmation({type:'suite',combinations});}
  function confirmRun(){const request=confirmation;setConfirmation(null);setSelected(null);if(request.type==='suite'){setSelectedSuite(null);action('/api/suites',{confirm:true,resilience_enabled:protection,verify_payments:payments,payment_budget:paymentBudget,combinations_only:request.combinations},'Starting experiment suite');}else action(`/api/experiments/${request.spec.experiment_type}`,{confirm:true,resilience_enabled:protection},'Checking fresh baseline and guardrails');}
  const ranking=data.planner?.recommendations ?? [];
  return <div className="app-shell">
    {confirmation&&<div className="confirmation-backdrop"><section className="panel confirmation-dialog" role="dialog" aria-modal="true" aria-labelledby="confirmation-title"><h2 id="confirmation-title">{confirmation.type==='suite'?'Confirm experiment suite':`Confirm ${confirmation.spec.name}`}</h2><p>{confirmation.type==='suite'?(confirmation.combinations?'Run two approved combinations: network slowdown + CPU stress, then packet loss + CPU stress. Each pair overlaps on LedgerWriter, with recovery verified before the next pair.':'Run all five single faults sequentially with fresh baselines and recovery checks between them.'):`Inject ${confirmation.spec.components?.length?confirmation.spec.components.join(" + ")+" concurrently":"a bounded fault"} into ${confirmation.spec.target} for at most ${confirmation.spec.max_seconds} seconds.`}</p><p>{protection?'Healthy standby routing will protect traffic.':'Protection is disabled for this comparison; safety controls restore the primary.'}</p>{confirmation.type==='suite'&&<p>{payments?`Creates up to ${paymentBudget} local demo payments of $0.01.`:'Authenticated home-page checks will run; no payments will be submitted.'}</p>}<div className="hero-actions"><button className="primary-button" disabled={disabled} onClick={confirmRun}>Confirm and start</button><button className="text-button" onClick={()=>setConfirmation(null)}>Cancel</button></div></section></div>}
    <header className="topbar"><div className="brand"><div className="brand-icon"><ShieldCheck/></div><div><div className="brand-name">BankResilience</div><div className="brand-sub">CONTROLLED FAILURE · MEASURED RECOVERY</div></div></div><div className="top-right"><span className={`connection-dot ${health.docker_connected?'live':''}`}/>{health.docker_connected?'Docker connected':'Control plane disconnected'}<button className="icon-button" onClick={refresh} title="Refresh"><RefreshCw size={16}/></button></div></header>
    <main>
      <section className="hero"><div><div className="eyebrow">BANK OF ANTHOS / LOCAL LAB</div><h1>Resilience control center</h1><p>Observe the baseline, choose a bounded fault, and follow evidence through diagnosis, safety approval and recovery.</p></div><div className="hero-actions"><button className="primary-button" disabled={disabled} onClick={()=>action('/api/baseline',{samples:30},'Collecting 30 baseline samples')}>Collect baseline</button><button className="primary-button" disabled={disabled||!data.baseline} onClick={()=>action('/api/planner',{},'Ranking experiments')}>Recommend experiments</button>{data.active&&<button className="danger-button" onClick={()=>action('/api/kill-switch',{},'Requesting immediate intervention')}>Activate kill switch</button>}</div></section>
      <section className="stats-grid">
        <Stat label="SERVICES RUNNING" value={`${data.services.filter(s=>s.status==='running').length} / 8`} detail="Live container state"/>
        <Stat label="EXPERIMENT" value={data.active?data.experiment?.phase:view?.status??'Idle'} detail={view?`${view.name??view.experiment_type} · #${view.id}`:'Recent idle telemetry'}/>
        <Stat label="SAFETY" value={data.safety_state??'idle'} detail="Measured thresholds · emergency timer backstop"/>
        <Stat label="RESILIENCE SCORE" value={view?.resilience_score?.value!=null?`${view.resilience_score.value} / 100`:'—'} detail="Measured probe score · transactions unverified"/>
      </section>
      {data.safety_state==='manual-escalation'&&<section className="panel"><p>Recovery was not verified. Check fresh service health before enabling further fault injections.</p><button className="primary-button" disabled={busy||data.active||data.suite_running||data.baseline_collecting} onClick={()=>action('/api/recovery/verify',{},'Verifying restored services')}>Verify restored services</button></section>}
      <SuitePanel suite={suiteView} suites={suites} running={data.suite_running} disabled={disabled} protection={protection} setProtection={setProtection} payments={payments} setPayments={setPayments} paymentBudget={paymentBudget} setPaymentBudget={setPaymentBudget} onRun={runSuite} onCancel={()=>action('/api/suites/cancel',{},'Stopping suite safely')} onSelect={setSelectedSuite} onExperiment={setSelected} resilience={data.resilience} onEnable={()=>action('/api/resilience/enable',{},'Preparing healthy standby protection')} onDisable={()=>action('/api/resilience/disable',{},'Restoring original service routing')}/>
      <FindingsPanel apiBase={API} disabled={disabled}/>
      <section className="panel planner-panel"><Heading icon={<Bot size={18}/>} title="AI Experiment Planner" detail={data.planner?data.planner.mode==='gemini'?`Gemini · ${data.planner.model}`:`AI unavailable · deterministic ordering · ${data.planner.fallback_reason}`:'Collect a baseline, then request an evidence-based ranking'}/><p className="panel-subtitle">Planner explanations are AI hypotheses. Historical faults were controlled injections; statements about missing controls need configuration verification. Dependencies describe potential caller impact.</p><div className="experiment-cards">{(ranking.length?ranking:catalogue.map(c=>({experiment_type:c.experiment_type}))).map((r,i)=>{
        const spec=catalogue.find(c=>c.experiment_type===r.experiment_type);if(!spec)return null;
        return <div className="experiment-card" key={r.experiment_type}><strong>{ranking.length?`${i+1}. `:''}{spec.name}</strong><span>{r.severity??'Available fault'} · {spec.target} · maximum {spec.max_seconds}s</span><p>{r.rationale??'Request planner ranking to see an explanation.'}</p>{r.expected_impact&&<p>{r.expected_impact}</p>}{r.graph_evidence&&<ul className="graph-reasons">{r.graph_evidence.map((e,i)=><li key={i}>{e}</li>)}</ul>}<button className="primary-button" disabled={disabled} onClick={()=>run(spec)}><Play size={14}/> Run experiment</button></div>;
      })}</div></section>
      <section className="panel metrics-panel"><Heading icon={<Activity size={18}/>} title={view?`${view.name??view.experiment_type} · #${view.id}`:'Live application metrics'} detail={data.active?'Live observations during this experiment':view?'Complete saved experiment timeline':'Recent idle observations; collect a baseline for statistical context'}/>
        <ToleranceReport view={view}/><SafetyPolicy view={view}/><div className="chart-grid">{metricCharts.map(c=><Chart key={c.title} points={points} events={events} {...c}/>)}</div>
        <div className="phase-legend"><span>● Baseline</span><span>● Fault</span><span>● Intervention</span><span>● Recovery</span><span>{points.length} timestamped observations</span></div>
        <div className="event-chips">{events.filter(e=>['fault-start','threshold-breach','kill-switch','recovery-started','recovered'].includes(e.kind)).map((e,i)=><span key={i}>{new Date(e.time).toLocaleTimeString()} · {e.kind}</span>)}</div>
        <div className="metric-details">CPU: {metric(points.at(-1)?.resources?.cpu_percent,'%')} · Restart count: {points.at(-1)?.restarts?.[target]??'—'} · Detection: {metric(view?.detection_time,'s')} · Recovery: {metric(view?.recovery_time,'s')}</div>
        {!data.active&&view&&<button className="text-button" onClick={()=>{setSelected('live');}}>Show recent live telemetry</button>}
      </section>
      <div className="main-grid"><section className="panel"><Heading icon={<Network size={18}/>} title="Application dependencies" detail="Declared topology with live container states"/><Topology services={data.services}/></section><section className="panel"><Heading icon={<Bot size={18}/>} title="Experiment lifecycle" detail="Recorded decisions, actions and verification"/><Lifecycle view={view}/><div className="message-box" role="status">{message}</div></section></div>
      <div className="lower-grid"><section className="panel"><Heading icon={<Clock3 size={18}/>} title="Experiment timeline" detail="Audit events belong to the selected experiment"/><div className="timeline">{events.length?[...events].reverse().map((e,i)=><div className="timeline-item" key={i}><i className={`timeline-marker ${e.level}`}/><div className="timeline-content"><div className="timeline-top"><strong>{e.agent}</strong><time>{new Date(e.time).toLocaleTimeString()}</time></div>{e.message}</div></div>):<div className="empty-state">No events for this selection.</div>}</div></section>
      <section className="panel"><Heading icon={<ShieldCheck size={18}/>} title="Diagnosis and recovery" detail={diagnosis?.mode==='gemini'?`Gemini · ${diagnosis.model} · #${view.id}`:diagnosis?'AI unavailable · deterministic containment only':'Awaiting a validated diagnosis for this experiment'}/><DiagnosisReport view={view} diagnosis={diagnosis}/></section></div>
      <section className="panel"><Heading icon={<Activity size={18}/>} title="Baseline and impact comparison" detail={baseline?`${baseline.sample_count??baseline.samples} samples · ${baseline.observation_duration_seconds}s · ${new Date(baseline.finished_at).toLocaleString()}`:'A persistent baseline has not been collected'}/><div className="history-table"><div className="comparison-row history-head"><span>Phase</span><span>Availability</span><span>Median / p95</span><span>Errors</span></div>{[['Baseline',baseline?.[target]],['Fault',view?.impact?.fault?.[target]],['Recovery',view?.impact?.recovery?.[target]]].map(([name,m])=><div className="comparison-row" key={name}><strong>{name}</strong><span>{metric(m?.availability_percent,'%')}</span><span>{metric(m?.latency_ms?.median)} / {metric(m?.latency_ms?.p95)} ms</span><span>{metric(m?.error_rate_percent,'%')}</span></div>)}</div><p className="panel-subtitle">Successful probe latency distribution; failed probes count as errors, never as zero latency. {baseline?.outlier_policy}</p></section>
      {view?.analysis&&<section className="panel analysis-panel"><Heading icon={<Zap size={18}/>} title={`Post-analysis · ${view.severity}`} detail={`${view.analysis.mode==='gemini'?'Gemini':'AI unavailable: deterministic summary'} · ${view.resilience_score?.scope}`}/><p>{view.analysis.recovery_effectiveness}</p><div className="score-breakdown">{Object.entries(view.resilience_score?.penalties??{}).map(([k,v])=><span key={k}>{k.replaceAll('_',' ')}: −{v}</span>)}</div><p className="panel-subtitle">{view.resilience_score?.formula}</p><strong>Gemini hypotheses to validate</strong><ol>{(view.action_review?.mode==='gemini'?[view.action_review.root_cause]:view.analysis.weaknesses).map((w,i)=><li key={i}>{w}</li>)}</ol><strong>Recommendations to evaluate</strong><ul>{(view.action_review?.mode==='gemini'?view.action_review.improvement_plan.map(p=>`${p.priority}: ${p.title}. Validate: ${p.verification}`):view.analysis.remediation).map((w,i)=><li key={i}>{w}</li>)}</ul></section>}
      {weaknesses.length>0&&<section className="panel"><Heading icon={<Zap size={18}/>} title="System weakness ranking" detail={systemAnalysis.ranking_method??'Measured severity and dependency criticality'}/><h3>Overall System Resilience Score: {systemAnalysis.overall_score??'—'} / 100</h3><p className="panel-subtitle">Coverage: {systemAnalysis.coverage?.measured_fault_types??0} / {systemAnalysis.coverage?.available_fault_types??5} single fault types. {systemAnalysis.score_method}</p>{weaknesses.map((w,i)=><article key={w.experiment_id}><div className="agent-row"><strong>{i+1}. {w.name}</strong><span>{w.severity} · {w.score}/100 · #{w.experiment_id}</span></div><p>{w.justification}</p><p className="panel-subtitle">Remediation source: {w.remediation_source}</p>{w.combination_comparison&&<p className="panel-subtitle">Single-fault references: {w.combination_comparison.single_fault_references.map(r=>`${r.experiment_type} #${r.experiment_id}: ${r.score}/100`).join("; ")||"Not yet measured"}. Score difference from their mean: {w.combination_comparison.score_delta_vs_single_mean??"Needs both single-fault reports"}. {w.combination_comparison.scope}</p>}<ul>{w.remediation?.map((fix,j)=><li key={j}>{typeof fix==='string'?fix:`${fix.title}. Validate: ${fix.verification}`}</li>)}</ul></article>)}</section>}
      <section className="panel history-panel"><Heading icon={<Clock3 size={18}/>} title="Experiment history" detail="Select a saved result to inspect its complete evidence and curve"/>{history.map(h=><button className={`history-row history-button ${selected===h.id?'selected':''}`} key={h.id} disabled={data.active} onClick={()=>setSelected(h.id)}><span>{h.result?.name??h.result?.experiment_type??(h.target==='ledgerwriter-network'?'Network slowdown':'Legacy outage')} · #{h.id}</span><span>{h.target}</span><span>{new Date(h.started_at).toLocaleString()}</span><span>{h.status} · {h.result?.resilience_score?.value??'—'}/100</span></button>)}</section>
      <footer>Local-only control plane · 5 sequential bounded faults · Healthy standby protection · AI advice and automatic controls labeled separately · Sampled demo flows</footer>
    </main></div>;
}
function Stat({label,value,detail}){return <div className="stat-card"><div className="stat-label">{label}</div><div className="stat-value small-value">{value}</div><div className="stat-foot">{detail}</div></div>;}
function Heading({icon,title,detail}){return <div className="panel-heading"><div><div className="panel-title">{icon}{title}</div><div className="panel-subtitle">{detail}</div></div></div>;}
const colors={idle:'#65dfb2',baseline:'#65dfb2',fault:'#ff7982',intervention:'#e8bd6c',recovery:'#70baff'};
function Chart({points,title,unit,value,baseline,threshold,max,events=[]}){
  const w=560,h=200,pad=38;
  const records=points.map(p=>({time:Date.parse(p.time),y:value(p),phase:p.phase})).filter(p=>Number.isFinite(p.time));
  const ys=records.filter(p=>p.y!=null&&Number.isFinite(p.y)).map(p=>p.y);
  const maxY=max??Math.max(1,...ys,baseline??0,threshold??0)*1.15;
  const start=records[0]?.time??0,end=Math.max(start+1000,records.at(-1)?.time??1000);
  const x=t=>pad+(t-start)/(end-start)*(w-pad*2),y=v=>h-pad-v/maxY*(h-pad*2);
  return <div className="chart-wrap"><div className="chart-header"><strong>{title}</strong><span>{metric(records.at(-1)?.y,` ${unit}`)}</span></div><svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`${title} over time`}>
    {[0,.5,1].map(f=><g key={f}><line x1={pad} x2={w-pad} y1={y(f*maxY)} y2={y(f*maxY)} stroke="#293648"/><text x={pad-5} y={y(f*maxY)+4} textAnchor="end" fill="#8fa0b6" fontSize="10">{(f*maxY).toFixed(0)}</text></g>)}
    {records.map((r,i)=><rect key={i} x={x(r.time)} y={pad} width={Math.max(1,x(records[i+1]?.time??end)-x(r.time))} height={h-pad*2} fill={colors[r.phase]??'#657286'} opacity=".07"/>)}
    {baseline!=null&&<line x1={pad} x2={w-pad} y1={y(baseline)} y2={y(baseline)} stroke="#65dfb2" strokeDasharray="5 5"/>}
    {threshold!=null&&<line x1={pad} x2={w-pad} y1={y(threshold)} y2={y(threshold)} stroke="#ff7982" strokeDasharray="3 4"><title>Critical policy limit: {threshold} {unit}</title></line>}
    {events.filter(e=>['fault-start','threshold-breach','kill-switch','recovery-started','recovered'].includes(e.kind)).map((e,i)=><line key={i} x1={x(Date.parse(e.time))} x2={x(Date.parse(e.time))} y1={pad} y2={h-pad} stroke="#e8bd6c" strokeDasharray="2 5"><title>{e.kind} · {e.time}</title></line>)}
    {records.map((r,i)=>r.y!=null&&Number.isFinite(r.y)?<g key={i}>{i>0&&records[i-1].y!=null&&<line x1={x(records[i-1].time)} y1={y(records[i-1].y)} x2={x(r.time)} y2={y(r.y)} stroke={colors[r.phase]??'#65dfb2'} strokeWidth="2"/>}<circle cx={x(r.time)} cy={y(r.y)} r="2.5" fill={colors[r.phase]??'#65dfb2'}><title>{new Date(r.time).toLocaleTimeString()} · {r.phase} · {metric(r.y,unit)}</title></circle></g>:null)}
    {!ys.length&&<text x={w/2} y={h/2} textAnchor="middle" fill="#8fa0b6" fontSize="13">Awaiting measurements</text>}
    <text x={pad} y={h-10} fill="#8fa0b6" fontSize="10">{records.length?new Date(start).toLocaleTimeString():''}</text><text x={w-pad} y={h-10} textAnchor="end" fill="#8fa0b6" fontSize="10">{records.length?`${((end-start)/1000).toFixed(1)}s elapsed`:''}</text>
  </svg><div className="chart-footer">{baseline!=null?`Baseline median ${metric(baseline,unit)} · `:''}{ys.length} measured points · peak {metric(ys.length?Math.max(...ys):null,unit)}{threshold!=null?` · critical ${metric(threshold,unit)}`:''}</div></div>;
}
function Topology({services}){
 const nodes=[['frontend',340,35],['userservice',60,135],['contacts',190,135],['balancereader',320,135],['ledgerwriter',450,135],['transactionhistory',590,135],['accounts-db',130,240],['ledger-db',460,240]];
 const edges=nodes.slice(1,6).map(([n])=>['frontend',n]).concat([['userservice','accounts-db'],['contacts','accounts-db'],['balancereader','ledger-db'],['ledgerwriter','ledger-db'],['ledgerwriter','balancereader'],['transactionhistory','ledger-db']]);
 return <svg viewBox="0 0 680 290" role="img" aria-label="Bank of Anthos service dependency graph"><defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L6,3z" fill="#5b7189"/></marker></defs>{edges.map(([a,b])=>{const from=nodes.find(n=>n[0]===a),to=nodes.find(n=>n[0]===b);const horizontal=from[2]===to[2];return <path key={a+b} d={horizontal?`M ${from[1]-56},${from[2]} L ${to[1]+56},${to[2]}`:`M ${from[1]},${from[2]+25} L ${to[1]},${to[2]-25}`} stroke="#5b7189" fill="none" markerEnd="url(#arrow)"/>;})}{nodes.map(([name,x,y])=>{const s=services.find(s=>s.service===name)?.status??'unknown';const color=s==='running'?'#65dfb2':s==='unknown'?'#8fa0b6':'#ff7982';return <g key={name}><rect x={x-58} y={y-23} width="116" height="46" rx="8" fill="#141f2d" stroke={color}/><text x={x} y={y-3} textAnchor="middle" fontSize="10" fill="#e8edf5">{name}</text><text x={x} y={y+13} textAnchor="middle" fontSize="9" fill={color}>{s}</text></g>;})}</svg>;
}
createRoot(document.getElementById('root')).render(<App/>);
