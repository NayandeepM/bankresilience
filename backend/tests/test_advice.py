import copy,json,unittest
from unittest.mock import MagicMock,patch
from advice import diagnostic_context,fallback_advice,validate_advice,advisory_decision,CAPABILITIES
from control import CATALOGUE,TOPOLOGY

def fixture(kind='network-slowdown',status='running',level='warning'):
 target=CATALOGUE[kind]['target']
 p={'time':'now','phase':'fault','experiment_type':kind,'target':target,'target_probe':{'ok':True,'latency_ms':1800},'frontend':{'ok':True,'latency_ms':5},'services':[{'service':target,'status':status}]}
 return {'id':'a','experiment_type':kind,'target':target,'fault_parameters':{'delay_ms':800},'injected':True,'fault_metrics':[p],'baseline':{target:{'latency_ms':{'p95':60}},'frontend':{'availability_percent':100}},'safety_policy':{'latency_ms':{'warning':250,'critical':1500},'max_duration_seconds':10},'policy_evaluation':{'level':level},'network_observation':{'controller_owned_netem':True}}
def answer(r):
 a=fallback_advice(r).model_dump();a.update(mode='gemini',evidence_refs=['observation-1'],inferred_condition='network-latency')
 return a
class AdviceTests(unittest.TestCase):
 def test_input_and_schema_withhold_fault_answer(self):
  from advice import RecoveryAdvice
  r=fixture();data=json.dumps(diagnostic_context(r))+json.dumps(RecoveryAdvice.model_json_schema())
  for label in CATALOGUE:self.assertNotIn(label,data)
  for name in ('fault_parameters','allowed_action','experiment_type','injected','phase','max_duration_seconds'):self.assertNotIn(name,data)
 def test_capabilities_identical_across_faults(self):
  choices=[diagnostic_context(fixture(k))['capabilities'] for k in CATALOGUE]
  self.assertTrue(all(c==CAPABILITIES for c in choices))
 def test_unknown_evidence_reference_rejected(self):
  r=fixture();a=answer(r);a['evidence_refs']=['invented']
  with self.assertRaises(ValueError):validate_advice(a,diagnostic_context(r),r)
 def test_improvement_evidence_checked(self):
  r=fixture();a=answer(r);a['improvement_plan'][0]['evidence_refs']=['invented']
  with self.assertRaises(ValueError):validate_advice(a,diagnostic_context(r),r)
 def test_wrong_id_or_target_rejected(self):
  r=fixture()
  for change in ({'experiment_id':'other'},{'target':'contacts'}):
   a=answer(r);a['recommended_action'].update(change)
   with self.assertRaises(ValueError):validate_advice(a,diagnostic_context(r),r)
 def test_observation_bounded_by_critical_policy(self):
  r=fixture();a=answer(r);a['recommended_action']['action']='observe'
  self.assertEqual(advisory_decision(a,r),'observe')
  r['policy_evaluation']['level']='critical'
  with self.assertRaises(ValueError):advisory_decision(a,r)
 def test_network_removal_requires_verified_policy(self):
  r=fixture();a=answer(r)
  self.assertEqual(advisory_decision(a,r),'mitigate')
  r['network_observation']['controller_owned_netem']=False
  with self.assertRaises(ValueError):advisory_decision(a,r)
 def test_start_running_service_rejected(self):
  r=fixture('ledgerwriter-outage');a=answer(r)
  with self.assertRaises(ValueError):advisory_decision(a,r)
  r['fault_metrics'][0]['services'][0]['status']='exited'
  self.assertEqual(advisory_decision(a,r),'mitigate')
 def test_paused_process_requires_unpause(self):
  r=fixture('contacts-pause',status='paused');a=answer(r)
  self.assertEqual(advisory_decision(a,r),'mitigate')
  r['fault_metrics'][0]['services'][0]['status']='running'
  with self.assertRaises(ValueError):advisory_decision(a,r)
 def test_low_confidence_mutation_rejected(self):
  r=fixture();a=answer(r);a['confidence']=.4
  with self.assertRaises(ValueError):advisory_decision(a,r)
 def test_inspection_uses_fixed_read_only_command(self):
  from main import inspect_network_policy
  helper=MagicMock();helper.exec_run.return_value=type('Response',(),{'exit_code':0,'output':b'qdisc netem delay 800ms'})()
  result=inspect_network_policy(helper)
  self.assertTrue(result['controller_owned_netem'])
  helper.exec_run.assert_called_once_with(['tc','qdisc','show','dev','eth0'],stdout=True,stderr=True)
 def test_mutation_cannot_use_only_baseline_reference(self):
  r=fixture();a=answer(r);a['evidence_refs']=['baseline']
  with self.assertRaises(ValueError):validate_advice(a,diagnostic_context(r),r)

 def test_provider_schema_limits_references_to_supplied_ids(self):
  from advice import advice_contract
  from pydantic import ValidationError
  r=fixture();c=diagnostic_context(r);model=advice_contract(c)
  a=answer(r);a.pop('mode');model.model_validate(a)
  a['evidence_refs']=['invented']
  with self.assertRaises(ValidationError):model.model_validate(a)
  schema=json.dumps(model.model_json_schema())
  self.assertIn('network-policy',schema);self.assertNotIn('network-slowdown',schema)

 def test_brief_schema_withholds_fault_and_contains_no_engineering_plan(self):
  from advice import advice_contract,ImmediateAdvice
  c=diagnostic_context(fixture());model=advice_contract(c,brief=True)
  self.assertTrue(issubclass(model,ImmediateAdvice))
  schema=json.dumps(model.model_json_schema())
  self.assertNotIn('experiment_type',schema)
  self.assertNotIn('improvement_plan',schema)
  self.assertIn('observation-1',schema)
