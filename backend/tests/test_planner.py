import unittest
from planner import assess_system
from findings import render_findings
from main import cleanup_compound
from unittest.mock import patch,MagicMock

def row(kind,score,target='ledgerwriter',verified=True):
    return {'result':{'id':kind,'name':kind,'experiment_type':kind,'target':target,'status':'recovered',
        'recovery_verified':verified,'resilience_score':{'value':score,'penalties':{'target_errors':20}},
        'analysis':{'mode':'gemini','severity':'HIGH','remediation':['Evaluate readiness checks']}}}

class SystemAssessmentTests(unittest.TestCase):
    def test_severity_and_graph_tie_breaking_and_overall_coverage(self):
        rows=[row('contacts-pause',69,'contacts'),row('ledgerwriter-outage',69),row('network-slowdown',85)]
        r=assess_system(rows)
        self.assertEqual(r['weaknesses'][0]['experiment_type'],'ledgerwriter-outage')
        self.assertEqual(r['overall_score'],74.3)
        self.assertEqual(r['coverage']['measured_fault_types'],3)
        self.assertIn('frontend',r['weaknesses'][0]['justification'])
        self.assertIn('ledger-db',r['weaknesses'][0]['justification'])

    def test_latest_only_missing_scores_and_compound_exclusion(self):
        r=assess_system([row('network-slowdown',80),row('network-slowdown',20),row('latency-cpu',50)])
        self.assertEqual(r['overall_score'],80)
        self.assertEqual(r['coverage']['measured_fault_types'],1)
        self.assertIsNone(assess_system([])['overall_score'])
        blocked=row('cpu-stress',100);blocked['result']['status']='blocked-before-injection'
        self.assertIsNone(assess_system([blocked])['overall_score'])

    def test_failed_recovery_ranks_first(self):
        r=assess_system([row('network-slowdown',90,verified=False),row('ledgerwriter-outage',10)])
        self.assertEqual(r['weaknesses'][0]['severity'],'CRITICAL')

    def test_report_marks_missing_evidence_and_ai_provenance(self):
        report=render_findings([row('network-slowdown',80)])
        self.assertIn('No recovered evidence',report)
        self.assertIn('No saved Gemini',report)

    def test_compound_cleanup_attempts_second_inverse_after_first_fails(self):
        with patch('main.remove_cpu_stress',side_effect=RuntimeError('CPU cleanup failed')) as cpu,patch('main.remove_network_delay') as network:
            with self.assertRaises(RuntimeError):cleanup_compound(MagicMock(),MagicMock())
        cpu.assert_called_once();network.assert_called_once()
