"""Offline regression tests: evidence snippets, real names, leads and finish checks."""
import json
from unittest.mock import MagicMock
import test_check_limits as fixtures

from collect_images.common import CollectorError
from check_limits.agent import validate_findings
from check_limits.tools import EvidenceTools, MCPClient, MCPToolError
from test_check_limits import Base, SCHEMAS, extracted, final, message, tool_call


class SearchTests(Base):
    def test_actual_constructor_hints_do_not_guess_or_repeat_read_targets(self):
        self.mcp.call = lambda *a: {'response': 'class Page { void show(){ new obf.Callback(); new java.util.Date(); new Simple(); } }'}
        out = self.tools.call('get_class_source', {'class_name': 'test.Page'})
        self.assertEqual([v['target'] for v in out['unread_direct_targets']], ['obf.Callback'])
        self.assertEqual(out['unread_direct_targets'][0]['source_id'], 'E001')
        self.tools.call('get_class_source', {'class_name': 'obf.Callback'})
        self.assertEqual(self.tools.unread_source_targets(), [])

    def test_mcp_line_number_gutters_are_not_required_in_source_quotes(self):
        from test_check_limits import SOURCE
        numbered = '\n'.join('%2d| %s' % (i, line) for i, line in enumerate(SOURCE.splitlines(), 1))
        self.mcp.call = lambda *a: {'response': numbered}
        out = self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertNotIn('1|', out['text'])
        validate_findings(final(), extracted()['rules'], self.tools.evidence)
        raw = json.loads((self.rec.root / 'code_evidence/E001.json').read_text())
        self.assertIn('1|', raw['result']['response'])

    def test_evidence_search_can_reveal_unseen_source_and_preserve_citations(self):
        self.tools.max_chars = 55
        original = self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertTrue(original['truncated'])
        with self.assertRaises(CollectorError):
            validate_findings(final(), extracted()['rules'], self.tools.evidence)
        self.tools.max_chars = 2000
        found = self.tools.call('search_evidence', {'query': 'STOPAFTER'})
        self.assertEqual(found['matches'][0]['evidence_id'], 'E001')
        self.assertIn('stopAfter', found['matches'][0]['text'])
        validate_findings(final(), extracted()['rules'], self.tools.evidence)
        self.assertEqual(len(self.mcp.calls), 1)

    def test_evidence_search_handles_long_line_and_paginates_without_skipping(self):
        long = 'x' * 22000 + 'ACTUAL_MATCH' + 'y' * 2000
        self.mcp.call = lambda *a: {'response': long + '\nACTUAL_MATCH again\nACTUAL_MATCH third'}
        self.tools.call('get_class_source', {'class_name': 'test.Long'})
        first = self.tools.call('search_evidence', {'query': 'ACTUAL_MATCH', 'count': 1})
        self.assertIn('ACTUAL_MATCH', first['matches'][0]['text'])
        self.assertTrue(first['matches'][0]['clipped_line'])
        self.assertEqual(first['next_offset'], 1)
        second = self.tools.call('search_evidence', {'query': 'ACTUAL_MATCH', 'offset': first['next_offset'], 'count': 1})
        self.assertEqual(second['total_matches'], 3)
        self.assertEqual(second['matches'][0]['start_line'], 2)
        for args in ({'query': ' '}, {'query': 'x', 'id': 'E999'}, {'query': 'x', 'offset': -1}):
            with self.assertRaises(CollectorError):
                self.tools.call('search_evidence', args)

    def test_class_name_index_is_metadata_only_complete_and_cached(self):
        schemas = dict(SCHEMAS, get_all_classes={'name': 'get_all_classes', 'inputSchema': {}})
        replies = iter([
            {'items': ['test.Timer', 'test.Outer.a'], 'pagination': {'total': 3, 'has_more': True, 'next_offset': 2}},
            {'items': ['mixed.Callback'], 'pagination': {'total': 3, 'has_more': False}}])
        self.mcp.call = MagicMock(side_effect=lambda *a: next(replies))
        tools = EvidenceTools(self.mcp, schemas, self.rec)
        result = json.loads(tools.call('find_class_names', {'query': 'test.Outer'})['text'])
        self.assertEqual(result['items'], ['test.Outer.a'])
        self.assertEqual(result['index_classes'], 3)
        tools.call('find_class_names', {'query': 'Callback'})
        self.assertEqual(self.mcp.call.call_count, 2)
        self.assertTrue(all(c.args[0] == 'get_all_classes' for c in self.mcp.call.call_args_list))

    def test_broken_class_index_is_not_cached_as_complete(self):
        schemas = dict(SCHEMAS, get_all_classes={'name': 'get_all_classes', 'inputSchema': {}})
        self.mcp.call = lambda *a: {'items': ['x'], 'pagination': {'total': 2, 'has_more': False}}
        tools = EvidenceTools(self.mcp, schemas, self.rec)
        with self.assertRaisesRegex(CollectorError, '缺失'):
            tools.call('find_class_names', {'query': 'x'})
        self.assertIsNone(tools.class_names)

    def test_duplicate_display_aliases_are_not_missing_index_entries(self):
        schemas = dict(SCHEMAS, get_all_classes={'name': 'get_all_classes', 'inputSchema': {}})
        self.mcp.call = lambda *a: {'items': ['test.Outer.a', 'test.Outer.a'],
                                   'pagination': {'total': 2, 'has_more': False}}
        tools = EvidenceTools(self.mcp, schemas, self.rec)
        out = json.loads(tools.call('find_class_names', {'query': 'Outer'})['text'])
        self.assertEqual(out['index_classes'], 2)
        self.assertEqual(out['total_matches'], 2)

    def test_missing_class_does_not_reparse_apk_and_failure_is_classified(self):
        self.mcp.call = MagicMock(side_effect=MCPToolError('HTTP 404: Class Missing not found'))
        recover = MagicMock()
        tools = EvidenceTools(self.mcp, SCHEMAS, self.rec, recovery=recover)
        out = tools.call('get_class_source', {'class_name': 'test.Missing'})
        self.assertEqual(out['error_kind'], 'class_not_found')
        recover.assert_not_called()
        with self.assertRaises(CollectorError):
            tools.call('get_class_source', {'class_name': 'test.Missing'})
        self.assertEqual(self.mcp.call.call_count, 1)

    def test_mcp_preserves_error_category_in_structured_or_text_output(self):
        client = MCPClient('http://127.0.0.1:12345/mcp')
        for payload in (
            {'structuredContent': {'error': 'HTTP 404: Method foo not found in class T'}},
            {'content': [{'type': 'text', 'text': json.dumps({'error': 'HTTP 404: Method foo not found in class T'})}]},
            {'isError': True, 'content': [{'type': 'text', 'text': 'Method foo not found'}]}):
            client.rpc = lambda *a: payload
            with self.assertRaises(MCPToolError) as error:
                client.call('get_method_by_name', {'class_name': 'T', 'method_name': 'foo'})
            self.assertEqual(error.exception.category, 'method_not_found')

    def test_lead_status_requires_actual_evidence_and_stable_identity(self):
        self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        lead = {'id': 'L1', 'target': 'Timer.start', 'source_id': 'E001', 'status': 'pending', 'note': ''}
        self.assertEqual(len(self.tools.call('record_lead', lead)['pending']), 1)
        for edit in ({'status': 'checked', 'note': 'done'}, {'status': 'blocked'}, {'target': 'Different'}):
            with self.assertRaises(CollectorError):
                self.tools.call('record_lead', dict(lead, **edit))
        closed = dict(lead, status='checked', note='已读方法', result_id='E001')
        self.assertEqual(self.tools.call('record_lead', closed)['pending'], [])
        self.assertTrue((self.rec.root / 'leads.json').is_file())

    def test_final_review_is_mandatory_but_does_not_change_semantic_verdict(self):
        unknown = final('unknown')
        self.assertEqual(validate_findings(unknown, extracted()['rules'], {})['findings'][0]['verdict'], 'unknown')
        del unknown['search_review']
        with self.assertRaisesRegex(CollectorError, 'search_review'):
            validate_findings(unknown, extracted()['rules'], {})


