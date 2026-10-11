"""Dependency-aware system assessment; model advice never changes measured scores."""
from control import CATALOGUE
from policies import graph_features

SEVERITY_ORDER = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}

def assess_system(history):
    latest = {}
    for row in history:
        r = row.get('result', row)
        kind = r.get('experiment_type')
        if kind in CATALOGUE and kind not in latest and r.get('status') in ('recovered','manual-escalation') and r.get('resilience_score', {}).get('value') is not None:
            latest[kind] = r
    graph = graph_features()
    weaknesses = []
    for kind, r in latest.items():
        target = r['target']; g = graph[target]
        score = r['resilience_score']['value']
        # Severity is reproducible; Gemini's narrative severity is retained separately.
        severity = 'CRITICAL' if not r.get('recovery_verified') else 'HIGH' if score < 75 else 'MEDIUM' if score < 90 else 'LOW'
        penalties = r['resilience_score'].get('penalties', {})
        biggest = sorted(((k,v) for k,v in penalties.items() if v > 0), key=lambda x:x[1], reverse=True)[:2]
        penalty_text = ', '.join(f'{k.replace("_", " ")} (−{v:g})' for k,v in biggest) or 'no measured penalties'
        callers = ', '.join(g['transitive_dependents']) or 'no declared callers'
        dependencies = ', '.join(g['direct_dependencies']) or 'none'
        justification = (f'{severity}: {score}/100; largest penalties: {penalty_text}. '
                         f'{target} can affect callers {callers}; it depends on {dependencies}. '
                         f'Dependency criticality {g["criticality_score"]} breaks severity/score ties. '
                         'Caller impact is potential unless measured.')
        review = r.get('action_review') or {}
        remediation = review.get('improvement_plan', []) if review.get('mode') == 'gemini' else r.get('analysis', {}).get('remediation', [])
        weaknesses.append({'experiment_type':kind, 'name':r.get('name',CATALOGUE[kind]['name']),
            'target':target,'experiment_id':r['id'],'score':score,'severity':severity,
            'ai_severity':r.get('analysis',{}).get('severity'),'dependency_criticality':g['criticality_score'],
            'dependency_context':g,'justification':justification,'recovery_verified':r.get('recovery_verified',False),
            'remediation':remediation,'remediation_source':'gemini' if review.get('mode')=='gemini' else r.get('analysis',{}).get('mode','deterministic-fallback')})
    weaknesses.sort(key=lambda w:(SEVERITY_ORDER[w['severity']],w['score'],-w['dependency_criticality'],w['experiment_type']))
    for w in weaknesses:
        components=CATALOGUE[w['experiment_type']].get('components',[])
        if components:
            references=[{'experiment_type':k,'experiment_id':latest[k]['id'],'score':latest[k]['resilience_score']['value']} for k in components if k in latest]
            w['combination_comparison']={'single_fault_references':references,
                'score_delta_vs_single_mean':round(w['score']-sum(x['score'] for x in references)/len(references),1) if len(references)==len(components) else None,
                'scope':'Historical single-fault comparisons; different baselines/timing may affect results. Combined measurements do not establish each component’s isolated causal effect.'}
    # Equal weight per SINGLE fault type; repeats and compound runs do not inflate coverage.
    singles=[w for w in weaknesses if not CATALOGUE[w['experiment_type']].get('components')]
    overall=round(sum(w['score'] for w in singles)/len(singles),1) if singles else None
    expected=sum(not s.get('components') for s in CATALOGUE.values())
    return {'mode':'measured-history','weaknesses':weaknesses,'overall_score':overall,
        'coverage':{'measured_fault_types':len(singles),'available_fault_types':expected},
        'score_method':'Equal-weight mean of the latest completed primary-probe score for each single fault type. Missing types are excluded and coverage is explicit. Not an SLA or transaction-integrity score.',
        'ranking_method':'Measured severity, then lower score, then higher dependency criticality.'}
