"""Offline tests for visual recovery, handback verification, and the single entry."""

import base64
import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from collect_images.common import CODE_ROOT, CollectorError, NeedUser, load_config
from collect_images.recovery import RecoveryClient
from collect_images.recorder import Recorder
from collect_images.workflow import Workflow
from check_limits.__main__ import load_settings
from check_limits.report import write_report
from check_limits.server import ensure_server
from check_limits.tools import apk_metadata
from run_check import prepare_apk, run
from test_collector import BOX, FakeADB, Scripted, config, frame, stop


SWIPE = {'action': 'swipe', 'direction': 'up', 'length': 'long'}
HAND_BACK = {'action': 'hand_back', 'progress_confirmed': True, 'reason': '操作后已进入设置页', 'hint': '在设置继续，不要回侧栏重复上滑'}


class RecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rec = Recorder(Path(self.temp.name))
        self.cfg = config()
        self.cfg['evidence'] = []

    def run_flow(self, frames, jev, recovery):
        adb = FakeADB(frames)
        nav, fallback = Scripted(jev), Scripted(recovery)
        workflow = Workflow(self.cfg, adb, nav, None, self.rec, sleeper=lambda _: None, recovery=fallback)
        with contextlib.redirect_stdout(io.StringIO()):
            result = workflow.run()
        return result, nav, fallback, adb

    def test_three_stalled_swipes_delegate_then_confirm_before_jev_resumes(self):
        old, new = frame('MENU'), frame('SETTINGS', 'skyblue')
        result, nav, fallback, adb = self.run_flow([old]*4+[new], [SWIPE]*3+[stop()],
            [{'action': 'tap', 'box': BOX}, HAND_BACK])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['recovery'], {'delegations': 1, 'model_calls': 2, 'confirmed_handbacks': 1,
                                            'last_hint': HAND_BACK['hint']})
        self.assertEqual(len(nav.frames), 4)
        self.assertEqual(nav.frames[-1], new.png)
        self.assertFalse(fallback.contexts[0]['after_action'])
        self.assertTrue(fallback.contexts[1]['after_action'])
        self.assertEqual(fallback.frames, [old.png, new.png])
        self.assertEqual(nav.contexts[-1]['recovery_hint'], HAND_BACK['hint'])
        trace = [json.loads(line) for line in self.rec.trace_path.read_text().splitlines()]
        actors = [r['actor'] for r in trace]
        self.assertIn('recovery_llm', actors)
        handback = next(r for r in trace if r['actor'] == 'recovery_llm' and r['action'] == 'return_to_jev')
        self.assertEqual(handback['status'], 'confirmed')
        self.assertEqual(len(adb.actions), 4)

    def test_normal_scrolling_with_changed_content_does_not_delegate(self):
        frames = [frame(color=c) for c in ('white', 'red', 'blue', 'green')]
        result, _, fallback, _ = self.run_flow(frames, [SWIPE]*3+[stop()], [])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(fallback.frames, [])

    def test_no_first_call_handback_even_if_fake_model_claims_success(self):
        result, nav, _, _ = self.run_flow([frame()], [SWIPE]*3, [HAND_BACK])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['recovery']['confirmed_handbacks'], 0)
        self.assertEqual(len(nav.frames), 3)

    def test_changed_picture_alone_is_not_handback_confirmation(self):
        self.cfg['recovery']['max_actions'] = 1
        result, nav, _, adb = self.run_flow([frame()]*4+[frame('NEW', 'skyblue')], [SWIPE]*3,
            [{'action': 'tap', 'box': BOX}, {'action': 'tap', 'box': BOX}])
        self.assertEqual(result['status'], 'insufficient')
        self.assertEqual(len(nav.frames), 3)
        self.assertEqual(len(adb.actions), 4)
        self.assertEqual(result['recovery']['confirmed_handbacks'], 0)

    def test_recovery_api_failure_keeps_screens_and_results(self):
        result, _, _, _ = self.run_flow([frame()], [SWIPE]*3, [CollectorError('mock API offline')])
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.rec.root/'result.json').exists())
        self.assertGreaterEqual(result['image_count'], 4)

    def test_black_screenshot_is_saved_but_not_sent_to_jev(self):
        result, nav, _, _ = self.run_flow([frame(color='black')], [], [])
        self.assertEqual(result['status'], 'need_user')
        self.assertEqual(result['image_count'], 1)
        self.assertEqual(nav.frames, [])


