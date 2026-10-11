"""Fault-blind reasoning input, grounded proposals and safe advisory choices."""
from __future__ import annotations
import copy
from typing import Literal
from pydantic import Field, create_model
from control import Contract, Action, SERVICES, TOPOLOGY, approve, fallback_diagnosis
from policies import graph_features, observed_affected

Condition = Literal['process-stopped','process-paused','network-latency','network-loss','resource-pressure','dependency-failure','unknown']
ActionName = Literal['start-service','remove-network-impairment','stop-resource-pressure','remove-compound-impairment','unpause-service','observe','inspect-network','escalate']
class Alternative(Contract):
    action: ActionName
    reason: str = Field(min_length=1,max_length=400)
class Improvement(Contract):
    title: str = Field(min_length=1,max_length=180)
    priority: Literal['HIGH','MEDIUM','LOW']
    target: Literal['frontend','userservice','contacts','balancereader','ledgerwriter','transactionhistory','accounts-db','ledger-db']
    hypothesis: str = Field(min_length=1,max_length=400)
    evidence_refs: list[str] = Field(min_length=1,max_length=6)
    verification: str = Field(min_length=1,max_length=400)
class RecoveryAdvice(Contract):
    experiment_id: str
    root_cause: str = Field(min_length=1,max_length=600)
    inferred_condition: Condition
    evidence: list[str] = Field(min_length=1,max_length=6)
    evidence_refs: list[str] = Field(min_length=1,max_length=6)
    confidence: float = Field(ge=0,le=1)
    blast_radius: list[str] = Field(max_length=8)
    impacted_services: list[str] = Field(max_length=8)
    severity: Literal['LOW','MEDIUM','HIGH','CRITICAL']
    current_system_state: str = Field(max_length=400)
    recommended_action: Action
    action_rationale: str = Field(min_length=1,max_length=600)
    expected_effect: str = Field(min_length=1,max_length=400)
    action_risks: str = Field(min_length=1,max_length=400)
    alternatives: list[Alternative] = Field(min_length=2,max_length=3)
    verification_plan: list[str] = Field(min_length=2,max_length=4)
    improvement_plan: list[Improvement] = Field(min_length=1,max_length=3)

class ImmediateAdvice(Contract):
    experiment_id: str
    root_cause: str = Field(min_length=1,max_length=250)
    inferred_condition: Condition
    evidence_refs: list[str] = Field(min_length=1,max_length=3)
    confidence: float = Field(ge=0,le=1)
    impacted_services: list[str] = Field(max_length=8)
    severity: Literal['LOW','MEDIUM','HIGH','CRITICAL']
    recommended_action: Action
    action_rationale: str = Field(min_length=1,max_length=250)
    expected_effect: str = Field(min_length=1,max_length=180)
    action_risks: str = Field(min_length=1,max_length=180)
    alternatives: list[Alternative] = Field(min_length=2,max_length=2)
    verification_plan: list[str] = Field(min_length=2,max_length=2)

# General capabilities are identical for every fault; no selected inverse is supplied.
CAPABILITIES=[
 {'action':'start-service','targets':['ledgerwriter'],'when':'Stopped service; may require cold-start readiness checks'},
 {'action':'unpause-service','targets':['contacts'],'when':'Paused process; preserve existing process state'},
 {'action':'remove-network-impairment','targets':['ledgerwriter'],'when':'Trusted inspection confirms a controller-owned netem policy; preserve the process'},
 {'action':'stop-resource-pressure','targets':['ledgerwriter'],'when':'Current observations show controller-owned resource pressure; preserve the service process'},
 {'action':'remove-compound-impairment','targets':['ledgerwriter'],'when':'Current observations show both owned network impairment and resource pressure; cleanup both, never only one'},
 {'action':'observe','targets':['ledgerwriter','contacts'],'when':'Uncertain evidence below critical limits; one extra sample, at most two additional diagnosis rounds'},
 {'action':'inspect-network','targets':['ledgerwriter'],'when':'Read-only traffic-policy inspection through the trusted adapter, if available; never changes networking'},
 {'action':'escalate','targets':['ledgerwriter','contacts'],'when':'Evidence does not support safe automated mitigation; controller ends the experiment safely and flags review'},
]