class LeadFinishTests(Base):
    def run_agent(self, answers):
        return fixtures.AgentTests.run_agent(self, answers)

    def test_unread_direct_callback_gets_one_recheck_not_an_endless_gate(self):
        self.mcp.call = lambda *a: {'response': 'class Page { void show(){ new obf.Callback(); } }'}
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Page'})]),
            message(calls=[tool_call('submit_result', final('unknown'), 'first')]),
            message(calls=[tool_call('submit_result', final('unknown'), 'second')]),
        ])
        result = agent.run()
        self.assertEqual(result['findings'][0]['verdict'], 'unknown')
        self.assertIn('obf.Callback', client.requests[-1][0][-1]['content'])
        self.assertEqual(self.rec.trace_path.read_text().count('"event": "finish_recheck"'), 1)

    def test_pending_lead_blocks_premature_submit_until_actual_update(self):
        lead = {'id': 'L1', 'target': 'Timer.start', 'source_id': 'E001', 'status': 'pending', 'note': ''}
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'})]),
            message(calls=[tool_call('record_lead', lead)]),
            message(calls=[tool_call('submit_result', final('unknown'), 'early')]),
            message(calls=[tool_call('record_lead', dict(lead, status='checked', note='已读方法', result_id='E001'))]),
            message(calls=[tool_call('submit_result', final('unknown'), 'done')]),
        ])
        result = agent.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['leads'][0]['status'], 'checked')
        self.assertIn('pending', client.requests[4][0][-1]['content'])

    def test_budget_finish_preserves_unresolved_leads_without_extra_model(self):
        self.cfg['max_model_calls'] = 4
        lead = {'id': 'L1', 'target': 'Timer.start', 'source_id': 'E001', 'status': 'pending', 'note': ''}
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'})]),
            message(calls=[tool_call('record_lead', lead)]),
            message(calls=[tool_call('submit_result', final('unknown'), 'done')])])
        result = agent.run()
        self.assertTrue(client.requests[-1][1]['final_only'])
        self.assertIn('L1', result['limitations'][-1])
        self.assertEqual(result['findings'][0]['verdict'], 'unknown')
