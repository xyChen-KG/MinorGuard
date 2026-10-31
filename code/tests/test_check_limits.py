"""No paid calls or server changes: mock model, mocked tools, local MCP HTTP server."""

import base64
import contextlib
import copy
import io
import json
import os
import struct
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from collect_images.common import CODE_ROOT, CollectorError
from collect_images.recorder import Recorder
from check_limits.__main__ import load_settings, main, prepare_images, verify_identity
from check_limits.agent import (BudgetEnded, ChatClient, LimitsAgent, parse_rules, validate_findings)
from check_limits.report import render_report
from check_limits.tools import (EvidenceTools, MCPClient, READ_ONLY, SSHConnection,
                               _binary_manifest, apk_metadata, manifest_identity)


SOURCE = 'class Timer {\n void start() { if (enabled) stopAfter(limit); }\n}\n'
SCHEMAS = {
    'get_class_source': {'name': 'get_class_source', 'description': 'read source', 'inputSchema': {
        'type': 'object', 'properties': {'class_name': {'type': 'string'}}, 'required': ['class_name']}},
    'get_method_by_name': {'name': 'get_method_by_name', 'inputSchema': {
        'type': 'object', 'properties': {'class_name': {'type': 'string'}, 'method_name': {'type': 'string'}},
        'required': ['class_name', 'method_name']}},
    'search_classes_by_keyword': {'name': 'search_classes_by_keyword', 'inputSchema': {
        'type': 'object', 'properties': {'search_term': {'type': 'string'}, 'count': {'type': 'integer'},
                                     'offset': {'type': 'integer'}, 'search_in': {'type': 'string'}, 'package': {'type': 'string'}},
        'required': ['search_term']}}
}


def extracted():
    return {'screen_state': '模式未开启；规则描述开启之后的效果', 'rules': [
        {'id': 'R1', 'statement': '每日最多 45 分钟', 'condition': '未成年人模式开启',
         'source_image': 'I001', 'source_text': '每日最多45分钟'}], 'limitations': []}


def final(verdict='supported', quote='if (enabled) stopAfter(limit);'):
    return {'summary': '发现与页面对应的客户端实现线索。', 'findings': [
        {'rule_id': 'R1', 'verdict': verdict, 'reason': '存在模式判断和限制调用。',
         'code_evidence': [] if verdict == 'unknown' else [{'id': 'E001', 'quote': quote}]}], 'limitations': [],
        'search_review': {'forward': '已读模式条件与调用', 'reverse': '已核对限制动作的调用位置',
                          'missing': '测试数据不代表运行效果', 'stop_reason': '本次测试路径核对结束'}}


