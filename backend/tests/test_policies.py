import unittest
from policies import safety_policy,evaluate,graph_features
from control import latency_summary,fallback_plan,TOPOLOGY
class PolicyTests(unittest.TestCase):
 def setUp(self):self.policy=safety_policy('network-slowdown',{'ledgerwriter':{'latency_ms':{'p95':100}}})
 def point(self,ok=True,latency=60):return {'time':'now','target':'ledgerwriter','target_probe':{'ok':ok,'latency_ms':latency},'resources':{}}
 def test_no_fixed_duration_primary_trigger(self):
  self.assertEqual(evaluate([self.point(latency=1800)],self.policy)['level'],'warning')
  self.assertEqual(evaluate([self.point(latency=1800),self.point(latency=1600)],self.policy)['level'],'critical')
 def test_one_outlier_does_not_trip(self):
  self.assertNotEqual(evaluate([self.point(latency=1800),self.point()],self.policy)['level'],'critical')
 def test_availability_and_errors(self):
  r=evaluate([self.point(False),self.point(False)],self.policy)
  self.assertEqual(r['level'],'critical');self.assertEqual(r['rolling_error_rate_percent'],100)
 def test_unknown_is_not_measured_failure(self):
  p=self.point(None,None)
  self.assertEqual(evaluate([p,p],self.policy)['level'],'normal')
  summary=latency_summary([{'ok':None,'latency_ms':None}])
  self.assertIsNone(summary['availability_percent']);self.assertEqual(summary['unavailable_measurement_count'],1)
 def test_observed_blast_radius_immediate(self):
  p=self.point(False);p['service_probes']={'contacts':{'ok':False}}
  self.assertEqual(evaluate([p],self.policy)['level'],'critical')
 def test_resources_confirmed(self):
  p=self.point();p['resources']['cpu_percent']=99
  self.assertEqual(evaluate([p,p],self.policy)['level'],'critical')
 def test_topology_changes_fallback_order(self):
  edges=list(TOPOLOGY)+[('ledgerwriter','contacts'),('transactionhistory','contacts')]
  self.assertGreater(graph_features(edges)['contacts']['criticality_score'],graph_features()['contacts']['criticality_score'])
  plan=fallback_plan({'topology':edges,'history':[]})
  self.assertEqual(plan.recommendations[0].target,'contacts')

 def test_latency_degraded_target_counts_as_observed_impact(self):
  from policies import observed_affected
  p=self.point(latency=1800)
  self.assertEqual(observed_affected(p,self.policy),['ledgerwriter'])
  self.assertEqual(evaluate([p,p],self.policy)['affected_services'],['ledgerwriter'])
