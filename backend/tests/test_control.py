import unittest
from pydantic import ValidationError
from control import *
class GuardrailTests(unittest.TestCase):
    def test_all_inverses(self):
        for kind,spec in CATALOGUE.items():
            a=Action(action=spec['action'],target=spec['target'],experiment_id='a')
            self.assertEqual(approve(a,{'id':'a','experiment_type':kind},active=True,recovery_due=True),a.model_dump())
    def test_cross_experiment_and_wrong_action_rejected(self):
        exp={'id':'a','experiment_type':'network-slowdown'}
        for a in [Action(action='start-service',target='ledgerwriter',experiment_id='a'),Action(action='remove-network-impairment',target='contacts',experiment_id='a'),Action(action='remove-network-impairment',target='ledgerwriter',experiment_id='old')]:
            with self.assertRaises(ValueError): approve(a,exp,active=True,recovery_due=True)
    def test_idle_or_not_due_rejected(self):
        a=Action(action='start-service',target='ledgerwriter',experiment_id='a')
        for active,due in [(False,True),(True,False)]:
            with self.assertRaises(ValueError): approve(a,{'id':'a','experiment_type':'ledgerwriter-outage'},active=active,recovery_due=due)
    def test_arbitrary_commands_and_extra_fields_rejected(self):
        for data in [dict(action='shell',target='ledgerwriter',experiment_id='a'),dict(action='start-service',target='ledger-db',experiment_id='a'),dict(action='start-service',target='ledgerwriter',experiment_id='a',command='rm -rf /')]:
            with self.assertRaises(ValidationError): Action.model_validate(data)
    def test_service_health_requires_all_expected(self):
        services=[{'service':s,'status':'running'} for s in SERVICES]
        self.assertTrue(healthy(services));self.assertFalse(healthy(services[:-1]))
        services[0]['health']='unhealthy';self.assertFalse(healthy(services))
    def test_network_outlier_requires_consecutive_breaches(self):
        def p(ms,ok=True):return {'target_probe':{'latency_ms':ms,'ok':ok}}
        self.assertFalse(network_breached([p(1900)]))
        self.assertFalse(network_breached([p(1900),p(60)]))
        self.assertTrue(network_breached([p(1800),p(1700)]))
        self.assertTrue(network_breached([p(None,False),p(None,False)]))
    def test_baseline_errors_not_hidden(self):
        report=latency_summary([{'ok':True,'latency_ms':10},{'ok':True,'latency_ms':20},{'ok':False,'latency_ms':3000}])
        self.assertEqual(report['latency_ms']['median'],15)
        self.assertEqual(report['timeout_count'],1)
        self.assertEqual(report['error_rate_percent'],33.33)
    def test_measured_score_changes_with_recovery_failure(self):
        p={'target_probe':{'ok':True,'latency_ms':60},'frontend':{'ok':True}}
        r={'target':'ledgerwriter','fault_metrics':[p]*3,'baseline':{'ledgerwriter':{'latency_ms':{'p95':100}}},'recovery_time':0,'detection_time':0,'time_to_mitigation':0,'recovery_verified':True}
        self.assertEqual(score(r)['value'],100)
        r['recovery_verified']=False;self.assertEqual(score(r)['value'],80)
    def test_fallback_diagnoses_are_fault_specific(self):
        for kind,spec in CATALOGUE.items():
            r={'id':'a','experiment_type':kind,'target':spec['target'],'fault_metrics':[],'injected':True}
            d=fallback_diagnosis(r)
            self.assertEqual(d.experiment_type,kind);self.assertEqual(d.recommended_action.action,spec['action'])
if __name__=='__main__':unittest.main()

class MountPreflightTests(unittest.TestCase):
    def test_missing_or_directory_public_key_blocks_restart_fault(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from main import validate_bind_mounts
        with tempfile.TemporaryDirectory() as directory:
            for source in [str(Path(directory)/'missing'),directory]:
                container=SimpleNamespace(attrs={'Mounts':[{'Type':'bind','Source':source,'Destination':'/tmp/.ssh/publickey'}]})
                with self.assertRaises(RuntimeError):validate_bind_mounts(container)
            key=Path(directory)/'key.pub';key.write_text('public-key-fixture')
            validate_bind_mounts(SimpleNamespace(attrs={'Mounts':[{'Type':'bind','Source':str(key),'Destination':'/tmp/.ssh/publickey'}]}))