def tool_call(name, arguments, identity='t1'):
    return {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def message(content=None, calls=None):
    result = {'role': 'assistant', 'content': content}
    if calls is not None:
        result['tool_calls'] = calls
    return result


class FakeMCP:
    timeout = 45
    def __init__(self):
        self.calls = []
    def close(self):
        pass
    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'get_package_tree':
            return {'total_classes': 1, 'packages': [{'name': 'test.feature', 'class_count': 1}]}
        if name in ('get_class_source', 'get_method_by_name'):
            return {'response': SOURCE}
        return {'classes': ['test.Timer'], 'pagination': {'total': 1}}


class FakeClient:
    def __init__(self, answers):
        self.answers, self.requests = iter(answers), []
    def chat(self, messages, timeout, **kwargs):
        self.requests.append((copy.deepcopy(messages), kwargs))
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer, {'total_tokens': 10}, 'mock-visual-model'


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rec = Recorder(Path(self.temp.name) / 'runs')
        (self.rec.root / 'code_evidence').mkdir()
        self.cfg = load_settings(CODE_ROOT / 'check_config.json')
        self.mcp = FakeMCP()
        self.tools = EvidenceTools(self.mcp, SCHEMAS, self.rec, max_chars=2000)


class InputTests(Base):
    def test_default_model_limit_stays_at_one_hundred(self):
        self.assertEqual(self.cfg['max_model_calls'], 100)
        self.assertEqual(self.cfg['thinking'], 'enabled')
        self.assertEqual(self.cfg['reasoning_effort'], 'high')

    def test_order_images_and_version_metadata_with_both_directory_forms(self):
        folder = Path(self.temp.name) / 'collection'
        (folder / 'images').mkdir(parents=True)
        for name in ('10.png', '2.png', '1.png'):
            Image.new('RGB', (200, 400), 'white').save(folder / 'images' / name)
        (folder / 'result.json').write_text(json.dumps({'package': 'test.app', 'app_version': '1.0'}))
        first, identity = prepare_images(folder, 20, self.rec)
        second, _ = prepare_images(folder / 'images', 20)
        self.assertEqual([Path(x['source']).name for x in first], ['1.png', '2.png', '10.png'])
        self.assertEqual(first, second)
        self.assertEqual(identity, {'package': 'test.app', 'app_version': '1.0'})
        self.assertTrue((self.rec.root / first[0]['file']).is_file())
        self.assertTrue(first[0]['data_url'].startswith('data:image/jpeg;base64,'))
        with self.assertRaises(CollectorError):
            prepare_images(folder, 2)

    def test_no_rules_invented_and_image_reference_validated(self):
        rules = parse_rules(extracted(), {'I001'})
        self.assertIn('45', rules['rules'][0]['statement'])
        self.assertNotIn('40', json.dumps(rules, ensure_ascii=False))
        self.assertEqual(parse_rules({'screen_state': '未知', 'rules': [], 'limitations': []}, {'I001'})['rules'], [])
        with self.assertRaises(CollectorError):
            parse_rules(extracted(), {'I002'})
        duplicate = extracted()
        duplicate['rules'] *= 2
        with self.assertRaises(CollectorError):
            parse_rules(duplicate, {'I001'})

    def test_manifest_text_apk_and_mismatch_handling(self):
        xml = b'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="test.app" android:versionName="1.0" android:versionCode="1"/>'
        apk = Path(self.temp.name) / 'app.apk'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('AndroidManifest.xml', xml)
        local = apk_metadata(apk)
        manifest = manifest_identity({'content': xml.decode()})
        self.assertTrue(verify_identity(local, {'sha256': local['sha256']}, manifest, {'package': 'test.app', 'app_version': '1.0'}))
        self.assertFalse(verify_identity(local, {'sha256': local['sha256']}, manifest, {}))
        with self.assertRaises(CollectorError):
            verify_identity(local, {'sha256': 'wrong'}, manifest, {})
        with self.assertRaises(CollectorError):
            verify_identity(local, {'sha256': local['sha256']}, manifest, {'app_version': '2'})

    def test_binary_manifest_parser_utf8_and_utf16(self):
        for utf8 in (True, False):
            strings = ['manifest', 'package', 'test.app', 'versionName', '2.0', 'versionCode']
            offsets, data = [], b''
            for text in strings:
                offsets.append(len(data))
                encoded = text.encode('utf-8' if utf8 else 'utf-16-le')
                data += (bytes([len(text), len(encoded)]) + encoded + b'\0') if utf8 else (struct.pack('<H', len(text)) + encoded + b'\0\0')
            start = 28 + len(strings) * 4
            pool = struct.pack('<HHIIIIII', 1, 28, start + len(data), len(strings), 0, 0x100 if utf8 else 0, start, 0)
            pool += b''.join(struct.pack('<I', i) for i in offsets) + data
            attrs = b''.join(struct.pack('<IIIHBBI', 0xffffffff, name, literal, 8, 0, typ, value) for name, literal, typ, value in
                             ((1, 2, 3, 2), (3, 4, 3, 4), (5, 0xffffffff, 0x10, 22)))
            element = struct.pack('<HHIII', 0x102, 16, 36 + len(attrs), 1, 0xffffffff)
            element += struct.pack('<IIHHHHHH', 0xffffffff, 0, 20, 20, 3, 0, 0, 0) + attrs
            binary = struct.pack('<HHI', 3, 8, 8 + len(pool) + len(element)) + pool + element
            self.assertEqual(_binary_manifest(binary), {'package': 'test.app', 'version_name': '2.0', 'version_code': '22'})

    def test_invalid_config_and_corrupt_apk_fail(self):
        path = Path(self.temp.name) / 'bad.apk'
        path.write_bytes(b'not a zip')
        with self.assertRaises(CollectorError):
            apk_metadata(path)
        for key, value in [('max_model_calls', True), ('max_tool_calls', 0), ('thinking', 'automatic'),
                           ('reasoning_effort', 'automatic'), ('api_timeout', 601)]:
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = value
            path = Path(self.temp.name) / 'config.json'
            path.write_text(json.dumps(cfg))
            with self.assertRaises(CollectorError):
                load_settings(path)

    def test_extended_model_and_tool_budgets_are_bounded(self):
        path = Path(self.temp.name) / 'extended.json'
        for model_calls, tool_calls in ((100, 200), (200, 400)):
            cfg = dict(self.cfg, max_model_calls=model_calls, max_tool_calls=tool_calls)
            path.write_text(json.dumps(cfg))
            self.assertEqual(load_settings(path)['max_model_calls'], model_calls)
        for key, value in (('max_model_calls', 201), ('max_tool_calls', 401)):
            path.write_text(json.dumps(dict(self.cfg, **{key: value})))
            with self.assertRaises(CollectorError):
                load_settings(path)


class EvidenceTests(Base):
    def resources_tool(self, pages):
        schemas = dict(SCHEMAS, get_strings={'name': 'get_strings', 'inputSchema': {
            'type': 'object', 'properties': {'offset': {'type': 'integer'}, 'count': {'type': 'integer'}}}})
        replies = iter(pages)
        def read(name, arguments):
            self.mcp.calls.append((name, arguments))
            if name != 'get_strings':
                raise AssertionError('No shell or source calls for a resource search')
            return next(replies)
        self.mcp.call = read
        return EvidenceTools(self.mcp, schemas, self.rec, max_chars=4000)

    @staticmethod
    def resource_page(xml, pagination=None):
        return {'items': [{'file': 'res/values/strings.xml', 'content': xml}],
                'pagination': {'has_more': False} if pagination is None else pagination}

    def test_resource_search_is_literal_whitespace_tolerant_and_cached(self):
        tools = self.resources_tool([self.resource_page(
            '<resources><string name="limit_tip">每天最多 45 分钟</string>'
            '<string name="open">开启青少年模式</string></resources>')])
        first = tools.call('search_ui_resources', {'query': '45分钟'})
        value = json.loads(first['text'])
        self.assertEqual(value['matches'][0]['name'], 'limit_tip')
        self.assertEqual(value['matches'][0]['text'], '每天最多 45 分钟')
        self.assertTrue(value['resource_scan']['complete'])
        second = tools.call('search_ui_resources', {'query': 'LIMIT_TIP'})
        self.assertEqual(json.loads(second['text'])['pagination']['total'], 1)
        self.assertEqual(len(self.mcp.calls), 1)
        self.assertTrue((self.rec.root / 'code_evidence/ui_resources.json').is_file())
        self.assertNotIn('每天最多', json.dumps(tools.definitions(), ensure_ascii=False))
        # A declaration is not implementation evidence, even with a real match.
        with self.assertRaisesRegex(CollectorError, '源码证据'):
            validate_findings(final(quote='每天最多 45 分钟'), extracted()['rules'], tools.evidence)

    def test_resource_search_reads_multiple_file_pages_and_paginates_matches(self):
        tools = self.resources_tool([
            self.resource_page('<resources><string name="a">限制甲</string></resources>', {'has_more': True, 'next_offset': 1}),
            self.resource_page('<resources><string name="b">限制乙</string></resources>')])
        first = json.loads(tools.call('search_ui_resources', {'query': '限制', 'count': 1})['text'])
        self.assertEqual(first['pagination']['total'], 2)
        self.assertTrue(first['pagination']['has_more'])
        second = json.loads(tools.call('search_ui_resources', {'query': '限制', 'offset': 1})['text'])
        self.assertEqual(second['matches'][0]['name'], 'b')
        self.assertEqual(self.mcp.calls, [('get_strings', {'offset': 0, 'count': 200}),
                                          ('get_strings', {'offset': 1, 'count': 200})])

    def test_resource_search_invalid_xml_or_missing_pagination_is_incomplete(self):
        for page in (self.resource_page('<bad'), self.resource_page(
                '<!DOCTYPE resources [<!ENTITY x "unsafe">]><resources/>'),
                {'items': [], 'pagination': {}}, {'items': 'not a list'}):
            tools = self.resources_tool([page])
            output = json.loads(tools.call('search_ui_resources', {'query': '限制'})['text'])
            self.assertFalse(output['resource_scan']['complete'])
            self.assertTrue(output['resource_scan']['limitations'])

    def test_resource_search_broken_pagination_does_not_loop(self):
        tools = self.resources_tool([self.resource_page('<resources/>', {'has_more': True, 'next_offset': 0})])
        output = json.loads(tools.call('search_ui_resources', {'query': '限制'})['text'])
        self.assertFalse(output['resource_scan']['complete'])
        self.assertEqual(len(self.mcp.calls), 1)

    def test_resource_search_is_not_stopped_at_five_pages(self):
        page = self.resource_page('<resources/>', {'has_more': True})
        tools = self.resources_tool([page] * 6 + [self.resource_page('<resources/>')])
        output = json.loads(tools.call('search_ui_resources', {'query': '限制'})['text'])
        self.assertEqual(len(self.mcp.calls), 7)
        self.assertTrue(output['resource_scan']['complete'])

    def test_resource_search_rejects_invalid_parameters_and_unavailable_tool(self):
        self.assertNotIn('search_ui_resources', [x['function']['name'] for x in self.tools.definitions()])
        with self.assertRaises(CollectorError):
            self.tools.call('search_ui_resources', {'query': '限制'})
        tools = self.resources_tool([])
        for args in ({'query': '   '}, {'query': 12}, {'query': '限制', 'count': 0},
                     {'query': '限制', 'offset': -1}, {'query': '限制', 'shell': 'bad'}):
            with self.assertRaises(CollectorError):
                tools.call('search_ui_resources', args)
        self.assertEqual(self.mcp.calls, [])

    def test_source_code_field_is_unwrapped_and_quotes_match_real_lines(self):
        self.mcp.call = lambda name, args: {'class': 'test.Timer', 'method': 'start', 'code': SOURCE}
        output = self.tools.call('get_method_by_name', {'class_name': 'test.Timer', 'method_name': 'start'})
        self.assertEqual(output['text'], SOURCE.rstrip('\n'))
        validate_findings(final(), extracted()['rules'], self.tools.evidence)
        raw = json.loads((self.rec.root / 'code_evidence/E001.json').read_text())
        self.assertEqual(raw['result']['method'], 'start')

    def test_empty_or_broken_source_is_never_cached_as_success(self):
        for source in ('', '/* JADX ERROR */\n/* Method dump skipped */'):
            self.mcp.call = MagicMock(return_value={'response': source})
            tools = EvidenceTools(self.mcp, SCHEMAS, self.rec)
            first = tools.call('get_class_source', {'class_name': 'test.Timer'})
            self.assertIn('warning', first)
            self.assertNotIn('cached', first)
            self.assertFalse(tools.query_cache)
            with self.assertRaisesRegex(CollectorError, '不复用为正常源码'):
                tools.call('get_class_source', {'class_name': 'test.Timer'})
            self.assertEqual(self.mcp.call.call_count, 1)

    def test_good_source_still_cached(self):
        self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertTrue(self.tools.call('get_class_source', {'class_name': 'test.Timer'})['cached'])
        self.assertEqual(len(self.mcp.calls), 1)

    def test_bad_gui_source_automatically_uses_independent_parse(self):
        self.mcp.call = MagicMock(return_value={'response': '/* JADX ERROR */'})
        recovery = MagicMock(return_value={'code': SOURCE, 'backend': 'independent_full_apk',
                                          'metadata': {'load_returned': True}, 'attempts': []})
        tools = EvidenceTools(self.mcp, SCHEMAS, self.rec, recovery=recovery)
        output = tools.call('get_method_by_name', {'class_name': 'test.Timer', 'method_name': 'start'})
        self.assertEqual(output['tool'], 'independent_class_source')
        self.assertIn('stopAfter', output['text'])
        self.assertIn('整类', output['note'])
        self.assertEqual(len(tools.evidence), 2)
        tools.call('get_method_by_name', {'class_name': 'test.Timer', 'method_name': 'start'})
        other = tools.call('get_method_by_name', {'class_name': 'test.Timer', 'method_name': 'stop'})
        self.assertTrue(other['cached'])
        self.assertEqual(other['scope'], 'whole_class')
        self.assertEqual(recovery.call_count, 1)
        result = final(); result['findings'][0]['code_evidence'][0]['id'] = 'E002'
        validate_findings(result, extracted()['rules'], tools.evidence)

    def test_source_exception_uses_available_smali_before_independent_parse(self):
        schemas = dict(SCHEMAS, get_smali_of_class={'name': 'get_smali_of_class', 'inputSchema': SCHEMAS['get_class_source']['inputSchema']})
        def read(name, args):
            if name == 'get_class_source': raise CollectorError('timeout')
            return {'smali': '.class public Ltest/Timer;\n.method public start()V\n    return-void\n.end method'}
        self.mcp.call = read
        recovery = MagicMock(side_effect=CollectorError('independent parse failed'))
        tools = EvidenceTools(self.mcp, schemas, self.rec, recovery=recovery)
        output = tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertEqual(output['tool'], 'get_smali_of_class')
        self.assertEqual(output['representation'], 'smali')
        recovery.assert_not_called()

    def test_partial_independent_java_uses_its_saved_instructions(self):
        self.mcp.call = MagicMock(return_value={'response': ''})
        tools = EvidenceTools(self.mcp, SCHEMAS, self.rec, recovery=lambda args, timeout: {
            'code': '/* Method not decompiled */', 'smali': '.class public Ltest/Timer;\n.method start()V\n.end method'})
        output = tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertEqual(output['representation'], 'smali')
        self.assertEqual(len(tools.evidence), 3)
        self.assertEqual(self.mcp.call.call_count, 1)

    def test_independent_smali_without_java_is_not_mislabelled_as_java(self):
        self.mcp.call = MagicMock(return_value={'response': ''})
        tools = EvidenceTools(self.mcp, SCHEMAS, self.rec, recovery=lambda args, timeout: {
            'smali': '.class public Ltest/Timer;\n.method start()V\n.end method'})
        output = tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertEqual(output['tool'], 'get_smali_of_class')
        self.assertEqual(output['representation'], 'smali')
        self.assertEqual(len(tools.evidence), 2)

    def test_read_only_allowlist_and_parameter_checks(self):
        for name, args in [('rename_class', {}), ('get_class_source', {'class_name': 'x', 'shell': 'rm'}),
                           ('get_class_source', {'class_name': 1}), ('search_classes_by_keyword', {'search_term': 'x', 'count': 0})]:
            with self.assertRaises(CollectorError):
                self.tools.call(name, args)
        self.assertEqual(self.mcp.calls, [])

    def test_full_code_search_requires_a_package_scope(self):
        with self.assertRaisesRegex(CollectorError, 'package'):
            self.tools.call('search_classes_by_keyword', {'search_term': 'timer'})
        self.assertEqual(self.mcp.calls, [])
        self.tools.call('search_classes_by_keyword', {'search_term': 'timer', 'package': 'test.feature'})
        self.assertEqual(len(self.mcp.calls), 2)

    def test_large_package_scope_does_not_start_full_decompilation(self):
        self.mcp.call = lambda name, args: {'total_classes': 10000, 'packages': [{'name': 'test.feature', 'class_count': 10000}]}
        with self.assertRaisesRegex(CollectorError, '超过单次全文扫描预算'):
            self.tools.call('search_classes_by_keyword', {'search_term': 'timer', 'package': 'test'})

    def test_zero_simple_name_matches_cannot_allow_broad_scans(self):
        with self.assertRaisesRegex(CollectorError, '零匹配'):
            self.tools.call('search_classes_by_keyword', {'search_term': 'timer', 'package': 'nonexistent'})
        self.assertEqual(self.mcp.calls, [('get_package_tree', {})])

    def test_method_and_field_searches_require_small_explicit_package(self):
        for location in ('method', 'field', 'class,method'):
            with self.assertRaisesRegex(CollectorError, 'package'):
                self.tools.call('search_classes_by_keyword', {'search_term': 'timer', 'search_in': location})
        self.assertEqual(self.mcp.calls, [])
        self.tools.call('search_classes_by_keyword', {'search_term': 'timer', 'search_in': 'method', 'package': 'test.feature'})
        self.assertEqual(self.mcp.calls[0], ('get_package_tree', {}))

    def test_global_method_search_does_not_scan_large_apk(self):
        schemas = dict(SCHEMAS, search_method_by_name={'name': 'search_method_by_name', 'inputSchema': {
            'type': 'object', 'properties': {'method_name': {'type': 'string'}}, 'required': ['method_name']}})
        tools = EvidenceTools(self.mcp, schemas, self.rec, max_search_classes=500)
        self.mcp.call = lambda name, args: {'total_classes': 10000, 'packages': [{'name': 'test.feature', 'class_count': 10000}]}
        with self.assertRaisesRegex(CollectorError, '全局方法扫描'):
            tools.call('search_method_by_name', {'method_name': 'isMinor'})

    def test_global_method_search_still_works_for_small_apk(self):
        schemas = dict(SCHEMAS, search_method_by_name={'name': 'search_method_by_name', 'inputSchema': {
            'type': 'object', 'properties': {'method_name': {'type': 'string'}}, 'required': ['method_name']}})
        EvidenceTools(self.mcp, schemas, self.rec).call('search_method_by_name', {'method_name': 'isMinor'})
        self.assertEqual([c[0] for c in self.mcp.calls], ['get_package_tree', 'search_method_by_name'])

    def test_read_source_saved_and_unknown_quotes_rejected(self):
        output = self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertEqual(output['text'], SOURCE.rstrip('\n'))
        validated = validate_findings(final(), extracted()['rules'], self.tools.evidence)
        self.assertEqual(validated['findings'][0]['verdict'], 'supported')
        self.assertTrue((self.rec.root / validated['findings'][0]['code_evidence'][0]['file']).is_file())
        for quote in ('imaginary code();', 'if (...) stopAfter(limit);'):
            with self.assertRaises(CollectorError):
                validate_findings(final(quote=quote), extracted()['rules'], self.tools.evidence)

    def test_only_seen_pages_can_be_quoted_and_next_page_can_be_read(self):
        self.tools.max_chars = 60
        first = self.tools.call('get_class_source', {'class_name': 'test.Timer'})
        self.assertTrue(first['truncated'])
        with self.assertRaises(CollectorError):
            validate_findings(final(), extracted()['rules'], self.tools.evidence)
        later = self.tools.call('read_evidence', {'id': first['evidence_id'], 'start_line': first['next_start_line']})
        self.assertIn('stopAfter', later['text'])
        validate_findings(final(), extracted()['rules'], self.tools.evidence)

    def test_search_results_are_not_source_evidence(self):
        self.tools.call('search_classes_by_keyword', {'search_term': 'Timer', 'search_in': 'class'})
        with self.assertRaises(CollectorError):
            validate_findings(final(quote='test.Timer'), extracted()['rules'], self.tools.evidence)
        self.assertEqual(validate_findings(final('unknown'), extracted()['rules'], {})['findings'][0]['verdict'], 'unknown')
        with self.assertRaises(CollectorError):
            validate_findings({'summary': 'x', 'findings': [], 'limitations': []}, extracted()['rules'], {})


class AgentTests(Base):
    def run_agent(self, answers):
        client = FakeClient(answers)
        agent = LimitsAgent(self.cfg, client, self.tools, self.rec, {'package': 'test.app'},
                            [{'id': 'I001', 'data_url': 'data:image/png;base64,mock'}], progress=lambda *a, **k: None)
        return agent, client

    def test_same_conversation_reads_image_then_uses_tools_and_finishes(self):
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'})]),
            message(calls=[tool_call('submit_result', final(), 'final')]),
        ])
        result = agent.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(agent.model_calls, 3)
        self.assertEqual(agent.tool_calls, 1)
        self.assertEqual(len(client.requests[-1][0][1]['content']), 3)
        self.assertIn('role', client.requests[-1][0][-1])
        log = self.rec.trace_path.read_text()
        self.assertNotIn('data:image', log)
        self.assertTrue((self.rec.root / 'rules.json').is_file())
        analysis_request = client.requests[1][0][-1]['content']
        self.assertIn('search_ui_resources', analysis_request)
        self.assertIn('不补动态实验', analysis_request)
        self.assertIn('100 次', analysis_request)
        self.assertIn('ui_anchor_then_expand', log)

    def test_unknown_tool_is_returned_as_error_not_executed(self):
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('run_shell', {'cmd': 'rm'}, 'bad')]),
            message(calls=[tool_call('submit_result', final('unknown'), 'done')])])
        self.assertEqual(agent.run()['status'], 'completed')
        self.assertEqual(self.mcp.calls, [])
        self.assertIn('error', client.requests[-1][0][-1]['content'])

    def test_last_model_call_can_submit_and_tool_budget_stops_new_reads(self):
        self.cfg['max_model_calls'] = 2
        agent, client = self.run_agent([
            message(json.dumps(extracted())), message(calls=[tool_call('submit_result', final('unknown'))])])
        self.assertEqual(agent.run()['status'], 'completed')
        self.assertTrue(client.requests[-1][1]['final_only'])

    def test_malformed_final_does_not_turn_into_supported(self):
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('submit_result', final(quote='fabricated'))]),
            message(calls=[tool_call('submit_result', final('unknown'), 'f2')])])
        self.assertEqual(agent.run()['findings'][0]['verdict'], 'unknown')
        self.assertIn('error', client.requests[-1][0][-1]['content'])

    def test_empty_rules_end_without_code_calls(self):
        value = {'screen_state': '未见相关规则', 'rules': [], 'limitations': []}
        agent, _ = self.run_agent([message(json.dumps(value))])
        self.assertEqual(agent.run()['status'], 'insufficient')
        self.assertEqual(self.mcp.calls, [])

    def test_identical_failed_read_is_not_retried(self):
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'}, 'read1')]),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'}, 'read2')]),
            message(calls=[tool_call('submit_result', final('unknown'), 'done')])])
        with patch.object(self.tools, 'call', side_effect=CollectorError('MCP timeout')) as query:
            result = agent.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(query.call_count, 1)
        self.assertIn('不重复执行', client.requests[-1][0][-1]['content'])

    def test_budget_and_truncated_api_response(self):
        self.cfg['max_model_calls'] = 2
        agent, _ = self.run_agent([message(json.dumps(extracted())), message('please wait')])
        with self.assertRaises(BudgetEnded):
            agent.run()
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'secret', 'LLM_MODEL': 'mock'}, clear=True):
            client = ChatClient(self.cfg, transport=lambda *args: {'choices': [{'message': message('{}'), 'finish_reason': 'length'}]})
            with self.assertRaises(CollectorError):
                client.chat([], 5)

    def test_chat_payload_and_thinking_not_persisted(self):
        sent = []
        def transport(url, key, payload, timeout):
            sent.append(payload)
            return {'choices': [{'message': message('{}'), 'finish_reason': 'stop'}], 'usage': {'total_tokens': 8}, 'model': 'm'}
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'secret', 'LLM_MODEL': 'mock'}, clear=True):
            client = ChatClient(self.cfg, transport=transport)
            client.chat([], 5, tools=[{'type': 'function', 'function': {'name': 'read'}},
                                     {'type': 'function', 'function': {'name': 'submit_result'}}], final_only=True)
        self.assertEqual(sent[0]['thinking']['type'], 'enabled')
        self.assertEqual(sent[0]['reasoning_effort'], 'high')
        self.assertEqual(sent[0]['tool_choice'], 'auto')
        self.assertEqual([t['function']['name'] for t in sent[0]['tools']], ['submit_result'])

    def test_disabled_mode_can_still_force_finishing_tool(self):
        sent = []
        def transport(url, key, payload, timeout):
            sent.append(payload)
            return {'choices': [{'message': message('{}'), 'finish_reason': 'stop'}]}
        cfg = dict(self.cfg, thinking='disabled', reasoning_effort=None)
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'secret', 'LLM_MODEL': 'mock'}, clear=True):
            ChatClient(cfg, transport).chat([], 5, tools=[{'type': 'function'}], final_only=True)
        self.assertEqual(sent[0]['tool_choice']['function']['name'], 'submit_result')
        self.assertNotIn('reasoning_effort', sent[0])

    def test_reasoning_is_replayed_for_all_turns_but_not_written_to_logs(self):
        first = dict(message(json.dumps(extracted())), reasoning_content='PRIVATE_REASONING_FIRST')
        second = dict(message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'})]),
                      reasoning_content='PRIVATE_REASONING_TOOL')
        third = dict(message('继续核对'), reasoning_content='PRIVATE_REASONING_NO_TOOL')
        agent, client = self.run_agent([first, second, third, message(calls=[tool_call('submit_result', final())])])
        self.assertEqual(agent.run()['status'], 'completed')
        assistants = [m for m in client.requests[-1][0] if m['role'] == 'assistant']
        self.assertEqual([m['reasoning_content'] for m in assistants],
                         ['PRIVATE_REASONING_FIRST', 'PRIVATE_REASONING_TOOL', 'PRIVATE_REASONING_NO_TOOL'])
        log = self.rec.trace_path.read_text()
        self.assertNotIn('PRIVATE_REASONING', log)
        self.assertIn('"reasoning_returned": true', log)

    def test_reasoning_usage_counts_are_recorded_without_raw_reasoning(self):
        def transport(*args):
            return {'choices': [{'message': message('{}'), 'finish_reason': 'stop'}],
                    'usage': {'completion_tokens': 31, 'completion_tokens_details': {'reasoning_tokens': 20}}}
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'secret', 'LLM_MODEL': 'mock'}, clear=True):
            _, usage, _ = ChatClient(self.cfg, transport).chat([], 5)
        self.assertEqual(usage['reasoning_tokens'], 20)

    def test_context_budget_reserves_submission_without_discarding_history(self):
        agent, client = self.run_agent([
            message(json.dumps(extracted())),
            message(calls=[tool_call('get_class_source', {'class_name': 'test.Timer'})]),
            message(calls=[tool_call('submit_result', final('unknown'))]),
        ])
        self.mcp.call = lambda *args: {'response': 'class Timer {\n' + 'long source line\n' * 2000 + '}'}
        self.tools.max_chars = 30000
        self.cfg['max_context_chars'] = 25000
        result = agent.run()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(agent.model_calls, 3)
        self.assertTrue(client.requests[-1][1]['final_only'])
        self.assertIn('long source line', json.dumps(client.requests[-1][0]))
        self.assertIn('context_budget_final_turn', self.rec.trace_path.read_text())

    def test_report_is_short_and_preserves_not_completed_distinction(self):
        rule_data = extracted()
        result = {'status': 'error', 'summary': 'API 超时', 'rules': rule_data, 'findings': [],
                  'apk': {'package': 'test.app', 'version_name': '1'}, 'stats': {}, 'limitations': [],
                  'images': [{'id': 'I001', 'file': 'images/a.png'}]}
        report = render_report(result)
        self.assertIn('未完成', report)
        self.assertNotIn('| 代码支持 |', report)
        self.assertIn('不证明运行时有效', report)
        self.assertLess(len(report), 800)


