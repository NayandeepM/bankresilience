import React from 'react';

const number=(v,unit='')=>v==null?'—':`${Number(v).toFixed(1)}${unit}`;
export function SuitePanel({suite,suites,running,disabled,protection,setProtection,payments,setPayments,paymentBudget,setPaymentBudget,onRun,onCancel,onSelect,onExperiment,resilience,onEnable,onDisable}){
  return <section className="panel suite-panel">
    <div className="panel-heading"><div><div className="panel-title">Experiment Suite Agent</div><p className="panel-subtitle">Runs all five faults one at a time, with a fresh baseline and verified recovery between experiments.</p></div></div>
    <div className="suite-controls">
      <label><input type="checkbox" checked={protection} disabled={running||disabled} onChange={e=>setProtection(e.target.checked)}/> Keep traffic usable with the Resilience Agent</label>
      <label><input type="checkbox" checked={payments} disabled={running||disabled} onChange={e=>setPayments(e.target.checked)}/> Verify demo payments ($0.01 each, at most 15 per suite)</label>
      <label>Maximum demo payments <input aria-label="Maximum demo payments" type="number" min="0" max="15" value={paymentBudget} disabled={running||disabled||!payments} onChange={e=>setPaymentBudget(Math.max(0,Math.min(15,Number(e.target.value))))}/></label>
      <p className="panel-subtitle">Authenticated home-page checks run with every suite. Demo payments are optional, use the local demo account and are never automatically retried.</p>
      <div className="hero-actions"><button className="primary-button" disabled={disabled} onClick={()=>onRun(false)}>Run all faults</button><button className="primary-button" disabled={disabled} onClick={()=>onRun(true)}>Run fault combinations</button>{running&&<button className="danger-button" onClick={onCancel}>Stop suite and restore service</button>}</div>
      <p className="panel-subtitle">Combinations overlap network slowdown + CPU stress, then packet loss + CPU stress. The agent verifies recovery between pairs and saves a report for each.</p>
    </div>
    <div className="resilience-status"><strong>Resilience Agent: {resilience?.enabled?'enabled':'not enabled'}</strong><p className="panel-subtitle">Healthy standby instances protect LedgerWriter and Contacts. The original fault remains active for a bounded test window; recovery restores the original instance afterward.</p>
      {Object.entries(resilience?.targets??{}).map(([target,r])=><div className="agent-row" key={target}><strong>{target}</strong><span>{r.available?'Protected route healthy':'Route unavailable'} · {r.route??'unknown'} instance</span></div>)}
      <button className="text-button" disabled={disabled} onClick={resilience?.enabled?onDisable:onEnable}>{resilience?.enabled?'Disable protection and restore original routing':'Enable protection'}</button>
    </div>
    {suite&&<div className="suite-result"><h3>Suite #{suite.id} · {suite.status}</h3><p className="panel-subtitle">Demo payments attempted: {suite.payments_attempted??"in progress"} / {suite.verify_payments?(suite.payment_budget??15):0}</p><p role="status">{running?`${suite.current_index} / ${suite.order.length} · ${suite.phase}`:`${suite.results.length} / ${suite.order.length} experiments recorded`}</p>{suite.stop_reason&&<p className="message-box">{suite.stop_reason}</p>}
      <div className="suite-results">{suite.results.map(r=><article className="suite-fault" key={r.experiment_id}>
        <h3>{r.name}</h3><p>{r.recovery_verified?'Recovery verified':'Recovery not verified'} · {r.tolerance_verified?'Tolerance demonstrated while fault active':'Tolerance not demonstrated'}</p>
        {r.components?.length>0&&<p>Concurrent faults: {r.components.join(' + ')}. Cleanup: {r.component_actions?.map(a=>`${a.action}: ${a.verified?'verified':'failed'}`).join(' · ')||'Awaiting verification'}.</p>}
        <dl><dt>Primary baseline / fault p95</dt><dd>{number(r.baseline?.latency_ms?.p95)} / {number(r.fault?.latency_ms?.p95)} ms</dd>
        <dt>Primary fault availability</dt><dd>{number(r.fault?.availability_percent,'%')}</dd>
        <dt>Protected route availability / p95</dt><dd>{number(r.protected_route?.availability_percent,'%')} / {number(r.protected_route?.latency_ms?.p95,' ms')}</dd>
        <dt>Measured tolerance with fault active</dt><dd>{number(r.fault_active_tolerance_seconds,' s')} · {r.tolerated_observations} observations</dd>
        <dt>Detection / primary recovery</dt><dd>{number(r.detection_seconds,' s')} / {number(r.recovery_seconds,' s')}</dd>
        <dt>Authenticated home success</dt><dd>{number(r.business_during_fault?.home?.availability_percent,'%')} · {r.business_during_fault?.home?.sample_count??0} samples</dd>
        <dt>Demo payment success</dt><dd>{number(r.business_during_fault?.payments?.availability_percent,'%')} · {r.business_during_fault?.payments?.sample_count??0} samples</dd>
        <dt>Resilience score</dt><dd>{r.score??'—'} / 100 · primary-probe score</dd></dl>
        {r.resilience_decision&&<p><strong>{r.resilience_decision.mode==='gemini'?'Gemini resilience advice':'Deterministic resilience advice'}:</strong> {r.resilience_decision.rationale}<br/>{r.resilience_decision.execution}</p>}
        <p className="panel-subtitle">Automatic routing performs failover. Sampled application success does not establish every transaction's accounting correctness.</p>
        <button className="text-button" disabled={running} onClick={()=>onExperiment(r.experiment_id)}>Inspect experiment #{r.experiment_id}</button>
      </article>)}</div>
      {suite.summary&&<div className="suite-summary"><h3>Final summary · {suite.summary.mode==='gemini'?'Gemini':'Measured fallback'}</h3><p>{suite.measured_summary?.headline}</p><p>{suite.summary.headline}</p><ul>{suite.summary.findings.map((f,i)=><li key={i}>{f}</li>)}</ul><strong>Next steps</strong><ul>{suite.summary.next_steps.map((f,i)=><li key={i}>{f}</li>)}</ul></div>}
    </div>}
    {suites.length>0&&<details><summary>Saved suite reports ({suites.length})</summary>{suites.map(s=><button className="history-row history-button" disabled={running} key={s.id} onClick={()=>onSelect(s.id)}><span>Suite #{s.id}</span><span>{s.status}</span><span>{s.results.length} / {s.order.length}</span></button>)}</details>}
  </section>;
}

export function ToleranceReport({view}){
  const r=view?.tolerance_report;
  if(!r||!view.resilience_enabled)return null;
  return <div className="tolerance-report"><strong>Resilience Agent · {r.tolerance_verified?'tolerance demonstrated':'tolerance not demonstrated'}</strong><p>Protected route: {number(r.protected_route.availability_percent,'%')} availability · {number(r.protected_route.latency_ms.p95,' ms')} p95. {r.tolerated_observations} standby observations with the primary fault active, spanning {number(r.fault_active_tolerance_seconds,' s')}.</p><p className="panel-subtitle">The primary's charts below still show the fault. Protection uses a healthy standby; recovery later restores the primary.</p></div>;
}