class RecoveryProtocolTests(unittest.TestCase):
    def test_handback_requires_a_post_action_confirmation(self):
        value = dict(HAND_BACK, scope='navigation')
        with self.assertRaises(CollectorError):
            RecoveryClient.parse(value, frame(), after_action=False)
        value['progress_confirmed'] = False
        with self.assertRaises(CollectorError):
            RecoveryClient.parse(value, frame(), after_action=True)
        value['progress_confirmed'] = True
        self.assertEqual(RecoveryClient.parse(value, frame(), True)['action'], 'hand_back')

    def test_only_narrow_navigation_and_mode_password_actions(self):
        for value in ({'action': 'run_shell'}, {'action': 'enter_pin'},
                      {'action': 'tap', 'scope': 'navigation', 'box': {'left': 1}},
                      {'action': 'swipe', 'scope': 'navigation', 'direction': 'diagonal', 'length': 'long'}):
            with self.assertRaises(CollectorError):
                RecoveryClient.parse(value, frame(), False)
        with self.assertRaises(NeedUser):
            RecoveryClient.parse({'action': 'tap', 'scope': 'requires_user'}, frame(), False)
        self.assertEqual(RecoveryClient.parse({'action': 'handle_password', 'scope': 'mode_password'}, frame(), False)['action'], 'handle_password')

    def test_transport_receives_recent_raw_images_without_logging_credentials(self):
        sent=[]
        def transport(url,key,payload,timeout):
            sent.append(payload)
            return {'model':'mock','choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'action':'tap','scope':'navigation','box':BOX,'reason':'打开设置'})}}]}
        with patch.dict(os.environ, {'LLM_BASE_URL':'https://example.test','LLM_API_KEY':'secret','LLM_MODEL':'mock'}, clear=True):
            client=RecoveryClient({'timeout':45}, transport=transport)
            current=frame()
            decision=client.decide(current, {'recent_frames':[frame('BEFORE'), current], 'after_action':False},10)
        self.assertEqual(decision['action'],'tap')
        self.assertEqual(len([c for c in sent[0]['messages'][1]['content'] if c['type']=='image_url']),2)
        self.assertNotIn('secret',json.dumps(decision))

    def test_recovery_uses_original_resolution_not_jev_thumbnail(self):
        sent = []
        buffer = io.BytesIO()
        Image.new('RGB', (1260, 2800), 'white').save(buffer, format='PNG')
        from collect_images.common import Frame
        current = Frame.from_png(buffer.getvalue())
        def transport(url, key, payload, timeout):
            sent.append(payload)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'action': 'tap', 'scope': 'navigation', 'box': BOX})}}]}
        with patch.dict(os.environ, {'LLM_BASE_URL': 'https://example.test', 'LLM_API_KEY': 'secret', 'LLM_MODEL': 'mock'}, clear=True):
            RecoveryClient({'timeout': 45}, transport).decide(current, {'after_action': False}, 10)
        visual = next(c['image_url'] for c in sent[0]['messages'][1]['content'] if c['type'] == 'image_url')
        self.assertEqual(base64.b64decode(visual['url'].split(',', 1)[1]), current.png)
        self.assertEqual(visual['detail'], 'high')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.apk=self.root/'app.apk'
        xml=b'<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="test.app" android:versionName="1" android:versionCode="1"/>'
        with zipfile.ZipFile(self.apk,'w') as archive:archive.writestr('AndroidManifest.xml',xml)
        self.metadata=apk_metadata(self.apk)
        self.cfg=config()
        self.cfg['evidence']=[]
        self.args=type('Args',(),dict(package='test.app',serial='fake',apk=self.apk,
            output_dir=self.root/'runs',env_file=self.root/'absent.env',collect_config=CODE_ROOT/'config.json',
            analysis_config=CODE_ROOT/'check_config.json',launch=False,max_steps=None))()
        self.stages=[]
        metadata=self.metadata
        class Driver(FakeADB):
            def __init__(self,*args):super().__init__()
            def apk_paths(self,package):return ['/data/app/base.apk']
            def apk_hash(self,path):return metadata['sha256']
            def wake(self):pass
        self.driver=Driver

    def fake_analysis(self,args):
        self.stages.append('analysis')
        rec=Recorder(args.output_dir)
        result={'status':'completed','summary':'测试用的静态结果','rules':{'rules':[],'limitations':[]},
                'findings':[],'limitations':[],'apk':self.metadata,'images':[], 'screenshot_identity_verified':True}
        write_report(rec,result)
        return result,rec.root

    def invoke(self,decision):
        def server(*args):self.stages.append('server');return {'status':'reused'}
        with patch('run_check.load_config',return_value=copy.deepcopy(self.cfg)), \
             patch('run_check.ADB',self.driver), patch('run_check.JevClient',return_value=Scripted([decision])), \
             patch('run_check.ensure_server',side_effect=server), patch('run_check.analyze',side_effect=self.fake_analysis), \
             contextlib.redirect_stdout(io.StringIO()):
            return run(self.args)

    def test_one_entry_automatically_collects_prepares_analyzes_and_writes_report(self):
        result,root=self.invoke(stop())
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.stages,['server','analysis'])
        self.assertTrue((root/'report.md').is_file())
        self.assertTrue((root/result['collection_dir']/'result.json').is_file())
        self.assertIn('全流程',(root/'report.md').read_text())

    def test_auth_block_does_not_continue_with_paid_analysis_but_has_report(self):
        result,root=self.invoke(NeedUser('需要验证码'))
        self.assertEqual(result['status'],'need_user')
        self.assertEqual(self.stages,[])
        self.assertTrue((root/'report.md').is_file())

    def test_partial_collection_does_not_become_full_success(self):
        result,root=self.invoke(stop(reason='insufficient',done=False))
        self.assertEqual(result['status'],'partial')
        self.assertIn('采集未完整完成',(root/'report.md').read_text())

    def test_mismatched_apk_stops_before_gui_actions(self):
        driver=self.driver()
        driver.apk_hash=lambda path:'0'*64
        with self.assertRaises(CollectorError):prepare_apk(driver,'test.app',self.apk,self.root)

    def test_automatic_apk_export_when_no_file_provided(self):
        driver=self.driver()
        driver.pull_apk=lambda source,destination:destination.write_bytes(self.apk.read_bytes())
        destination,metadata=prepare_apk(driver,'test.app',None,self.root)
        self.assertEqual(destination,self.root/'input.apk')
        self.assertEqual(metadata['sha256'],self.metadata['sha256'])


