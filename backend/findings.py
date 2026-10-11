"""Export a judge-facing report from measured runs and saved Gemini reviews."""
import argparse
import json
import sqlite3
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from planner import assess_system

ORIGINAL = ('ledgerwriter-outage','network-slowdown','contacts-pause')

def selected_runs(history):
    chosen={}
    for row in history:
        r=row['result'];kind=r.get('experiment_type')
        if kind in ORIGINAL and kind not in chosen and r.get('status')=='recovered' and r.get('resilience_score'):
            chosen[kind]=r
    return chosen

def render_findings(history):
    chosen=selected_runs(history)
    assessment=assess_system([{'result':r} for r in chosen.values()])
    out=['# BankResilience — Findings Report','',
         '> Generated from persisted local Docker experiment evidence and saved Gemini engineering reviews. Metrics are deterministic; AI recommendations are proposals for validation, not applied fixes.','',
         '## Scope and provenance','',
         'This report covers the latest recovered run of each of the three original experiments in the selected database. Packet loss, CPU stress, and combined experiments are separate additions. No new faults or payments are injected by this exporter.','',
         f'**Overall System Resilience Score: {assessment["overall_score"] if assessment["overall_score"] is not None else "unavailable"} / 100** — coverage {len(chosen)} / 3 original experiment types. Equal-weight mean of their latest completed primary-probe scores. This is not a production SLA or proof of transaction correctness.','',
         '## Experiment summary','',
         '| Experiment | Evidence ID | Baseline → fault p95 | Fault availability | Recovery time | Score | Diagnosis |',
         '|---|---|---:|---:|---:|---:|---|']
    def number(x,suffix=''):
        return f'{x:.2f}{suffix}' if isinstance(x,(int,float)) else 'not measured'
    for kind in ORIGINAL:
        r=chosen.get(kind)
        if not r:
            out.append(f'| {kind} | No recovered evidence | — | — | — | — | unavailable |');continue
        t=r['target'];impact=r.get('impact',{})
        b=impact.get('baseline',{}).get(t,{});f=impact.get('fault',{}).get(t,{})
        out.append(f'| {r.get("name",kind)} | `{r["id"]}` | {number(b.get("latency_ms",{}).get("p95"))} → {number(f.get("latency_ms",{}).get("p95"))} ms | {number(f.get("availability_percent"),"%")} | {number(r.get("recovery_time"),"s")} | {r["resilience_score"]["value"]}/100 | {(r.get("diagnosis") or {}).get("mode","unavailable")} |')
    out+=['','All listed runs passed recovery verification. A missing fault p95 indicates there were no successful target probes; it does not indicate zero latency.','',
          '## Severity-ranked vulnerabilities','',
          'Ranking uses measured severity first, then lower score, then higher dependency criticality. Gemini narrative severity is shown separately where available.']
    for i,w in enumerate(assessment['weaknesses'],1):
        r=chosen[w['experiment_type']]
        out+=['',f'### {i}. {w["severity"]} — {w["name"]}', '',w['justification'], '',
              f'Observed affected services: {", ".join(r.get("impact",{}).get("observed_affected_services",[])) or "not recorded"}. Evidence: `{r["id"]}`, completed `{r.get("finished_at","unknown")}`. Saved AI analysis severity: {w.get("ai_severity") or "unavailable"}.']
    out+=['','## AI-recommended remediation and verification','',
          'The following recommendations are extracted from saved Gemini engineering reviews, with their original evidence references. Existing configuration was not inspected unless explicitly stated. Controlled shutdowns and pauses were injected by the experiment; recommendations do not establish an unexplained production incident or the absence of existing controls.']
    for kind in ORIGINAL:
        r=chosen.get(kind)
        if not r:continue
        review=r.get('action_review') or {}
        out+=['',f'### {r.get("name",kind)} — `{r["id"]}`','']
        if review.get('mode')!='gemini' or not review.get('improvement_plan'):
            out+=['No saved Gemini engineering review is available. Deterministic follow-up: verify readiness, inspect configuration, and reconcile demo transfers.'];continue
        for proposal in review.get('improvement_plan',[]):
            out += [f'- **{proposal["priority"]}: {proposal["title"]}** — {proposal["hypothesis"]} Validation: {proposal["verification"]} Evidence: {", ".join(proposal["evidence_refs"])}. Configuration: {proposal.get("configuration_status","not-inspected")}.']
    out+=['','## Measurement limits and next actions','',
          '- Only observed services are counted as affected; declared caller dependencies indicate potential impact.',
          '- Readiness and public-page success do not establish successful authenticated transfers or ledger integrity.',
          '- Saved AI suggestions must be checked against current standby protection and routing configuration before implementation.',
          '- Run the five-fault suite for broader coverage, and the combination suite to measure overlapping network/resource pressure. Compare against single-fault runs; differing baseline conditions prevent strict causal attribution.',
          '- Independently verify transfer IDs, balances, transaction history, and duplicate prevention before claiming transaction correctness.','']
    return '\n'.join(out)

