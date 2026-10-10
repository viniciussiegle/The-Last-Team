from __future__ import annotations

import base64
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


async def click_with_confirmation(session, locator, confirm_final=False):
    session.confirm_final = bool(confirm_final)
    try:
        await locator.click()
    finally:
        session.confirm_final = False


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
