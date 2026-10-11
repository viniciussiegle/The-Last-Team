from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urljoin
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
  // A label's text as shown. With a dropdown inside, that would include every option, so the name would change with
  // the options and a batch could no longer find the control; and in a collapsed section nothing is shown. In both
  // cases the text is taken as written, without the fields.
  const caption = l => { const shown=l.querySelector('select')?'':tidy(l.innerText); if(shown) return shown;
    const c=l.cloneNode(true); c.querySelectorAll('select,input,textarea,br').forEach(f=>f.replaceWith(' ')); return tidy(c.textContent); };
  let next=start;
  const elements=[];
  // Hidden controls retain attributes across redraws; clear the previous observation first.
  document.querySelectorAll('[data-rfq-observe]').forEach(e=>e.removeAttribute('data-rfq-observe'));
  const selector='a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],[role=radio],[onclick],[tabindex]';
  for (const e of document.querySelectorAll(selector)) {
    if(!visible(e)||e.getAttribute('type')==='hidden') continue;
    const id=next++; e.setAttribute('data-rfq-observe',String(id));
    let label=e.getAttribute('aria-label')||'';
    if(e.labels&&e.labels.length) label=tidy(Array.from(e.labels).map(caption).join(' '))||label;
    // A field's own text is its content (a dropdown's option list is reported separately), never its name. Other
    // controls are named by their text; in a collapsed section none is shown, so there the text as written is used.
    label=label||(/^(SELECT|TEXTAREA)$/.test(e.tagName)?'':tidy(e.innerText)||(e.closest('details:not([open])')?tidy(e.textContent):''))||e.getAttribute('placeholder')||e.getAttribute('title')||e.getAttribute('name')||'';
    const type=e.getAttribute('type')||e.getAttribute('role')||e.tagName.toLowerCase();
    const value=type==='password'?(e.value?'[secret]':''):('value' in e?e.value:'');
    const options=e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,text:o.text})):undefined;
    const checked=typeof e.checked==='boolean'?e.checked:(['checkbox','radio'].includes(type)?e.getAttribute('aria-checked')==='true':undefined);
    const rect=e.getBoundingClientRect(), x=rect.x+rect.width/2, y=rect.y+rect.height/2;
    let obstruction='';
    if(x>=0&&y>=0&&x<innerWidth&&y<innerHeight){
      const top=document.elementFromPoint(x,y);
      if(top&&!e.contains(top)&&!top.contains(e)){
        // Only a real cover counts: a dialog, or a layer laid over at least half of the window, which may be an
        // ancestor of the element at the control's centre. Ordinary page content is in front of a control only
        // while the control is scrolled away, and the browser scrolls it back to use it.
        for(let c=top;c&&!c.contains(e);c=c.parentElement){
          const r=c.getBoundingClientRect(), area=(Math.min(r.right,innerWidth)-Math.max(r.left,0))*(Math.min(r.bottom,innerHeight)-Math.max(r.top,0));
          if(c.matches('dialog,[role=dialog],[aria-modal=true]')||(area*2>=innerWidth*innerHeight&&/fixed|absolute/.test(getComputedStyle(c).position))){
            obstruction=tidy(c.getAttribute('aria-label')||c.innerText||c.tagName).slice(0,180); break;
          }
        }
      }
    }
    elements.push({id,tag:e.tagName.toLowerCase(),type,label:label.slice(0,350),value,checked,
      obstruction,
      modal:!!e.closest('dialog[open],[role=dialog],[aria-modal=true]'),
      href:e.getAttribute('href')||undefined,options,disabled:e.matches(':disabled'),readonly:!!e.readOnly,min:e.min,max:e.max});
  }
  // Named once as a heads-up; the dialog's own text and controls appear in the page text and control list.
  const dialogs=Array.from(document.querySelectorAll('dialog,[role=dialog],[aria-modal=true]')).filter(visible).map(e=>tidy((e.getAttribute('aria-label')||'')+' '+e.innerText).slice(0,160));
  const sections=Array.from(document.querySelectorAll('h1,h2,h3,summary')).filter(visible).map(e=>tidy(e.innerText));
  const clone=document.body?.cloneNode(true);
  let nav='';
  if(clone){
    const originalNodes=Array.from(document.body.querySelectorAll('*'));
    const clonedNodes=Array.from(clone.querySelectorAll('*'));
    originalNodes.forEach((e,i)=>{
      const s=getComputedStyle(e),copy=clonedNodes[i];
      if(s.display==='none'||s.visibility==='hidden'){copy.remove();return;}
      // Page text shows a dropdown's current choice; its options are listed once under CONTROLS.
      if(e.tagName==='SELECT'){copy.replaceWith(document.createTextNode('\n'+tidy(e.selectedOptions[0]?.text||'')+'\n'));return;}
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


class PageUnresponsive(RuntimeError):
    """The selected tab did not answer within the observation time limit."""


# Page scripts have no timeout of their own; a tab stuck in a navigation would block its worker.
SETTLE_LIMIT_S = 2.5
FRAME_LIMIT_S = 6.0


async def settle(page):
    try:
        await page.wait_for_load_state("networkidle", timeout=1200)
    except Exception:
        pass
    try:
        await asyncio.wait_for(page.evaluate("""() => new Promise(resolve => {
          let quiet; const done=()=>{observer.disconnect();clearTimeout(quiet);resolve()};
          const observer=new MutationObserver(()=>{clearTimeout(quiet);quiet=setTimeout(done,150)});
          observer.observe(document,{subtree:true,childList:true,attributes:true});
          quiet=setTimeout(done,150);setTimeout(done,900);
        })"""), SETTLE_LIMIT_S)
    except Exception:
        pass


async def frame_data(frame, next_id, expanded):
    ancestor = frame
    while ancestor.parent_frame is not None:
        container = await ancestor.frame_element()
        if not await container.is_visible():
            return None
        ancestor = ancestor.parent_frame
    return await frame.evaluate(SNAPSHOT_JS, {"start": next_id, "expanded": expanded})


def option_list(options, limit=60):
    """Each option once: its text, with the form value only where that differs."""
    shown = [o["text"] if o["value"] in ("", o["text"]) else f"{o['text']} ({o['value']})" for o in options[:limit]]
    if len(options) > limit:
        shown.append(f"+{len(options) - limit} more")
    return " | ".join(shown)


async def observe(session, max_chars: int = 26000, expanded: str = "") -> Snapshot:
    await settle(session.page)
    pieces = [f"URL {session.page.url}"]
    session.seen_addresses.add(session.page.url.split("#")[0])
    elements = {}
    next_id = 1
    for index, frame in enumerate(session.page.frames):
        try:
            data = await asyncio.wait_for(frame_data(frame, next_id, expanded), FRAME_LIMIT_S)
        except asyncio.TimeoutError:
            if frame is session.page.main_frame:
                raise PageUnresponsive("The page did not respond while it was being read") from None
            continue
        except Exception:
            continue
        if data is None:
            continue
        next_id = data["next"]
        frame_path = f"frame/{index}"
        pieces.append(f"{frame_path}: {data['title']} ({frame.url})")
        if data["dialogs"]:
            pieces.extend("DIALOG OPEN: " + text for text in data["dialogs"])
        nav_hash = hashlib.sha256(data["nav"].encode()).hexdigest()
        if nav_hash not in session.nav_hashes:
            pieces.append("Navigation: " + data["nav"])
            session.nav_hashes.add(nav_hash)
        pieces.append(data["body"])
        pieces.append("CONTROLS")
        for e in data["elements"]:
            elements[e["id"]] = {**e, "frame": frame, "frame_path": frame_path}
            if e.get("href"):
                session.seen_addresses.add(urljoin(frame.url, e["href"]).split("#")[0])
            details = f"[{e['id']}] {e['type']} {e['label']}"
            if e.get("value"):
                details += f" value={e['value']}"
            if e.get("type") in ("checkbox", "radio"):
                details += " CHECKED" if e.get("checked") else " UNCHECKED"
            if e.get("type") == "range":
                details += f" min={e.get('min')} max={e.get('max')}"
            if e.get("modal"):
                details += " MODAL"
            if e.get("obstruction"):
                details += " OBSCURED by " + e["obstruction"]
            if e.get("options"):
                details += " options: " + option_list(e["options"])
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
