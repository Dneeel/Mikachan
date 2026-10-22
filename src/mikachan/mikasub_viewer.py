"""Self-contained offline HTML viewer; QR code is generated only when clicked."""

from __future__ import annotations

import html
import json
from pathlib import Path


def offline_viewer(items: list[dict[str, str]], *, title: str) -> bytes:
    safe_title = html.escape(title[:100], quote=True)
    records = [{"uri": str(item["uri"]), "name": str(item["name"]),
                "protocol": str(item["protocol"])} for item in items]
    data = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    # Inline script content must not be terminated by a malicious source label.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    qr_library = (Path(__file__).parent / "assets" / "qrcode.js").read_text(encoding="utf-8")
    page = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>__TITLE__</title><style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif;background:#0d0d10;color:#f2eff1}
*{box-sizing:border-box}body{max-width:920px;margin:auto;padding:22px 16px 80px;background:radial-gradient(circle at top right,#3e121a55,transparent 40%)}
header{border-bottom:1px solid #64323a;padding:12px 0 22px}h1{font-size:clamp(25px,5vw,38px);margin:10px 0}h1 b{color:#e65c72}p{color:#bdb5ba;line-height:1.5}
.tag{color:#f4a0ac;letter-spacing:.15em;font-size:12px;font-weight:800}main{padding-top:18px}.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0}
input,select,button,textarea{font:inherit}input,select{background:#17171b;border:1px solid #62333c;border-radius:12px;color:#fff;padding:12px}
input{flex:1;min-width:200px}button{background:#391820;color:#fff;border:1px solid #8a3747;border-radius:11px;padding:10px 14px;cursor:pointer}
button:hover{background:#662633}.count{color:#f4a0ac;font-weight:700;margin:16px 0}.item{background:#17171b;border:1px solid #39272b;border-radius:15px;padding:15px;margin:10px 0}
.item strong{display:block;overflow-wrap:anywhere}.meta{font-size:12px;color:#b28e96;text-transform:uppercase;margin:6px 0}.uri{display:block;color:#cbd1d4;word-break:break-all;max-height:68px;overflow:auto;white-space:pre-wrap;font-size:11px}
.buttons{display:flex;gap:8px;margin-top:12px}.buttons button{font-size:13px}#fallback,#qrbox{display:none;position:fixed;inset:0;background:#08080beF;z-index:10;padding:20px;overflow:auto}
.dialog{max-width:600px;margin:6vh auto;background:#1b171a;border:1px solid #a5485b;border-radius:16px;padding:18px}textarea{width:100%;height:40vh;background:#0b0b0d;color:#fff;border:1px solid #9a4b5d;padding:12px}
#qrsvg{background:white;padding:16px;display:inline-block;max-width:100%;overflow:auto}small{color:#a7a0a3}
</style></head><body><header><div class="tag">MIKACHAN / OFFLINE COLLECTION</div>
<h1>__TITLE__ <b>◆</b></h1><p>Share configs are untested. Endpoint location is unknown and working access is not guaranteed. This file needs no Internet connection.</p></header>
<main><div class="toolbar"><input id="search" type="search" placeholder="Search names or configs" autocomplete="off" aria-label="Search">
<select id="protocol" aria-label="Protocol"><option value="">All protocols</option></select>
<select aria-label="Endpoint country" disabled><option>🏴‍☠️ Unknown endpoint</option></select>
<button id="all">Copy All</button></div><div id="count" class="count"></div><section id="list"></section>
<button id="more" style="display:none">Load more</button></main>
<div id="fallback"><div class="dialog"><h2>Manual copy</h2><p>Clipboard access is unavailable for this local file. Select the text below and copy it yourself.</p>
<textarea id="copytext" readonly></textarea><p><button data-close="fallback">Close</button></p></div></div>
<div id="qrbox"><div class="dialog"><h2>Config QR</h2><div id="qrsvg"></div><p id="qrerror"></p>
<p><button data-close="qrbox">Close</button></p></div></div>
<script>__QR_LIBRARY__</script><script>
const items=__DATA__;qrcode.stringToBytes=qrcode.stringToBytesFuncs['UTF-8'];
const $=id=>document.getElementById(id);let filtered=items,shown=0;
const protocols=[...new Set(items.map(x=>x.protocol))].sort();
for(const protocol of protocols){const option=document.createElement('option');option.value=protocol;option.textContent=protocol;$('protocol').append(option)}
function matches(){const term=$('search').value.toLowerCase(),protocol=$('protocol').value;
 filtered=items.filter(x=>(!protocol||x.protocol===protocol)&&(!term||(x.uri+' '+x.name).toLowerCase().includes(term)));
 shown=0;$('list').replaceChildren();$('count').textContent=filtered.length+' of '+items.length+' configurations';appendPage()}
function appendPage(){const fragment=document.createDocumentFragment();for(const x of filtered.slice(shown,shown+100)){
 const article=document.createElement('article');article.className='item';const heading=document.createElement('strong');heading.textContent=x.name;
 const meta=document.createElement('div');meta.className='meta';meta.textContent=x.protocol+' · endpoint unknown';
 const uri=document.createElement('code');uri.className='uri';uri.textContent=x.uri;
 const buttons=document.createElement('div');buttons.className='buttons';
 for(const [label,action] of [['Copy',()=>copy(x.uri)],['QR',()=>showQR(x.uri)]]){
  const button=document.createElement('button');button.textContent=label;button.addEventListener('click',action);buttons.append(button)}
 article.append(heading,meta,uri,buttons);fragment.append(article)}$('list').append(fragment);
 shown=Math.min(shown+100,filtered.length);$('more').style.display=shown<filtered.length?'inline-block':'none'}
async function copy(value){try{if(!navigator.clipboard?.writeText)throw Error('clipboard unavailable');await navigator.clipboard.writeText(value);
 $('count').textContent='Copied · '+filtered.length+' of '+items.length+' shown'}catch{
 $('copytext').value=value;$('fallback').style.display='block';$('copytext').focus();$('copytext').select()}}
function showQR(value){$('qrsvg').replaceChildren();$('qrerror').textContent='';$('qrbox').style.display='block';try{
 const qr=qrcode(0,'L');qr.addData(value,'Byte');qr.make();$('qrsvg').innerHTML=qr.createSvgTag(4,0)}catch{
 $('qrerror').textContent='This URI exceeds QR capacity. Use Copy or the TXT export.'}}
$('all').addEventListener('click',()=>copy(items.map(x=>x.uri).join('\n')));
$('search').addEventListener('input',matches);$('protocol').addEventListener('change',matches);
$('more').addEventListener('click',appendPage);
for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>$(button.dataset.close).style.display='none');
matches();
</script></body></html>'''
    return (page.replace("__TITLE__", safe_title).replace("__QR_LIBRARY__", qr_library)
            .replace("__DATA__", data)).encode("utf-8")
