import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, AlertTriangle, Bot, CheckCircle2, Circle, Clock3, Database, HeartPulse, Network, Play, RefreshCw, ShieldCheck, Terminal, Zap } from 'lucide-react';
import './style.css';

const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';
const fallbackExperiments = [
  { id: 'ledgerwriter-outage', name: 'LedgerWriter outage', target: 'ledgerwriter', failure_type: 'crash', summary: 'Stop ledgerwriter for 12 seconds, then restart it.' },
  { id: 'contacts-pause', name: 'Contacts slowdown/hang', target: 'contacts', failure_type: 'slowdown/hang', summary: 'Pause contacts for 12 seconds, then unpause it.' },
  { id: 'transactionhistory-network-disconnect', name: 'TransactionHistory network failure', target: 'transactionhistory', failure_type: 'network failure', summary: 'Disconnect transactionhistory from its Compose network for 12 seconds, then reconnect it.' },
  { id: 'edge-timeout', name: 'Request timeout injection', target: 'local proxy', failure_type: 'timeout', summary: 'Hold traffic through a local proxy and return 504 for a 12-second fault window.' }
];

function App() {
  const [data, setData] = useState({ services: [], events: [], active: false });
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState('Connect the control plane to inspect the local application.');
  const [history, setHistory] = useState([]);
  const [experiments, setExperiments] = useState(fallbackExperiments);
  const refresh = async () => {
    try {
      const [s, h, hist, exp] = await Promise.all([
        fetch(`${API}/api/state`).then(r => r.json()),
        fetch(`${API}/api/health`).then(r => r.json()),
        fetch(`${API}/api/history`).then(r => r.json()),
        fetch(`${API}/api/experiments`).then(r => r.json())
      ]);
      setData(s); setHealth({...h, api_connected: true}); setHistory(hist); setExperiments(exp);
    } catch { setHealth({ ok: false, docker_connected: false, api_connected: false }); }
  };
  useEffect(() => { refresh(); const id = setInterval(refresh, 1500); return () => clearInterval(id); }, []);
  const run = async (experiment) => {
    if (!window.confirm(`Run ${experiment.name}? This will inject a ${experiment.failure_type} fault into ${experiment.target} for up to 12 seconds, then attempt automatic recovery.`)) return;
    setLoading(true); setMessage('Running preflight safety checks…');
    try {
      const res = await fetch(`${API}/api/experiments/${experiment.id}`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({confirm:true}) });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not start experiment');
      setMessage(`Experiment ${body.id} started. Watch the event timeline below.`);
      await refresh();
    } catch (e) { setMessage(e.message); }
    finally { setLoading(false); }
  };
  const services = data.services || [];
  const statusOf = name => services.find(s => s.service === name)?.status || 'unknown';
  const healthClass = s => s === 'running' ? 'ok' : s === 'exited' ? 'bad' : 'unknown';
  const nodeNames = ['Frontend','UserService','Contacts','BalanceReader','LedgerWriter','TransactionHistory','Accounts DB','Ledger DB'];
  const displayName = n => ({'Accounts DB':'accounts-db','Ledger DB':'ledger-db','UserService':'userservice','BalanceReader':'balancereader','LedgerWriter':'ledgerwriter','TransactionHistory':'transactionhistory'}[n] || n.toLowerCase());
  const current = data.experiment;
  const liveTimeoutMetrics = data.active && current?.failure_type === 'timeout' ? current.live_metrics : null;
  const timeoutResult = data.last_result?.failure_type === 'timeout' ? data.last_result : null;
  const timeoutPhases = timeoutResult?.phases;
  const timeout504 = timeoutPhases?.during?.status_counts?.['504'] || 0;
  const measuredTimeoutDiagnosis = timeoutPhases && timeoutResult.diagnosis && !('confidence' in timeoutResult.diagnosis)
    ? timeoutResult.diagnosis : timeoutPhases ? {
    root_cause: timeout504 === timeoutPhases.during.requests && timeout504 > 0
      ? `All ${timeout504} fault-window probes returned HTTP 504.`
      : `${timeout504} of ${timeoutPhases.during.requests} fault-window probes returned HTTP 504.`,
    evidence: [
      `Before: ${timeoutPhases.before.requests} probes, errors: ${timeoutPhases.before.errors}, average latency: ${timeoutPhases.before.latency_avg_ms} ms.`,
      `During: ${timeoutPhases.during.requests} probes, errors: ${timeoutPhases.during.errors}, average latency: ${timeoutPhases.during.latency_avg_ms} ms.`,
      `After: ${timeoutPhases.after.requests} probes, errors: ${timeoutPhases.after.errors}, average latency: ${timeoutPhases.after.latency_avg_ms} ms.`,
      timeoutResult.time_to_recover_seconds != null ? `First healthy response ${timeoutResult.time_to_recover_seconds} s after clearing the fault.` : 'No healthy post-fault response was observed.',
      ...(timeoutResult.error ? [`Experiment warning: ${timeoutResult.error}`] : []),
    ],
    recommended_action: timeoutResult.recovered
      ? 'All post-fault probes succeeded. Keep monitoring the upstream for application-level recovery.'
      : 'Recovery was not verified. Check the upstream, proxy, and failed probe details before rerunning.',
  } : null;
  const diagnosis = measuredTimeoutDiagnosis || current?.diagnosis || current?.result?.diagnosis || data.last_result?.diagnosis;
  const ledgerwriterExperiment = experiments.find(experiment => experiment.id === 'ledgerwriter-outage');
  const contactsExperiment = experiments.find(experiment => experiment.id === 'contacts-pause');
  const networkExperiment = experiments.find(experiment => experiment.id === 'transactionhistory-network-disconnect');
  const timeoutExperiment = experiments.find(experiment => experiment.id === 'edge-timeout');
  const canRun = experiment => !!experiment && !loading && !data.active && (experiment.id === 'edge-timeout' ? health?.api_connected : health?.docker_connected);
  return <div className="app-shell">
    <header className="topbar"><div className="brand"><div className="brand-icon"><ShieldCheck size={22}/></div><div><div className="brand-name">BankResilience</div><div className="brand-sub">AUTONOMOUS RELIABILITY CONTROL PLANE</div></div></div><div className="top-right"><span className={`connection-dot ${health?.api_connected ? 'live' : ''}`}></span><span>{health?.api_connected ? 'Control plane connected' : 'Control plane disconnected'}</span><button className="icon-button" onClick={refresh} title="Refresh"><RefreshCw size={16}/></button></div></header>
    <main>
      <section className="hero"><div className="hero-copy"><div className="eyebrow">LOCAL LAB / BANK OF ANTHOS</div><h1>Resilience control center</h1><p>Inject a controlled fault, investigate the blast radius, and verify recovery on the running application.</p></div><div className="hero-actions"><div className="safe-pill"><ShieldCheck size={18}/> Guardrails enabled</div><button className="primary-button hero-run" disabled={!canRun(ledgerwriterExperiment)} onClick={() => run(ledgerwriterExperiment)}><Play size={18} fill="currentColor"/>Run LedgerWriter experiment</button><button className="primary-button hero-run" disabled={!canRun(contactsExperiment)} onClick={() => run(contactsExperiment)}><Network size={19}/>Run Contacts slowdown</button><button className="primary-button hero-run" disabled={!canRun(networkExperiment)} onClick={() => run(networkExperiment)}><Network size={19}/>Run Network failure</button><button className="primary-button hero-run" disabled={!canRun(timeoutExperiment)} onClick={() => run(timeoutExperiment)}><Clock3 size={19}/>Run Timeout injection</button></div></section>
      <section className="stats-grid">
        <div className="stat-card"><div className="stat-label">COMPOSE SERVICES</div><div className="stat-value">{services.filter(s=>s.status==='running').length}<span> / {services.filter(s=>s.service!=='Docker').length || 8}</span></div><div className="stat-foot"><HeartPulse size={14}/> live container state</div></div>
        <div className="stat-card"><div className="stat-label">CURRENT EXPERIMENT</div><div className="stat-value small-value">{data.active ? 'IN PROGRESS' : current?.status || 'Idle'}</div><div className="stat-foot"><Activity size={14}/> {current?.name || 'No active experiment'}</div></div>
        <div className="stat-card"><div className="stat-label">LAST RECOVERY</div><div className="stat-value">{timeoutResult ? (timeoutResult.time_to_recover_seconds != null ? `${timeoutResult.time_to_recover_seconds}s` : '—') : data.last_result?.recovery_duration_seconds != null ? `${data.last_result.recovery_duration_seconds}s` : '—'}</div><div className="stat-foot"><RefreshCw size={14}/> {timeoutResult ? 'proxy recovery check' : 'container + frontend check'}</div></div>
        <div className="stat-card"><div className="stat-label">DOCKER ENGINE</div><div className={`stat-value small-value ${health?.docker_connected?'green-text':'red-text'}`}>{health?.docker_connected?'CONNECTED':'OFFLINE'}</div><div className="stat-foot"><Circle size={9} fill="currentColor"/> Loopback control API</div></div>
      </section>
      <section className="panel experiments-panel"><div className="panel-heading"><div><div className="panel-title"><Zap size={18}/> Experiments</div><div className="panel-subtitle">Four bounded failure types with automatic recovery</div></div><span className="tag">12 SECOND WINDOW</span></div>
        <div className="experiment-grid">{experiments.map(experiment => <div className="experiment-card" key={experiment.id}><div className="experiment-type">{experiment.failure_type}</div><h3>{experiment.name}</h3><p>{experiment.summary}</p><div className="experiment-meta"><span>Target</span><strong>{experiment.target}</strong></div><button className="primary-button experiment-run" disabled={!canRun(experiment)} onClick={() => run(experiment)}><Play size={15} fill="currentColor"/>{data.active ? 'Experiment running' : loading ? 'Starting…' : 'Run'}</button></div>)}</div>
      </section>
      {liveTimeoutMetrics && <section className="panel history-panel"><div className="panel-heading"><div><div className="panel-title"><Activity size={18}/> Live timeout measurements</div><div className="panel-subtitle">Observability Agent · {current.phase === 'baseline' ? 'Baseline' : current.phase === 'fault-injected' ? 'Fault active' : 'Recovery checks'}</div></div><span className="tag">LIVE</span></div><div className="live-timeout-grid"><LivePhase name="Before" metrics={liveTimeoutMetrics.before}/><LivePhase name="During" metrics={liveTimeoutMetrics.during} target={12}/><LivePhase name="After" metrics={liveTimeoutMetrics.after}/></div></section>}
      <div className="main-grid">
        <section className="panel topology-panel"><div className="panel-heading"><div><div className="panel-title"><Network size={18}/> Service dependency map</div><div className="panel-subtitle">Declared application and database relationships</div></div><span className="tag">LIVE STATUS</span></div>
          <div className="topology"><div className="node-row one"><Node name="Frontend" service="frontend" status={statusOf('frontend')} icon={<Activity size={17}/>} /></div><div className="connector-label">API calls</div><div className="node-row two">{['UserService','Contacts','BalanceReader','LedgerWriter','TransactionHistory'].map(n=><Node key={n} name={n} service={displayName(n)} status={statusOf(displayName(n))} icon={n==='LedgerWriter'?<Zap size={17}/>:<Bot size={17}/>} focus={n==='LedgerWriter'}/>)}</div><div className="connector-label">Data / service dependencies</div><div className="node-row dbs"><Node name="Accounts DB" service="accounts-db" status={statusOf('accounts-db')} icon={<Database size={17}/>}/><Node name="Ledger DB" service="ledger-db" status={statusOf('ledger-db')} icon={<Database size={17}/>}/></div></div>
          <div className="legend"><span><i className="legend-dot green"></i> Running</span><span><i className="legend-dot red"></i> Stopped / failed</span><span><i className="legend-dot gray"></i> Unknown</span></div>
        </section>
        <section className="panel agent-panel"><div className="panel-heading"><div><div className="panel-title"><Bot size={18}/> Agent status</div><div className="panel-subtitle">Coordinated experiment lifecycle</div></div></div><div className="agent-list"><Agent icon={<Zap size={17}/>} name="Chaos Agent" desc="Injects allowlisted fault" active={data.active && ['running','fault-injected'].includes(current?.phase)} status={data.active?'Ready':'Standby'}/><Agent icon={<Activity size={17}/>} name="Observability Agent" desc="Captures state and evidence" active={data.active} status={data.active?'Collecting':'Standby'}/><Agent icon={<Bot size={17}/>} name="Diagnosis Agent" desc="Evidence-based root cause" active={!!diagnosis} status={diagnosis?'Diagnosis available':'Waiting'}/><Agent icon={<RefreshCw size={17}/>} name="Recovery Agent" desc="Restarts and verifies service" active={data.active || !!data.last_result?.recovery_verified} status={data.active?'Monitoring':data.last_result?.recovery_verified?'Recovered':'Standby'}/></div><div className="message-box"><div className="message-title"><Terminal size={15}/> CONTROL MESSAGE</div>{message}</div></section>
      </div>
      <div className="lower-grid"><section className="panel"><div className="panel-heading"><div><div className="panel-title"><Clock3 size={18}/> Experiment timeline</div><div className="panel-subtitle">Agent events in chronological order</div></div><span className="count-tag">{(data.events||[]).length} events</span></div><div className="timeline">{(data.events||[]).length ? [...data.events].reverse().map((e,i)=><div className="timeline-item" key={e.time+i}><div className={`timeline-marker ${e.level||''}`}></div><div className="timeline-content"><div className="timeline-top"><strong>{e.agent}</strong><time>{new Date(e.time).toLocaleTimeString()}</time></div><div>{e.message}</div></div></div>) : <div className="empty-state"><Clock3 size={24}/><strong>No experiment events yet</strong><span>Run an experiment to populate the timeline.</span></div>}</div></section>
      <section className="panel"><div className="panel-heading"><div><div className="panel-title"><AlertTriangle size={18}/> Diagnosis & recovery</div><div className="panel-subtitle">Latest evidence and verification</div></div></div>{diagnosis ? <div className="diagnosis"><div className="diagnosis-status"><AlertTriangle size={17}/><span>{timeoutResult ? 'MEASURED TIMEOUT OUTCOME' : 'ROOT CAUSE IDENTIFIED'}</span><strong>{timeoutResult ? `${timeout504}/${timeoutPhases?.during?.requests || 0} HTTP 504` : `${Math.round((diagnosis.confidence||0)*100)}% confidence`}</strong></div><h3>{diagnosis.root_cause}</h3><div className="evidence-title">Supporting evidence</div><ul>{(diagnosis.evidence||[]).map((e,i)=><li key={i}>{e}</li>)}</ul><div className="fix-box"><CheckCircle2 size={17}/><div><strong>Recommended recovery</strong><p>{diagnosis.recommended_action}</p></div></div>{data.last_result && <div className={`verification ${data.last_result.recovery_verified?'verified':'pending'}`}>{timeoutResult ? (timeoutResult.recovered ? 'Proxy recovery checks passed' : 'Proxy recovery could not be verified.') : data.last_result.recovery_verified ? 'Container + frontend checks passed' : 'Transaction-level recovery still requires verification in the banking UI.'}</div>}</div> : <div className="empty-state"><ShieldCheck size={24}/><strong>Awaiting diagnostic evidence</strong><span>The Diagnosis Agent reports only after an experiment produces evidence.</span></div>}</section></div>
      {timeoutResult && <section className="panel history-panel"><div className="panel-heading"><div className="panel-title"><Clock3 size={18}/> Timeout results</div></div><div className="diagnosis"><ul><li>Requests: {timeoutResult.requests}</li><li>Errors: {timeoutResult.errors}</li><li>Error rate: {Math.round((timeoutResult.error_rate || 0) * 100)}%</li><li>Baseline latency: {timeoutResult.baseline_latency_ms ?? '—'} ms</li><li>During fault: {timeoutResult.latency_during_ms?.avg ?? '—'} ms average, {timeoutResult.latency_during_ms?.max ?? '—'} ms maximum</li><li>Time to recover: {timeoutResult.time_to_recover_seconds ?? 'Not verified'} s</li></ul></div></section>}
      <section className="panel history-panel"><div className="panel-heading"><div><div className="panel-title"><Clock3 size={18}/> Recent experiments</div><div className="panel-subtitle">Saved locally in SQLite</div></div></div>{history.length ? <div className="history-table"><div className="history-row history-head"><span>Experiment</span><span>Target</span><span>Started</span><span>Result</span></div>{history.map(h=><div className="history-row" key={h.id}><span>#{h.id} {h.result?.name || 'Experiment'}</span><span>{h.target}</span><span>{new Date(h.started_at).toLocaleString()}</span><span className={h.result?.recovery_verified?'green-text':'amber-text'}>{h.result?.recovery_verified?'Recovery checks passed':'Needs verification'}</span></div>)}</div>:<div className="history-empty">Experiment results will be recorded here.</div>}</section>
      <footer><span>BankResilience MVP · Local-only control plane</span><span>Fault target allowlist: <b>ledgerwriter, contacts, transactionhistory</b> · Max fault window: <b>12 seconds</b></span></footer>
    </main>
  </div>;
}
function Node({name, service, status, icon, focus}) { const cls = status==='running'?'ok':status==='exited'?'bad':'unknown'; return <div className={`service-node ${cls} ${focus?'focus':''}`} title={`${service}: ${status}`}><div className="node-icon">{icon}</div><div className="node-name">{name}</div><div className="node-status"><i></i>{status}</div></div>; }
function Agent({icon,name,desc,active,status}) {return <div className="agent-row"><div className={`agent-icon ${active?'active':''}`}>{icon}</div><div className="agent-copy"><strong>{name}</strong><span>{desc}</span></div><span className={`agent-state ${active?'active':''}`}>{status}</span></div>;}
function LivePhase({name, metrics, target}) {return <div className="live-timeout-phase"><strong>{name}</strong><span>{metrics?.requests || 0}{target ? ` / ${target}` : ''} completed</span><span>{metrics?.status_counts?.['504'] || 0} HTTP 504 · {metrics?.errors || 0} errors</span><span>{metrics?.latency_avg_ms ?? '—'} ms average latency</span></div>;}

createRoot(document.getElementById('root')).render(<App/>);
