/* Admin file browser. Plain JavaScript, no libraries. Talks to /<admin>/files/api/*. */
(function () {
    'use strict';

    var root = document.getElementById('fb');
    if (!root) return;
    var BASE = root.dataset.base;
    var CSRF = root.dataset.csrf;
    var LIMIT = parseInt(root.dataset.limit, 10) || 300 * 1048576;
    var EDIT_LIMIT = parseInt(root.dataset.editLimit, 10) || 524288;

    var $ = function (sel) { return document.querySelector(sel); };
    var listEl = $('#fb-list'), treeEl = $('#fb-tree'), crumbsEl = $('#fb-crumbs'), paneEl = $('#fb-pane');
    var searchEl = $('#fb-search'), sortEl = $('#fb-sort'), dropEl = $('#fb-drop');
    var countEl = $('#fb-count'), clipEl = $('#fb-clip'), progEl = $('#fb-prog');
    var menuEl = $('#fb-menu'), toastsEl = $('#fb-toasts');

    var state = {
        cwd: '', entries: [], search: '', sel: [], anchor: null, clip: null, protectedDir: false,
        view: store('fb-view') === 'grid' ? 'grid' : 'list',
        sort: store('fb-sort') || 'name', desc: store('fb-desc') === '1', loadId: 0, truncated: false
    };
    sortEl.value = ['name', 'mtime', 'size', 'type'].indexOf(state.sort) >= 0 ? state.sort : 'name';
    var expanded = {};          // folder path -> true, for the tree

    /* ---------- small helpers ---------- */

    function store(key, value) {
        try {
            if (value === undefined) return window.localStorage.getItem(key);
            window.localStorage.setItem(key, value);
        } catch (e) { /* storage can be blocked */ }
        return null;
    }
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }
    function enc(s) { return encodeURIComponent(s); }
    function join(a, b) { return a ? a + '/' + b : b; }
    function parentOf(p) { var i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); }
    function baseOf(p) { return p.slice(p.lastIndexOf('/') + 1); }
    function size(n) {
        if (n < 1024) return n + ' B';
        var u = ['KB', 'MB', 'GB'], i = -1;
        do { n /= 1024; i++; } while (n >= 1024 && i < 2);
        return n.toFixed(n >= 100 ? 0 : 1) + ' ' + u[i];
    }
    function when(ts) {
        var d = new Date(ts * 1000), p = function (x) { return (x < 10 ? '0' : '') + x; };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }
    var ICON = { folder: '📁', image: '🖼', pdf: '📕', archive: '🗜', video: '🎞', audio: '🎵', text: '📝', doc: '📄' };

    function toast(msg, kind, action) {
        var t = el('div', 'fb-toast ' + (kind || ''));
        t.appendChild(el('span', null, msg));
        if (action) {
            var a = el('a', null, action.label);
            a.href = action.href;
            t.appendChild(a);
        }
        toastsEl.appendChild(t);
        setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 400); }, kind === 'error' ? 9000 : 4500);
    }

    function api(path, opts) {
        opts = opts || {};
        var init = { method: 'GET', headers: { 'X-CSRF-Token': CSRF }, credentials: 'same-origin' };
        if (opts.json) {
            init.method = 'POST';
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.json);
        }
        return fetch(BASE + '/api/' + path, init).then(function (r) {
            if (r.redirected && /login/.test(r.url)) {
                return { ok: false, error: 'Your session ended. Reload the page to sign in again.', _status: 401 };
            }
            return r.json().then(function (d) { d._status = r.status; return d; }, function () {
                return { ok: false, error: 'The server sent an unexpected reply (status ' + r.status + ').', _status: r.status };
            });
        }, function () { return { ok: false, error: 'Cannot reach the server.', _status: 0 }; });
    }

    /* ---------- dialogs ---------- */

    var dlg = $('#fb-dlg'), dlgTitle = $('#fb-dlg-title'), dlgBody = $('#fb-dlg-body'), dlgBtns = $('#fb-dlg-btns');

    /* modal({title, text|node, input:{value}, buttons:[{label,value,cls}]}) -> Promise({value, text}) ; value null = cancelled */
    function modal(o) {
        return new Promise(function (resolve) {
            dlgTitle.textContent = o.title || '';
            dlgBody.textContent = '';
            if (o.text) dlgBody.appendChild(el('p', null, o.text));
            if (o.node) dlgBody.appendChild(o.node);
            var input = null;
            if (o.input) {
                input = el('input', 'fb-input');
                input.type = 'text';
                input.value = o.input.value || '';
                input.autocomplete = 'off';
                input.spellcheck = false;
                dlgBody.appendChild(input);
            }
            dlgBtns.textContent = '';
            var buttons = o.buttons || [{ label: 'OK', value: 'ok', cls: 'primary' }, { label: 'cancel', value: null }];
            var result = null;
            buttons.forEach(function (b) {
                var btn = el('button', 'a-btn ' + (b.cls || ''), b.label);
                btn.type = 'button';
                btn.addEventListener('click', function () { result = b.value; dlg.close(); });
                dlgBtns.appendChild(btn);
            });
            function done() {
                dlg.removeEventListener('close', done);
                resolve({ value: result, text: input ? input.value : '' });
            }
            dlg.addEventListener('close', done);
            dlg.showModal();
            if (input) {
                input.focus();
                var dot = input.value.lastIndexOf('.');
                input.setSelectionRange(0, o.input.selectStem && dot > 0 ? dot : input.value.length);
                input.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter') { e.preventDefault(); result = buttons[0].value; dlg.close(); }
                });
            } else if (dlgBtns.firstChild) {
                dlgBtns.firstChild.focus();
            }
        });
    }

    function list(items, max) {
        var ul = el('ul', 'fb-dlg-list');
        items.slice(0, max || 8).forEach(function (t) { ul.appendChild(el('li', null, t)); });
        if (items.length > (max || 8)) ul.appendChild(el('li', 'dim', '… and ' + (items.length - (max || 8)) + ' more'));
        return ul;
    }

    function askConflicts(names, verb) {
        var wrap = el('div');
        wrap.appendChild(el('p', null, (names.length === 1 ? 'This name already exists' : names.length + ' names already exist') + ' in the destination:'));
        wrap.appendChild(list(names));
        return modal({
            title: verb + ': name clash', node: wrap,
            buttons: [{ label: 'replace', value: 'replace', cls: 'warn' }, { label: 'keep both', value: 'rename', cls: 'primary' },
                      { label: 'skip existing', value: 'skip' }, { label: 'cancel', value: null }]
        }).then(function (r) { return r.value; });
    }

    function showSkipped(skipped, title) {
        if (!skipped || !skipped.length) return Promise.resolve();
        var wrap = el('div');
        wrap.appendChild(el('p', null, skipped.length + ' file(s) were not used:'));
        wrap.appendChild(list(skipped.map(function (s) { return s[0] + ' - ' + s[1]; }), 12));
        return modal({ title: title || 'Some files were skipped', node: wrap, buttons: [{ label: 'OK', value: 'ok', cls: 'primary' }] });
    }

    /* ---------- loading ---------- */

    function load(keepSel) {
        var id = ++state.loadId;
        var url = 'list?path=' + enc(state.cwd) + (state.search ? '&q=' + enc(state.search) : '');
        return api(url).then(function (d) {
            if (id !== state.loadId) return;
            if (!d.ok) {
                toast(d.error || 'Could not open that folder.', 'error');
                if (state.cwd) { state.cwd = ''; state.search = ''; searchEl.value = ''; return load(); }
                return;
            }
            state.entries = d.entries;
            state.protectedDir = d.protected;
            state.truncated = d.truncated;
            if (keepSel) {
                var have = {};
                d.entries.forEach(function (e) { have[e.path] = 1; });
                state.sel = state.sel.filter(function (p) { return have[p]; });
            } else {
                state.sel = [];
                state.anchor = null;
            }
            render();
        });
    }

    function go(path, push) {
        state.cwd = path;
        state.search = '';
        searchEl.value = '';
        if (push !== false) {
            try { history.pushState({ p: path }, '', BASE + (path ? '?path=' + enc(path) : '')); } catch (e) { /* ignore */ }
        }
        load();
        syncTree();
    }
    window.addEventListener('popstate', function (e) {
        state.cwd = (e.state && e.state.p) || new URLSearchParams(location.search).get('path') || '';
        state.search = '';
        searchEl.value = '';
        load();
        syncTree();
    });

    /* ---------- rendering ---------- */

    function sorted() {
        var key = state.sort, dir = state.desc ? -1 : 1;
        return state.entries.slice().sort(function (a, b) {
            if (a.dir !== b.dir) return a.dir ? -1 : 1;
            var r;
            if (key === 'size') r = a.size - b.size;
            else if (key === 'mtime') r = b.mtime - a.mtime;
            else if (key === 'type') r = (a.ext || '').localeCompare(b.ext || '');
            else r = 0;
            if (!r) r = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
            return r * dir;
        });
    }

    function byPath(p) {
        for (var i = 0; i < state.entries.length; i++) if (state.entries[i].path === p) return state.entries[i];
        return null;
    }
    function selected() { return state.sel.map(byPath).filter(Boolean); }

    function renderCrumbs() {
        crumbsEl.textContent = '';
        var parts = state.cwd ? state.cwd.split('/') : [];
        var acc = '';
        function crumb(label, path) {
            var a = el('a', 'fb-crumb', label);
            a.href = BASE + (path ? '?path=' + enc(path) : '');
            a.dataset.path = path;
            a.dataset.drop = '1';
            a.addEventListener('click', function (e) { e.preventDefault(); go(path); });
            crumbsEl.appendChild(a);
        }
        crumb('static', '');
        parts.forEach(function (p) {
            acc = join(acc, p);
            crumbsEl.appendChild(el('span', 'fb-sep', '/'));
            crumb(p, acc);
        });
        if (state.search) {
            crumbsEl.appendChild(el('span', 'fb-sep', '›'));
            crumbsEl.appendChild(el('span', 'fb-search-tag', 'search: "' + state.search + '"' + (state.truncated ? ' (first results only)' : '')));
        }
    }

    function nameCell(e, grid) {
        var wrap = el('span', 'fb-name');
        var ic;
        if (grid && e.kind === 'image') {
            ic = el('img', 'fb-thumb');
            ic.src = e.url;
            ic.alt = '';
            ic.loading = 'lazy';
            ic.draggable = false;
        } else {
            ic = el('span', 'fb-ico', ICON[e.kind] || ICON.doc);
        }
        wrap.appendChild(ic);
        var label = el('span', 'fb-label', e.name);
        wrap.appendChild(label);
        if (e.role) wrap.appendChild(el('span', 'chip published fb-role', e.role));
        return wrap;
    }

    function render() {
        renderCrumbs();
        listEl.textContent = '';
        listEl.className = 'fb-list ' + state.view;
        var rows = sorted();
        if (!rows.length) {
            listEl.appendChild(el('div', 'fb-empty', state.search ? 'Nothing here matches "' + state.search + '".' :
                'This folder is empty. Drop files here, or use "upload".'));
        } else if (state.view === 'list') {
            var table = el('table', 't fb-table');
            var head = el('thead'), hr = el('tr');
            var th0 = el('th', 'fb-c-check'); hr.appendChild(th0);
            [['name', 'name'], ['size', 'size'], ['mtime', 'modified'], ['type', 'type']].forEach(function (c) {
                var th = el('th', c[0] === 'size' ? 'num' : '', c[1] + (state.sort === c[0] ? (state.desc ? ' ▾' : ' ▴') : ''));
                th.dataset.sort = c[0];
                th.classList.add('fb-sortable');
                hr.appendChild(th);
            });
            if (state.search) hr.appendChild(el('th', null, 'in folder'));
            head.appendChild(hr);
            table.appendChild(head);
            var body = el('tbody');
            rows.forEach(function (e) {
                var tr = el('tr', 'row fb-item');
                tr.dataset.path = e.path;
                tr.draggable = true;
                var c0 = el('td', 'fb-c-check');
                var cb = el('input'); cb.type = 'checkbox'; cb.className = 'fb-cb'; cb.setAttribute('aria-label', 'select ' + e.name);
                c0.appendChild(cb);
                tr.appendChild(c0);
                var c1 = el('td', 'fb-c-name'); c1.appendChild(nameCell(e, false)); tr.appendChild(c1);
                tr.appendChild(el('td', 'num dim', e.dir ? (e.count != null ? e.count + ' item' + (e.count === 1 ? '' : 's') : '') : size(e.size)));
                tr.appendChild(el('td', 'dim nowrap', when(e.mtime)));
                tr.appendChild(el('td', 'dim', e.dir ? 'folder' : (e.ext || '').replace('.', '') || 'file'));
                if (state.search) tr.appendChild(el('td', 'dim', '/' + (e.where || '')));
                if (e.dir) tr.dataset.dir = '1';
                if (e.allowed === false) { tr.classList.add('fb-dim'); tr.title = 'This type is not allowed on the site'; }
                body.appendChild(tr);
            });
            table.appendChild(body);
            listEl.appendChild(table);
        } else {
            rows.forEach(function (e) {
                var card = el('div', 'fb-item fb-card');
                card.dataset.path = e.path;
                card.draggable = true;
                if (e.dir) card.dataset.dir = '1';
                var thumb = el('div', 'fb-card-thumb');
                if (e.kind === 'image') {
                    var im = el('img'); im.src = e.url; im.alt = ''; im.loading = 'lazy'; im.draggable = false;
                    thumb.appendChild(im);
                } else {
                    thumb.appendChild(el('span', 'fb-ico big', ICON[e.kind] || ICON.doc));
                }
                card.appendChild(thumb);
                card.appendChild(el('div', 'fb-card-name', e.name));
                card.appendChild(el('div', 'fb-card-sub dim', e.dir ? (e.count != null ? e.count + ' item' + (e.count === 1 ? '' : 's') : '') : size(e.size)));
                if (e.role) card.appendChild(el('span', 'chip published fb-role', e.role));
                listEl.appendChild(card);
            });
        }
        paintSelection();
        updateButtons();
    }

    function itemEls() { return Array.prototype.slice.call(listEl.querySelectorAll('.fb-item')); }

    function paintSelection() {
        var on = {};
        state.sel.forEach(function (p) { on[p] = 1; });
        itemEls().forEach(function (n) {
            var s = !!on[n.dataset.path];
            n.classList.toggle('sel', s);
            n.setAttribute('aria-selected', s ? 'true' : 'false');
            var cb = n.querySelector('.fb-cb');
            if (cb) cb.checked = s;
        });
        var sel = selected();
        var total = sel.reduce(function (n, e) { return n + (e.dir ? 0 : e.size); }, 0);
        countEl.textContent = state.entries.length + ' item' + (state.entries.length === 1 ? '' : 's') +
            (sel.length ? ' · ' + sel.length + ' selected' + (total ? ' (' + size(total) + ')' : '') : '');
        clipEl.textContent = state.clip ? 'clipboard: ' + state.clip.paths.length + ' item(s) to ' + (state.clip.mode === 'cut' ? 'move' : 'copy') : '';
        updateButtons();
    }

    function updateButtons() {
        var sel = selected();
        var one = sel.length === 1, zip = one && sel[0].ext === '.zip';
        Array.prototype.forEach.call(root.querySelectorAll('[data-need]'), function (b) {
            var need = b.dataset.need, ok = true;
            if (need === '1') ok = sel.length > 0;
            else if (need === 'one') ok = one;
            else if (need === 'zip') ok = zip;
            else if (need === 'clip') ok = !!state.clip && !state.search;
            if ((b.dataset.act === 'rename' || b.dataset.act === 'delete' || b.dataset.act === 'cut') && sel.some(function (e) { return e.dir && isCore(e.path); })) ok = false;
            b.disabled = !ok;
        });
        var nonRoot = !state.search;
        ['mkdir', 'newfile', 'upload', 'upload-folder'].forEach(function (a) {
            var b = root.querySelector('[data-act="' + a + '"]');
            if (b) b.disabled = !nonRoot;
        });
    }
    function isCore(p) { return ['course', 'instructors', 'webinars', 'images', 'logo'].indexOf(p) >= 0; }

    /* ---------- selection ---------- */

    function select(paths, anchor) {
        state.sel = paths;
        if (anchor !== undefined) state.anchor = anchor;
        paintSelection();
    }

    listEl.addEventListener('click', function (e) {
        var th = e.target.closest('th.fb-sortable');
        if (th) { setSort(th.dataset.sort); return; }
        var item = e.target.closest('.fb-item');
        if (!item) { if (!e.target.closest('.fb-empty')) select([], null); return; }
        var path = item.dataset.path;
        if (e.target.classList.contains('fb-cb')) {
            toggle(path);
            return;
        }
        if (e.shiftKey && state.anchor) {
            var order = sorted().map(function (x) { return x.path; });
            var a = order.indexOf(state.anchor), b = order.indexOf(path);
            if (a >= 0 && b >= 0) { select(order.slice(Math.min(a, b), Math.max(a, b) + 1)); return; }
        }
        if (e.ctrlKey || e.metaKey) { toggle(path); return; }
        select([path], path);
    });
    function toggle(path) {
        var i = state.sel.indexOf(path);
        var next = state.sel.slice();
        if (i >= 0) next.splice(i, 1); else next.push(path);
        select(next, path);
    }
    listEl.addEventListener('dblclick', function (e) {
        var item = e.target.closest('.fb-item');
        if (item) openEntry(byPath(item.dataset.path));
    });

    function setSort(key) {
        if (state.sort === key) state.desc = !state.desc; else { state.sort = key; state.desc = false; }
        sortEl.value = key;
        store('fb-sort', state.sort); store('fb-desc', state.desc ? '1' : '0');
        render();
    }
    sortEl.addEventListener('change', function () { state.sort = sortEl.value; state.desc = false; store('fb-sort', state.sort); store('fb-desc', '0'); render(); });

    var searchTimer = null;
    searchEl.addEventListener('input', function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { state.search = searchEl.value.trim(); load(); }, 250);
    });
    searchEl.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { searchEl.value = ''; state.search = ''; load(); searchEl.blur(); }
    });

    /* ---------- opening things ---------- */

    function rawUrl(e, dl) { return BASE + '/raw?path=' + enc(e.path) + (dl ? '&dl=1' : ''); }

    function openEntry(e) {
        if (!e) return;
        if (e.dir) { go(e.path); return; }
        if (['image', 'pdf', 'video', 'audio'].indexOf(e.kind) >= 0) { preview(e); return; }
        if (e.text) { editor(e); return; }
        if (e.kind === 'archive') { extractFlow(e); return; }
        download([e]);
    }

    /* preview */
    var vdlg = $('#fb-view'), vbody = $('#fb-view-body'), vname = $('#fb-view-name'), vopen = $('#fb-view-open');
    var vlist = [], vindex = 0;
    function preview(e) {
        vlist = sorted().filter(function (x) { return ['image', 'pdf', 'video', 'audio'].indexOf(x.kind) >= 0; });
        vindex = Math.max(0, vlist.map(function (x) { return x.path; }).indexOf(e.path));
        showPreview();
        if (!vdlg.open) vdlg.showModal();
    }
    function showPreview() {
        var e = vlist[vindex];
        vbody.textContent = '';
        vname.textContent = e.name + '  ·  ' + size(e.size);
        vopen.href = rawUrl(e);
        var node;
        if (e.kind === 'image') { node = el('img'); node.src = rawUrl(e); node.alt = e.name; }
        else if (e.kind === 'pdf') { node = el('iframe'); node.src = rawUrl(e); node.title = e.name; }
        else if (e.kind === 'video') { node = el('video'); node.src = rawUrl(e); node.controls = true; }
        else { node = el('audio'); node.src = rawUrl(e); node.controls = true; }
        vbody.appendChild(node);
        $('#fb-view-prev').disabled = vindex <= 0;
        $('#fb-view-next').disabled = vindex >= vlist.length - 1;
    }
    function stepPreview(d) { var n = vindex + d; if (n >= 0 && n < vlist.length) { vindex = n; showPreview(); } }
    $('#fb-view-prev').addEventListener('click', function () { stepPreview(-1); });
    $('#fb-view-next').addEventListener('click', function () { stepPreview(1); });
    $('#fb-view-close').addEventListener('click', function () { vdlg.close(); });
    vdlg.addEventListener('close', function () { vbody.textContent = ''; });
    vdlg.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowLeft') stepPreview(-1);
        else if (e.key === 'ArrowRight') stepPreview(1);
    });

    /* text editor */
    var edlg = $('#fb-edit'), etext = $('#fb-edit-text'), estate = $('#fb-edit-state'), emsg = $('#fb-edit-msg');
    var editing = null;
    function editor(e) {
        api('text?path=' + enc(e.path)).then(function (d) {
            if (!d.ok) { toast(d.error, 'error'); return; }
            editing = { path: e.path, mtime: d.mtime, clean: d.text };
            $('#fb-edit-name').textContent = 'static/' + e.path;
            etext.value = d.text;
            estate.textContent = '';
            emsg.textContent = '';
            edlg.showModal();
            etext.focus();
            etext.setSelectionRange(0, 0);
            etext.scrollTop = 0;
        });
    }
    function dirty() { return editing && etext.value !== editing.clean; }
    etext.addEventListener('input', function () { estate.textContent = dirty() ? '● unsaved changes' : ''; });
    function saveEdit(force) {
        if (!editing) return Promise.resolve();
        emsg.textContent = '';
        return api('text', { json: { path: editing.path, text: etext.value, mtime: editing.mtime, force: !!force } }).then(function (d) {
            if (d.ok) {
                editing.clean = etext.value; editing.mtime = d.mtime;
                estate.textContent = 'saved ✓';
                return true;
            }
            if (d.changed) {
                return modal({ title: 'File changed', text: d.error + ' Save anyway and overwrite their version?',
                    buttons: [{ label: 'overwrite', value: 'go', cls: 'warn' }, { label: 'cancel', value: null }] }).then(function (r) {
                    return r.value ? saveEdit(true) : false;
                });
            }
            emsg.textContent = d.error;
            return false;
        });
    }
    $('#fb-edit-save').addEventListener('click', function () { saveEdit(); });
    function closeEditor() {
        if (!dirty()) { edlg.close(); return; }
        modal({ title: 'Unsaved changes', text: 'Close without saving?',
            buttons: [{ label: 'discard', value: 'x', cls: 'danger' }, { label: 'keep editing', value: null, cls: 'primary' }] }).then(function (r) {
            if (r.value) edlg.close();
        });
    }
    $('#fb-edit-close').addEventListener('click', closeEditor);
    edlg.addEventListener('cancel', function (e) { if (dirty()) { e.preventDefault(); closeEditor(); } });
    edlg.addEventListener('keydown', function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); saveEdit(); }
    });
    edlg.addEventListener('close', function () { editing = null; load(true); });

    /* ---------- actions ---------- */

    function download(entries) {
        if (entries.length === 1 && !entries[0].dir) {
            var a = el('a'); a.href = rawUrl(entries[0], true); a.download = entries[0].name;
            document.body.appendChild(a); a.click(); a.remove();
            return;
        }
        zipDownload(entries);
    }
    function zipDownload(entries) {
        var form = $('#fb-zipform');
        Array.prototype.slice.call(form.querySelectorAll('input[name="paths"]')).forEach(function (n) { n.remove(); });
        entries.forEach(function (e) {
            var i = el('input'); i.type = 'hidden'; i.name = 'paths'; i.value = e.path; form.appendChild(i);
        });
        form.submit();
    }

    function mkdir() {
        modal({ title: 'New folder', input: { value: '' }, buttons: [{ label: 'create', value: 'ok', cls: 'primary' }, { label: 'cancel', value: null }] }).then(function (r) {
            if (!r.value) return;
            api('mkdir', { json: { path: state.cwd, name: r.text } }).then(function (d) {
                if (!d.ok) { toast(d.error, 'error'); return; }
                load().then(function () { select([d.path], d.path); });
                refreshTree();
            });
        });
    }
    function newfile() {
        modal({ title: 'New text file (.txt .md .csv .json)', input: { value: 'notes.txt', selectStem: true },
            buttons: [{ label: 'create', value: 'ok', cls: 'primary' }, { label: 'cancel', value: null }] }).then(function (r) {
            if (!r.value) return;
            api('newfile', { json: { path: state.cwd, name: r.text } }).then(function (d) {
                if (!d.ok) { toast(d.error, 'error'); return; }
                load().then(function () { var e = byPath(d.path); select([d.path], d.path); if (e) openEntry(e); });
            });
        });
    }
    function rename(e) {
        if (!e) return;
        modal({ title: 'Rename', input: { value: e.name, selectStem: !e.dir }, buttons: [{ label: 'rename', value: 'ok', cls: 'primary' }, { label: 'cancel', value: null }] }).then(function (r) {
            if (!r.value || r.text === e.name) return;
            api('rename', { json: { path: e.path, name: r.text } }).then(function (d) {
                if (!d.ok) { toast(d.error, 'error'); return; }
                load().then(function () { select([d.path], d.path); });
                refreshTree();
            });
        });
    }
    function remove(entries) {
        if (!entries.length) return;
        var wrap = el('div');
        wrap.appendChild(el('p', null, 'Move ' + (entries.length === 1 ? 'this item' : entries.length + ' items') + ' to the trash? You can restore from the trash page.'));
        wrap.appendChild(list(entries.map(function (e) { return e.name; })));
        modal({ title: 'Delete', node: wrap, buttons: [{ label: 'move to trash', value: 'ok', cls: 'danger' }, { label: 'cancel', value: null, cls: 'primary' }] }).then(function (r) {
            if (!r.value) return;
            api('delete', { json: { paths: entries.map(function (e) { return e.path; }) } }).then(function (d) {
                if (!d.ok) { toast(d.error, 'error'); return; }
                toast('Moved ' + d.count + ' item(s) to the trash.', 'ok', { label: 'open trash', href: BASE.replace(/files$/, 'trash') });
                load();
                refreshTree();
            });
        });
    }

    function transfer(mode, paths, dest) {
        var ep = mode === 'cut' ? 'move' : 'copy';
        function run(policy) {
            return api(ep, { json: { paths: paths, dest: dest, policy: policy } }).then(function (d) {
                if (d.ok) {
                    var bits = [];
                    if (d.done) bits.push(d.done + ' file(s) ' + (mode === 'cut' ? 'moved' : 'copied'));
                    if (d.replaced) bits.push(d.replaced + ' replaced');
                    if (d.renamed) bits.push(d.renamed + ' kept as a copy');
                    if (d.skipped) bits.push(d.skipped + ' skipped');
                    toast(bits.join(', ') || 'Done.', 'ok');
                    load();
                    refreshTree();
                    return;
                }
                if (d._status === 409 && d.conflicts) {
                    return askConflicts(d.conflicts, mode === 'cut' ? 'Move' : 'Copy').then(function (choice) { if (choice) return run(choice); });
                }
                toast(d.error, 'error');
            });
        }
        return run('ask');
    }
    function paste() {
        if (!state.clip || state.search) return;
        var clip = state.clip;
        transfer(clip.mode, clip.paths, state.cwd).then(function () { if (clip.mode === 'cut') { state.clip = null; paintSelection(); } });
    }
    function copyOrCut(mode) {
        var sel = selected();
        if (!sel.length) return;
        state.clip = { mode: mode, paths: sel.map(function (e) { return e.path; }) };
        paintSelection();
        toast((mode === 'cut' ? 'Cut ' : 'Copied ') + sel.length + ' item(s). Open the destination folder and press paste.', '');
    }

    function extractFlow(e) {
        modal({ title: 'Unzip ' + e.name, text: 'Where should the files go?',
            buttons: [{ label: 'new folder "' + e.name.replace(/\.zip$/i, '') + '"', value: 'folder', cls: 'primary' }, { label: 'here', value: 'here' }, { label: 'cancel', value: null }] }).then(function (r) {
            if (!r.value) return;
            function run(policy) {
                return api('extract', { json: { path: e.path, mode: r.value, policy: policy } }).then(function (d) {
                    if (d.ok) {
                        toast('Unzipped ' + d.written + ' file(s)' + (d.replaced ? ', ' + d.replaced + ' replaced' : '') + (d.kept ? ', ' + d.kept + ' left alone' : '') + '.', 'ok');
                        load(); refreshTree();
                        return showSkipped(d.skipped);
                    }
                    if (d._status === 409 && d.conflicts) {
                        return askConflicts(d.conflicts, 'Unzip').then(function (c) { if (c) return run(c); });
                    }
                    toast(d.error, 'error');
                    return showSkipped(d.skipped);
                });
            }
            return run('ask');
        });
    }

    function props(e) {
        api('props?path=' + enc(e.path)).then(function (d) {
            if (!d.ok) { toast(d.error, 'error'); return; }
            var dl = el('dl', 'fb-props');
            function row(k, v) { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); }
            row('name', d.name);
            row('location', 'static/' + d.path);
            if (d.dir) { row('contains', d.files + ' file(s), ' + d.folders + ' folder(s)'); row('total size', size(d.size)); }
            else { row('size', size(d.size) + ' (' + d.size.toLocaleString() + ' bytes)'); row('type', d.type); }
            row('modified', when(d.mtime));
            var wrap = el('div'); wrap.appendChild(dl);
            var buttons = [{ label: 'close', value: null, cls: 'primary' }];
            if (!d.dir) {
                wrap.appendChild(el('p', 'dim', 'Public address on the website:'));
                var inp = el('input', 'fb-input'); inp.readOnly = true; inp.value = location.origin + d.url;
                wrap.appendChild(inp);
                buttons.unshift({ label: 'copy link', value: 'copy' });
            }
            modal({ title: 'Properties', node: wrap, buttons: buttons }).then(function (r) { if (r.value === 'copy') copyLink(e); });
        });
    }
    function copyLink(e) {
        var url = location.origin + e.url;
        var done = function () { toast('Link copied.', 'ok'); };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(done, function () { window.prompt('Copy this link:', url); });
        else window.prompt('Copy this link:', url);
    }

    /* ---------- toolbar ---------- */

    root.addEventListener('click', function (ev) {
        var b = ev.target.closest('[data-act]');
        if (!b || b.disabled) return;
        var sel = selected();
        switch (b.dataset.act) {
            case 'upload': $('#fb-pick').click(); break;
            case 'upload-folder': $('#fb-pick-dir').click(); break;
            case 'mkdir': mkdir(); break;
            case 'newfile': newfile(); break;
            case 'copy': copyOrCut('copy'); break;
            case 'cut': copyOrCut('cut'); break;
            case 'paste': paste(); break;
            case 'rename': rename(sel[0]); break;
            case 'delete': remove(sel); break;
            case 'download': download(sel); break;
            case 'zip': zipDownload(sel); break;
            case 'extract': extractFlow(sel[0]); break;
            case 'props': props(sel[0]); break;
            case 'sortdir': state.desc = !state.desc; store('fb-desc', state.desc ? '1' : '0'); render(); break;
            case 'view': state.view = state.view === 'list' ? 'grid' : 'list'; store('fb-view', state.view); render(); break;
            case 'refresh': load(true); refreshTree(); break;
        }
    });

    /* ---------- context menu ---------- */

    function showMenu(items, x, y) {
        menuEl.textContent = '';
        items.forEach(function (it) {
            if (it === '-') { menuEl.appendChild(el('div', 'fb-menu-sep')); return; }
            var b = el('button', 'fb-menu-item' + (it.danger ? ' danger' : ''), it.label);
            b.type = 'button';
            b.disabled = !!it.disabled;
            b.addEventListener('click', function () { hideMenu(); it.run(); });
            menuEl.appendChild(b);
        });
        menuEl.hidden = false;
        var w = menuEl.offsetWidth, h = menuEl.offsetHeight;
        menuEl.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 6)) + 'px';
        menuEl.style.top = Math.max(4, Math.min(y, window.innerHeight - h - 6)) + 'px';
    }
    function hideMenu() { menuEl.hidden = true; }
    document.addEventListener('click', function (e) { if (!menuEl.contains(e.target)) hideMenu(); });
    window.addEventListener('scroll', hideMenu, true);
    window.addEventListener('blur', hideMenu);

    listEl.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        var item = e.target.closest('.fb-item');
        if (item && state.sel.indexOf(item.dataset.path) < 0) select([item.dataset.path], item.dataset.path);
        if (!item) select([], null);
        var sel = selected(), one = sel.length === 1 ? sel[0] : null;
        var core = sel.some(function (x) { return x.dir && isCore(x.path); });
        var items = [];
        if (sel.length) {
            if (one) items.push({ label: one.dir ? 'open' : (one.text ? 'edit' : 'open / preview'), run: function () { openEntry(one); } });
            if (one && one.edit) items.push({ label: 'edit this ' + one.role + ' in the admin ↗', run: function () { window.open(one.edit, '_blank'); } });
            if (one && state.search) items.push({ label: 'show in its folder', run: function () { go(one.dir ? parentOf(one.path) : (one.where || '')); } });
            if (one && one.kind === 'archive') items.push({ label: 'unzip…', run: function () { extractFlow(one); } });
            items.push({ label: 'download' + (sel.length > 1 || (one && one.dir) ? ' as zip' : ''), run: function () { download(sel); } });
            if (one && !one.dir) items.push({ label: 'copy public link', run: function () { copyLink(one); } });
            items.push('-');
            items.push({ label: 'copy', run: function () { copyOrCut('copy'); } });
            items.push({ label: 'cut', disabled: core, run: function () { copyOrCut('cut'); } });
            if (one && one.dir && state.clip) items.push({ label: 'paste into this folder', run: function () { var c = state.clip; transfer(c.mode, c.paths, one.path); } });
            items.push({ label: 'rename', disabled: !one || core, run: function () { rename(one); } });
            items.push({ label: 'info', disabled: !one, run: function () { props(one); } });
            items.push('-');
            items.push({ label: 'delete', danger: true, disabled: core, run: function () { remove(sel); } });
        } else {
            var here = !state.search;
            items.push({ label: 'upload files…', disabled: !here, run: function () { $('#fb-pick').click(); } });
            items.push({ label: 'new folder…', disabled: !here, run: mkdir });
            items.push({ label: 'new text file…', disabled: !here, run: newfile });
            items.push({ label: 'paste', disabled: !state.clip || !here, run: paste });
            items.push('-');
            items.push({ label: 'refresh', run: function () { load(true); } });
        }
        showMenu(items, e.clientX, e.clientY);
    });

    /* ---------- keyboard ---------- */

    document.addEventListener('keydown', function (e) {
        var t = e.target;
        if (t.closest && (t.closest('dialog[open]') || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
        if (document.querySelector('dialog[open]')) return;
        var mod = e.ctrlKey || e.metaKey, key = e.key, sel = selected();
        if (key === 'Escape') { hideMenu(); select([], null); return; }
        if (mod && key.toLowerCase() === 'a') { e.preventDefault(); select(sorted().map(function (x) { return x.path; })); return; }
        if (mod && key.toLowerCase() === 'c') { if (sel.length) { e.preventDefault(); copyOrCut('copy'); } return; }
        if (mod && key.toLowerCase() === 'x') { if (sel.length && !sel.some(function (x) { return x.dir && isCore(x.path); })) { e.preventDefault(); copyOrCut('cut'); } return; }
        if (mod && key.toLowerCase() === 'v') { if (state.clip) { e.preventDefault(); paste(); } return; }
        if (key === 'Delete') { if (sel.length && !sel.some(function (x) { return x.dir && isCore(x.path); })) { e.preventDefault(); remove(sel); } return; }
        if (key === 'F2') { if (sel.length === 1) { e.preventDefault(); rename(sel[0]); } return; }
        if (key === 'Enter') { if (sel.length === 1) { e.preventDefault(); openEntry(sel[0]); } return; }
        if (key === 'Backspace' || (e.altKey && key === 'ArrowUp')) { if (state.cwd) { e.preventDefault(); go(parentOf(state.cwd)); } return; }
        if (['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft'].indexOf(key) >= 0) {
            var order = sorted().map(function (x) { return x.path; });
            if (!order.length) return;
            e.preventDefault();
            var cur = state.sel.length ? order.indexOf(state.sel[state.sel.length - 1]) : -1;
            var next = (key === 'ArrowDown' || key === 'ArrowRight') ? Math.min(order.length - 1, cur + 1) : Math.max(0, cur < 0 ? 0 : cur - 1);
            if (e.shiftKey && state.anchor) {
                var a = order.indexOf(state.anchor);
                select(order.slice(Math.min(a, next), Math.max(a, next) + 1));
            } else {
                select([order[next]], order[next]);
            }
            var node = listEl.querySelector('[data-path="' + CSS.escape(order[next]) + '"]');
            if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
        }
    });

    /* ---------- tree ---------- */

    function treeNode(f, depth) {
        var li = el('div', 'fb-node');
        li.dataset.path = f.path;
        var row = el('div', 'fb-node-row');
        row.style.paddingLeft = (depth * 14 + 4) + 'px';
        row.dataset.path = f.path;
        row.dataset.drop = '1';
        var tw = el('span', 'fb-tw', f.more ? (expanded[f.path] ? '▾' : '▸') : '');
        tw.dataset.path = f.path;
        row.appendChild(tw);
        row.appendChild(el('span', 'fb-node-name', f.name));
        li.appendChild(row);
        var kids = el('div', 'fb-kids');
        li.appendChild(kids);
        return li;
    }
    function fillTree(container, path, depth) {
        return api('tree?path=' + enc(path)).then(function (d) {
            if (!d.ok) return;
            container.textContent = '';
            var chain = d.folders.map(function (f) {
                var node = treeNode(f, depth);
                container.appendChild(node);
                if (expanded[f.path] && f.more) return fillTree(node.querySelector('.fb-kids'), f.path, depth + 1);
            });
            return Promise.all(chain);
        });
    }
    function buildTree() {
        treeEl.textContent = '';
        var rootRow = el('div', 'fb-node-row fb-root');
        rootRow.dataset.path = '';
        rootRow.dataset.drop = '1';
        rootRow.appendChild(el('span', 'fb-tw', '▾'));
        rootRow.appendChild(el('span', 'fb-node-name', 'static'));
        treeEl.appendChild(rootRow);
        var kids = el('div', 'fb-kids');
        treeEl.appendChild(kids);
        return fillTree(kids, '', 1).then(paintTree);
    }
    function refreshTree() { return buildTree(); }
    function paintTree() {
        Array.prototype.forEach.call(treeEl.querySelectorAll('.fb-node-row'), function (r) {
            r.classList.toggle('on', r.dataset.path === state.cwd);
        });
    }
    function syncTree() {
        // open every folder on the way to the current one, then highlight it
        var parts = state.cwd ? state.cwd.split('/') : [], acc = '', changed = false;
        parts.slice(0, -1).forEach(function (p) { acc = join(acc, p); if (!expanded[acc]) { expanded[acc] = true; changed = true; } });
        if (parts.length && !expanded[state.cwd]) { /* the current folder stays as it is */ }
        if (changed) buildTree(); else paintTree();
    }
    treeEl.addEventListener('click', function (e) {
        var tw = e.target.closest('.fb-tw');
        var row = e.target.closest('.fb-node-row');
        if (!row) return;
        if (tw && tw.textContent && row.dataset.path) {
            var p = row.dataset.path;
            if (expanded[p]) delete expanded[p]; else expanded[p] = true;
            buildTree();
            return;
        }
        go(row.dataset.path);
    });

    /* ---------- drag and drop ---------- */

    var DRAG = 'application/x-belajar-paths';
    var dragging = null;

    listEl.addEventListener('dragstart', function (e) {
        var item = e.target.closest('.fb-item');
        if (!item) return;
        if (state.sel.indexOf(item.dataset.path) < 0) select([item.dataset.path], item.dataset.path);
        dragging = state.sel.slice();
        e.dataTransfer.setData(DRAG, JSON.stringify(dragging));
        e.dataTransfer.setData('text/plain', dragging.join('\n'));
        e.dataTransfer.effectAllowed = 'copyMove';
    });
    document.addEventListener('dragend', function () { dragging = null; clearDropMarks(); dropEl.hidden = true; });

    function dropTarget(e) {
        var t = e.target.closest('[data-drop], .fb-item[data-dir]');
        if (!t) return { path: state.cwd, node: null };
        return { path: t.dataset.drop ? t.dataset.path : t.dataset.path, node: t };
    }
    function clearDropMarks() {
        Array.prototype.forEach.call(document.querySelectorAll('.drop-on'), function (n) { n.classList.remove('drop-on'); });
    }
    function isFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0; }

    paneEl.addEventListener('dragover', function (e) { handleDragOver(e); });
    treeEl.addEventListener('dragover', function (e) { handleDragOver(e); });
    function handleDragOver(e) {
        var internal = dragging && e.dataTransfer.types.indexOf(DRAG) >= 0;
        if (!internal && !isFiles(e)) return;
        if (state.search && !e.target.closest('[data-drop]')) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = internal ? ((e.ctrlKey || e.altKey) ? 'copy' : 'move') : 'copy';
        clearDropMarks();
        var t = dropTarget(e);
        if (t.node) t.node.classList.add('drop-on');
        if (!internal) { dropEl.hidden = !!t.node; if (!t.node) dropEl.querySelector('span').textContent = 'drop to upload into /' + state.cwd; }
    }
    document.addEventListener('dragleave', function (e) { if (!e.relatedTarget) { dropEl.hidden = true; clearDropMarks(); } });

    function handleDrop(e) {
        var internal = dragging && e.dataTransfer.types.indexOf(DRAG) >= 0;
        if (!internal && !isFiles(e)) return;
        e.preventDefault();
        var t = dropTarget(e);
        clearDropMarks();
        dropEl.hidden = true;
        if (internal) {
            var paths = dragging;
            dragging = null;
            var dest = t.path;
            if (state.search && !t.node) return;
            if (paths.indexOf(dest) >= 0) return;
            transfer((e.ctrlKey || e.altKey) ? 'copy' : 'cut', paths, dest);
        } else {
            collectDropped(e.dataTransfer).then(function (files) { if (files.length) uploadFiles(files, t.path); });
        }
    }
    paneEl.addEventListener('drop', handleDrop);
    treeEl.addEventListener('drop', handleDrop);

    function collectDropped(dt) {
        var out = [], pending = [];
        function walk(entry, prefix) {
            if (entry.isFile) {
                pending.push(new Promise(function (res) {
                    entry.file(function (f) { out.push({ file: f, rel: prefix + f.name }); res(); }, function () { res(); });
                }));
            } else if (entry.isDirectory) {
                pending.push(new Promise(function (res) {
                    var reader = entry.createReader(), all = [];
                    (function more() {
                        reader.readEntries(function (batch) {
                            if (!batch.length) { all.forEach(function (c) { walk(c, prefix + entry.name + '/'); }); res(); return; }
                            all = all.concat(Array.prototype.slice.call(batch));
                            more();
                        }, function () { res(); });
                    })();
                }));
            }
        }
        var items = dt.items;
        if (items && items.length && items[0].webkitGetAsEntry) {
            for (var i = 0; i < items.length; i++) {
                var en = items[i].webkitGetAsEntry();
                if (en) walk(en, '');
            }
            // directory reads finish asynchronously and may start more reads
            return new Promise(function (resolve) {
                (function wait() {
                    var n = pending.length;
                    Promise.all(pending).then(function () { if (pending.length === n) resolve(out); else wait(); });
                })();
            });
        }
        return Promise.resolve(Array.prototype.map.call(dt.files, function (f) { return { file: f, rel: f.name }; }));
    }

    /* ---------- upload ---------- */

    $('#fb-pick').addEventListener('change', function (e) {
        var files = Array.prototype.map.call(e.target.files, function (f) { return { file: f, rel: f.name }; });
        e.target.value = '';
        if (files.length) uploadFiles(files, state.cwd);
    });
    $('#fb-pick-dir').addEventListener('change', function (e) {
        var files = Array.prototype.map.call(e.target.files, function (f) { return { file: f, rel: f.webkitRelativePath || f.name }; });
        e.target.value = '';
        if (files.length) uploadFiles(files, state.cwd);
    });

    function uploadFiles(files, dest) {
        var tooBig = files.filter(function (f) { return f.file.size > LIMIT - 1048576; });
        files = files.filter(function (f) { return f.file.size <= LIMIT - 1048576; });
        var pre = tooBig.length ? [tooBig.map(function (f) { return f.rel; }).join(', '), 'bigger than the upload limit of ' + Math.round(LIMIT / 1048576) + ' MB (MAX_UPLOAD_MB)'] : null;
        if (!files.length) { if (pre) showSkipped([pre]); return; }
        var zips = files.filter(function (f) { return /\.zip$/i.test(f.rel); });
        api('exists', { json: { dest: dest, names: files.map(function (f) { return f.rel; }) } }).then(function (d) {
            var step = d.ok && d.count ? askConflicts(d.exists, 'Upload') : Promise.resolve('rename');
            return step.then(function (policy) {
                if (!policy) return;
                var ask = zips.length ? modal({ title: 'Zip file' + (zips.length > 1 ? 's' : ''), text: zips.length + ' zip file(s) in this upload. Unpack them into folders, or keep them as zip files?',
                    buttons: [{ label: 'unpack', value: '1', cls: 'primary' }, { label: 'keep as zip', value: '0' }, { label: 'cancel', value: null }] }).then(function (r) { return r.value; }) : Promise.resolve('0');
                return ask.then(function (extract) {
                    if (extract === null) return;
                    sendBatches(files, dest, policy, extract, pre ? [pre] : []);
                });
            });
        });
    }

    function sendBatches(files, dest, policy, extract, skipped) {
        var cap = Math.max(5 * 1048576, Math.min(64 * 1048576, LIMIT - 4 * 1048576));
        var batches = [], cur = [], curSize = 0;
        files.forEach(function (f) {
            if (cur.length && curSize + f.file.size > cap) { batches.push(cur); cur = []; curSize = 0; }
            cur.push(f); curSize += f.file.size;
        });
        if (cur.length) batches.push(cur);
        var total = files.reduce(function (n, f) { return n + f.file.size; }, 0), sent = 0;
        var sum = { saved: 0, replaced: 0, renamed: 0, kept: 0, extracted: 0 };
        progEl.hidden = false;
        function progress(p) {
            var pct = total ? Math.min(100, Math.round((sent + p) / total * 100)) : 100;
            progEl.querySelector('i').style.width = pct + '%';
            progEl.querySelector('b').textContent = 'uploading ' + pct + '%';
        }
        progress(0);
        var i = 0;
        (function next() {
            if (i >= batches.length) {
                progEl.hidden = true;
                var bits = [];
                if (sum.saved) bits.push(sum.saved + ' uploaded');
                if (sum.extracted) bits.push(sum.extracted + ' unpacked from zip');
                if (sum.replaced) bits.push(sum.replaced + ' replaced');
                if (sum.renamed) bits.push(sum.renamed + ' kept as copies');
                if (sum.kept) bits.push(sum.kept + ' left alone');
                toast(bits.join(', ') || 'Nothing was uploaded.', skipped.length && !sum.saved && !sum.extracted ? 'error' : 'ok');
                load(); refreshTree();
                showSkipped(skipped);
                return;
            }
            var batch = batches[i++];
            var fd = new FormData();
            fd.append('path', dest); fd.append('policy', policy); fd.append('extract', extract === '1' ? '1' : '0');
            var bytes = 0;
            batch.forEach(function (f) { fd.append('files', f.file, f.file.name); fd.append('relpath', f.rel); bytes += f.file.size; });
            var xhr = new XMLHttpRequest();
            xhr.open('POST', BASE + '/api/upload');
            xhr.setRequestHeader('X-CSRF-Token', CSRF);
            xhr.upload.onprogress = function (ev) { if (ev.lengthComputable) progress(ev.loaded * (bytes / Math.max(ev.total, 1))); };
            xhr.onload = function () {
                var d;
                try { d = JSON.parse(xhr.responseText); } catch (e) { d = null; }
                if (d && d.ok) {
                    ['saved', 'replaced', 'renamed', 'kept', 'extracted'].forEach(function (k) { sum[k] += d[k] || 0; });
                    skipped = skipped.concat(d.skipped || []);
                } else {
                    var why = d && d.error ? d.error : (xhr.status === 413 ? 'The upload is bigger than the limit (' + Math.round(LIMIT / 1048576) + ' MB per batch, MAX_UPLOAD_MB).' : 'Upload failed (status ' + xhr.status + ').');
                    skipped = skipped.concat(batch.map(function (f) { return [f.rel, why]; }));
                }
                sent += bytes;
                next();
            };
            xhr.onerror = function () {
                skipped = skipped.concat(batch.map(function (f) { return [f.rel, 'the connection failed']; }));
                sent += bytes;
                next();
            };
            xhr.send(fd);
        })();
    }

    /* ---------- go ---------- */

    var startPath = root.dataset.start || new URLSearchParams(location.search).get('path') || '';
    state.cwd = startPath;
    try { history.replaceState({ p: startPath }, '', location.href); } catch (e) { /* ignore */ }
    var chain = startPath.split('/').filter(Boolean), acc0 = '';
    chain.slice(0, -1).forEach(function (p) { acc0 = join(acc0, p); expanded[acc0] = true; });
    buildTree();
    load();
})();
