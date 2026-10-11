import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
import main
from findings import export_report, read_report, report_snapshot


def evidence(review=None):
    return {'result':{'id':'saved-network','name':'Network slowdown','experiment_type':'network-slowdown',
        'target':'ledgerwriter','status':'recovered','recovery_verified':True,
        'resilience_score':{'value':85},'action_review':review or {}}}


class FindingsTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'FINDINGS.md'

    def test_export_preserves_gemini_proposal_and_target(self):
        proposal={'priority':'HIGH','title':'Evaluate caller timeouts','target':'frontend',
            'hypothesis':'Bound the caller wait.','verification':'Measure timeout behavior.',
            'evidence_refs':['saved-network'],'configuration_status':'not-inspected'}
        snapshot=export_report([evidence({'mode':'gemini','improvement_plan':[proposal]})],self.path)
        saved=read_report(self.path)
        self.assertTrue(saved['insights_available'])
        self.assertEqual(saved['markdown'],snapshot['markdown'])
        self.assertEqual(saved['insights'][0]['target'],'frontend')
        self.assertEqual(saved['insights'][0]['source'],'gemini')
        self.assertIn(proposal['hypothesis'],saved['markdown'])
        self.assertEqual(saved['coverage'],{'measured':1,'expected':3})

    def test_missing_report_and_empty_history(self):
        self.assertFalse(read_report(self.path)['available'])
        snapshot=report_snapshot([])
        self.assertIsNone(snapshot['overall_score'])
        self.assertEqual(snapshot['insights'],[])

    def test_manual_edit_hides_stale_insights_until_refresh(self):
        export_report([evidence()],self.path)
        self.path.write_text('Edited report')
        saved=read_report(self.path)
        self.assertEqual(saved['markdown'],'Edited report')
        self.assertTrue(saved['available'])
        self.assertFalse(saved['insights_available'])
        self.assertEqual(saved['insights'],[])
        export_report([evidence()],self.path)
        self.assertTrue(read_report(self.path)['insights_available'])

    def test_invalid_metadata_keeps_markdown_accessible(self):
        export_report([evidence()],self.path)
        for metadata in ('not-json','[]','{}'):
            self.path.with_suffix('.json').write_text(metadata)
            self.assertFalse(read_report(self.path)['insights_available'])
            self.assertTrue(read_report(self.path)['available'])

    def test_fallback_is_not_labeled_gemini(self):
        snapshot=report_snapshot([evidence({'mode':'deterministic-fallback'})])
        self.assertEqual(snapshot['insights'][0]['source'],'deterministic-follow-up')
        self.assertIn('No saved Gemini',snapshot['markdown'])

    def test_endpoints_generate_from_history_and_offer_attachment(self):
        with patch.object(main,'FINDINGS_PATH',self.path),patch.object(main,'history_rows',return_value=[evidence()]) as history,patch.dict(main.state,active=False,suite_running=False,baseline_collecting=False):
            with self.assertRaises(HTTPException) as missing:main.download_findings()
            self.assertEqual(missing.exception.status_code,404)
            generated=main.generate_findings()
            history.assert_called_once_with(limit=None)
            self.assertEqual(main.findings_report()['markdown'],generated['markdown'])
            response=main.download_findings()
            self.assertEqual(response.path,self.path)
            self.assertEqual(response.headers['content-disposition'],'attachment; filename="FINDINGS.md"')
            self.assertEqual(response.headers['cache-control'],'no-store')

    def test_generation_rejects_active_operations(self):
        for flag in ('active','suite_running','baseline_collecting'):
            with patch.dict(main.state,active=False,suite_running=False,baseline_collecting=False),patch.object(main,'history_rows') as history:
                main.state[flag]=True
                with self.assertRaises(HTTPException) as blocked:main.generate_findings()
                self.assertEqual(blocked.exception.status_code,409)
                history.assert_not_called()
