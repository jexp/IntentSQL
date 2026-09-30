"""Twenty fresh release prompts checked against independent SQLite answers.

Requires a running server and configured provider. Only an isolated imported
fixture is used for mutation preview; nothing is committed. The report retains
every event, typed program, SQL, parameters, expected answer and actual result.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from e2e import http_json, run_sse

CASES = [
    ('filter and ordering', 'cyberchase.db', 'Return only the titles of episodes in season 4, alphabetically by title.', 'SELECT title FROM episodes WHERE season=4 ORDER BY title'),
    ('month and year', 'cyberchase.db', 'How many episodes first aired during March 2003?', "SELECT COUNT(*) FROM episodes WHERE air_date BETWEEN '2003-03-01' AND '2003-03-31'"),
    ('unsupported window', 'cyberchase.db', 'Rank episodes within each season by air date and return the first two of each season.', None),
    ('multiple predicates', 'moneyball.db', 'How many performances in 1998 had at least 40 home runs?', 'SELECT COUNT(*) FROM performances WHERE year=1998 AND HR>=40'),
    ('implicit category', 'moneyball.db', 'How many left-handed batters are in the players table?', "SELECT COUNT(*) FROM players WHERE bats='L'"),
    ('range', 'moneyball.db', 'How many players weigh between 180 and 190 inclusive?', 'SELECT COUNT(*) FROM players WHERE weight BETWEEN 180 AND 190'),
    ('null', 'fixture', 'Return only the names of inventory items whose note is null, ordered by id descending.', 'SELECT name FROM inventory WHERE note IS NULL ORDER BY id DESC'),
    ('not null', 'fixture', 'How many inventory items have a note recorded?', 'SELECT COUNT(*) FROM inventory WHERE note IS NOT NULL'),
    ('text match', 'cyberchase.db', "Return only episode titles containing 'Snow', alphabetically by title.", "SELECT title FROM episodes WHERE title LIKE '%Snow%' ORDER BY title"),
    ('same-column alternatives', 'cyberchase.db', 'How many episodes are in season 3 or season 5?', 'SELECT COUNT(*) FROM episodes WHERE season IN (3,5)'),
    ('cross-column OR', 'fixture', 'Return only names of inventory items with stock equal to 0 or price greater than 100, alphabetically by name.', 'SELECT name FROM inventory WHERE stock=0 OR price>100 ORDER BY name'),
    ('two sort keys', 'fixture', 'Return name and price from inventory, ordered by price ascending then name descending.', 'SELECT name,price FROM inventory ORDER BY price ASC,name DESC'),
    ('limit', 'cyberchase.db', 'Return only the titles of the seven earliest aired episodes, ordered by air date ascending.', 'SELECT title FROM episodes ORDER BY air_date ASC LIMIT 7'),
    ('distinct', 'dese.db', 'Return distinct school types, alphabetically by type.', 'SELECT DISTINCT type FROM schools ORDER BY type'),
    ('aggregate', 'fixture', 'What is the total stock across all inventory items?', 'SELECT SUM(stock) FROM inventory'),
    ('grouping and HAVING', 'cyberchase.db', 'Count episodes by season, keeping seasons with at least 12 episodes. Return season and count, ordered by season ascending.', 'SELECT season,COUNT(*) FROM episodes GROUP BY season HAVING COUNT(*)>=12 ORDER BY season'),
    ('join', 'dese.db', 'For schools in Springfield, return school name and district name, ordered by school name alphabetically.', "SELECT schools.name,districts.name FROM schools JOIN districts ON schools.district_id=districts.id WHERE schools.city='Springfield' ORDER BY schools.name"),
    ('grouped analysis', 'moneyball.db', 'For performances from 1998 through 2000, return year and total home runs, grouped by year, highest total first.', 'SELECT year,SUM(HR) FROM performances WHERE year BETWEEN 1998 AND 2000 GROUP BY year ORDER BY SUM(HR) DESC,year'),
    ('empty result', 'fixture', 'Return only names of inventory items priced below zero.', 'SELECT name FROM inventory WHERE price<0'),
    ('mutation preview', 'fixture', 'Set price to 29.75 for the inventory item whose id is 2.', 'preview'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:7862')
    parser.add_argument('--output', type=Path, default=Path('workspace/polish-validation.json'))
    args = parser.parse_args()
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        fixture = Path(temporary) / f'polish_fixture_{time.time_ns()}.db'
        with closing(sqlite3.connect(fixture)) as conn:
            conn.execute('CREATE TABLE inventory (id INTEGER PRIMARY KEY,name TEXT,category TEXT,status TEXT,stock INTEGER,price REAL,note TEXT,added TEXT)')
            conn.executemany('INSERT INTO inventory VALUES (?,?,?,?,?,?,?,?)', [
                (1,'Atlas','Hardware','active',12,25.5,None,'2026-02-01'),
                (2,'Beacon','Hardware','active',4,80,'new','2026-02-28'),
                (3,'Cedar','Supplies','pending',20,10,None,'2026-01-01'),
                (4,'Delta','Hardware','retired',0,80,'old','2026-03-01'),
                (5,'Echo','Supplies','active',8,40,None,'2025-02-12'),
                (6,'Flux','Services','pending',1,120,'plan','2026-02-14'),
            ])
            conn.commit()
        database = http_json(args.url, '/api/databases', method='POST', body={'path':str(fixture)})['id']
        for name, source, prompt, oracle in CASES:
            db = database if source == 'fixture' else source
            response = run_sse(args.url, db, prompt, 'change' if oracle == 'preview' else 'read', 120)
            result = response.get('result') or {}
            expected = None
            if oracle is None:
                passed = 'unsupported window' in (response.get('error') or '') and not any(e['kind']=='compiled' for e in response['events'])
            elif oracle == 'preview':
                unchanged = http_json(args.url, f'/api/inspect/{db}', method='POST', body={'sql':'SELECT price FROM inventory WHERE id=2'})['rows']
                passed = (result.get('supported') is True and result.get('affected')==1 and result['after'][0]['price']==29.75 and unchanged==[[80.0]])
                expected = {'price':29.75,'affected':1,'stored_price':80.0}
            else:
                path = fixture if source=='fixture' else Path(__file__).resolve().parents[1]/'data'/source
                with closing(sqlite3.connect(path)) as conn:
                    expected = [list(r) for r in conn.execute(oracle)]
                    replay = [list(r) for r in conn.execute(result['sql'],result['params'])] if result.get('supported') else None
                passed = result.get('supported') is True and result.get('rows')==expected and replay==expected and bool(result.get('program',{}).get('typed_query'))
            records.append({'case':name,'database':db,'prompt':prompt,'oracle':oracle,'expected':expected,'passed':passed,'response':response})
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(records,indent=2),encoding='utf-8')
            print(f"{'PASS' if passed else 'FAIL'} {len(records):02d} {name}: {result.get('sql') or response.get('error')}",flush=True)
    print(f"{sum(r['passed'] for r in records)}/{len(records)} passed. Full evidence: {args.output}")
    raise SystemExit(0 if all(r['passed'] for r in records) else 1)


if __name__=='__main__':
    main()
