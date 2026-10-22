"""Scoped administration commands. The OS mapping supplies the acting subject."""
import json
import sys
import yaml
from atraxis.account_admin import AccountAdmin
from atraxis.account_delegation import checked_request
from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private

MAX_REQUEST_BYTES = 32768


def add_parser(commands):
    parser=commands.add_parser('accounts-admin',help='scoped account administration')
    parser.add_argument('--config',required=True)
    actions=parser.add_subparsers(dest='admin_command',required=True)
    actions.add_parser('show');actions.add_parser('tui')
    search=actions.add_parser('search',help='search the complete authorized account scope')
    search.add_argument('-q','--query',default='')
    search.add_argument('--kind',choices=('all','tenant','user','group','organization'),default='all')
    search.add_argument('-o','--organization',default='')
    search.add_argument('--page',type=int,default=1);search.add_argument('--limit',type=int,default=50)
    search.add_argument('--tui',action='store_true')
    for name in ('check','apply'):
        command=actions.add_parser(name);command.add_argument('--file',required=True)
        if name=='apply':
            command.add_argument('--expected-revision',required=True)
            command.add_argument('--confirm',required=True,action='store_true')


def main(options):
    try:
        admin=AccountAdmin(options.config)
        if options.admin_command=='search':
            from atraxis.account_search import AccountQuery
            query=AccountQuery(q=options.query,kind=options.kind,organization=options.organization,page=options.page,limit=options.limit)
            if options.tui:
                from atraxis.account_search_textual import run
                return run(admin,query)
            print(json.dumps(admin.search(query),indent=2));return 0
        if options.admin_command=='tui':
            from atraxis.account_textual import run
            return run(admin)
        data=admin.inspect()
        if options.admin_command=='show':result=data
        else:
            request=checked_request(yaml.load(read_private(options.file,MAX_REQUEST_BYTES),Loader=_StrictLoader))
            revision=options.expected_revision if options.admin_command=='apply' else data['revision']
            plan=admin.prepare(request,revision)
            result=({'revision':admin.commit(plan)} if options.admin_command=='apply' else
                    {'revision':plan['revision'],'request':plan['request'],'actor':plan['audit']})
        print(json.dumps(result,indent=2));return 0
    except PermissionError:
        print('Account administration denied.',file=sys.stderr);return 3
    except Exception:
        print('Account operation unconfirmed. Check the current directory before retrying.',file=sys.stderr);return 2
