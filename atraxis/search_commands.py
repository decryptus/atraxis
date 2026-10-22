"""Explicit read-only search command, shared remote semantics for CLI and Textual."""
from dataclasses import replace
import json
import sys
from atraxis.accounts import identifier
from atraxis.client_config import DEFAULT_CLIENT_CONFIG, load_client_config
from atraxis.inventory_search import SearchQuery, FILTER_FIELDS
from atraxis.search_client import read_search


def add_parser(commands):
    parser=commands.add_parser('search',help='search authorized inventory before pagination')
    parser.add_argument('section',choices=('inventory',))
    parser.add_argument('--config',default=DEFAULT_CLIENT_CONFIG)
    parser.add_argument('-o','--organization')
    parser.add_argument('-q','--query',default='',dest='q')
    for name in FILTER_FIELDS[1:]:
        parser.add_argument('--'+name,default='all' if name in ('state','kind') else '')
    parser.add_argument('--page',type=int,default=1)
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--tui',action='store_true')


def main(options):
    try:
        if options.organization is not None and not identifier(options.organization):
            raise ValueError('invalid_organization')
        config=replace(load_client_config(options.config),organization=options.organization)
        query=SearchQuery(**{key:getattr(options,key) for key in (*FILTER_FIELDS,'page','limit')})
        if options.tui:
            if options.json:raise ValueError('incompatible_output_modes')
            from atraxis.search_textual import run
            return run(config,query)
        data,error=read_search(config,query)
        if error:
            print('Choose an organization with -o ID.' if error=='organization_required' else 'Inventory search failed: '+error,file=sys.stderr)
            return 1
        if options.json:print(json.dumps(data,ensure_ascii=True,allow_nan=False,indent=2))
        else:
            if data['context'] is not None:
                print('Organization: '+json.dumps(data['context']['organization'],ensure_ascii=True))
            print('Collection: '+('Ready' if data['ready'] else data['reason']))
            print(str(data['total'])+' matching resources; page '+str(query.page))
            for row in data['items']:
                name=row['fields']['name'] if row['kind']=='container' else ', '.join(row['fields']['domains'])
                print(' | '.join(json.dumps(v,ensure_ascii=True) for v in (row['source_id'],row['kind'],row['native_id'],name,row['state'])))
            if data['unavailable']:print('Some sources have no current observations. Absence from results is not proof of absence.')
            if data['has_more']:print('More results: use --page '+str(query.page+1))
        return 0 if data['ready'] else 2
    except KeyboardInterrupt:return 130
    except ImportError:
        print('Install the Textual optional dependency for --tui.',file=sys.stderr);return 1
    except Exception:
        print('Inventory search unavailable; check configuration and filters.',file=sys.stderr);return 1