def diagnostic_context(result):
    target=result['target'];evidence={}
    evidence['baseline']={k:copy.deepcopy(result['baseline'].get(k)) for k in ('frontend',target,'service_metrics') if result['baseline'].get(k) is not None}
    for i,p in enumerate(result.get('fault_metrics',[])[-4:]):
        # Deliberately exclude phase, injection metadata and experiment_type.
        evidence[f'observation-{i+1}']={k:copy.deepcopy(p.get(k)) for k in ('time','services','frontend','target_probe','service_probes','resources','restarts','affected_services') if p.get(k) is not None}
    if result.get('target_logs'):evidence['target-logs']=result['target_logs'][-2000:]
    if result.get('network_observation'):evidence['network-policy']=copy.deepcopy(result['network_observation'])
    evidence['topology']={'edges':TOPOLOGY,'features':graph_features()}
    points=result.get('fault_metrics',[])
    fronts=[p.get('frontend',{}) for p in points if p.get('frontend',{}).get('ok') is not None]
    evidence['measurement-scope']={
        'frontend_failed_public_page_probes':sum(p.get('ok') is False for p in fronts),
        'frontend_observed_public_page_probes':len(fronts),
        'observed_affected_services':sorted({s for p in points for s in observed_affected(p,result.get('safety_policy'))}),
        'business_operation_impact':'Not measured. No transfer correctness or business-path performance conclusion is established.',
        'resilience_configuration':'Not inspected. Circuit breakers, retries, redundancy and alerting may already exist.',
        'caller_impact':'Potential only unless actually observed. Successful frontend probes do not establish cascading failure.'}

    protected=result.get('tolerance_observations',[])
    if protected:
        evidence['protected-routing']=[{'observed_at':p.get('time'),'primary_degraded':p.get('primary_fault_active'),
            'route':p.get('resilience',{}).get('routing',{}).get('route'),
            'protected_probe':p.get('resilience',{}).get('protected_probe'),
            'health':p.get('resilience',{}).get('routing',{}).get('health')} for p in protected[-4:]]
        evidence['measurement-scope']['resilience_configuration']='Owned standby/routing guard observed. Other retry, alerting and resilience configuration remains uninspected.'
    business=[p for p in result.get('business_observations',[]) if p.get('phase')=='fault']
    if business:
        evidence['sampled-business-responses']=[{'home':p['home'],
            'payment':{k:v for k,v in p['payment'].items() if k in ('attempted','ok','latency_ms','outcome_unknown','retried')}} for p in business[-4:]]
        evidence['measurement-scope']['business_operation_impact']='Only supplied authenticated page and payment responses are measured; full accounting correctness and unsampled banking flows remain unverified.'

    # Keep metric limits; the controller's fault-specific emergency duration is private.
    limits={k:copy.deepcopy(v) for k,v in result.get('safety_policy',{}).items() if k in ('latency_ms','availability_percent','error_rate_percent','cpu_percent','memory_bytes','blast_radius','confirmation_samples')}
    return {'experiment_id':result['id'],'observed_target':target,'evidence':evidence,
            'metric_limits':limits,'policy_level':result.get('policy_evaluation',{}).get('level','unknown'),
            'capabilities':copy.deepcopy(CAPABILITIES),'evidence_ids':list(evidence),
            'scope':'Readiness plus supplied protected-routing/business response samples when present. Unsampled flows and other configuration remain unverified.'}

def advice_contract(context,brief=False):
    """Constrain Gemini's citation strings to this request's actual evidence IDs."""
    EvidenceID=Literal.__getitem__(tuple(context['evidence']))
    if brief:
        return create_model('GroundedImmediateAdvice',__base__=ImmediateAdvice,
            evidence_refs=(list[EvidenceID],Field(min_length=1,max_length=3)))
    GroundedImprovement=create_model('GroundedImprovement',__base__=Improvement,
        evidence_refs=(list[EvidenceID],Field(min_length=1,max_length=6)))
    return create_model('GroundedRecoveryAdvice',__base__=RecoveryAdvice,
        evidence_refs=(list[EvidenceID],Field(min_length=1,max_length=6)),
        improvement_plan=(list[GroundedImprovement],Field(min_length=1,max_length=3)))

