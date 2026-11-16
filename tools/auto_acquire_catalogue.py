#!/usr/bin/env python3
"""Automatically research registered official sources and stage app import candidates."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from cardscanr_worldwide.auto_acquisition import plan,run_cycle,stage_app_candidates

def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed',type=Path,default=ROOT/'config/catalogue_source_feed.json')
    parser.add_argument('--archive',type=Path,default=Path('D:/CardScanR_Archive'))
    parser.add_argument('--worldwide-db',type=Path)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('plan',help='List registered automatic discovery sources and real staging gaps')
    collect=commands.add_parser('collect',help='Run bounded resumable source discovery and downloads; NO URL input required')
    collect.add_argument('--max-items',type=int,default=50)
    collect.add_argument('--per-source-items',type=int,default=25)
    export=commands.add_parser('stage',help='Build versioned app import candidate package, never publish')
    export.add_argument('--output',required=True,type=Path)
    export.add_argument('--crosswalk',type=Path)
    args=parser.parse_args()
    if args.command=='plan':result=plan(args.feed,database=args.worldwide_db)
    elif args.command=='collect':result=run_cycle(args.feed,args.archive,database=args.worldwide_db,
        per_source_limit=args.per_source_items,total_limit=args.max_items)
    else:result=stage_app_candidates(args.archive,args.output,crosswalk=args.crosswalk,worldwide_db=args.worldwide_db)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,PermissionError,OSError,sqlite3.Error) as exc:
        print(f'catalogue automation: {type(exc).__name__}: {exc}',file=sys.stderr)
        raise SystemExit(2) from exc