def report_snapshot(history):
    chosen=selected_runs(history)
    markdown=render_findings(history)
    assessment=assess_system([{'result':r} for r in chosen.values()])
    insights=[]
    for kind,r in chosen.items():
        review=r.get('action_review') or {}
        proposals=review.get('improvement_plan',[]) if review.get('mode')=='gemini' else []
        if not proposals:
            proposals=[{'title':'Verify readiness and inspect existing configuration',
                'priority':'MEDIUM','hypothesis':'Evaluate monitoring and recovery controls before proposing changes.',
                'verification':'Check readiness, current protection settings, and reconcile any demo transfers.',
                'evidence_refs':[],'configuration_status':'not-inspected'}]
        for proposal in proposals:
            insights.append({**proposal,'experiment_id':r['id'],'experiment_type':kind,
                'fault_name':r.get('name',kind),'target':proposal.get('target',r['target']),
                'source':'gemini' if review.get('mode')=='gemini' and review.get('improvement_plan') else 'deterministic-follow-up'})
    order={'CRITICAL':0,'HIGH':1,'MEDIUM':2,'LOW':3}
    insights.sort(key=lambda p:order.get(p.get('priority'),4))
    return {'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
        'scope':'Latest recovered runs of the three original experiments. Additional faults and combinations remain in experiment history.',
        'coverage':{'measured':len(chosen),'expected':len(ORIGINAL)},'overall_score':assessment['overall_score'],
        'experiment_ids':[r['id'] for r in chosen.values()],
        'insights':insights,'markdown':markdown,
        'content_sha256':hashlib.sha256(markdown.encode('utf-8')).hexdigest()}

def atomic_write(path,content):
    path=Path(path)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as file:
        temporary=Path(file.name)
        file.write(content)
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def export_report(history,path):
    snapshot=report_snapshot(history)
    atomic_write(path,snapshot['markdown'])
    atomic_write(Path(path).with_suffix('.json'),json.dumps({k:v for k,v in snapshot.items() if k!='markdown'},indent=2))
    return {**snapshot,'available':True,'insights_available':True}

def read_report(path):
    path=Path(path)
    if not path.exists():return {'available':False,'insights_available':False,'insights':[]}
    markdown=path.read_text(encoding='utf-8')
    digest=hashlib.sha256(markdown.encode('utf-8')).hexdigest()
    try:
        metadata=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        synced=isinstance(metadata,dict) and metadata.get('schema_version')==1 and metadata.get('content_sha256')==digest and isinstance(metadata.get('insights'),list)
    except (OSError,ValueError):metadata={};synced=False
    if not synced:metadata={'insights':[],'scope':'Saved Markdown report; generate or refresh to synchronize structured insights.'}
    return {**metadata,'available':True,'insights_available':synced,'markdown':markdown,
        'content_sha256':digest,'modified_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path(__file__).with_name('experiments.db'))
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'FINDINGS.md')
    args=parser.parse_args()
    with sqlite3.connect(f'file:{args.database.resolve()}?mode=ro',uri=True) as conn:
        history=[{'result':json.loads(row[0])} for row in conn.execute('SELECT result_json FROM experiments ORDER BY started_at DESC')]
    export_report(history,args.output)
    print(f'Findings written to {args.output}')

if __name__=='__main__':main()
