from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class Snapshot:
    text: str
    digest: str
    elements: dict


SNAPSHOT_JS = r"""({start, expanded}) => {
  const visible = e => { const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; };
  const tidy = s => (s||'').replace(/[\u200B-\u200D\uFEFF]/g,'').replace(/\s+/g,' ').trim();
  let next=start;
  const elements=[];
  // Hidden controls retain attributes across redraws; clear the previous observation first.
  document.querySelectorAll('[data-rfq-observe]').forEach(e=>e.removeAttribute('data-rfq-observe'));
  const selector='a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],[role=radio],[onclick],[tabindex]';
  for (const e of document.querySelectorAll(selector)) {
    if(!visible(e)||e.getAttribute('type')==='hidden') continue;
    const id=next++; e.setAttribute('data-rfq-observe',String(id));
    let label=e.getAttribute('aria-label')||'';
    if(e.labels&&e.labels.length) label=Array.from(e.labels).map(l=>tidy(l.innerText)).join(' ');
    label=label||tidy(e.innerText)||e.getAttribute('placeholder')||e.getAttribute('title')||e.getAttribute('name')||'';
    const type=e.getAttribute('type')||e.getAttribute('role')||e.tagName.toLowerCase();
    const value=type==='password'?(e.value?'[secret]':''):('value' in e?e.value:'');
    const options=e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,text:o.text})):undefined;
    const checked=typeof e.checked==='boolean'?e.checked:(['checkbox','radio'].includes(type)?e.getAttribute('aria-checked')==='true':undefined);
    elements.push({id,tag:e.tagName.toLowerCase(),type,label:label.slice(0,350),value,checked,
      modal:!!e.closest('dialog[open],[role=dialog],[aria-modal=true]'),
      href:e.getAttribute('href')||undefined,options,disabled:e.matches(':disabled'),readonly:!!e.readOnly,min:e.min,max:e.max});
  }
  const dialogs=Array.from(document.querySelectorAll('dialog,[role=dialog],[aria-modal=true]')).filter(visible).map(e=>e.innerText);
  const sections=Array.from(document.querySelectorAll('h1,h2,h3,summary')).filter(visible).map(e=>tidy(e.innerText));
  const clone=document.body?.cloneNode(true);
  let nav='';
  if(clone){
    const originalNodes=Array.from(document.body.querySelectorAll('*'));
    const clonedNodes=Array.from(clone.querySelectorAll('*'));
    originalNodes.forEach((e,i)=>{
      const s=getComputedStyle(e),copy=clonedNodes[i];
      if(s.display==='none'||s.visibility==='hidden'){copy.remove();return;}
      if(['block','flex','grid','list-item','table-row','table-cell'].includes(s.display)){
        copy.before(document.createTextNode('\n'));copy.after(document.createTextNode('\n'));
      }
    });
    clone.querySelectorAll('br,hr').forEach(e=>e.replaceWith(document.createTextNode('\n')));
    clone.querySelectorAll('script,style,svg,noscript').forEach(e=>e.remove());
    clone.querySelectorAll('nav,header,footer').forEach(e=>{nav+=tidy(e.textContent)+'\n';e.remove();});
    clone.querySelectorAll('table').forEach(t=>{
      const rows=Array.from(t.querySelectorAll('tr')).map(r=>Array.from(r.querySelectorAll('th,td')).map(c=>tidy(c.textContent)).join(' | '));
      t.replaceWith(document.createTextNode('\n'+rows.join('\n')+'\n'));
    });
  }
  let body=(clone?.textContent||'').replace(/[\u200B-\u200D\uFEFF]/g,'').split('\n').map(s=>s.trim()).filter(Boolean).join('\n');
  if(expanded){const target=Array.from(document.querySelectorAll('h1,h2,h3,section,article,table')).find(e=>tidy(e.innerText).includes(expanded));if(target)body=target.innerText;}
  return {title:document.title,body,nav,dialogs,sections,elements,next};
}"""


async def settle(page):
    try:
        await page.wait_for_load_state("networkidle", timeout=1200)
    except Exception:
        pass
    try:
        await page.evaluate("""() => new Promise(resolve => {
          let quiet; const done=()=>{observer.disconnect();clearTimeout(quiet);resolve()};
          const observer=new MutationObserver(()=>{clearTimeout(quiet);quiet=setTimeout(done,150)});
          observer.observe(document,{subtree:true,childList:true,attributes:true});
          quiet=setTimeout(done,150);setTimeout(done,900);
        })""")
    except Exception:
        pass


async def observe(session, max_chars: int = 26000, expanded: str = "") -> Snapshot:
    await settle(session.page)
    pieces = [f"URL {session.page.url}"]
    elements = {}
    next_id = 1
    for index, frame in enumerate(session.page.frames):
        try:
            data = await frame.evaluate(SNAPSHOT_JS, {"start": next_id, "expanded": expanded})
        except Exception:
            continue
        next_id = data["next"]
        frame_path = f"frame/{index}"
        pieces.append(f"{frame_path}: {data['title']} ({frame.url})")
        if data["dialogs"]:
            pieces.append("DIALOGS\n" + "\n".join(data["dialogs"]))
        nav_hash = hashlib.sha256(data["nav"].encode()).hexdigest()
        if nav_hash not in session.nav_hashes:
            pieces.append("Navigation: " + data["nav"])
            session.nav_hashes.add(nav_hash)
        pieces.append(data["body"])
        pieces.append("CONTROLS")
        for e in data["elements"]:
            elements[e["id"]] = {**e, "frame": frame, "frame_path": frame_path}
            details = f"[{e['id']}] {e['type']} {e['label']}"
            if e.get("value"):
                details += f" value={e['value']}"
            if e.get("type") in ("checkbox", "radio"):
                details += " CHECKED" if e.get("checked") else " UNCHECKED"
            if e.get("type") == "range":
                details += f" min={e.get('min')} max={e.get('max')}"
            if e.get("modal"):
                details += " MODAL"
            if e.get("options"):
                details += " options=" + str(e["options"])
            if e.get("href"):
                details += " href=" + e["href"]
            if e.get("disabled"):
                details += " DISABLED"
            if e.get("readonly"):
                details += " READONLY"
            pieces.append(details)
        pieces.append("Sections: " + " | ".join(data["sections"]))
    if session.notices:
        pieces.insert(0, "NOTICES\n" + "\n".join(session.notices))
        session.notices.clear()
    pieces.append("TABS " + " | ".join(f"{i}: {p.url}" for i, p in enumerate(session.context.pages)))
    text = "\n".join(pieces)
    if len(text) > max_chars:
        # Retain controls at the tail and provide explicit model-selected expansion.
        tail = "\n".join(p for p in pieces if p.startswith("[") or p.startswith("Sections:"))[-max_chars // 3:]
        text = text[:max_chars - len(tail) - 120] + "\n[TRUNCATED; expand a section heading or scroll]\n" + tail
    # Ignore transient control values only for navigation dedupe? No: all content matters.
    digest = hashlib.sha256(text.encode()).hexdigest()
    result = Snapshot(text, digest, elements)
    session.elements = elements
    session.last_snapshot = result
    return result