class ServerPreparationTests(unittest.TestCase):
    def setUp(self):
        deployment = patch('check_limits.server.ensure_runtime', return_value={'changed': False})
        deployment.start()
        self.addCleanup(deployment.stop)
        self.cfg=load_settings(CODE_ROOT/'check_config.json')['jadx']
        self.metadata={'sha256':'a'*64,'package':'test.app','version_name':'1','version_code':'1'}
        self.marker='MG_MANAGED_ROOT='+self.cfg['managed_root']
        self.status={'current_sha256':'a'*64,'target_sha256':'a'*64,'sessions':{
            name:self.marker for name in ('minorguard-xvfb','minorguard-jadx','minorguard-mcp')}}

    def test_matching_server_is_reused_without_upload_or_restart(self):
        with patch('check_limits.server.SSHConnection') as connection, patch('check_limits.server._check_manifest',return_value=True), \
             patch('check_limits.server.subprocess.run') as command:
            connection.return_value.remote.return_value=self.status
            self.assertEqual(ensure_server(Path('/test.apk'),self.metadata,self.cfg)['status'],'reused')
            command.assert_not_called()
            self.assertEqual(connection.return_value.remote.call_count,1)

    def test_unmanaged_session_is_never_stopped(self):
        self.status['sessions']['minorguard-jadx']='MG_MANAGED_ROOT=/other/project'
        with patch('check_limits.server.SSHConnection') as connection:
            connection.return_value.remote.return_value=self.status
            with self.assertRaises(CollectorError):ensure_server(Path('/test.apk'),self.metadata,self.cfg)

    def test_different_apk_uses_owned_setup_and_checks_manifest(self):
        self.status['current_sha256']='b'*64
        with patch('check_limits.server.SSHConnection') as connection, patch('check_limits.server._check_manifest',return_value=True):
            connection.return_value.remote.side_effect=[self.status,{'status':'loaded','sha256':'a'*64}]
            result=ensure_server(Path('/test.apk'),self.metadata,self.cfg)
            self.assertEqual(result['status'],'loaded')
            setup_script=connection.return_value.remote.call_args[0][0]
            self.assertIn('Unmanaged session; not stopped',setup_script)
            compile(setup_script,'remote-setup','exec')

    def test_unsafe_remote_root_rejected(self):
        for root in ('/','/home/../etc','/home/cxy/x;bad'):
            with self.assertRaises(CollectorError):ensure_server(Path('/test.apk'),self.metadata,dict(self.cfg,managed_root=root))


if __name__=='__main__':unittest.main()
