"""Pure contracts, deterministic guardrails, telemetry summaries and scoring."""
from __future__ import annotations
import math
import statistics
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

SERVICES = ['frontend','userservice','contacts','balancereader','ledgerwriter','transactionhistory','accounts-db','ledger-db']
TOPOLOGY = [('frontend',s) for s in SERVICES[1:6]] + [('userservice','accounts-db'),('contacts','accounts-db'),('balancereader','ledger-db'),('ledgerwriter','ledger-db'),('ledgerwriter','balancereader'),('transactionhistory','ledger-db')]
CATALOGUE = {
    'ledgerwriter-outage': {'name':'LedgerWriter outage','target':'ledgerwriter','action':'start-service','max_seconds':12},
    'network-slowdown': {'name':'Network slowdown','target':'ledgerwriter','action':'remove-network-impairment','max_seconds':10},
    'packet-loss': {'name':'Packet loss','target':'ledgerwriter','action':'remove-network-impairment','max_seconds':12},
    'cpu-stress': {'name':'CPU stress','target':'ledgerwriter','action':'stop-resource-pressure','max_seconds':12},
    # Adapted from teammate feature/milestone2-experiments, contacts pause/unpause.
    'contacts-pause': {'name':'Contacts hang','target':'contacts','action':'unpause-service','max_seconds':12},
    'latency-cpu': {'name':'Network slowdown + CPU stress','target':'ledgerwriter','action':'remove-compound-impairment','max_seconds':10,'components':['network-slowdown','cpu-stress']},
    'loss-cpu': {'name':'Packet loss + CPU stress','target':'ledgerwriter','action':'remove-compound-impairment','max_seconds':12,'components':['packet-loss','cpu-stress']},
}
class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
class Action(Contract):
    action: Literal['start-service','remove-network-impairment','stop-resource-pressure','remove-compound-impairment','unpause-service','observe','inspect-network','escalate']
    target: Literal['ledgerwriter','contacts']
    experiment_id: str
class Diagnosis(Contract):
    experiment_id: str
    experiment_type: Literal['ledgerwriter-outage','network-slowdown','packet-loss','cpu-stress','contacts-pause','latency-cpu','loss-cpu']
    root_cause: str
    evidence: list[str]
    confidence: float = Field(ge=0,le=1)
    blast_radius: list[str]
    recommended_action: Action
    severity: Literal['LOW','MEDIUM','HIGH','CRITICAL'] = 'MEDIUM'
    impacted_services: list[str] = Field(default_factory=list)
    current_system_state: str = ''
    resilience_improvements: list[str] = Field(default_factory=list)
class Recommendation(Contract):
    experiment_type: Literal['ledgerwriter-outage','network-slowdown','packet-loss','cpu-stress','contacts-pause','latency-cpu','loss-cpu']
    rationale: str
    expected_impact: str
    rank: int = Field(ge=1,le=7)
    target: Literal['ledgerwriter','contacts']
    severity: Literal['LOW','MEDIUM','HIGH']
    graph_evidence: list[str]
class Plan(Contract):
    recommendations: list[Recommendation] = Field(min_length=1,max_length=7)
class Analysis(Contract):
    severity: Literal['LOW','MEDIUM','HIGH','CRITICAL']
    recovery_effectiveness: str
    weaknesses: list[str]
    remediation: list[str]

def healthy(services):
    by_name = {s['service']:s for s in services}
    return all(by_name.get(s,{}).get('status') == 'running' and by_name[s].get('health') not in ('unhealthy','starting') for s in SERVICES)

def approve(action, experiment, *, active, recovery_due):
    spec = CATALOGUE.get(experiment.get('experiment_type'))
    if not active or not recovery_due or not spec:
        raise ValueError('Recovery is permitted only for an active, bounded experiment when intervention is due')
    if action.experiment_id != experiment['id'] or action.target != spec['target'] or action.action != spec['action']:
        raise ValueError('Action, target or experiment ID does not match the approved fault inverse')
    return action.model_dump()

def latency_summary(probes):
    values = sorted(float(p['latency_ms']) for p in probes if p.get('ok') and p.get('latency_ms') is not None and math.isfinite(p['latency_ms']))
    unknown=sum(p.get('ok') is None for p in probes)
    probes=[p for p in probes if p.get('ok') is not None]
    n = len(probes)
    success = sum(bool(p.get('ok')) for p in probes)
    return {'availability_percent':round(100*success/n,2) if n else None,
            'error_rate_percent':round(100*(n-success)/n,2) if n else None,
            'sample_count':n,'unavailable_measurement_count':unknown,'latency_ms':{'median':round(statistics.median(values),1) if values else None,
            'p95':values[max(0, math.ceil(.95*len(values))-1)] if values else None,
            'min':min(values) if values else None,'max':max(values) if values else None},
            'timeout_count':sum(not p.get('ok') for p in probes)}

def summarize(points, target):
    return {'frontend':latency_summary([p['frontend'] for p in points]), target:latency_summary([p['target_probe'] for p in points])}

def network_breached(points, threshold=1500):
    # Two consecutive observations; a single cold probe cannot trip latency safety.
    return len(points)>=2 and all(not p['target_probe'].get('ok') or (p['target_probe'].get('latency_ms') or 0)>=threshold for p in points[-2:])

