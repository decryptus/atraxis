"""Disposable CA and TLS ingress; forwards bytes to fixed loopback backends only."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import ssl
import threading
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class Authority:
    def __init__(self, root, name='authority'):
        self.root=root;self.name=name
        self.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)])
        now=datetime.now(timezone.utc)
        self.cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(self.key,hashes.SHA256()))
        self.path=root/(name+'.pem');self.path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM));self.path.chmod(0o600)

    def leaf(self, name, *, wrong_host=False, expired=False):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        now=datetime.now(timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)]))
            .issuer_name(self.cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(days=2))
            .not_valid_after(now-timedelta(days=1) if expired else now+timedelta(hours=2))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.2' if wrong_host else '127.0.0.1'))]),critical=False)
            .sign(self.key,hashes.SHA256()))
        certfile=self.root/(name+'.crt');keyfile=self.root/(name+'.key')
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM));certfile.chmod(0o600)
        keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));keyfile.chmod(0o600)
        return certfile,keyfile


class TLSIngress:
    def __init__(self, origin, certificate):
        backend=urlsplit(origin)
        if backend.scheme!='http' or backend.hostname!='127.0.0.1':raise ValueError('loopback_backend_required')
        self.calls=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def exchange(self):
                owner.calls.append((self.command,self.path))
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                # No forwarded identity headers become authentication. Backends
                # authenticate the explicit Authorization header themselves.
                headers={key:value for key,value in self.headers.items() if key.lower() in
                         ('authorization','content-type','origin','sec-fetch-site','accept')}
                connection=http.client.HTTPConnection('127.0.0.1',backend.port,timeout=10)
                try:
                    connection.request(self.command,self.path,body or None,headers)
                    response=connection.getresponse();data=response.read()
                    self.send_response(response.status)
                    for key,value in response.getheaders():
                        if key.lower() not in ('content-length','connection','transfer-encoding','server','date'):
                            self.send_header(key,value)
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except OSError:
                    self.close_connection=True
                finally:connection.close()
            do_GET=do_POST=do_HEAD=exchange
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);self.context.load_cert_chain(*certificate)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.socket=self.context.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='https://127.0.0.1:'+str(self.server.server_port)

    def certificate(self, paths):self.context.load_cert_chain(*paths)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
