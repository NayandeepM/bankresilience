"""Evidence-derived topology, explicit safety policies and measured impact."""
from __future__ import annotations
import statistics
from control import SERVICES, TOPOLOGY, CATALOGUE, summarize

def graph_features(edges=TOPOLOGY):
    dependencies={s:set() for s in SERVICES};dependents={s:set() for s in SERVICES}
    for caller,callee in edges:
        dependencies.setdefault(caller,set()).add(callee)
        dependents.setdefault(callee,set()).add(caller)
    def reach(graph,start):
        found=set();todo=list(graph.get(start,()))
        while todo:
            item=todo.pop()
            if item not in found and item!=start:
                found.add(item);todo.extend(graph.get(item,()))
        return sorted(found)
    return {s:{'direct_dependencies':sorted(dependencies[s]),'direct_dependents':sorted(dependents[s]),
        'transitive_dependencies':reach(dependencies,s),'transitive_dependents':reach(dependents,s),
        'user_facing_path':'frontend' in reach(dependents,s),
        'criticality_score':len(reach(dependents,s))*2+len(dependencies[s])+int('frontend' in reach(dependents,s))*3} for s in SERVICES}

def safety_policy(kind,baseline):
    target=CATALOGUE[kind]['target'];p95=baseline.get(target,{}).get('latency_ms',{}).get('p95')
    if p95 is None:raise ValueError('Measured baseline p95 required for a safety policy')
    memory=[p.get('resources',{}).get('memory_bytes') for p in baseline.get('telemetry',[])]
    memory=[m for m in memory if m is not None]
    return {'latency_ms':{'warning':round(max(250,p95*2),1),'critical':round(max(1500 if kind in ('network-slowdown','packet-loss','latency-cpu','loss-cpu') else 800,p95*4),1)},
        'availability_percent':{'warning':99,'critical':95},'error_rate_percent':{'warning':5,'critical':20},
        'cpu_percent':{'warning':80,'critical':95},
        'memory_bytes':{'critical':max(256*1024*1024,statistics.median(memory)*1.5) if memory else None},
        'blast_radius':{'max_affected_services':1},'confirmation_samples':2,'rolling_samples':3,
        'ai_decision_budget_seconds':2,'max_duration_seconds':CATALOGUE[kind]['max_seconds'],
        'description':'Metric breaches decide intervention; duration is emergency cleanup only. Two consecutive bad probes avoid one-sample latency trips.'}

def observed_affected(point,policy=None):
    affected=set()
    probe=point.get("target_probe",{})
    limit=(policy or {}).get("latency_ms",{}).get("warning")
    if limit is not None and probe.get("ok") is True and probe.get("latency_ms") is not None and probe["latency_ms"]>limit:
        affected.add(point["target"])
    for s in point.get('services',[]):
        if s.get('service') in SERVICES and (s.get('status') not in ('running','paused') or s.get('health')=='unhealthy'):
            affected.add(s['service'])
    for service,probe in point.get('service_probes',{}).items():
        if probe.get('ok') is False:affected.add(service)
    if point.get('target_probe',{}).get('ok') is False:affected.add(point['target'])
    if point.get('frontend',{}).get('ok') is False:affected.add('frontend')
    return sorted(affected)

def evaluate(points,policy):
    if not points:return {'level':'normal','breaches':[],'affected_services':[]}
    recent=points[-policy['rolling_samples']:]
    confirm=policy['confirmation_samples']; tail=recent[-confirm:]
    breaches=[]; level='normal'
    def record(metric,value,limit,severity):
        nonlocal level
        breaches.append({'metric':metric,'observed':value,'limit':limit,'level':severity})
        if severity=='critical' or level=='normal':level=severity
    probs=[p['target_probe'] for p in recent if p['target_probe'].get('ok') is not None]
    errors=100*sum(p['ok'] is False for p in probs)/len(probs) if probs else None
    if len(tail)>=confirm:
        if all(p['target_probe'].get('ok') is False for p in tail) and 100-errors<policy['availability_percent']['critical'] and errors>policy['error_rate_percent']['critical']:
            record('availability_percent',100-errors,policy['availability_percent']['critical'],'critical')
            record('error_rate_percent',errors,policy['error_rate_percent']['critical'],'critical')
        elif all(p['target_probe'].get('ok') is True and (p['target_probe'].get('latency_ms') or 0)>=policy['latency_ms']['critical'] for p in tail):
            record('latency_ms',min(p['target_probe']['latency_ms'] for p in tail),policy['latency_ms']['critical'],'critical')
        for name in ('cpu_percent','memory_bytes'):
            limit=policy[name]['critical']
            vals=[p.get('resources',{}).get(name) for p in tail]
            if limit is not None and all(v is not None and v>=limit for v in vals):record(name,max(vals),limit,'critical')
    affected=observed_affected(points[-1],policy)
    if len(affected)>policy['blast_radius']['max_affected_services']:
        record('affected_services',len(affected),policy['blast_radius']['max_affected_services'],'critical')
    last=points[-1]['target_probe']
    if level=='normal' and (last.get('ok') is False or (last.get('latency_ms') or 0)>policy['latency_ms']['warning']):
        record('target_degradation',last.get('latency_ms'),policy['latency_ms']['warning'],'warning')
    return {'level':level,'breaches':breaches,'affected_services':affected,'rolling_availability_percent':100-errors if errors is not None else None,
            'rolling_error_rate_percent':errors,'time':points[-1]['time']}

def derived_impact(result):
    target=result['target']; baseline=summarize(result.get('baseline',{}).get('telemetry',[]),target)
    fault=summarize(result['fault_metrics'],target);recovery=summarize(result['recovery_metrics'],target)
    affected=sorted({s for p in result['fault_metrics'] for s in observed_affected(p,result.get("safety_policy"))})
    base=baseline[target]; during=fault[target]
    b=base['latency_ms']['p95']; f=during['latency_ms']['p95']
    def difference(field):
        a=base[field];z=during[field];return round(z-a,2) if a is not None and z is not None else None
    return {'baseline':baseline,'fault':fault,'recovery':recovery,'observed_affected_services':affected,
        'potential_affected_services':graph_features()[target]['transitive_dependents'],
        'affected_service_count':len(affected),'blast_radius_score':round(100*len(affected)/len(SERVICES),1),
        'latency_increase_percent':round((f/b-1)*100,1) if b and f is not None else None,
        'error_rate_increase_percentage_points':difference('error_rate_percent'),
        'availability_reduction_percentage_points':-difference('availability_percent') if difference('availability_percent') is not None else None,
        'detection_time':result.get('detection_time'),'time_to_mitigation':result.get('time_to_mitigation'),
        'recovery_time':result.get('recovery_time'),'total_degraded_duration':result.get('total_degraded_duration'),
        'returned_to_baseline':result.get('recovery_verified',False),'transaction_correctness':'unverified; manually validate banking flows'}
