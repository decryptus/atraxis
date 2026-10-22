"""Private SQLite account adapter; directory, projection and audit commit together."""
from contextlib import contextmanager
import os
import json
from pathlib import Path
import sqlite3
import stat
from sonicprobe.libs import anysql
from sonicprobe.libs.urisup import uri_help_unsplit
from atraxis.account_schema import SCHEMA_VERSION, APPLICATION_ID, SCHEMA, DIRECTORY_TABLES, ENTITIES

from atraxis.accounts import AccountError
from atraxis.account_lock import private_lock



def check_private(path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1):
        raise AccountError('account_store_not_private')


class SQLiteTransaction:
    def __init__(self, connection):
        self.connection = connection

    def read(self, *, audit=True):
        c = self.connection
        meta = c.execute('SELECT revision FROM account_meta WHERE id=1').fetchone()
        if meta is None:
            return None
        document = {'version': 3}
        for table in ENTITIES:
            cursor = c.execute('SELECT * FROM '+table+' ORDER BY position')
            keys = [column[0] for column in cursor.description]
            document[table] = []
            for row in cursor:
                item = dict(zip(keys,row)); item.pop('position')
                item['enabled'] = bool(item['enabled'])
                if table == 'groups': item['inherit_parent'] = bool(item['inherit_parent'])
                document[table].append(item)
        memberships = [(pos,dict(user=user,organization=org,group=None)) for user,org,pos in c.execute('SELECT user,organization,position FROM organization_memberships')]
        memberships += [(pos,dict(user=user,organization=org,group=group)) for user,org,group,pos in c.execute('SELECT user,organization,group_id,position FROM group_memberships')]
        document['memberships'] = [row for _,row in sorted(memberships)]
        document['delegations'] = []
        for row in c.execute('SELECT id,subject,organization,parent,organization_membership,delegable,enabled FROM delegations ORDER BY position'):
            item = dict(zip(('id','subject','organization','parent','organization_membership','delegable','enabled'),row))
            for key in ('organization_membership','delegable','enabled'): item[key] = bool(item[key])
            item['groups'] = [r[0] for r in c.execute('SELECT group_id FROM delegation_groups WHERE delegation=? ORDER BY position',(item['id'],))]
            item['operations'] = [r[0] for r in c.execute('SELECT operation FROM delegation_operations WHERE delegation=? ORDER BY position',(item['id'],))]
            document['delegations'].append(item)
        document['entity_delegations'] = []
        for row in c.execute('SELECT id,subject,scope_kind,tenant,organization,group_id,user_id,parent,enabled,delegable FROM entity_delegations ORDER BY position'):
            grant = dict(id=row[0],subject=row[1],scope=dict(kind=row[2],id=next((x for x in row[3:7] if x is not None),None)),
                         parent=row[7],enabled=bool(row[8]),delegable=bool(row[9]))
            grant['operations'] = [r[0] for r in c.execute('SELECT operation FROM entity_delegation_operations WHERE delegation=? ORDER BY position',(grant['id'],))]
            document['entity_delegations'].append(grant)
        events = []
        query = 'SELECT revision,previous,uid,time,sha256,actor_subject,actor_operation,actor_organization,actor_target FROM account_audit '
        for row in c.execute(query + ('ORDER BY id' if audit else 'ORDER BY id DESC LIMIT 1')):
            event = dict(zip(('revision','previous','uid','time','sha256'),row[:5]))
            if row[5] is not None: event['actor'] = dict(zip(('subject','operation','organization','target'),row[5:]))
            events.append(event)
        members = []
        for subject,org in c.execute('SELECT subject,organization FROM membership_members ORDER BY subject,organization'):
            groups = [r[0] for r in c.execute('SELECT group_id FROM membership_groups WHERE subject=? AND organization=? ORDER BY group_id',(subject,org))]
            members.append(dict(subject=subject,organization=org,groups=groups))
        return dict(version=3,revision=meta[0],directory=document,audit=events,membership=dict(version=1,revision=meta[0],members=members))

    def write(self, data):
        from atraxis.accounts import Directory
        from atraxis.account_delegation import view
        directory = Directory(data['directory'])
        c = self.connection
        # All directory relationships and derived read models change in one commit.
        for table in DIRECTORY_TABLES: c.execute('DELETE FROM '+table)
        for table in ENTITIES:
            for position,item in enumerate(data['directory'][table]):
                row = dict(item,position=position)
                columns = list(row)
                c.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(row.values()))
        for position,item in enumerate(data['directory']['memberships']):
            if item['group'] is None:
                c.execute('INSERT INTO organization_memberships VALUES(?,?,?)',(item['user'],item['organization'],position))
            else:
                c.execute('INSERT INTO group_memberships VALUES(?,?,?,?)',(item['user'],item['organization'],item['group'],position))
        for position,item in enumerate(data['directory']['delegations']):
            c.execute('INSERT INTO delegations VALUES(?,?,?,?,?,?,?,?)',tuple(item[k] for k in ('id','subject','organization','parent','organization_membership','delegable','enabled'))+(position,))
            for i,group in enumerate(item['groups']): c.execute('INSERT INTO delegation_groups VALUES(?,?,?,?)',(item['id'],item['organization'],group,i))
            for i,operation in enumerate(item['operations']): c.execute('INSERT INTO delegation_operations VALUES(?,?,?)',(item['id'],operation,i))
        for position, grant in enumerate(data['directory']['entity_delegations']):
            scoped = [grant['scope']['id'] if grant['scope']['kind']==kind else None for kind in ('tenant','organization','group','user')]
            c.execute('INSERT INTO entity_delegations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (grant['id'],grant['subject'],grant['scope']['kind'],*scoped,grant['parent'],grant['enabled'],grant['delegable'],position))
            c.executemany('INSERT INTO entity_delegation_operations VALUES(?,?,?)',[(grant['id'],operation,i) for i,operation in enumerate(grant['operations'])])
        for member in data['membership']['members']:
            c.execute('INSERT INTO membership_members VALUES(?,?)',(member['subject'],member['organization']))
            c.executemany('INSERT INTO membership_groups VALUES(?,?,?)',[(member['subject'],member['organization'],g) for g in member['groups']])
        for subject in {g['subject'] for g in [*directory.delegations.values(),*directory.entity_delegations.values()]}:
            for org in view(directory,subject):
                rows = [('organization',org['id'],org['name'])]
                rows += [('group',g['id'],g['name']) for g in org['groups']]
                rows += [('user',user,user) for user in sorted({m['user'] for m in org['memberships']})]
                c.executemany('INSERT INTO account_search_scope VALUES(?,?,?,?,?)',[(subject,org['id'],*row) for row in rows])
        from atraxis.account_entities import view as entity_view
        for subject in {g['subject'] for g in directory.entity_delegations.values()}:
            for kind, entities in entity_view(directory, subject)['entities'].items():
                c.executemany('INSERT INTO entity_search_scope VALUES(?,?,?,?,?)', [
                    (subject, e.get('organization', e['id'] if kind=='organizations' else None), kind[:-1], e['id'], e['name']) for e in entities])
        self.append_audit(data['audit'][-1])
        c.execute('INSERT INTO account_meta VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',(data['revision'],))

    def append_audit(self, event):
        actor = event.get('actor',{})
        self.connection.execute('INSERT INTO account_audit(revision,previous,uid,time,sha256,actor_subject,actor_operation,actor_organization,actor_target) VALUES(?,?,?,?,?,?,?,?,?)',tuple(event[k] for k in ('revision','previous','uid','time','sha256'))+tuple(actor.get(k) for k in ('subject','operation','organization','target')))


class SQLiteStorage:
    def __init__(self, config):
        self.path = Path(config['url'][len('sqlite:///'):])

    @contextmanager
    def _connect(self, *, initialize=False, writable=False):
        connection = None
        try:
            if initialize:
                try:
                    descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            if os.getuid() != os.geteuid():
                raise AccountError('account_store_not_private')
            parent = self.path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) & 0o077):
                raise AccountError('account_store_not_private')
            check_private(self.path)
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(self.path) + suffix)
                if os.path.lexists(sidecar):
                    check_private(sidecar)
            uri = uri_help_unsplit(('sqlite3',None,str(self.path),[('mode','rw' if writable else 'ro'),('timeout_ms','10000')],None))
            owner = anysql.connect_by_uri(uri, auto_reconnect=False)
            connection = owner.driver_connection
            connection.isolation_level = None
            connection.execute('PRAGMA foreign_keys=ON')
            connection.create_function('casefold',1,str.casefold,deterministic=True)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA synchronous=FULL')
        except (sqlite3.Error, OSError):
            if connection is not None:
                connection.close()
            raise AccountError('account_store_unavailable') from None
        try:
            yield connection
        except sqlite3.Error:
            raise AccountError('account_store_unavailable') from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _schema(connection, initialize):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version == 0 and initialize:
            if connection.execute('SELECT name FROM sqlite_master').fetchone() is not None:
                raise AccountError('unsupported_account_store_schema')
            for statement in SCHEMA: connection.execute(statement)
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        elif version != SCHEMA_VERSION or connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise AccountError('unsupported_account_store_schema')

    def read(self, *, audit=True):
        with self._connect() as connection:
            connection.execute('BEGIN')
            self._schema(connection, False)
            return SQLiteTransaction(connection).read(audit=audit)

    @contextmanager
    def transaction(self, *, initialize=False):
        with private_lock(self.path), self._connect(initialize=initialize, writable=True) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                self._schema(connection, initialize)
                yield SQLiteTransaction(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def revision(self):
        with self._connect() as connection:
            self._schema(connection,False)
            row = connection.execute('SELECT revision FROM account_meta WHERE id=1').fetchone()
            if row is None: raise AccountError('account_store_unavailable')
            return row[0]

    def search(self, subject, query):
        with self._connect() as c:
            c.execute('BEGIN'); self._schema(c,False)
            revision = c.execute('SELECT revision FROM account_meta WHERE id=1').fetchone()[0]
            if c.execute('SELECT 1 FROM account_search_scope WHERE subject=? UNION ALL SELECT 1 FROM entity_search_scope WHERE subject=? LIMIT 1',(subject,subject)).fetchone() is None:
                raise PermissionError('account_administration_denied')
            clauses, values = ['subject=?'], [subject]
            if query.organization: clauses.append('organization=?'); values.append(query.organization)
            if query.kind!='all': clauses.append('kind=?'); values.append(query.kind)
            if query.q:
                clauses.append('(instr(casefold(id),?)>0 OR instr(casefold(name),?)>0)')
                values.extend([query.q.casefold()]*2)
            where = ' AND '.join(clauses)
            projection = ('SELECT organization,kind,id,name FROM account_search_scope WHERE '+where+
                          " UNION SELECT COALESCE(organization,''),kind,id,name FROM entity_search_scope WHERE "+where)
            total = c.execute('SELECT COUNT(*) FROM ('+projection+')',values*2).fetchone()[0]
            rows = c.execute('SELECT * FROM ('+projection+') ORDER BY organization,kind,id LIMIT ? OFFSET ?',values*2+[query.limit,(query.page-1)*query.limit])
            items = [dict(zip(('organization','kind','id','name'),row)) for row in rows]
            return dict(subject=subject,revision=revision,query=query.parameters(),total=total,items=items,has_more=query.page*query.limit<total)
