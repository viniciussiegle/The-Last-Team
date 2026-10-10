from __future__ import annotations

import base64
import re
from urllib.parse import urljoin

from .guards import login
from .session import origin


def element(session, value):
    item = session.elements.get(int(value))
    if not item:
        raise ValueError("Unknown element id; use only the latest snapshot ids")
    return item, item["frame"].locator(f'[data-rfq-observe="{int(value)}"]')


async def set_value(item, locator, value):
    """Apply a model-selected form value according to the observed control type."""
    text = str(value)
    if item.get("type") in ("checkbox", "radio"):
        await locator.set_checked(text.casefold() in {"true", "1", "yes", "on", "checked"})
    elif item.get("type") == "range":
        await locator.evaluate("(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}", text)
    elif item.get("tag") == "select":
        try:
            await locator.select_option(label=text)
        except Exception:
            await locator.select_option(value=text)
    else:
        await locator.fill(text)


CLICK_LIMIT_MS = 4000
BATCH_LIMIT = 14
# Their output is what the model asked to read, so nothing may run after them in the same batch.
READING_KINDS = {"look", "extract_table", "expand", "open_document"}


class Blocked(ValueError):
    """Another visible element covers the control the model chose."""


def blocker(message: str) -> str | None:
    """Name the covering element from the browser's own explanation of a refused click."""
    found = re.search(r"<([a-z0-9-]+)([^>]*)>[^\n]*?intercepts pointer events", message)
    if not found:
        return None
    label = re.search(r'aria-label="([^"]*)"', found.group(2))
    return label.group(1) if label and label.group(1) else found.group(1)


# Same notion of "what covers this control" as the snapshot's OBSCURED marker: the enclosing notice, not
# whichever of its children happens to lie under the click point.
COVERING_JS = r"""e => {
  const r=e.getBoundingClientRect(), top=document.elementFromPoint(r.x+r.width/2, r.y+r.height/2);
  if(!top||e.contains(top)||top.contains(e)) return '';
  const box=top.closest('dialog,[role=dialog],[aria-modal=true],section,aside')||top;
  return (box.getAttribute('aria-label')||box.innerText||box.tagName).replace(/\s+/g,' ').trim().slice(0,120);
}"""


async def click_with_confirmation(session, locator, confirm_final=False):
    session.confirm_final = bool(confirm_final)
    try:
        await locator.click(timeout=CLICK_LIMIT_MS)
    except Exception as error:
        if "intercepts pointer events" not in str(error):
            raise
        try:
            covering = await locator.evaluate(COVERING_JS, timeout=1500)
        except Exception:
            covering = ""
        covering = covering or blocker(str(error)) or "another element"
        raise Blocked(f"Click refused: '{covering}' covers this control. Use that element's own controls first "
                      "(close it, or complete what it asks), then repeat this action.") from None
    finally:
        session.confirm_final = False


def signature(item: dict) -> tuple:
    # The link target separates look-alike links whose order can change between renders.
    return item.get("tag"), item.get("type"), item.get("label"), item.get("frame_path"), item.get("href")


def identity(elements: dict, element_id) -> tuple | None:
    """What the model meant by an id: the control's description and its rank among identical ones."""
    item = elements.get(int(element_id))
    if item is None:
        return None
    wanted = signature(item)
    return wanted, sum(1 for key, other in elements.items() if key < int(element_id) and signature(other) == wanted)


def relocate(elements: dict, wanted: tuple | None) -> int | None:
    if wanted is None:
        return None
    described, rank = wanted
    same = [key for key in sorted(elements) if signature(elements[key]) == described]
    return same[rank] if rank < len(same) else None


def rebind(action: dict, seen: dict, current: dict) -> dict:
    """Point a later action of a batch at the same controls after the page was observed again."""
    def moved(element_id):
        target = relocate(current, identity(seen, element_id))
        if target is None:
            label = (seen.get(int(element_id)) or {}).get("label", "")
            raise LookupError(f"control [{element_id}] '{label[:60]}' is no longer present as observed")
        return target
    rebound = dict(action)
    for key in ("id", "submit_id"):
        if rebound.get(key) is not None:
            rebound[key] = moved(rebound[key])
    if isinstance(rebound.get("fields"), list):
        rebound["fields"] = [{**field, "id": moved(field["id"])} if isinstance(field, dict) and "id" in field else field
                             for field in rebound["fields"]]
    return rebound


