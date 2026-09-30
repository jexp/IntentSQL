"""Repeatable DEVELOPMENT regression runs; never an unseen external score.

Uses the existing frozen cases, gold SQL and semantic evaluators. All outputs
and extracted third-party data live in INTENTSQL_ARTIFACTS. No case replacement.
"""
from __future__ import annotations
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.artifacts import artifact_root
from benchmarks.v1.run_benchmark import (validate_corpus, DEFAULT_CORPUS, rows_equivalent,
    semantic_check, listify, percentile, is_infrastructure_error)
from benchmarks.spider.prepare import ARCHIVE, preflight, selection_settings
from benchmarks.spider.run import (extract_selected, preflight_gold, semantic_check as spider_semantic,
                                  architecture_digest)
from intentsql import mutations, read_engine
from intentsql.database import connect
from intentsql.jev_client import JevClient
from intentsql.semantic_read import run_read


def cases_for(suites):
    cases=[]
    if 'core' in suites:
        corpus=validate_corpus(DEFAULT_CORPUS, verbose=False)
        # Validation returns the full frozen manifest.
        for c in corpus['cases']:
            if c['suite']=='core':
                cases.append({**c,'suite':'core','path':ROOT/'data'/c['database'],
                    'question':c['prompt'],'gold_sql':c['reference_sql'],
                    'ordered':c['result_ordered']})
    if 'cs50' in suites:
        from benchmarks.cs50_cases import CASES, ALPHA_APPLICABLE_IDS
        cases += [{**c,'suite':'cs50','path':ROOT/'data'/c['database']}
                  for c in CASES if c['id'] in ALPHA_APPLICABLE_IDS]
    if 'semantic' in suites:
        plan=json.loads((ROOT/'benchmarks/semantic-final-plan.json').read_text())
        cases += [{**c,'suite':'semantic','path':ROOT/'data'/c.get('database','cyberchase.db')}
                  for c in plan['targeted_cases']]
    if 'spider' in suites:
        # Only the six previously executed samples; no fresh sample is selected.
        for sample in range(1,7):
            _,manifest,_=selection_settings(sample)
            frozen=preflight(ARCHIVE,manifest,sample)
            paths=extract_selected(ARCHIVE,frozen)
            _,metadata=preflight_gold(frozen,paths)
            cases += [{**c,'suite':f'spider-{sample}','path':paths[c['db_id']],
                       'ordered':bool(c['gold_ast']['orderBy']),
                       '_metadata':metadata[c['db_id']]}
                      for c in frozen['cases']]
    return cases


