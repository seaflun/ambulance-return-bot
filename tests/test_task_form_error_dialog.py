import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path(__file__).resolve().parents[1] / "WinPython_公務電腦使用包" / "templates"


class _DialogMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self.li_text = []
        self.current_li = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.elements[attrs["id"]] = dict(tag=tag, attrs=attrs)
        if tag == "li":
            self.current_li = []

    def handle_data(self, text):
        if self.current_li is not None:
            self.current_li.append(text)

    def handle_endtag(self, tag):
        if tag == "li" and self.current_li is not None:
            self.li_text.append("".join(self.current_li))
            self.current_li = None


def _balanced_block(source, start):
    depth, quote, escaped = 0, None, False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("JavaScript block did not close")


NODE_DOM = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const nodes = {}, listeners = new Map();
let unsafeHTML = 0, browserDialogs = 0, focused = '', showCount = 0, closeCount = 0, reloadCount = 0;
const on = (target, name, handler) => {
  const key = target.id + ':' + name;
  if (!listeners.has(key)) listeners.set(key, []);
  listeners.get(key).push(handler);
};
const fire = (target, name, values = {}) => {
  const event = {target, key: '', defaultPrevented: false, preventDefault() {this.defaultPrevented = true;},
    stopPropagation() {}, ...values};
  for (const callback of listeners.get(target.id + ':' + name) || []) callback(event);
  return event;
};
class Element {
  constructor(tag, id = '', attrs = {}) {
    this.tagName = tag.toUpperCase(); this.id = id; this.attributes = {...attrs};
    this.children = []; this.hidden = 'hidden' in attrs; this.open = 'open' in attrs;
    this.textContent = ''; this.disabled = false; this.required = false;
    this.dataset = {}; this.value = ''; this.style = {};
    this.classList = {add() {}, remove() {}, toggle() {}, contains: () => false};
  }
  addEventListener(name, callback) {on(this, name, callback);}
  setAttribute(name, value) {this.attributes[name] = value; if (name === 'open') this.open = true;}
  getAttribute(name) {return this.attributes[name] ?? null;}
  removeAttribute(name) {delete this.attributes[name]; if (name === 'open') this.open = false;}
  appendChild(child) {this.children.push(child); return child;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children;}
  set textContent(value) {this._textContent = String(value); this.children = [];}
  get textContent() {return this.children.length ? this.children.map(child => child.textContent).join('') : this._textContent;}
  set innerHTML(value) {if (value) unsafeHTML += 1; this.children = [];}
  get innerHTML() {return '';}
  querySelector(selector) {
    if (selector === 'ul' || selector.includes('client-form-errors-list')) return nodes['client-form-errors-list'];
    if (selector.includes('button')) return nodes['form-error-confirm'];
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector) {
    if (selector.includes('field-visual')) return [wrapper];
    if (selector.includes('vehicle-card')) return [];
    if (selector.includes(':invalid') || selector.includes('has-error') || /input|select|textarea/.test(selector)) return [field];
    return [];
  }
  focus() {focused = this.id; document.activeElement = this;}
  scrollIntoView() {}
  checkValidity() {return !input.invalid;}
  showModal() {if (this.open) throw Error('showModal called on open dialog'); this.open = true; showCount += 1;}
  close() {this.open = false; closeCount += 1; fire(this, 'close');}
}
for (const [id, item] of Object.entries(input.markup)) nodes[id] = new Element(item.tag, id, item.attrs);
const dialog = nodes['client-form-errors'];
const listNode = nodes['client-form-errors-list'];
const confirmNode = nodes['form-error-confirm'];
if (!dialog || dialog.tagName !== 'DIALOG' || !listNode || listNode.tagName !== 'UL'
    || !confirmNode || confirmNode.tagName !== 'BUTTON' || confirmNode.getAttribute('type') !== 'button') {
  throw Error('Expected native dialog, error list, and non-submit confirmation button');
}
for (const text of input.initialItems) {const li = new Element('li'); li.textContent = text; listNode.appendChild(li);}
const field = new Element('input', 'invalid-mileage'); field.required = true;
const wrapper = new Element('label', 'invalid-wrapper'); wrapper.dataset.requiredMessage = '請完成必填欄位';
const submitButton = new Element('button', 'submit-task'); submitButton.formAction = '/tasks';
const form = new Element('form', input.formId || 'task-form');
const document = {id: 'document', readyState: input.readyState || 'loading', activeElement: field,
  getElementById: id => nodes[id] || (id === form.id ? form : null), createElement: tag => new Element(tag),
  querySelector: selector => selector === 'form:focus-within' ? null :
    (selector.startsWith('#') ? nodes[selector.slice(1)] || null : field),
  querySelectorAll: selector => selector === 'dialog' ? [dialog] : [field],
  addEventListener(name, callback) {on(this, name, callback);}};