async def act_many(session, actions: list[dict]) -> str | dict:
    """Run model-chosen actions in order from one observation, stopping at the first that cannot proceed."""
    from .snapshot import observe
    seen = dict(session.elements)
    notes, image = [], None
    for position, action in enumerate(actions[:BATCH_LIMIT], start=1):
        kind = action.get("kind", "?")
        try:
            if position > 1:
                await observe(session)
                action = rebind(action, seen, session.elements)
            output = await act(session, action)
        except LookupError as error:
            notes.append(f"{position}. {kind} not run: {error}. Remaining actions skipped.")
            break
        except Exception as error:
            notes.append(f"{position}. {kind} failed: {str(error).splitlines()[0][:400]}. Remaining actions skipped.")
            break
        if isinstance(output, dict) and output.get("image"):
            image = output["image"]
            notes.append(f"{position}. {kind}: screenshot attached")
        else:
            notes.append(f"{position}. {kind}: {output}")
        if kind in READING_KINDS:
            if position < len(actions):
                notes.append("Reading action ends the batch; later actions were not run.")
            break
    else:
        if len(actions) > BATCH_LIMIT:
            notes.append(f"Only the first {BATCH_LIMIT} actions run in one batch.")
    text = "\n".join(notes)
    return {"image": image, "text": text} if image else text


async def act(session, action: dict) -> str | dict:
    kind = action["kind"]
    page = session.page
    if kind == "login":
        return await login(session)
    if kind == "goto":
        url = urljoin(page.url, action["url"])
        if origin(url) not in session.allowed_origins:
            raise ValueError("Navigation must stay on this run's websites")
        await page.goto(url, wait_until="domcontentloaded")
    elif kind == "click":
        item, locator = element(session, action["id"])
        if item.get("href"):
            href = urljoin(page.url, item["href"])
            if href.startswith(("http:", "https:")) and origin(href) not in session.allowed_origins:
                raise ValueError("Link leaves the allowed websites")
        await click_with_confirmation(session, locator, action.get("confirm_final"))
    elif kind in ("type", "select", "check"):
        _, locator = element(session, action["id"])
        if kind == "type":
            item, _ = element(session, action["id"])
            await set_value(item, locator, action.get("text", ""))
            if action.get("submit"):
                await locator.press("Enter")
        elif kind == "select":
            try:
                await locator.select_option(label=str(action["option"]))
            except Exception:
                await locator.select_option(value=str(action["option"]))
        else:
            await locator.set_checked(bool(action.get("checked", True)))
    elif kind == "fill":
        fields = action.get("fields")
        if fields is None and action.get("id") is not None and "text" in action:
            fields = [{"id": action["id"], "text": action["text"]}]
        if not isinstance(fields, list) or any(not isinstance(value, dict) or "id" not in value or "text" not in value for value in fields):
            raise ValueError("fill needs fields=[{id, text}, ...], or one observed id and text; use type for a single field")
        for value in fields:
            item, locator = element(session, value["id"])
            await set_value(item, locator, value["text"])
        if action.get("submit_id") is not None:
            _, locator = element(session, action["submit_id"])
            await click_with_confirmation(session, locator, action.get("confirm_final"))
    elif kind == "press":
        if action.get("id") is not None:
            _, locator = element(session, action["id"])
            await locator.press(action["key"])
        else:
            await page.keyboard.press(action["key"])
    elif kind == "scroll":
        await page.mouse.wheel(0, -600 if action.get("direction") == "up" else 600)
    elif kind == "back":
        await page.go_back(wait_until="domcontentloaded")
    elif kind == "switch_tab":
        session.page = session.context.pages[int(action["index"])]
        await session.page.bring_to_front()
    elif kind == "look":
        data = await page.screenshot(type="jpeg", quality=65)
        return {"image": base64.b64encode(data).decode()}
    elif kind == "extract_table":
        if action.get("id"):
            _, locator = element(session, action["id"])
            return await locator.inner_text()
        return "\n\n".join(await page.locator("table").all_inner_texts()) or "No tables; read the current snapshot."
    elif kind == "expand":
        from .snapshot import observe
        return (await observe(session, expanded=action["section"])).text
    elif kind == "open_document":
        from .documents import open_document
        return await open_document(session, action["id"])
    else:
        raise ValueError(f"Unsupported browser action: {kind}")
    return "Action completed. Verify the new observation."