def fallback_advice(result):
    # Used locally after AI failure only. Not part of the outbound model input.
    d=fallback_diagnosis(result).model_dump()
    return RecoveryAdvice(**{k:v for k,v in d.items() if k in RecoveryAdvice.model_fields},
        inferred_condition='unknown',evidence_refs=['baseline'],
        action_rationale='Independent controller containment uses the recorded fault inverse; no Gemini inference.',
        expected_effect='Undo the controlled mutation; then measure whether readiness and latency recover.',
        action_risks='Cleanup can fail if Docker is unavailable; escalate on failed verification.',
        alternatives=[Alternative(action='observe',reason='Cannot delay a safety deadline.'),Alternative(action='escalate',reason='Flag failed cleanup or verification for review.')],
        verification_plan=['Three consecutive healthy service/readiness checks.','Latency within measured baseline tolerance.'],
        improvement_plan=[Improvement(title='Validate banking operations after recovery',priority='HIGH',target='frontend',hypothesis='A readiness-only recovery may leave banking operation failures undetected.',evidence_refs=['baseline'],verification='Perform a demo transfer and verify balance and transaction history.')])

def validate_advice(answer,context,result,brief=False):
    proposal=(ImmediateAdvice if brief else RecoveryAdvice).model_validate({k:v for k,v in answer.items() if k not in ('mode','model','fallback_reason')})
    if proposal.experiment_id!=result['id'] or proposal.recommended_action.experiment_id!=result['id']:
        raise ValueError('Recommendation experiment identity mismatch')
    if proposal.recommended_action.target!=result['target']:
        raise ValueError('Recommendation targets a different service')
    known=set(context['evidence'])
    refs=set(proposal.evidence_refs)|{ref for item in getattr(proposal,'improvement_plan',[]) for ref in item.evidence_refs}
    if not refs<=known:raise ValueError('Recommendation cites unknown evidence IDs')
    # Require an actual current observation, not topology/baseline alone, for a mutation.
    if proposal.recommended_action.action in ('start-service','unpause-service','remove-network-impairment','stop-resource-pressure','remove-compound-impairment') and not any(ref.startswith('observation-') or ref=='network-policy' for ref in proposal.evidence_refs):
        raise ValueError('Mutating recommendation lacks current observed evidence')
    measured={s for name,p in context['evidence'].items() if name.startswith('observation-') for s in observed_affected({**p,'target':context['observed_target']},result.get('safety_policy'))}
    if not set(proposal.impacted_services)<=measured:raise ValueError('Model claimed unobserved impacted services')
    return proposal

def advisory_decision(diagnosis,result):
    """Never expands the mutation allowlist; observation cannot override critical safety."""
    action=Action.model_validate(diagnosis['recommended_action'])
    if action.experiment_id!=result['id'] or action.target!=result['target']:
        raise ValueError('Advisory identity or target mismatch')
    name=action.action;critical=result.get('policy_evaluation',{}).get('level')=='critical'
    if name=='escalate':return 'escalate'
    if name in ('observe','inspect-network'):
        if critical:raise ValueError('Critical policy forbids delaying containment for observation')
        if name=='inspect-network' and action.target!='ledgerwriter':raise ValueError('Network inspector is restricted to LedgerWriter')
        return name
    approve(action,result,active=True,recovery_due=True)
    if diagnosis.get('confidence',0)<.7:raise ValueError('Low-confidence mutation requires containment fallback or inspection')
    states={s['service']:s.get('status') for s in result.get('fault_metrics',[])[-1].get('services',[])} if result.get('fault_metrics') else {}
    if name=='start-service' and states.get(action.target) not in ('exited','dead','created'):
        raise ValueError('Start recommendation is unsupported by the observed process state')
    if name=='unpause-service' and states.get(action.target)!='paused':
        raise ValueError('Unpause recommendation is unsupported by the observed process state')
    if name=='remove-network-impairment' and result.get('network_observation',{}).get('controller_owned_netem') is not True:
        raise ValueError('Network removal requires verified controller-owned netem evidence')
    if name=='remove-compound-impairment' and result.get('network_observation',{}).get('controller_owned_netem') is not True:
        raise ValueError('Combined cleanup requires verified owned network evidence')
    if name in ('stop-resource-pressure','remove-compound-impairment'):
        cpu=result.get('fault_metrics',[])[-1].get('resources',{}).get('cpu_percent') if result.get('fault_metrics') else None
        warning=result.get('safety_policy',{}).get('cpu_percent',{}).get('warning',80)
        if cpu is None or cpu < warning:raise ValueError('CPU stress removal requires current observed resource pressure')
    return 'mitigate'