const window = {id: 'window', document, addEventListener(name, callback) {on(this, name, callback);},
  requestAnimationFrame: callback => callback(), setTimeout: callback => callback(),
  location: {reload() {reloadCount += 1;}},
  alert() {browserDialogs += 1;}, confirm() {browserDialogs += 1; return true;}};
Object.assign(global, {document, window, requestAnimationFrame: window.requestAnimationFrame});
global.alert = window.alert; global.confirm = window.confirm;
for (const script of input.scripts) new Function(script)();
if (input.wrapper) {
  new Function('clientFormErrors', input.wrapper + '\n global.showClientFormErrors = showClientFormErrors;')(dialog);
}
if (input.submitBody) {
  Object.assign(global, {taskForm: form, form, list: form, twoVehicleToggle: {checked: false},
    taskFormDirty: false,
    syncPatientSummary() {}, syncFuelDrivers() {}, reindexCards() {},
    fuelRecordErrors: prefix => input.invalid && !prefix ? ['加油單價需為0–100'] : [],
    mileageChangeErrors: () => [], volunteerAssistErrors: () => [],
    requiredControl: () => field});
  on(form, 'submit', new Function('event', input.submitBody));
}
const events = [];
for (const action of input.actions) {
  if (action.type === 'ready') {
    document.readyState = 'complete'; fire(document, 'DOMContentLoaded'); fire(window, 'DOMContentLoaded'); fire(window, 'load');
  } else if (action.type === 'show') {
    if (typeof window.showTaskFormErrors !== 'function') throw Error('Shared error dialog API missing');
    window.showTaskFormErrors(action.errors);
  } else if (action.type === 'confirm') {
    fire(confirmNode, 'click');
  } else if (action.type === 'escape') {
    const event = fire(dialog, 'cancel');
    if (!event.defaultPrevented) dialog.close();
    events.push({type: 'escape', prevented: event.defaultPrevented});
  } else if (action.type === 'backdrop') {
    fire(dialog, 'click', {target: dialog});
  } else if (action.type === 'submit') {
    const event = fire(form, 'submit', {submitter: submitButton});
    events.push({type: 'submit', prevented: event.defaultPrevented});
  } else if (action.type === 'refresh') {
    document.activeElement = confirmNode;
    new Function(input.refreshBody)();
  }
}
process.stdout.write(JSON.stringify({open: dialog.open, items: listNode.children.map(item => item.textContent),
  focused, showCount, closeCount, unsafeHTML, browserDialogs, events,
  reloadCount, dialogCount: document.querySelectorAll('dialog').length}));
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required for dialog behavior tests")
class TaskFormErrorDialogTests(unittest.TestCase):
    def mileage_error(self, template, current):
        source = (TEMPLATES / template).read_text(encoding="utf-8")
        marker = source.index("function mileageChangeErrors")
        block = source.index("{", marker)
        function = source[marker:block] + _balanced_block(source, block)
        code = r"""
        const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
        const marks = new Set();
        const wrapper = {classList: {add: name => marks.add(name)}};
        const vehicle = {value: '新坡15'};
        const mileage = {value: String(input.current), closest: () => wrapper};
        const querySelector = selector => selector.includes('case_date') ? {value: '2026/10/09'} :
          selector.includes('case_time') ? {value: '2234'} : selector.includes('vehicle') ? vehicle : mileage;
        const document = {querySelector};
        const form = {querySelector};
        const card = {querySelector};
        const run = new Function('document', 'form', 'card', 'lastVehicleMileages',
          'lastVehicleMileageTimes', 'maxVehicleMileageChangeKm', 'enforceMileageChangeLimit',
          input.source + '\n return ' + (input.disaster ? 'mileageChangeErrors(card, 0)' :
          'mileageChangeErrors("vehicle", "mileage")') + ';');
        const errors = run(document, form, card, {'新坡15': 25159}, {}, 300, true);
        process.stdout.write(JSON.stringify({errors, highlighted: marks.has('has-error')}));
        """
        completed = subprocess.run([shutil.which("node"), "-e", code],
                                   input=json.dumps(dict(source=function, current=current,
                                                        disaster=template.startswith("disaster"))),
                                   text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def execute(self, actions, *, server_errors=None, template=None, invalid=False):
        include = TEMPLATES / "_task_form_error_dialog.html"
        self.assertTrue(include.exists(), "Both task forms need the shared error dialog")
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        rendered = env.get_template(include.name).render(form_errors=server_errors or [])
        parsed = _DialogMarkup()
        parsed.feed(rendered)
        payload = dict(markup=parsed.elements, initialItems=parsed.li_text, actions=actions,
                       invalid=invalid, scripts=re.findall(r"<script\b[^>]*>(.*?)</script>", rendered, re.S))
        self.assertTrue(payload["scripts"], "Rendered dialog must provide its client behavior")
        if template:
            source = (TEMPLATES / template).read_text(encoding="utf-8")
            marker = source.index("function showClientFormErrors")
            block = source.index("{", marker)
            payload["wrapper"] = source[marker:block] + _balanced_block(source, block)
            listener = re.search(r"(?:taskForm|form)\.addEventListener\(\s*['\"]submit['\"]\s*,\s*(?:\(event\)|event)\s*=>\s*\{", source)
            self.assertIsNotNone(listener, "Task form must intercept invalid submissions")
            payload["submitBody"] = _balanced_block(source, listener.end() - 1)[1:-1]
            payload["formId"] = "disaster-form" if template.startswith("disaster") else "task-form"
            refresh = re.search(r"window\.setTimeout\(\s*\(\)\s*=>\s*\{\s*(?:const activeTag|if\(!document\.querySelector\('form:focus-within'\))", source)
            self.assertIsNotNone(refresh, "Task form's existing auto-refresh callback must remain available")
            payload["refreshBody"] = _balanced_block(source, source.index("{", refresh.start()))[1:-1]
        completed = subprocess.run([shutil.which("node"), "-e", NODE_DOM], input=json.dumps(payload),
                                   text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_server_errors_open_the_dialog_after_dom_ready(self):
        text = "</script><script>alert('server error')</script>"
        result = self.execute([dict(type="ready")], server_errors=["請核對里程", "請核對里程", text])
        self.assertTrue(result["open"])
        self.assertEqual(["請核對里程", text], result["items"])
        self.assertEqual(0, result["browserDialogs"])

    def test_client_errors_are_deduplicated_and_rendered_as_text(self):
        text = '<img src=x onerror="alert(1)">'
        result = self.execute([dict(type="show", errors=["里程錯誤", "里程錯誤", "", "  ", text])])
        self.assertTrue(result["open"])
        self.assertEqual(["里程錯誤", text], result["items"])
        self.assertEqual(0, result["unsafeHTML"])
        self.assertEqual(0, result["browserDialogs"])

    def test_only_confirmation_closes_and_focuses_the_invalid_field(self):
        result = self.execute([dict(type="show", errors=["里程錯誤"]), dict(type="confirm")], invalid=True)
        self.assertFalse(result["open"])
        self.assertEqual("invalid-mileage", result["focused"])
        self.assertEqual(1, result["closeCount"])

    def test_escape_and_backdrop_cannot_dismiss_the_dialog(self):
        result = self.execute([dict(type="show", errors=["里程錯誤"]), dict(type="escape"), dict(type="backdrop")])
        self.assertTrue(result["open"])
        self.assertTrue(result["events"][0]["prevented"])
        self.assertEqual(0, result["closeCount"])

    def test_repeated_errors_replace_the_same_dialog_without_stacking(self):
        result = self.execute([dict(type="show", errors=["第一次"]), dict(type="show", errors=["第二次", "第二次"])])
        self.assertTrue(result["open"])
        self.assertEqual(["第二次"], result["items"])
        self.assertEqual(1, result["dialogCount"])

    def test_empty_errors_do_not_open_a_dialog(self):
        result = self.execute([dict(type="ready"), dict(type="show", errors=["", " "])])
        self.assertFalse(result["open"])
        self.assertEqual(0, result["showCount"])

    def test_empty_errors_cannot_dismiss_a_dialog_awaiting_confirmation(self):
        result = self.execute([dict(type="show", errors=["等待確認"]), dict(type="show", errors=[])])
        self.assertTrue(result["open"])
        self.assertEqual(["等待確認"], result["items"])

    def test_dialog_has_accessible_title_and_description(self):
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        parsed = _DialogMarkup()
        parsed.feed(env.get_template("_task_form_error_dialog.html").render(form_errors=[]))
        attrs = parsed.elements["client-form-errors"]["attrs"]
        for attr in ("aria-labelledby", "aria-describedby"):
            self.assertTrue(attrs.get(attr))
            for referenced_id in attrs[attr].split():
                self.assertIn(referenced_id, parsed.elements)

    def test_auto_refresh_waits_until_the_dialog_is_confirmed(self):
        for template in ("new_task.html", "disaster_task.html"):
            with self.subTest(template=template):
                waiting = self.execute([dict(type="show", errors=["等待確認"]), dict(type="refresh")], template=template)
                confirmed = self.execute([dict(type="show", errors=["等待確認"]), dict(type="confirm"), dict(type="refresh")], template=template)
                self.assertEqual(0, waiting["reloadCount"])
                self.assertTrue(waiting["open"])
                self.assertEqual(1, confirmed["reloadCount"])

    def test_both_task_forms_block_invalid_submit_and_open_the_shared_dialog(self):
        for template in ("new_task.html", "disaster_task.html"):
            with self.subTest(template=template):
                result = self.execute([dict(type="submit")], template=template, invalid=True)
                self.assertTrue(result["events"][0]["prevented"])
                self.assertTrue(result["open"])
                self.assertTrue(result["items"])
                self.assertEqual(0, result["browserDialogs"])

    def test_both_task_forms_allow_valid_submissions_without_a_dialog(self):
        for template in ("new_task.html", "disaster_task.html"):
            with self.subTest(template=template):
                result = self.execute([dict(type="submit")], template=template, invalid=False)
                self.assertFalse(result["events"][0]["prevented"])
                self.assertFalse(result["open"])

    def test_numeric_mileage_range_errors_mark_the_field_for_confirmation_focus(self):
        for template in ("new_task.html", "disaster_task.html"):
            for current in (25117, 25460):
                with self.subTest(template=template, current=current):
                    result = self.mileage_error(template, current)
                    self.assertTrue(result["errors"])
                    self.assertTrue(result["highlighted"])

    def test_valid_numeric_mileage_is_not_marked_as_an_error(self):
        for template in ("new_task.html", "disaster_task.html"):
            with self.subTest(template=template):
                result = self.mileage_error(template, 25172)
                self.assertEqual([], result["errors"])
                self.assertFalse(result["highlighted"])
