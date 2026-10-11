import asyncio
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
os.environ['BANKRESILIENCE_DB_PATH']=tempfile.mktemp(suffix='.sqlite3')
import main
from control import CATALOGUE

def observation(target='ledgerwriter',phase='baseline',experiment_id='a',ok=True):
    return {'time':main.utc_now(),'experiment_id':experiment_id,'phase':phase,'target':target,
        'services':[{'service':s,'status':'running'} for s in main.SERVICES],
        'frontend':{'ok':True,'latency_ms':5},'target_probe':{'ok':ok,'latency_ms':60 if ok else None},'resources':{},'restarts':{}}
def baseline(samples=10,interval=.15,target='ledgerwriter'):
    ps=[observation(target) for _ in range(10)]
    return {**main.summarize(ps,target),'telemetry':ps}
def result(kind):
    return {'id':'a','started_at':main.utc_now(),'status':'running','experiment_type':kind,'target':CATALOGUE[kind]['target'],
        'events':[],'safety_events':[],'fault_metrics':[],'recovery_metrics':[],'telemetry':[],'injected':False,'recovery_verified':False,'diagnosis':None,'recovery_due':False}
def fallback_reason(task,model,ctx,fallback):return {**fallback.model_dump(),'mode':'deterministic-fallback'}
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.manual_kill.clear()
    async def test_every_fault_cleans_up_and_persists_complete_curve(self):
        for kind in CATALOGUE:
            r=result(kind);target=MagicMock(status='running');target.logs.return_value=b'test logs'
            spec={**CATALOGUE[kind],'max_seconds':.03}
            def probe(t,phase,id):
                if phase=='fault':time.sleep(.08)
                return observation(t,phase,id)
            with patch.dict(CATALOGUE,{kind:spec}),patch.object(main,'collect_baseline',baseline),patch.object(main,'save_baseline'),patch.object(main,'get_service_container',return_value=target),patch.object(main,'apply_network_delay',return_value=MagicMock()),patch.object(main,'apply_packet_loss',return_value=MagicMock()),patch.object(main,'apply_cpu_stress',return_value={'exec_id':'test'}),patch.object(main,'remove_network_delay') as network_cleanup,patch.object(main,'remove_cpu_stress') as cpu_cleanup,patch.object(main,'point',side_effect=probe),patch.object(main,'reason',side_effect=fallback_reason),patch.object(main,'persist') as persisted:
                await main.run_experiment(r)
            self.assertTrue(r['recovery_verified'],r)
            self.assertEqual(r['diagnosis']['experiment_type'],kind)
            self.assertGreaterEqual(len(r['telemetry']),13)
            self.assertEqual(r['approved_action']['action'],CATALOGUE[kind]['action'])
            self.assertTrue(any(e['kind']=='kill-switch' for e in r['events']))
            self.assertLess(r['detection_time'],.2)
            persisted.assert_called_once()
            if kind=='ledgerwriter-outage':target.start.assert_called_once()
            if kind in ('network-slowdown','packet-loss'):network_cleanup.assert_called_once()
            if kind=='cpu-stress':cpu_cleanup.assert_called_once()

class CompoundFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_injection_failure_cleans_both_components(self):
        main.manual_kill.clear()
        r=result('latency-cpu');target=MagicMock(status='running')
        with patch.object(main,'collect_baseline',baseline),patch.object(main,'save_baseline'),patch.object(main,'get_service_container',return_value=target),patch.object(main,'apply_network_delay',return_value=MagicMock()),patch.object(main,'apply_cpu_stress',side_effect=RuntimeError('CPU exec failed')),patch.object(main,'remove_network_delay') as network,patch.object(main,'remove_cpu_stress') as cpu,patch.object(main,'point',side_effect=observation),patch.object(main,'reason',side_effect=fallback_reason),patch.object(main,'persist'):
            await main.run_experiment(r)
        network.assert_called_once();cpu.assert_called_once()
        self.assertTrue(r['cleanup_succeeded'])
        self.assertTrue(r['recovery_verified'])
        self.assertEqual(len(r['component_actions']),2)
        self.assertIn('CPU exec failed',r['error'])

