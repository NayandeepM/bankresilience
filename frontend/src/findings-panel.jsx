import React,{useEffect,useState} from 'react';
import {FileText,Download,RefreshCw,Lightbulb} from 'lucide-react';

function Inline({text}){
  return String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part,i)=>part.startsWith('**')?<strong key={i}>{part.slice(2,-2)}</strong>:part.startsWith('`')?<code key={i}>{part.slice(1,-1)}</code>:part);
}
function MarkdownReport({text}){
  const lines=text.split('\n'),blocks=[];
  for(let i=0;i<lines.length;i++){
    const line=lines[i];if(!line.trim())continue;
    if(line.startsWith('|')){
      const rows=[];while(i<lines.length&&lines[i].startsWith('|')){rows.push(lines[i].split('|').slice(1,-1).map(c=>c.trim()));i++;}i--;
      blocks.push(<div className="findings-table-scroll" key={i}><table><thead><tr>{rows[0].map((c,j)=><th key={j}><Inline text={c}/></th>)}</tr></thead><tbody>{rows.slice(2).map((row,j)=><tr key={j}>{row.map((c,k)=><td key={k}><Inline text={c}/></td>)}</tr>)}</tbody></table></div>);
    }else if(line.startsWith('- ')){
      const items=[];while(i<lines.length&&lines[i].startsWith('- ')){items.push(lines[i].slice(2));i++;}i--;
      blocks.push(<ul key={i}>{items.map((t,j)=><li key={j}><Inline text={t}/></li>)}</ul>);
    }else if(/^#{1,6} /.test(line)){
      const depth=line.match(/^#+/)[0].length,Tag=depth<3?'h3':'h4';blocks.push(<Tag key={i}>{line.replace(/^#+ /,'')}</Tag>);
    }else if(line.startsWith('> '))blocks.push(<blockquote key={i}><Inline text={line.slice(2)}/></blockquote>);
    else blocks.push(<p key={i}><Inline text={line}/></p>);
  }
  return <div className="findings-document">{blocks}</div>;
}
export function FindingsPanel({apiBase,disabled}){
  const [report,setReport]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false),[open,setOpen]=useState(false);
  async function load(generate=false){
    if(generate)setLoading(true);
    try{
      const response=await fetch(`${apiBase}/api/findings`,generate?{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',cache:'no-store'}:{cache:'no-store'});
      if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail??'Findings access is unavailable. Restart the backend if it has not loaded this update.');}
      setReport(await response.json());setError('');
    }catch(e){setError(e.message);}finally{if(generate)setLoading(false);}
  }
  useEffect(()=>{load();const timer=setInterval(()=>load(),15000);return()=>clearInterval(timer);},[apiBase]);
  return <section className="panel findings-panel" aria-labelledby="findings-title">
    <div className="panel-heading"><div><div className="panel-title" id="findings-title"><FileText size={18}/> Findings report & system improvements</div><p className="panel-subtitle">Saved experiment evidence and recommendations for improving the system.</p></div></div>
    <div className="hero-actions findings-actions">
      <button className="primary-button" disabled={disabled||loading} onClick={()=>load(true)}><RefreshCw size={15}/>{loading?'Generating…':report?.available?'Refresh findings':'Generate findings'}</button>
      <button className="text-button" disabled={!report?.available} aria-expanded={open} onClick={()=>setOpen(!open)}><FileText size={15}/>{open?'Hide report':'View report'}</button>
      {report?.available&&<a className="findings-download" href={`${apiBase}/api/findings/download`} download="FINDINGS.md"><Download size={15}/> Download FINDINGS.md</a>}
    </div>
    {error&&<p className="message-box" role="status">{error}</p>}
    {report?.available?<>
      <p className="panel-subtitle">Saved {new Date(report.generated_at??report.modified_at).toLocaleString()}. {report.scope}</p>
      {report.coverage&&<p className="findings-summary">Report score: <strong>{report.overall_score??'—'} / 100</strong> · Coverage: {report.coverage.measured} / {report.coverage.expected} original experiments</p>}
      <p className="panel-subtitle">These improvements are proposals to evaluate and test; they have not been deployed automatically. Detection and recovery recommendations may reduce impact without preventing the underlying failure.</p>
      {!report.insights_available&&<p>Refresh findings to synchronize improvement cards with this saved report.</p>}
      <div className="findings-insights">{report.insights?.map((insight,i)=><article className="findings-insight" key={`${insight.experiment_id}-${i}`}>
        <div className="findings-insight-heading"><Lightbulb size={17}/><span className={`findings-priority ${String(insight.priority).toLowerCase()}`}>{insight.priority}</span><span>{insight.source==='gemini'?'Saved Gemini recommendation':'Deterministic follow-up'}</span></div>
        <h3>{insight.title}</h3><p className="panel-subtitle">{insight.fault_name} · {insight.target} · #{insight.experiment_id}</p>
        <strong>Improvement to evaluate</strong><p>{insight.hypothesis}</p><strong>How to validate it</strong><p>{insight.verification}</p>
        <p className="panel-subtitle">Configuration: {insight.configuration_status??'not-inspected'} · Evidence: {insight.evidence_refs?.join(', ')||'See saved experiment'}</p>
      </article>)}</div>
      {report.insights_available&&!report.insights?.length&&<p>No recovered original experiments are available yet. Complete experiments, then refresh findings.</p>}
      {open&&<div className="findings-report" role="region" aria-label="Saved findings report"><MarkdownReport text={report.markdown}/></div>}
    </>:report&&<p>No findings report is saved yet. Generate one from completed experiment history; this does not inject faults or submit payments.</p>}
  </section>;
}
