import asyncio
import json
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
import requests
from gateway import RoutingGuard, handlers
from resilience import tolerance_verified
from suite import DemoProbe, PaymentBudget, experiment_summary
import main


def ready(guard, name):
    guard.observe(name, True, 20); guard.observe(name, True, 20)


class GuardTests(unittest.TestCase):
    def test_one_outlier_does_not_drop_a_verified_standby(self):
        g=RoutingGuard('p','s');ready(g,'primary');ready(g,'standby');g.switch('standby','test','x')
        g.observe('standby',True,384)
        self.assertTrue(g.snapshot()['available']);self.assertEqual(g.destination(),('standby','s'))
        g.observe('standby',False,400)
        self.assertFalse(g.snapshot()['available'])
    def test_network_delay_selects_healthy_standby_without_removing_fault(self):
        g=RoutingGuard('primary','standby'); ready(g,'primary'); ready(g,'standby')
        g.observe('primary',True,800); self.assertEqual(g.route,'primary')
        g.observe('primary',True,800); self.assertEqual(g.route,'standby')
        self.assertFalse(g.health['primary']['ok'])
        self.assertTrue(g.snapshot()['available'])
        self.assertEqual(g.events[-1]['source'],'automatic-protection')

    def test_no_failover_to_unhealthy_standby_and_no_automatic_failback(self):
        g=RoutingGuard('p','s'); ready(g,'primary')
        g.observe('primary',False,400); g.observe('primary',False,400)
        self.assertEqual(g.destination(),(None,None))
        ready(g,'standby'); self.assertEqual(g.route,'standby')
        ready(g,'primary'); self.assertEqual(g.route,'standby')
        g.switch('primary','test','verified'); self.assertEqual(g.route,'primary')
        with self.assertRaises(ValueError): g.switch('arbitrary','test','bad')

    def test_stale_health_cannot_authorize_route(self):
        g=RoutingGuard('p','s'); ready(g,'primary'); ready(g,'standby')
        g.health['standby']['observed_at']=time.time()-3
        with self.assertRaises(ValueError): g.switch('standby','test','stale')

    def test_write_timeout_dispatches_once_and_marks_unknown_outcome(self):
        g=RoutingGuard('p','s'); ready(g,'primary'); ready(g,'standby')
        Proxy,_=handlers(g,'secret')
        server=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch('gateway.urllib.request.urlopen',side_effect=urllib.error.URLError('timeout')) as upstream:
                response=requests.post(f'http://127.0.0.1:{server.server_port}/transactions',json={'uuid':'one'},timeout=2)
            self.assertEqual(response.status_code,502); upstream.assert_called_once()
            self.assertEqual(g.requests['writes'],1); self.assertEqual(g.requests['standby'],0)
            self.assertEqual(g.requests['ambiguous_writes'],1)
        finally:server.shutdown(); server.server_close(); thread.join()

    def test_admin_requires_token_and_verified_destination(self):
        g=RoutingGuard('p','s'); ready(g,'primary')
        _,Admin=handlers(g,'secret'); server=ThreadingHTTPServer(('127.0.0.1',0),Admin)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            self.assertEqual(requests.get(base+'/status').status_code,403)
            self.assertEqual(requests.post(base+'/route',json={'route':'standby'},headers={'X-Resilience-Token':'secret'}).status_code,409)
            self.assertEqual(requests.get(base+'/status',headers={'X-Resilience-Token':'secret'}).status_code,200)
        finally:server.shutdown(); server.server_close(); thread.join()

    def test_tolerance_requires_actual_protected_probe_and_standby(self):
        g=RoutingGuard('p','s'); ready(g,'primary'); ready(g,'standby'); g.switch('standby','test','x')
        s={'enabled':True,'routing':g.snapshot(),'protected_probe':{'ok':True}}
        self.assertTrue(tolerance_verified(s)); s['protected_probe']['ok']=False
        self.assertFalse(tolerance_verified(s))