def summary(records):
    n=len(records)
    inp=sum(r['usage'].get('input_tokens',0) for r in records)
    out=sum(r['usage'].get('output_tokens',0) for r in records)
    lat=[r['latency_seconds'] for r in records]
    return {'passed':sum(r['passed'] for r in records),'total':n,
        'execution_correct':sum(r['execution'] for r in records),
        'semantic_evaluated':sum(r['semantic'] is not None for r in records),
        'semantic_correct':sum(r['semantic'] is True for r in records),
        'rejected':sum(r['rejected'] and not r['infrastructure_error'] for r in records),
        'wrong_executions':sum(not r['rejected'] and not r['execution'] for r in records),
        'infrastructure_errors':sum(r['infrastructure_error'] for r in records),
        'calls':sum(r['usage'].get('jev_calls',0) for r in records),
        'input_tokens':inp,'output_tokens':out,'total_tokens':inp+out,
        'estimated_cost_usd':(inp*read_engine.JEV_INPUT_USD_PER_MTOK+out*read_engine.JEV_OUTPUT_USD_PER_MTOK)/1e6,
        'median_latency_seconds':statistics.median(lat) if lat else None,
        'p95_latency_seconds':percentile(lat,.95) if lat else None}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',nargs='+',choices=['core','cs50','semantic','spider'],required=True)
    parser.add_argument('--case',nargs='*')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--preflight-only',action='store_true')
    parser.add_argument('--saved-connection',action='store_true')
    args=parser.parse_args()
    cases=cases_for(args.suite)
    if args.case:
        cases=[c for c in cases if c['id'] in args.case]
    print(f'Prepared {len(cases)} development cases; zero provider calls so far.',flush=True)
    if args.preflight_only:return
    if args.output.exists():raise ValueError('Output exists; preserve previous evidence')
    if args.saved_connection:
        from intentsql.web import connection_settings
        cfg=connection_settings()
        for name,value in [('SYSTEM_ONE_API_KEY',cfg['api_key']),('SYSTEM_ONE_URL',cfg['url']),('SYSTEM_ONE_MODEL',cfg['model'])]:
            os.environ[name]=value
            setattr(read_engine,name,value)
    records=[]
    report={'kind':'development_regression','architecture_sha256':architecture_digest(),'records':records}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter()
    for case in cases:
        steps=[];client=JevClient()
        start=time.perf_counter()
        row={'id':case['id'],'suite':case['suite'],'question':case['question'],
             'gold_sql':case.get('gold_sql'), 'execution':False,'semantic':None,
             'passed':False,'rejected':True,'infrastructure_error':False}
        try:
            if case.get('kind')=='mutation':
                with tempfile.TemporaryDirectory() as folder:
                    path=Path(folder)/'fixture.db'
                    with connect(path) as conn:
                        conn.execute('CREATE TABLE inventory(id INTEGER PRIMARY KEY,name TEXT,stock INTEGER)')
                        conn.executemany('INSERT INTO inventory VALUES(?,?,?)',[(1,'Alpha',8),(2,'Beta',4)])
                    result=mutations.plan_mutation(path,case['question'])
                    row['rejected']=not result['supported']
                    if result['supported']:
                        from intentsql import web
                        with patch.dict(web.PENDING, {'test': {'database': 'fixture', 'plan': result}}, clear=True), \
                             patch.dict(web.UNDO, {}, clear=True), \
                             patch.object(web, 'database_path', return_value=path), \
                             patch.object(web, 'BACKUP_DIR', Path(folder)), \
                             patch.object(web, 'schema', return_value={}):
                            web.commit(web.CommitInput(token='test',confirm=True))
                            with connect(path) as conn:
                                actual=[list(r) for r in conn.execute('SELECT * FROM inventory ORDER BY id')]
                            web.undo(web.UndoInput(token='test'))
                            with connect(path) as conn:
                                restored=[list(r) for r in conn.execute('SELECT * FROM inventory ORDER BY id')]
                        row['execution']=(actual==case['expected_after'] and restored==[[1,'Alpha',8],[2,'Beta',4]])
                        row['semantic']=bool(result['vet']['passed'])
            else:
                with connect(case['path']) as conn:
                    expected=conn.execute(case['gold_sql']).fetchall() if case.get('gold_sql') else []
                result=run_read(case['path'],case['question'],client,on_step=steps.append)
                row['rejected']=False
                row['execution']=rows_equivalent(result['rows'],expected,case.get('ordered',False))
                if case['suite'].startswith('spider'):
                    row['semantic'],row['semantic_reasons']=spider_semantic(case,case['_metadata'],result['program'])
                elif 'semantic' in case:
                    row['semantic'],row['semantic_reasons']=semantic_check(case['semantic'],result['program'])
                elif 'expected_typed' in case:
                    typed=listify(result['program']['typed_query'])
                    row['semantic']=all(typed.get(k)==v for k,v in case['expected_typed'].items())
            row.update(sql=result['sql'],params=result['params'],program=result['program'])
            row['passed']=row['execution'] and row['semantic'] is not False and case.get('kind')!='reject'
        except Exception as exc:
            row['error']=str(exc)
            row['infrastructure_error']=is_infrastructure_error(str(exc))
            if case.get('kind')=='reject' and not row['infrastructure_error']:
                row.update(passed=True,execution=True,semantic=True)
        row['usage']=read_engine.usage_stats() if case.get('kind')=='mutation' else client.usage_stats()
        row['latency_seconds']=time.perf_counter()-start
        if not row['passed']:row['steps']=steps
        records.append(row)
        report['summary']=summary(records)
        report['suites']={suite:summary([r for r in records if r['suite']==suite]) for suite in sorted({r['suite'] for r in records})}
        report['wall_seconds']=time.perf_counter()-started
        args.output.write_text(json.dumps(report,indent=2,default=str))
        print(f"{case['suite']} {case['id']}: {'PASS' if row['passed'] else row.get('error','MISMATCH')}",flush=True)
    print(json.dumps(report['suites'],indent=2))


if __name__=='__main__':main()
