"""Real HTTPdis/Chromium source configuration, offline until explicit restart."""
from pathlib import Path
import socket
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright
from atraxis.config import load_config
from atraxis.server import build_context, _Handler
from test_configuration_admin import ConfigurationFixture


def main():
    fixture=ConfigurationFixture();fixture.setUp()
    context=None;thread=None
    try:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin='http://127.0.0.1:'+str(port)
        fixture.document['general'].update(listen_port=port,configuration_web_origin=origin);fixture.save()
        context=build_context(load_config(fixture.path));started=threading.Event()
        context.named_commands['GET /healthz'].at_start=lambda options:started.set()
        thread=threading.Thread(target=lambda:context.run(http_req_handler=_Handler),daemon=True);thread.start()
        assert started.wait(5)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch()
            authenticated=browser.new_context(http_credentials=dict(username='alice',password='synthetic'))
            page=authenticated.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(origin+'/ui/configuration')
            page.get_by_role('link',name='Add source',exact=True).click()
            page.get_by_label('Source ID',exact=True).fill('browser-monitor')
            page.get_by_label('Approved connection',exact=True).select_option('monitor')
            page.get_by_label('Request timeout (seconds)',exact=True).fill('2')
            page.get_by_label('Collection interval (seconds)',exact=True).fill('15')
            page.get_by_label('Maximum observation age (seconds)',exact=True).fill('45')
            original=fixture.path.read_bytes()
            page.get_by_role('button',name='Review source change',exact=True).click()
            assert page.get_by_role('heading',name='Proposed source',exact=True).is_visible()
            assert fixture.path.read_bytes()==original
            assert 'private-source-secret' not in page.content() and 'source.json' not in page.content()
            page.get_by_label('I confirm this source configuration change.',exact=True).check()
            page.get_by_role('button',name='Save configuration',exact=True).click()
            page.wait_for_url(origin+'/ui/configuration')
            assert 'Saved changes are pending' in page.locator('main').inner_text()
            assert page.get_by_role('heading',name='browser-monitor',exact=True).is_visible()
            output=ROOT/'web-browser-artifacts';output.mkdir(exist_ok=True)
            for name,width,height in [('desktop',1280,900),('mobile',390,844)]:
                page.set_viewport_size(dict(width=width,height=height))
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output/('configuration-'+name+'.png')),full_page=True)
            page.get_by_role('link',name='Edit source',exact=True).click()
            page.get_by_label('Collection interval (seconds)',exact=True).fill('20')
            page.get_by_role('button',name='Review source change',exact=True).click()
            page.get_by_label('I confirm this source configuration change.',exact=True).check()
            page.get_by_role('button',name='Save configuration',exact=True).click()
            page.wait_for_url(origin+'/ui/configuration')
            page.get_by_role('link',name='Remove source',exact=True).click()
            page.get_by_label('I confirm this source configuration change.',exact=True).check()
            page.get_by_role('button',name='Save configuration',exact=True).click()
            page.wait_for_url(origin+'/ui/configuration')
            assert 'No saved sources' in page.locator('main').inner_text()
            assert 'Saved sources match the running configuration' in page.locator('main').inner_text()
            assert len(fixture.admin.inspect()['audit'])==3 and not errors
            authenticated.close();browser.close()
        print('Browser configuration: add/update/remove, review, private fields, restart state, desktop/mobile passed')
    finally:
        if context is not None:context.stop()
        if thread is not None:thread.join(7)
        fixture.doCleanups()


if __name__=='__main__':main()
