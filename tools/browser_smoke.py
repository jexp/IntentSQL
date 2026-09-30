"""Replay real polish_validate.py events to test geometry without extra model calls.

Requires the local app and workspace/polish-validation.json. Replay is test-only;
the shipped UI always uses the live stream. Settings checks change and restore
the active profile: run against an isolated validation workspace.
"""
import argparse
import json
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

REGIONS=['.compact-intro','.journey','.output-panel','.trace-panel','.composer-dock','.sql-box','#output']


def boxes(page):
    return {s:page.locator(s).bounding_box() for s in REGIONS}


def stable(page, baseline, state):
    current=boxes(page)
    for selector, rect in baseline.items():
        for dim in ('x','y','width','height'):
            assert abs(current[selector][dim]-rect[dim])<1,(state,selector,dim,rect,current[selector])
    assert page.evaluate('() => window.scrollY')==0,state
    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth'),state


def replay(page, events, baseline=None):
    page.evaluate('''() => {
        clearQuery(); setBusy(true); startAt=performance.now();
        document.querySelector('#trace').innerHTML='';
    }''')
    for event in events:
        page.evaluate('(event) => handleEvent(event)',event)
        if baseline: stable(page,baseline,event['kind'])
    page.evaluate('() => setBusy(false)')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:7862')
    parser.add_argument('--report',type=Path,default=Path('workspace/polish-validation.json'))
    parser.add_argument('--screenshots',type=Path,default=Path('workspace/browser'))
    parser.add_argument('--browser-executable',default=None,
                        help='Optional Chromium/Chrome executable when Playwright browsers are not installed')
    args=parser.parse_args()
    records=json.loads(args.report.read_text(encoding='utf-8'))
    assert len(records)==20 and all(r['passed'] for r in records),'Run the complete semantic pack first'
    args.screenshots.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as p:
        executable = args.browser_executable or shutil.which('chromium') or shutil.which('google-chrome')
        browser=p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        page=browser.new_page(viewport={'width':1366,'height':768})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        for width,height in [(1440,900),(1366,768),(1024,768)]:
            page.set_viewport_size({'width':width,'height':height})
            page.goto(args.url,wait_until='networkidle')
            baseline=boxes(page)
            page.locator('#prompt').fill('A long request with many conditions. '*30)
            stable(page,baseline,'long composer')
            for index in [0,1,2,17,18,19]:
                replay(page,records[index]['response']['events'],baseline)
                page.locator('#expand-trace').click()
                assert page.locator('.evidence-json[open]').count()==0
                page.locator('#run-details > summary').click()
                page.locator('#typed-program > summary').click()
                if page.locator('#execution-details').is_visible():
                    page.locator('#execution-details > summary').click()
                stable(page,baseline,'expanded disclosures')
                page.locator('#collapse-trace').click()
                assert page.locator('.decision-details[open]').count()==0
            large=records[0]['response']['result'].copy()
            large['rows']=[[f'Row {i} with a wide value '*10] for i in range(450)]
            large['total_rows']=450
            page.evaluate('(r) => { clearQuery(); renderResult(r); }',large)
            stable(page,baseline,'large table')
            page.locator('#page-next').click()
            expect(page.locator('.table-meta').first).to_contain_text('101')
            page.locator('#database').select_option('dese.db')
            expect(page.locator('#schema')).to_contain_text('schools')
            expect(page.locator('#program-status')).to_have_text('Waiting')
            assert page.locator('.trace-call').count()==0
            stable(page,baseline,'database switch')
            print(f'PASS stable shell {width}x{height}',flush=True)
        page.set_viewport_size({'width':1366,'height':768})
        page.goto(args.url,wait_until='networkidle')
        replay(page,records[0]['response']['events'])
        page.screenshot(animations="disabled",path=str(args.screenshots/'result.png'))
        assert '?' not in page.locator('#sql').inner_text()
        assert '?' in page.locator('#parameterized-sql').text_content()
        expect(page.locator('#sql .sql-value')).to_have_text('4')
        with page.expect_download() as download: page.locator('#export-csv').click()
        assert download.value.suggested_filename=='intentsql-results.csv'
        with page.expect_download() as download: page.locator('#export-trace').click()
        assert download.value.suggested_filename=='intentsql-trace.json'
        page.locator('#open-settings').click()
        expect(page.locator('#api-key')).to_have_value('')
        page.locator('#provider').select_option('laya')
        page.locator('#reset-provider').click()
        page.locator('#endpoint-port').fill('8765')
        expect(page.locator('#endpoint')).to_have_value('http://127.0.0.1:8765/v1/systemone')
        page.locator('#endpoint-host').fill('localhost')
        expect(page.locator('#endpoint')).to_have_value('http://localhost:8765/v1/systemone')
        page.locator('#save-connection').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        expect(page.locator('#active-engine')).to_contain_text('Laya local')
        page.locator('#open-settings').click()
        page.locator('#provider').select_option('jev')
        page.locator('#save-connection').click()
        expect(page.locator('#settings-dialog')).not_to_be_visible()
        expect(page.locator('#active-engine')).to_contain_text('Jev')
        page.locator('#open-settings').click()
        page.locator('#model').fill('unsaved-model')
        page.locator('#close-settings').click()
        page.locator('#open-settings').click()
        expect(page.locator('#model')).not_to_have_value('unsaved-model')
        page.locator('#close-settings').click()
        page.locator('#inspect-db').click()
        expect(page.locator('#inspector-overview')).to_contain_text('episodes')
        page.locator('#inspector-sql').fill('SELECT COUNT(*) AS count FROM episodes')
        page.locator('#run-inspector-query').click()
        expect(page.locator('#inspector-output')).to_contain_text('140')
        page.locator('#inspector-sql').fill('DELETE FROM episodes')
        page.locator('#run-inspector-query').click()
        expect(page.locator('#inspector-error')).to_be_visible()
        page.locator('#close-inspector').click()
        for width,height in [(800,900),(390,844)]:
            page.set_viewport_size({'width':width,'height':height})
            replay(page,records[0]['response']['events'])
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(animations="disabled",path=str(args.screenshots/f'mobile-{width}.png'))
        page.set_viewport_size({'width':1366,'height':768})
        page.locator('#theme').click()
        page.screenshot(animations="disabled",path=str(args.screenshots/'light.png'))
        page.locator('#theme').click()
        page.emulate_media(reduced_motion='reduce')
        assert page.locator('#output').evaluate('(e) => getComputedStyle(e).animationName')=='none'
        page.emulate_media(reduced_motion='no-preference')
        events=records[17]['response']['events']
        last_call=max(i for i,e in enumerate(events) if e['kind']=='call_start')
        replay(page,events[:last_call+1])
        page.locator('#expand-trace').click()
        page.screenshot(animations="disabled",path=str(args.screenshots/'live-program.png'))
        assert not errors,errors
        browser.close()
        print('PASS disclosures, exports, settings, explorer, mobile, theme, reduced motion; no browser errors')


if __name__=='__main__': main()