class FaultAdapterTests(unittest.TestCase):
    def test_packet_loss_uses_owned_netem_policy(self):
        target=MagicMock(id='target-id',name='ledgerwriter')
        helper=MagicMock();helper.exec_run.return_value=type('Response',(),{'exit_code':0,'output':b''})()
        client=MagicMock();client.containers.run.return_value=helper
        with patch.object(main,'docker_client',client):returned=main.apply_packet_loss(target)
        self.assertIs(returned,helper)
        command=helper.exec_run.call_args.args[0]
        self.assertEqual(command[-2:],['loss',f'{main.PACKET_LOSS_PERCENT}%'])
        labels=client.containers.run.call_args.kwargs['labels']
        self.assertEqual(labels['bankresilience.fault'],'packet-loss')

    def test_cpu_stress_runs_in_target_and_cleanup_is_verified(self):
        target=MagicMock(id='target-id')
        target.exec_run.return_value=type('Response',(),{'exit_code':0,'output':b''})()
        client=MagicMock();client.api.exec_create.return_value={'Id':'exec-id'}
        with patch.object(main,'docker_client',client):
            handle=main.apply_cpu_stress(target)
            main.remove_cpu_stress(target,handle)
        self.assertEqual(handle['target_id'],'target-id')
        command=client.api.exec_create.call_args.args[1]
        self.assertIn(main.CPU_STRESS_MARKER,command[-1])
        self.assertIn(str(main.CPU_STRESS_WORKERS),command[-1])
        client.api.exec_start.assert_called_once_with('exec-id',detach=True)

class RuntimeContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.manual_kill.clear()

    async def test_single_healthy_baseline_error_blocks_injection(self):
        r=result('ledgerwriter-outage');report=baseline();report['ledgerwriter']['availability_percent']=90
        target=MagicMock()
        with patch.object(main,'collect_baseline',return_value=report),patch.object(main,'get_service_container',return_value=target),patch.object(main,'reason',side_effect=fallback_reason),patch.object(main,'persist'):
            await main.run_experiment(r)
        target.stop.assert_not_called()
        self.assertEqual(r['status'],'blocked-before-injection')
    async def test_ai_wrong_action_is_audited_and_emergency_inverse_used(self):
        r=result('ledgerwriter-outage');r['diagnosis']={**main.fallback_diagnosis(r).model_dump(),'mode':'gemini'}
        r['diagnosis']['recommended_action']['action']='unpause-service'
        target=MagicMock(status='running')
        spec={**CATALOGUE['ledgerwriter-outage'],'max_seconds':.01}
        def probe(t,phase,id):
            if phase=='fault':time.sleep(.04)
            return observation(t,phase,id)
        with patch.dict(CATALOGUE,{'ledgerwriter-outage':spec}),patch.object(main,'collect_baseline',baseline),patch.object(main,'save_baseline'),patch.object(main,'get_service_container',return_value=target),patch.object(main,'point',side_effect=probe),patch.object(main,'reason',side_effect=fallback_reason),patch.object(main,'persist'):
            await main.run_experiment(r)
        self.assertTrue(any(e['kind']=='action-rejected' for e in r['events']))
        self.assertEqual(r['approved_action']['action'],'start-service')
        target.start.assert_called_once()

class MetricRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_metric_intervenes_before_emergency_bound_with_ai_action(self):
        main.manual_kill.clear();r=result('ledgerwriter-outage');target=MagicMock(status='running');target.logs.return_value=b'controlled shutdown'
        def probe(t,phase,id):
            p=observation(t,phase,id,ok=phase!='fault')
            if phase=='fault':
                next(s for s in p['services'] if s['service']==t)['status']='exited'
            return p
        def ai(task,model,ctx,fallback):
            return {**fallback.model_dump(),'evidence_refs':['observation-1'],'mode':'gemini','model':'test-fixture'}
        spec={**CATALOGUE['ledgerwriter-outage'],'max_seconds':5}
        with patch.dict(CATALOGUE,{'ledgerwriter-outage':spec}),patch.object(main,'collect_baseline',baseline),patch.object(main,'save_baseline'),patch.object(main,'get_service_container',return_value=target),patch.object(main,'point',side_effect=probe),patch.object(main,'reason',side_effect=ai),patch.object(main,'persist'):
            await main.run_experiment(r)
        self.assertEqual(r['intervention_trigger'],'metric')
        self.assertLess(r['time_to_mitigation'],1)
        self.assertEqual(r['recovery_mode'],'gemini-recommended')
        self.assertTrue(r['threshold_breaches'])
        self.assertTrue(r['recovery_verified'])
        target.start.assert_called_once()

class AdvisoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):main.manual_kill.clear()
    async def exercise(self,choice,critical=False):
        r=result('network-slowdown');target=MagicMock(status='running');target.logs.return_value=b'readiness healthy'
        responses=[]
        def ai(task,model,ctx,fallback):
            d={**fallback.model_dump(),'mode':'gemini','model':'test-fixture'}
            if issubclass(model,main.ImmediateAdvice):
                d['recommended_action']['action']=choice;d['evidence_refs']=['observation-1'];responses.append(ctx)
            return d
        def probe(t,phase,id):
            p=observation(t,phase,id)
            if phase=='fault':p['target_probe']['latency_ms']=1800 if critical else 600
            return p
        spec={**CATALOGUE['network-slowdown'],'max_seconds':2}
        with patch.dict(CATALOGUE,{'network-slowdown':spec}),patch.object(main,'collect_baseline',baseline),patch.object(main,'save_baseline'),patch.object(main,'get_service_container',return_value=target),patch.object(main,'apply_network_delay',return_value=MagicMock()),patch.object(main,'remove_network_delay') as cleanup,patch.object(main,'inspect_network_policy',return_value={'controller_owned_netem':True}),patch.object(main,'point',side_effect=probe),patch.object(main,'reason',side_effect=ai),patch.object(main,'persist'):
            await main.run_experiment(r)
        cleanup.assert_called_once();self.assertTrue(r['recovery_verified']);return r,responses
    async def test_gemini_can_mitigate_warning_before_critical_threshold(self):
        r,ctx=await self.exercise('remove-network-impairment')
        self.assertEqual(r['intervention_trigger'],'ai-recommendation')
        self.assertEqual(r['recovery_mode'],'gemini-recommended')
        self.assertNotIn('experiment_type',ctx[0])
    async def test_observe_cannot_disable_emergency_cleanup_or_query_forever(self):
        r,ctx=await self.exercise('observe')
        self.assertEqual(r['intervention_trigger'],'emergency')
        self.assertLessEqual(len(ctx),3)
        self.assertTrue(r['advisory_decisions'])
        self.assertEqual(r['recovery_mode'],'deterministic-emergency-fallback')
    async def test_inspect_is_executed_as_advisory_choice(self):
        r,_=await self.exercise('inspect-network')
        self.assertTrue(any(d['action']=='inspect-network' for d in r['advisory_decisions']))
        self.assertEqual(r['intervention_trigger'],'emergency')
    async def test_critical_limits_override_observe(self):
        r,_=await self.exercise('observe',critical=True)
        self.assertEqual(r['intervention_trigger'],'metric')
        self.assertEqual(r['recovery_mode'],'deterministic-emergency-fallback')
        self.assertIn('Critical policy',r['safety_override_reason'])

    async def test_escalation_flags_review_and_preserves_containment(self):
        r,_=await self.exercise('escalate')
        self.assertEqual(r['intervention_trigger'],'ai-escalation')
        self.assertTrue(r['review_requested'])
        self.assertEqual(r['recovery_mode'],'deterministic-emergency-fallback')

class ReviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_recovery_review_does_not_replace_executed_advice(self):
        r=result('network-slowdown');r['baseline']=baseline();r['fault_metrics']=[observation(phase='fault')]
        r['diagnosis']={'recommended_action':{'action':'remove-network-impairment'}}
        r['recovery_action']={'action':'remove-network-impairment'};r['recovery_mode']='gemini-recommended'
        def ai(task,model,ctx,fallback):
            d={**fallback.model_dump(),'mode':'gemini'};d['recommended_action']['action']='observe';return d
        with patch.object(main,'reason',side_effect=ai):await main.review_actions(r)
        self.assertEqual(r['diagnosis']['recommended_action']['action'],'remove-network-impairment')
        self.assertEqual(r['recovery_action']['action'],'remove-network-impairment')
        self.assertEqual(r['recovery_mode'],'gemini-recommended')
        self.assertEqual(r['action_review']['recommended_action']['action'],'observe')
