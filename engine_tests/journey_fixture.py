"""Disposable Auton process and authenticated alert contract receiver."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
import yaml
from test_web_engine import free_port as port


def private(path,content):
    path.write_text(content);path.chmod(0o600)


def wait_for(probe,seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if probe():return
        except (OSError,http.client.HTTPException):pass
        time.sleep(.03)
    raise AssertionError('Disposable service readiness deadline exceeded')


class AutonDaemon:
    def __init__(self):
        source=os.environ.get('AUTON_TEST_SOURCE');binary=os.environ.get('AUTON_TEST_DAEMON')
        if not source or not binary or not Path(binary).is_absolute():
            raise RuntimeError('Set AUTON_TEST_SOURCE and AUTON_TEST_DAEMON to the pinned source and installed daemon')
        self.directory=tempfile.TemporaryDirectory(prefix='atropos-auton-');self.root=Path(self.directory.name)
        self.process=None;self.log=None;self.port=port();self.origin='http://127.0.0.1:'+str(self.port)
        self.password=secrets.token_urlsafe(32);self.auth=('operator',self.password)
        try:
            digest=base64.b64encode(hashlib.sha1(self.password.encode()).digest()).decode()
            private(self.root/'users','operator:{SHA}'+digest+'\n')
            config=yaml.safe_load((Path(source)/'etc/auton/auton.yml.example').read_text())
            config['general'].update(listen_addr='127.0.0.1',listen_port=self.port,max_life_time=0,max_requests=0,
                auth_basic='Integration',auth_basic_file=str(self.root/'users'),
                job_storage=dict(backend='sqlite',path=str(self.root/'jobs.db'),timeout=3))
            config.pop('import_modules',None)
            config['modules']=yaml.safe_load((Path(source)/'etc/auton/modules/job.yml').read_text())
            config['endpoints']={'approved':dict(plugin='subproc',config=dict(prog=str(Path(binary).parent/'python'),timeout=10))}
            private(self.root/'config.yml',yaml.safe_dump(config))
            self.log=(self.root/'process.log').open('ab')
            env={key:os.environ[key] for key in ('PATH','LANG') if key in os.environ}
            env['PYTHONNOUSERSITE']='1'
            self.process=subprocess.Popen([binary,'-f','-c',str(self.root/'config.yml'),'-p',str(self.root/'daemon.pid'),
                '--logfile',str(self.root/'daemon.log')],cwd=self.root,env=env,stdout=self.log,stderr=self.log)
            wait_for(lambda:self.process.poll() is None and self.request('GET','/health')[0]==200)
        except Exception:
            self.close();raise

    def request(self, method, path, body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        try:
            headers={'Authorization':'Basic '+base64.b64encode(('operator:'+self.password).encode()).decode()}
            conn.request(method,path,body=body,headers=headers)
            response=conn.getresponse();return response.status,json.loads(response.read())
        finally:conn.close()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate();self.process.wait(timeout=10)
        if self.log:self.log.close()
        self.directory.cleanup()


class AlertReceiver:
    """Authenticated contract fixture, not an Alertmanager server or mail sink."""
    def __init__(self):
        self.token=secrets.token_urlsafe(32);self.requests=[];self.mode='ok'
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+owner.token:
                    self.send_error(401);return
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path,body))
                if owner.mode=='lost':self.close_connection=True;return
                response=(dict(version=1,accepted=True,test_context=body['test_context'],delivery_id=body['delivery_id'])
                          if self.path=='/v1/test-alerts' else None)
                raw=json.dumps(response).encode() if response else b''
                self.send_response(202 if response else 200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