class BusinessTests(unittest.TestCase):
    def test_shared_budget_cannot_exceed_authorized_count(self):
        budget=PaymentBudget(13)
        self.assertEqual(sum(budget.reserve() for _ in range(20)),13)
        self.assertEqual(budget.attempted,13)
    def test_payment_timeout_is_not_retried_and_cap_is_three(self):
        session=MagicMock(); session.cookies={'token':'private'}
        response=MagicMock(status_code=200,text='<select name="account_num"><option value="1234567890">Demo</option></select>')
        session.post.return_value=response; session.get.return_value=response
        frontend=MagicMock(attrs={'Config':{'Env':['DEFAULT_USERNAME=demo','DEFAULT_PASSWORD=private']}})
        with patch('suite.requests.Session',return_value=session): probe=DemoProbe(frontend,True)
        session.headers.update.assert_called_once_with({'Connection':'close'})
        session.post.reset_mock(); session.post.side_effect=requests.Timeout()
        reports=[probe.check('fault') for _ in range(4)]
        self.assertEqual(session.post.call_count,3)
        self.assertTrue(reports[0]['payment']['outcome_unknown'])
        self.assertFalse(reports[0]['payment']['retried'])
        self.assertFalse(reports[3]['payment']['attempted'])
        ids=[r['payment']['request_id'] for r in reports[:3]]
        self.assertEqual(len(set(ids)),3)

    def test_one_payment_allowance_is_reserved_for_the_active_fault(self):
        session=MagicMock();session.cookies={'token':'private'}
        response=MagicMock(status_code=200,text='<select name="account_num"><option value="1234567890">Demo</option></select>Payment successful')
        session.post.return_value=response;session.get.return_value=response
        frontend=MagicMock(attrs={'Config':{'Env':['DEFAULT_USERNAME=demo','DEFAULT_PASSWORD=private']}})
        with patch('suite.requests.Session',return_value=session):probe=DemoProbe(frontend,True,PaymentBudget(1),1)
        session.post.reset_mock()
        self.assertFalse(probe.check('baseline')['payment']['attempted'])
        self.assertTrue(probe.check('fault')['payment']['ok'])
        self.assertFalse(probe.check('fault')['payment']['attempted'])
        self.assertFalse(probe.check('recovery')['payment']['attempted'])
        session.post.assert_called_once()

    def test_summary_does_not_treat_cleanup_observations_as_tolerance(self):
        r={'id':'x','target':'ledgerwriter','experiment_type':'network-slowdown','status':'recovered',
           'diagnosis':{},'fault_metrics':[], 'recovery_verified':True}
        summary=experiment_summary(r)
        self.assertFalse(summary['tolerance_verified']); self.assertEqual(summary['protected_route']['sample_count'],0)


class SuiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_escalation_cannot_end_healthy_tolerance_and_history_is_scoped(self):
        r=main.new_result('network-slowdown');r['fault_start_time']=main.utc_now()
        sample={'enabled':True,'protected_probe':{'ok':True},'routing':{'available':True,'route':'standby',
                'health':{'standby':{'healthy_streak':10}},'events':[{'time':1,'reason':'old fault'}],
                'requests':{'failed':99},'last_failure':{'time':1}}}
        captured=[]
        def advice(task,model,ctx,fallback):
            captured.append(ctx)
            return {**fallback.model_dump(),'action':'escalate','mode':'gemini'}
        with patch.object(main,'reason',side_effect=advice),patch.object(main,'resilience') as manager:
            manager.sample.return_value=sample
            await main.resilience_advice(r,{'resilience':sample,'target_probe':{'ok':False},'services':[]})
        self.assertTrue(r['review_requested']);self.assertNotIn('resilience_escalate',r)
        routing=captured[0]['evidence']['routing-observation']['routing']
        self.assertEqual(routing['events'],[]);self.assertNotIn('requests',routing);self.assertNotIn('last_failure',routing)

    async def asyncSetUp(self):
        main.suite_cancel.clear(); main.manual_kill.clear()
        self.saved=dict(main.state)

    async def asyncTearDown(self):
        main.state.clear(); main.state.update(self.saved)

    async def exercise(self, fail_at=None, cancel_at=None):
        suite={'id':'suite','status':'running','started_at':main.utc_now(),'order':list(main.CATALOGUE),
               'results':[],'experiment_ids':[],'resilience_enabled':True,'verify_payments':False}
        seen=[]
        async def run(r,budget=None):
            seen.append(r['experiment_type'])
            r['recovery_verified']=len(seen)!=fail_at
            r['status']='recovered' if r['recovery_verified'] else 'manual-escalation'
            if len(seen)==cancel_at:main.suite_cancel.set()
            main.state['active']=False
        services=[{'service':s,'status':'running'} for s in main.SERVICES]
        def reason(task,model,ctx,fallback):return {**fallback.model_dump(),'mode':'deterministic-fallback'}
        with patch.object(main,'run_experiment',side_effect=run),patch.object(main,'snapshot_containers',return_value=services),patch.object(main,'persist'),patch.object(main,'persist_suite'),patch.object(main,'reason',side_effect=reason):
            await main.run_suite(suite)
        return suite,seen

    async def test_suite_runs_each_fault_once_in_order_and_records_summary(self):
        suite,seen=await self.exercise()
        self.assertEqual(seen,list(main.CATALOGUE));self.assertEqual(suite['status'],'completed')
        self.assertEqual(len(suite['results']),len(main.CATALOGUE));self.assertEqual(len(set(suite['experiment_ids'])),len(main.CATALOGUE))
        self.assertIn('headline',suite['summary'])

    async def test_suite_stops_before_next_fault_if_recovery_unverified(self):
        suite,seen=await self.exercise(fail_at=1)
        self.assertEqual(len(seen),1);self.assertEqual(suite['status'],'failed')

    async def test_suite_cancellation_prevents_next_injection(self):
        suite,seen=await self.exercise(cancel_at=1)
        self.assertEqual(len(seen),1);self.assertEqual(suite['status'],'cancelled')

    async def test_other_operations_are_blocked_during_suite(self):
        main.state['suite_running']=True
        with self.assertRaises(main.HTTPException) as e:
            await main.start_experiment('network-slowdown',main.StartExperimentRequest(confirm=True))
        self.assertEqual(e.exception.status_code,409)
        with self.assertRaises(main.HTTPException):await main.refresh_baseline(main.BaselineRequest())

    async def test_restoration_requires_all_five_backend_probes_and_preserves_failed_report(self):
        from test_runtime import baseline
        report=baseline();report['id']='fresh-baseline'
        for p in report['telemetry']:p['service_probes']={s:{'ok':True} for s in ['userservice','contacts','ledgerwriter','balancereader','transactionhistory']}
        prior={'recovery_verified':False};main.state.update(active=False,suite_running=False,baseline_collecting=False,safety_state='manual-escalation',last_result=prior)
        with patch.object(main,'collect_baseline',return_value=report),patch.object(main,'save_baseline'),patch.object(main,'resilience') as manager:
            manager.installed.return_value=False
            response=await main.verify_restoration()
        self.assertTrue(response['verified']);self.assertEqual(main.state['safety_state'],'idle')
        self.assertFalse(prior['recovery_verified'])
        report['telemetry'][0]['service_probes']['contacts']['ok']=False
        main.state['safety_state']='manual-escalation'
        with patch.object(main,'collect_baseline',return_value=report):
            with self.assertRaises(main.HTTPException):await main.verify_restoration()
        self.assertEqual(main.state['safety_state'],'manual-escalation')


class ToleranceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, unsafe=False, lose_standby=False):
        from test_runtime import result, baseline, observation, fallback_reason
        r=result('ledgerwriter-outage');r['resilience_enabled']=True
        target=MagicMock(status='running');target.logs.return_value=b'process exited'
        routing={'route':'standby','available':True,'health':{'standby':{'healthy_streak':10},
                 'primary':{'ok':False,'http_ready':False,'latency_ms':400}}}
        seen=[]
        def point(t,phase,id):
            p=observation(t,phase,id,ok=phase!='fault')
            p['resilience']={'enabled':True,'routing':routing,'protected_probe':{'ok':True,'latency_ms':10}}
            if phase=='fault':
                seen.append(p)
                if unsafe:p['frontend']['ok']=False
                if lose_standby and len(seen)>1:p['resilience']['protected_probe']['ok']=False
            return p
        spec={**main.CATALOGUE['ledgerwriter-outage'],'max_seconds':1.5}
        main.manual_kill.clear()
        with patch.dict(main.CATALOGUE,{'ledgerwriter-outage':spec}),patch.object(main,'resilience') as manager,patch.object(main,'collect_baseline',baseline),patch.object(main,'get_service_container',return_value=target),patch.object(main,'point',side_effect=point),patch.object(main,'reason',side_effect=fallback_reason),patch.object(main,'persist'),patch.object(main,'save_baseline'):
            manager.sample.side_effect=lambda target:{'enabled':True,'routing':routing,
                'protected_probe':{'ok':not(lose_standby and len(seen)>1),'latency_ms':10}}
            await main.run_experiment(r)
        return r,target

    async def test_healthy_standby_keeps_fault_active_until_bounded_window(self):
        r,t=await self.exercise()
        self.assertEqual(r['intervention_trigger'],'tolerance-window-complete')
        self.assertGreaterEqual(r['time_to_mitigation'],1.4)
        self.assertTrue(r['recovery_verified']);t.start.assert_called_once()
        self.assertTrue(r['tolerance_report']['tolerance_verified'])

    async def test_other_service_failure_still_forces_immediate_containment(self):
        r,t=await self.exercise(unsafe=True)
        self.assertEqual(r['intervention_trigger'],'metric')
        self.assertLess(r['time_to_mitigation'],1)
        t.start.assert_called_once()

    async def test_loss_of_standby_forces_cleanup_before_next_observation(self):
        r,t=await self.exercise(lose_standby=True)
        self.assertEqual(r['intervention_trigger'],'resilience-failed')
        self.assertLess(r['time_to_mitigation'],1)
        t.start.assert_called_once()
