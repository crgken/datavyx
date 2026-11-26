"""Validate and assemble a local draft only; never upload, authenticate or mutate a service."""
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

import jsonschema

ROOT=Path(__file__).resolve().parent
PACKAGE=ROOT/'datavyx'
PROJECT=ROOT.parent.parent

def sha(data):return hashlib.sha256(data).hexdigest()

def save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

class Page(HTMLParser):
    def __init__(self):super().__init__();self.hidden=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden and data.strip():self.parts.append(data.strip())

def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'Datavyx-Public-Plugin-Preparation/1.0'})
    with urllib.request.urlopen(request,timeout=25) as response:
        data=response.read(2_000_001)
        assert len(data)<=2_000_000,'Unexpectedly large evidence response'
        return response.status,response.url,response.headers.get('Content-Type',''),data

def main():
    verification=ROOT/'verification';verification.mkdir(exist_ok=True)
    schemas={}
    for name in ('plugin','mcp'):
        url=f'https://agent-plugins.org/schemas/1.0.0/{name}.schema.json'
        status,final,content,raw=fetch(url);assert status==200
        schemas[name]=json.loads(raw)
        save_json(verification/'schemas'/f'{name}.schema.json',schemas[name])
    plugin=json.loads((PACKAGE/'plugin.json').read_text(encoding='utf-8'))
    mcp=json.loads((PACKAGE/'mcp.json').read_text(encoding='utf-8'))
    for schema in schemas.values():jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schemas['plugin']).validate(plugin)
    mcp_full_errors=list(jsonschema.Draft202012Validator(schemas['mcp']).iter_errors(mcp))
    # The published base schema currently excludes server-level vendor extensions.
    # Keep the documented OpenAI auth extension, validate only the portable projection
    # here, and explicitly report that full target-host import has not been verified.
    portable_mcp={**mcp,'mcpServers':{name:{k:v for k,v in server.items() if k!='extensions'}
        for name,server in mcp['mcpServers'].items()}}
    jsonschema.Draft202012Validator(schemas['mcp']).validate(portable_mcp)
    interface=plugin['extensions']['com.openai']['interface']
    review=plugin['extensions']['com.openai']['review']
    publication=plugin['extensions']['com.openai']['publication']
    assert plugin['name']=='datavyx' and plugin['version']=='1.0.0'
    assert plugin['author']['name']==interface['developerName']=='Datavyx, LLC'
    assert len(interface['displayName'])<=30 and len(interface['shortDescription'])<=30
    assert len(interface['longDescription'])<=4000 and interface['category']=='Productivity'
    assert len(interface['defaultPrompt'])==3 and len(set(interface['defaultPrompt']))==3
    assert all(0<len(p)<=128 and '\n' not in p and '@' not in p for p in interface['defaultPrompt'])
    assert review['commerce'] is False and publication['countries']==[]
    cases=review['test_cases'];assert len(cases['positive'])==5 and len(cases['negative'])==3
    for case in cases['positive']:
        assert all(isinstance(case[k],str) and case[k].strip() for k in
            ('description','prompt','tools_triggered','expected_behavior'))
    assert 'apps' not in plugin and 'apps' not in plugin['extensions']['com.openai']
    assert not (PACKAGE/'.app.json').exists()
    connection=mcp['mcpServers']['datavyx'];auth=connection['extensions']['com.openai']['auth']
    assert connection['type']=='streamable-http' and connection['url']=='https://kb.datavyx.com/mcp'
    assert auth['client']=={'mode':'provided','clientId':'7CoGooGTzCivdedhDG2P2sjhGbcQJZo1',
        'tokenEndpointAuthMethod':'client_secret_post'}
    expected={'read','record','update','delete','restore','history','bulk'}
    assert set(auth['defaultScopes'])=={'knowledge:'+p for p in expected}
    assert auth['baseScopes']==['knowledge:read']
    def secret_keys(value):
        if isinstance(value,dict):
            assert not set(value)&{'clientSecret','client_secret','access_token','refresh_token','password','api_key'}
            for v in value.values():secret_keys(v)
        elif isinstance(value,list):
            for v in value:secret_keys(v)
    secret_keys(plugin);secret_keys(mcp)
    logo=PACKAGE/'assets/datavyx-logo.png';raw=logo.read_bytes()
    assert raw[:8]==b'\x89PNG\r\n\x1a\n'
    width,height=struct.unpack('>II',raw[16:24]);assert width==height and 256<=width<=4096 and len(raw)<=5*1024*1024
    original=PROJECT/'tmp/datavyx-landing-submit/datavyx-logo.png'
    assert sha(raw)==sha(original.read_bytes())
    sys.path.insert(0,str(PROJECT/'knowledge-management-demo'))
    from mcp_backend.server import TOOL_INPUTS,TOOL_OUTPUTS,WRITES,DESTRUCTIVE,DESCRIPTIONS
    assert len(TOOL_INPUTS)==11 and 'purge_information' not in TOOL_INPUTS
    catalog={name:{'description':DESCRIPTIONS[name],'inputSchema':model.model_json_schema(),
        'outputSchema':TOOL_OUTPUTS[name].model_json_schema(),
        'annotations':{'readOnlyHint':name not in WRITES,'destructiveHint':name in DESTRUCTIVE,'openWorldHint':False}}
        for name,model in TOOL_INPUTS.items()}
    for case in cases['positive']:
        assert set(case['tools_triggered'].split(', '))<=set(catalog)
    save_json(verification/'source-tool-catalog.json',catalog)
    pages={}
    website_commit='a37ce6ab12379685b2388597f611385c8ea4227c'
    public_files={'website':('websiteURL','index.html'), 'support':('supportURL','support.html'),
        'privacy':('privacyPolicyURL','privacy.html'), 'terms':('termsOfServiceURL','terms.html')}
    for name,(field,filename) in public_files.items():
        url=interface[field]
        assert url=='https://www.datavyx.com/'+('' if name=='website' else filename)
        try:
            status,final,content,raw=fetch(url)
            parser=Page();parser.feed(raw.decode('utf-8'))
            text='\n'.join(parser.parts)
            assert 'Datavyx' in text
            expected_page=subprocess.check_output(['git','show',website_commit+':'+filename],
                cwd=PROJECT/'tmp/datavyx-landing-submit')
            assert raw==expected_page,'Public page differs from the verified website commit'
            if name!='website':assert 'support@datavyx.com' in text
            folder=verification/'public-pages';folder.mkdir(exist_ok=True)
            (folder/f'{name}.txt').write_text(text,encoding='utf-8')
            pages[name]={'status':status,'final_url':final,'content_type':content,'sha256':sha(raw),
                'content_read':True,'matches_website_commit':website_commit}
        except Exception as error:
            pages[name]={'url':url,'verified':False,'error_type':type(error).__name__}
    assert all(row.get('status')==200 and row.get('matches_website_commit')==website_commit
        for row in pages.values()),'All four published pages must pass before packaging'
    inventory={str(path.relative_to(PACKAGE)).replace('\\','/'):{'sha256':sha(path.read_bytes()),'bytes':path.stat().st_size}
        for path in PACKAGE.rglob('*') if path.is_file()}
    assert set(inventory)=={'plugin.json','mcp.json','assets/datavyx-logo.png'}
    assert not any(path.is_symlink() for path in PACKAGE.rglob('*'))
    archive=ROOT/'datavyx-1.0.0-public-DRAFT.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as stream:
        for name in sorted(inventory):stream.write(PACKAGE/name,'datavyx/'+name)
    with zipfile.ZipFile(archive) as stream:
        assert set(stream.namelist())=={'datavyx/'+name for name in inventory}
        assert stream.testzip() is None
        for name,meta in inventory.items():assert sha(stream.read('datavyx/'+name))==meta['sha256']
        assert json.loads(stream.read('datavyx/mcp.json'))['mcpServers']['datavyx']==connection
    gaps=['Verified publisher identity in Portal','OAuth callback/refresh verification (client_secret_post confirmed by application screenshot)',
        'Dedicated reviewer account','Real demo recording and accessible URL','Five positive and three negative cases not run',
        'Portal connection, scans and attestations not performed']
    report={'status':'local_draft_checks_complete_portal_unverified','publisher':'Datavyx, LLC',
        'countries':'all platform-permitted; countries=[]','commerce':False,'registration':'provided',
        'public_tools':sorted(catalog),'icon':{'width':width,'height':height,'bytes':logo.stat().st_size,'unchanged':True},
        'subtitle_characters':len(interface['shortDescription']),'review_cases':{'positive':5,'negative':3,'actual_run':'Not run'},
        'schemas':{'plugin_manifest':'passed','portable_mcp_projection':'passed',
            'full_mcp_generic_schema':'failed: published schema excludes documented OpenAI server extensions' if mcp_full_errors else 'passed',
            'openai_extensions':'documented field rules checked locally; target Portal import and scan not performed'},
        'public_pages':pages,'inventory':inventory,'archive':str(archive),'archive_sha256':sha(archive.read_bytes()),
        'remaining_gaps':gaps,'uploaded':False,'submitted':False,'published':False}
    save_json(verification/'preparation.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('public_pages','inventory','remaining_gaps')},ensure_ascii=False))

if __name__=='__main__':main()
