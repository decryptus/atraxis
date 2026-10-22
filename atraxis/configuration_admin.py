"""Host-approved source configuration; reviewed writes and audit share one file."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml
from atraxis.account_lock import private_lock
from atraxis.account_storage import storage_config
from atraxis.account_store import AccountStore
from atraxis.accounts import AccountError, Directory, identifier
from atraxis.config import _StrictLoader, _MAX_CONFIG_BYTES
from atraxis.rule_files import read_private
from atraxis.source_config import parse_sources, MAX_SOURCES

MAX_PROFILE_BYTES = 262144
MAX_HISTORY = 100
SETTINGS = ('timeout', 'poll_interval', 'max_age')
CONNECTION_FIELDS = frozenset({'kind','origin','credentials_file','ca_file','allow_loopback_http'})
OPERATIONS = frozenset({'source.add','source.update','source.remove'})
HASH = re.compile(r'[0-9a-f]{64}')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def require(value):
    if not value:raise AccountError('invalid_configuration_administration')


def load_profile(path):
    path=Path(path).absolute()
    document=yaml.load(read_private(path,MAX_PROFILE_BYTES),Loader=_StrictLoader)
    require(type(document) is dict and set(document)=={'version','store','identities','administrators','connections'}
            and type(document['version']) is int and document['version']==1)
    store=storage_config(document['store'],path.parent)
    identities=document['identities'];admins=document['administrators'];connections=document['connections']
    require(type(identities) is list and 1<=len(identities)<=128 and type(admins) is list and len(admins)<=128)
    seen=set()
    for item in identities:
        require(type(item) is dict and set(item)=={'username','subject','organization'}
                and all(identifier(value) for value in item.values()) and item['username'] not in seen)
        seen.add(item['username'])
    seen=set()
    for item in admins:
        require(type(item) is dict and set(item)=={'subject','organization'} and all(identifier(value) for value in item.values()))
        key=(item['subject'],item['organization']);require(key not in seen);seen.add(key)
    require(type(connections) is dict and len(connections)<=MAX_SOURCES)
    for name,spec in connections.items():
        require(identifier(name) and type(spec) is dict and {'kind','origin','credentials_file'}<=set(spec)<=CONNECTION_FIELDS)
        parse_sources([dict(spec,source_id=name)],path)
    return document,store,fingerprint(document)


def validate_history(history):
    require(type(history) is list and len(history)<=MAX_HISTORY)
    seen=set()
    for event in history:
        require(type(event) is dict and set(event)=={'id','actor','organization','operation','source_id','previous','time'}
                and type(event['id']) is str and re.fullmatch(r'[0-9a-f]{32}',event['id'])
                and event['id'] not in seen and identifier(event['actor']) and identifier(event['organization'])
                and type(event['operation']) is str and event['operation'] in OPERATIONS
                and type(event['source_id']) is str and 0<len(event['source_id'])<=128
                and type(event['previous']) is str and HASH.fullmatch(event['previous'])
                and type(event['time']) is str and len(event['time'])<=64)
        datetime.fromisoformat(event['time']);seen.add(event['id'])


class ConfigurationAdmin:
    def __init__(self,profile_path,config_path,username):
        self.path=Path(profile_path).absolute();self.config_path=Path(config_path).absolute();self.username=username
        self.initial=self.entry()

    def entry(self):
        profile,store,_=load_profile(self.path)
        matches=[e for e in profile['identities'] if e['username']==self.username]
        if len(matches)!=1:raise PermissionError('configuration_identity_denied')
        entry=dict(matches[0],store=store)
        if hasattr(self,'initial') and entry!=self.initial:raise PermissionError('configuration_identity_changed')
        return entry

    def state(self):
        entry=self.entry();profile,store,revision=load_profile(self.path)
        snapshot=AccountStore(store).snapshot(audit=False);directory=Directory(snapshot['directory'])
        actor={key:entry[key] for key in ('subject','organization')}
        if actor not in profile['administrators'] or not directory.allows_membership(entry['subject'],entry['organization']):
            raise PermissionError('configuration_administration_denied')
        document=yaml.load(read_private(self.config_path,_MAX_CONFIG_BYTES),Loader=_StrictLoader)
        from atraxis.config import parse_config
        parsed=parse_config(document,self.config_path)
        if parsed.configuration_web_file!=str(self.path):raise PermissionError('configuration_profile_changed')
        if self.entry()!=entry:raise PermissionError('configuration_identity_changed')
        return entry,profile,revision,snapshot,document

    def inspect(self,active_sources=None):
        entry,profile,revision,snapshot,document=self.state()
        result=dict(identity=entry,revision=fingerprint(document),sources=[self.summary(s,profile) for s in document.get('sources',[])],
            connections=[dict(id=name,kind=s['kind'],origin=s['origin']) for name,s in profile['connections'].items()],
            audit=deepcopy(document.get('configuration_history',[])[-20:]))
        if active_sources is not None:
            result['restart_required']=parse_sources(document.get('sources',[]),self.config_path)!=active_sources
        current=self.state()
        if current[2]!=revision or current[3]['revision']!=snapshot['revision'] or fingerprint(current[4])!=result['revision']:
            raise AccountError('configuration_revision_conflict')
        return result

    def connection(self,profile,name):
        require(type(name) is str and name in profile['connections'])
        spec=deepcopy(profile['connections'][name])
        for key in ('credentials_file','ca_file'):
            if key in spec:
                path=Path(spec[key]);spec[key]=str(path if path.is_absolute() else self.path.parent/path)
        return spec

    def summary(self,source,profile):
        connection=None
        for name in profile['connections']:
            spec=self.connection(profile,name)
            candidate={key:deepcopy(source[key]) for key in CONNECTION_FIELDS if key in source}
            for key in ('credentials_file','ca_file'):
                if key in candidate:
                    path=Path(candidate[key]);candidate[key]=str(path if path.is_absolute() else self.config_path.parent/path)
            if candidate==spec:connection=name;break
        return dict(source_id=source['source_id'],kind=source['kind'],origin=source['origin'],connection=connection,
            timeout=source.get('timeout',10),poll_interval=source.get('poll_interval',60),max_age=source.get('max_age',180))

    def propose(self,request):
        entry,profile,profile_revision,snapshot,document=self.state()
        require(type(request) is dict and type(request.get('operation')) is str and request['operation'] in OPERATIONS)
        operation=request['operation'];fields={'operation','source_id'}
        require(set(request)==(fields if operation=='source.remove' else fields|{'connection',*SETTINGS}))
        source_id=request['source_id'];require(type(source_id) is str and identifier(source_id))
        sources=document.get('sources',[]);matches=[s for s in sources if s['source_id']==source_id]
        require(len(matches)==(0 if operation=='source.add' else 1))
        before=deepcopy(matches[0]) if matches else None
        after=None
        if operation!='source.remove':
            after=deepcopy(before) if before else {}
            for key in CONNECTION_FIELDS:after.pop(key,None)
            after.update(self.connection(profile,request['connection']),source_id=source_id,
                         **{key:request[key] for key in SETTINGS})
            parse_sources([after],self.config_path)
        candidate=deepcopy(document)
        candidate['sources']=[after if s['source_id']==source_id else deepcopy(s) for s in sources]
        if operation=='source.add':candidate['sources'].append(after)
        if operation=='source.remove':candidate['sources']=[s for s in candidate['sources'] if s is not None]
        from atraxis.config import parse_config
        parse_config(candidate,self.config_path)
        require(candidate!=document)
        plan=dict(request=deepcopy(request),identity=entry,revision=fingerprint(document),profile_revision=profile_revision,
            directory_revision=snapshot['revision'],before=self.summary(before,profile) if before else None,
            after=self.summary(after,profile) if after else None)
        latest=self.state()
        if (latest[0]!=entry or latest[2]!=profile_revision or latest[3]['revision']!=snapshot['revision']
                or fingerprint(latest[4])!=plan['revision']):
            raise AccountError('configuration_revision_conflict')
        return plan,candidate

    def prepare(self,request,revision):
        plan,_=self.propose(request)
        if plan['revision']!=revision:raise AccountError('configuration_revision_conflict')
        return plan

    def commit(self,plan):
        entry=self.entry();store=AccountStore(entry['store'])
        if store.path in (self.path,self.config_path) or self.path==self.config_path:
            raise AccountError('invalid_configuration_paths')
        # Account -> administration profile -> daemon configuration: one lock order.
        with store._locked(),private_lock(self.path),private_lock(self.config_path):
            current,candidate=self.propose(plan['request'])
            if current!=plan:raise AccountError('configuration_revision_conflict')
            history=candidate.setdefault('configuration_history',[])
            require(len(history)<MAX_HISTORY)
            history.append(dict(id=uuid.uuid4().hex,actor=entry['subject'],organization=entry['organization'],
                operation=plan['request']['operation'],source_id=plan['request']['source_id'],previous=plan['revision'],
                time=datetime.now(timezone.utc).isoformat()))
            from atraxis.config import parse_config
            parse_config(candidate,self.config_path)
            raw=yaml.safe_dump(candidate,sort_keys=False).encode();require(len(raw)<=_MAX_CONFIG_BYTES)
            descriptor,temporary=tempfile.mkstemp(prefix='.configuration-',dir=self.config_path.parent)
            try:
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                # Recheck current authority and every reviewed revision before publication.
                if self.propose(plan['request'])[0]!=plan:raise AccountError('configuration_revision_conflict')
                os.replace(temporary,self.config_path)
                parent=os.open(self.config_path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(parent)
                finally:os.close(parent)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
            return history[-1]['id']