class MCPProtocolTests(unittest.TestCase):
    def test_structured_content_is_preferred_without_duplicate_text(self):
        client = MCPClient('http://127.0.0.1:12345/mcp')
        client.rpc = lambda *args: {'structuredContent': {'response': SOURCE},
                                    'content': [{'type': 'text', 'text': 'malformed duplicate'}]}
        self.assertEqual(client.call('get_class_source', {'class_name': 'test.Timer'})['response'], SOURCE)

    def test_ordinary_resource_response_above_old_four_mb_is_accepted(self):
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'padding': 'x' * (4 * 1024 * 1024)}}).encode()
        class Response(io.BytesIO):
            headers = {'Content-Type': 'application/json'}
            status = 200
        for tool in ('get_package_tree', 'get_strings', 'get_class_source'):
            client = MCPClient('http://127.0.0.1:12345/mcp')
            client.opener = MagicMock()
            client.opener.open.return_value = Response(raw)
            self.assertIn('padding', client.rpc('tools/call', {'name': tool}))

    def test_response_above_soft_limit_is_saved_and_read_not_rejected(self):
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'padding': 'x' * (33 * 1024 * 1024)}}).encode()
        class Response(io.BytesIO):
            headers = {'Content-Type': 'application/json'}
            status = 200
        with tempfile.TemporaryDirectory() as directory:
            client = MCPClient('http://127.0.0.1:12345/mcp')
            client.wire_directory = Path(directory)
            client.opener = MagicMock()
            client.opener.open.return_value = Response(raw)
            self.assertEqual(len(client.rpc('tools/call', {'name': 'get_strings'})['padding']), 33 * 1024 * 1024)
            self.assertEqual((Path(directory) / 'mcp-1.wire.json').stat().st_size, len(raw))

    def test_heartbeat_cannot_extend_total_rpc_deadline(self):
        client = MCPClient('http://127.0.0.1:12345/mcp', timeout=1)
        response = MagicMock()
        response.headers = {'Content-Type': 'text/event-stream'}
        response.status = 200
        response.readline.return_value = b': ping\n'
        response.fp = None
        response.__enter__.return_value = response
        client.opener = MagicMock()
        client.opener.open.return_value = response
        with patch('check_limits.tools.time.monotonic', side_effect=[0, .1, 2]):
            with self.assertRaisesRegex(CollectorError, '时间预算'):
                client.rpc('initialize', {})
        self.assertEqual(response.readline.call_count, 1)

    def test_sse_initialize_notification_discovery_and_read(self):
        events = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                events.append((request, dict(self.headers)))
                if 'id' not in request:
                    self.send_response(202)
                    self.end_headers()
                    return
                if request['method'] == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {}}
                elif request['method'] == 'tools/list':
                    result = {'tools': list(SCHEMAS.values()) + [{'name': 'rename_class'}]}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps({'response': SOURCE})}]}
                body = ('event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}) + '\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Mcp-Session-Id', 'local-test')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = MCPClient('http://127.0.0.1:%d/mcp' % server.server_port)
            schemas = client.initialize()
            self.assertNotIn('rename_class', schemas)
            self.assertEqual(client.call('get_class_source', {'class_name': 'test.Timer'})['response'], SOURCE)
            self.assertTrue(any(h.get('Mcp-Session-Id') == 'local-test' for _, h in events))
            with self.assertRaises(CollectorError):
                client.call('rename_class', {})
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class ReadinessTests(unittest.TestCase):
    def test_http_200_without_initialized_passes_never_counts_as_ready(self):
        connection = SSHConnection({'remote_apk_state_file': '/unused', 'ready_timeout': 2})
        # Execute the actual remote-probe source locally against a mocked HTTP 200.
        def execute(script, timeout):
            response = MagicMock()
            response.read.return_value = json.dumps({'protocol': 'minorguard-jadx-ready-v1',
                'ready': True, 'load_returned': True, 'passes_initialized': False}).encode()
            response.__enter__.return_value = response
            opener = MagicMock()
            opener.open.return_value = response
            ticks = iter(range(20))
            output = io.StringIO()
            with patch('urllib.request.build_opener', return_value=opener), \
                 patch('time.monotonic', side_effect=lambda: next(ticks)), patch('time.sleep'), \
                 contextlib.redirect_stdout(output):
                exec(compile(script, 'ready-probe', 'exec'), {})
            return json.loads(output.getvalue())
        connection.remote = execute
        with self.assertRaisesRegex(CollectorError, '初始化'):
            connection.wait_ready()

    def test_load_returned_and_passes_initialized_are_required(self):
        source = (CODE_ROOT / 'jadx_runtime/PluginServer.java').read_text()
        self.assertIn('DECOMPILER_UPDATE_SYNC', source)
        self.assertIn('field.get(pass) != null', source)
        self.assertIn('loadedDecompiler = candidate', source)
        self.assertIn('Analyzer not initialized', source)

    def test_remote_endpoint_and_ssh_option_injection_rejected(self):
        with self.assertRaises(CollectorError):
            MCPClient('http://example.com:8651/mcp')
        with self.assertRaises(CollectorError):
            SSHConnection({'ssh_host': '-oProxyCommand=bad'}).__enter__()


class EndToEndFailureTests(Base):
    def test_api_failure_or_interrupt_preserves_rules_and_short_report(self):
        folder = Path(self.temp.name) / 'screens'
        folder.mkdir()
        Image.new('RGB', (200, 400), 'white').save(folder / '001.png')
        apk = Path(self.temp.name) / 'app.apk'
        xml = b'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="test.app" android:versionName="1.0" android:versionCode="1"/>'
        with zipfile.ZipFile(apk, 'w') as archive:
            archive.writestr('AndroidManifest.xml', xml)
        local = apk_metadata(apk)
        class Connection:
            url = 'http://127.0.0.1:12345/mcp'
            def __init__(self, config):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def identity(self):
                return {'sha256': local['sha256'], 'path': '/tmp/test.apk'}
            def wait_ready(self):
                return {'ready': True}
            def recover_source(self, args, timeout):
                raise CollectorError('not needed')
        class MCP(FakeMCP):
            def __init__(self, *args):
                super().__init__()
            def initialize(self):
                return SCHEMAS
            def call(self, name, arguments):
                if name == 'get_android_manifest':
                    return {'content': xml.decode()}
                return super().call(name, arguments)
        for failure, status, exit_code in ((CollectorError('API unavailable'), 'error', 1), (KeyboardInterrupt(), 'interrupted', 130)):
            cfg = copy.deepcopy(self.cfg)
            cfg['output_dir'] = str(Path(self.temp.name) / status)
            fake = FakeClient([message(json.dumps(extracted())), failure])
            with patch('check_limits.__main__.load_settings', return_value=cfg), \
                 patch('check_limits.__main__.SSHConnection', Connection), \
                 patch('check_limits.__main__.MCPClient', MCP), \
                 patch('check_limits.__main__.ChatClient', return_value=fake), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = main(['--apk', str(apk), '--images', str(folder), '--env-file', str(folder / 'missing.env')])
            self.assertEqual(code, exit_code)
            output = next(Path(cfg['output_dir']).iterdir())
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['status'], status)
            self.assertEqual(len(result['rules']['rules']), 1)
            self.assertEqual(result['findings'], [])
            self.assertIn('未完成', (output / 'report.md').read_text())
            self.assertNotIn('data:image', (output / 'inputs.json').read_text())


if __name__ == '__main__':
    unittest.main()