def score(result):
    points=result.get('fault_metrics',[]);target=result['target']
    target_stats=latency_summary([p['target_probe'] for p in points])
    front_stats=latency_summary([p['frontend'] for p in points])
    base=result.get('baseline',{}).get(target,{}).get('latency_ms',{}).get('p95')
    p95=target_stats['latency_ms']['p95']
    ratio=max(0,(p95/base-1)/9) if base and p95 is not None else 0
    impact=result.get('impact',{})
    emergency=result.get('max_fault_seconds',CATALOGUE.get(result.get('experiment_type'),{}).get('max_seconds',12))
    def time_penalty(key,weight,bound):
        value=result.get(key)
        return round(weight*min(1,max(0,value)/bound),2) if value is not None else weight
    penalties={
        'target_errors':round(20*(target_stats['error_rate_percent']/100),2) if target_stats['error_rate_percent'] is not None else 20,
        'latency_degradation':round(10*min(1,ratio),2),
        'frontend_errors':round(10*front_stats['error_rate_percent']/100,2) if front_stats['error_rate_percent'] is not None else 10,
        'detection_delay':time_penalty('detection_time',10,emergency),
        'mitigation_delay':time_penalty('time_to_mitigation',10,emergency),
        'recovery_duration':time_penalty('recovery_time',10,35),
        'observed_blast_radius':round(10*min(1,impact.get('affected_service_count',0)/len(SERVICES)),2),
        'unverified_recovery':0 if result.get('recovery_verified') else 20}
    missing=[key for key in ('detection_time','time_to_mitigation','recovery_time') if result.get(key) is None]
    if target_stats['unavailable_measurement_count'] or front_stats['unavailable_measurement_count']:missing.append('some HTTP observations unavailable')
    return {'value':max(0,round(100-sum(penalties.values()))),'penalties':penalties,'missing_measurements':missing,
        'scope':'Local readiness-probe score; no transaction correctness measurement. Missing timing evidence receives its full penalty.',
        'formula':'100 minus target errors (20×fraction), p95 latency (10×clamp((fault/baseline−1)/9)), frontend errors (10×fraction), detection (10×seconds/emergency bound), mitigation (10×seconds/emergency bound), recovery (10×seconds/35), observed blast radius (10×affected/8), unverified restoration (20). All ratios capped at 1.'}

def fallback_diagnosis(result):
    kind = result['experiment_type']; target=result['target']
    points=result.get('fault_metrics',[])
    summary=summarize(points,target)
    causes={'ledgerwriter-outage':'LedgerWriter stopped during the controlled outage.',
        'network-slowdown':'The injected netem delay degraded the LedgerWriter network path.',
        'packet-loss':'The injected netem loss intermittently dropped packets on the LedgerWriter network path.',
        'cpu-stress':'Bounded CPU workers exhausted LedgerWriter compute capacity.',
        'latency-cpu':'Concurrent controlled network delay and CPU workers degraded LedgerWriter.',
        'loss-cpu':'Concurrent controlled packet loss and CPU workers degraded LedgerWriter.',
        'contacts-pause':'The Contacts process was paused and cannot serve requests.'}
    return Diagnosis(experiment_id=result['id'],experiment_type=kind,root_cause=causes[kind],confidence=.85 if points else .5,
        evidence=[f'Fault: {kind}; successful injection recorded: {result.get("injected",False)}',f'Target readiness availability: {summary[target]["availability_percent"]}%; successful median: {summary[target]["latency_ms"]["median"]}ms',f'Frontend availability: {summary["frontend"]["availability_percent"]}%' ],
        blast_radius=['Fallback containment evidence only; Gemini diagnosis unavailable'],
        impacted_services=sorted({s for p in points for s in p.get('affected_services',[])}),
        severity='HIGH' if any(not p['target_probe'].get('ok') for p in points) else 'MEDIUM',
        current_system_state= 'Degraded target; see measured readiness and service state',
        recommended_action=Action(action=CATALOGUE[kind]['action'],target=target,experiment_id=result['id']))

def fallback_plan(context):
    # Diagnostic ordering only, never presented as AI. All features come from the supplied graph/history.
    from policies import graph_features
    graph=graph_features(context.get('topology',TOPOLOGY));history=context.get('history',[])
    def priority(kind):
        target=CATALOGUE[kind]['target'];g=graph[target]
        matching=[h['result'] for h in history if h['result'].get('experiment_type')==kind]
        prior=matching[0].get('resilience_score',{}).get('value') if matching else None
        return g['criticality_score'] + ((100-prior)/10 if prior is not None else 0)
    ranked=sorted(CATALOGUE,key=priority,reverse=True)
    return Plan(recommendations=[Recommendation(rank=i+1,experiment_type=k,target=CATALOGUE[k]['target'],
        rationale=f"Graph criticality {graph[CATALOGUE[k]['target']]['criticality_score']}; observed-history priority {priority(k):.1f}. This is a deterministic evidence ordering, not Gemini ranking.",
        expected_impact='Potential caller impact inferred from declared topology; not measured transaction impact.',
        severity='HIGH' if graph[CATALOGUE[k]['target']]['criticality_score']>=6 else 'MEDIUM',
        graph_evidence=[f"Dependencies: {graph[CATALOGUE[k]['target']]['direct_dependencies']}",f"Callers: {graph[CATALOGUE[k]['target']]['transitive_dependents']}"])
        for i,k in enumerate(ranked)])

def fallback_analysis(result):
    value=score(result)['value']
    return Analysis(severity='CRITICAL' if not result.get('recovery_verified') else 'HIGH' if value<75 else 'MEDIUM' if value<90 else 'LOW',
        recovery_effectiveness='Readiness, frontend and service-state checks passed; banking transfer unverified.' if result.get('recovery_verified') else 'Recovery checks failed; manual escalation required.',
        weaknesses=[f'{result["target"]}: {result["experiment_type"]} scored {value}/100 on measured probes'],
        remediation=['Add bounded retries and circuit breakers to dependent callers.','Evaluate service redundancy and readiness-based routing.','Validate recovery with a demo transfer and transaction history.'])
