"""Reproducible live regression pack on a synthetic SQLite database.

Requires the local server and a configured provider. Creates an isolated imported
fixture, checks read answers against SQLite, then previews three changes and
commits/undoes one UPDATE. Never uses a user's database.
"""
import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from contextlib import closing

from e2e import http_json, run_sse

READS = [
    ('implicit category', 'How many active inventory items are there?', "SELECT COUNT(*) FROM inventory WHERE status='active'"),
    ('numeric range', 'Return only the names of inventory items priced between 20 and 80 inclusive, alphabetically by name.', 'SELECT name FROM inventory WHERE price BETWEEN 20 AND 80 ORDER BY name'),
    ('null', 'Return only the names of inventory items with no note recorded, alphabetically by name.', 'SELECT name FROM inventory WHERE note IS NULL ORDER BY name'),
    ('alternatives', 'Return only the names of inventory items whose status is active or pending, alphabetically by name.', "SELECT name FROM inventory WHERE status IN ('active','pending') ORDER BY name"),
    ('grouping and having', 'Which categories have at least two inventory items? Return category and count, largest count first, then category alphabetically.', 'SELECT category,COUNT(*) FROM inventory GROUP BY category HAVING COUNT(*)>=2 ORDER BY COUNT(*) DESC,category'),
    ('rounding', 'What is the average price of inventory items, rounded to two decimal places?', 'SELECT ROUND(AVG(price),2) FROM inventory'),
    ('month year', 'How many inventory items were added during February 2026?', "SELECT COUNT(*) FROM inventory WHERE added BETWEEN '2026-02-01' AND '2026-02-28'"),
    ('secondary ordering', 'Return only the name and price of inventory items, price highest first then name alphabetically.', 'SELECT name,price FROM inventory ORDER BY price DESC,name ASC'),
    ('worded limit', 'Return only the names of the three most expensive inventory items, highest price first.', 'SELECT name FROM inventory ORDER BY price DESC LIMIT 3'),
    ('empty answer', 'Return only the names of inventory items with stock greater than 999.', 'SELECT name FROM inventory WHERE stock>999'),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:7862')
    parser.add_argument('--output',type=Path,default=Path('workspace/release-validation.json'))
    args=parser.parse_args()
    records=[]
    with tempfile.TemporaryDirectory() as directory:
        source=Path(directory)/f'release_fixture_{time.time_ns()}.db'
        with closing(sqlite3.connect(source)) as conn:
            conn.execute('CREATE TABLE inventory (id INTEGER PRIMARY KEY,name TEXT,category TEXT,status TEXT,stock INTEGER,price REAL,note TEXT,added TEXT)')
            conn.executemany('INSERT INTO inventory VALUES (?,?,?,?,?,?,?,?)',[
                (1,'Atlas','Hardware','active',12,25.5,None,'2026-02-01'),
                (2,'Beacon','Hardware','active',4,80,'new','2026-02-28'),
                (3,'Cedar','Supplies','pending',20,10,None,'2026-01-01'),
                (4,'Delta','Hardware','retired',0,80,'old','2026-03-01'),
                (5,'Echo','Supplies','active',8,40,None,'2025-02-12'),
                (6,'Flux','Services','pending',1,120,'plan','2026-02-14'),
            ])
            conn.commit()
            database=http_json(args.url,'/api/databases',method='POST',body={'path':str(source)})['id']
            for name,prompt,sql in READS:
                response=run_sse(args.url,database,prompt,'read',120)
                result=response.get('result') or {}
                expected=[list(row) for row in conn.execute(sql)]
                passed=result.get('supported') is True and result.get('rows')==expected
                records.append({'case':name,'passed':passed,'expected':expected,'response':response})
                print(f"{'PASS' if passed else 'FAIL'} {name}",flush=True)
            changes=[
                ('update', "Set stock to 17 for the inventory item whose id is 1.", {'id':1,'stock':17}),
                ('insert', "Add an inventory item with name 'Grove', category 'Supplies', status 'active', stock 9, and price 23.5.", {'name':'Grove','stock':9}),
                ('delete', 'Delete the inventory item whose id is 3.', None),
            ]
            for name,prompt,after in changes:
                response=run_sse(args.url,database,prompt,'change',120)
                result=response.get('result') or {}
                passed=result.get('supported') is True and result.get('affected')==1
                if passed and after:
                    passed=all(result['after'][0].get(key)==value for key,value in after.items())
                if passed and name=='update':
                    committed=http_json(args.url,'/api/commit',method='POST',body={'token':result['commit_token'],'confirm':True})
                    changed=http_json(args.url,f'/api/inspect/{database}',method='POST',body={'sql':'SELECT stock FROM inventory WHERE id=1'})
                    passed=changed['rows']==[[17]]
                    http_json(args.url,'/api/undo',method='POST',body={'token':committed['undo_token']})
                    restored=http_json(args.url,f'/api/inspect/{database}',method='POST',body={'sql':'SELECT stock FROM inventory WHERE id=1'})
                    passed=passed and restored['rows']==[[12]]
                records.append({'case':name,'passed':passed,'response':response})
                print(f"{'PASS' if passed else 'FAIL'} {name} preview"+(' / commit / undo' if name=='update' else ''),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(records,indent=2),encoding='utf-8')
    print(f'{sum(item["passed"] for item in records)}/{len(records)} passed. Report: {args.output}')
    raise SystemExit(0 if all(item['passed'] for item in records) else 1)


if __name__=='__main__':
    main()
