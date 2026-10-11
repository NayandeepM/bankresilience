import os
import unittest
from unittest.mock import patch, MagicMock
import requests
from control import Plan,fallback_plan
from reasoning import reason
class ReasoningTests(unittest.TestCase):
    def test_missing_key_honestly_labels_fallback(self):
        with patch.dict(os.environ,{'GEMINI_API_KEY':''}):
            output=reason('rank',Plan,{},fallback_plan({}))
        self.assertEqual(output['mode'],'deterministic-fallback')
    def test_timeout_and_invalid_json_preserve_safe_fallback(self):
        for response in [requests.Timeout(),MagicMock(json=lambda:{'candidates':[{'content':{'parts':[{'text':'{"shell":"delete"}'}]}}]})]:
            with patch.dict(os.environ,{'GEMINI_API_KEY':'test-key'}),patch('reasoning.requests.post',side_effect=response if isinstance(response,Exception) else None,return_value=response):
                output=reason('rank',Plan,{},fallback_plan({}))
            self.assertEqual(output['mode'],'deterministic-fallback')
            self.assertNotIn('test-key',str(output))
    def test_valid_structured_response_is_labeled_gemini(self):
        fallback=fallback_plan({})
        response=MagicMock();response.json.return_value={'candidates':[{'content':{'parts':[{'text':fallback.model_dump_json()}]}}]}
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-key'}),patch('reasoning.requests.post',return_value=response) as post:
            output=reason('rank',Plan,{},fallback)
        self.assertEqual(output['mode'],'gemini')
        self.assertIn('responseJsonSchema',post.call_args.kwargs['json']['generationConfig'])
