/* Admin CSV table editor. Plain JavaScript, no libraries. Opened from the file browser for every .csv file.
   The server parses and writes the file (api/csv), so quoting, delimiter, BOM, line endings and encoding stay as they were.
   Only the rows on screen are drawn, so a file with tens of thousands of rows stays fast. */
(function () {
    'use strict';

    var RH = 30, HH = 36, GW = 56, MINW = 60, MAXW = 700, DEFW = 160;
    var hooks = null;           // { api, modal, toast, reload, textEditor } given by admin_files.js
    var S = null;               // the open table
    var dlg, ui = {};

    function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
    function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
    function letters(i) { var s = ''; i++; while (i > 0) { var m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = Math.floor((i - 1) / 26); } return s; }
    function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
    function ncols() { return S.rows[0].length; }
    function bodyStart() { return S.hdr ? 1 : 0; }

    /* ---------- building the dialog once ---------- */

    function build() {
        if (dlg) return;
        dlg = el('dialog', 'fb-dlg gx');
        dlg.innerHTML =
            '<div class="gx-top">' +
            '<b class="gx-name"></b><span class="dim gx-state"></span><span class="a-spacer"></span>' +
            '<button type="button" class="a-btn small primary" data-act="save">save (Ctrl+S)</button>' +
            '<button type="button" class="a-btn small" data-act="astext" title="Open the same file in the plain text box">edit as text</button>' +
            '<button type="button" class="a-btn small" data-act="close">close</button></div>' +
            '<div class="gx-tools">' +
            '<button type="button" class="a-btn small" data-act="undo" title="Ctrl+Z">↶ undo</button>' +
            '<button type="button" class="a-btn small" data-act="redo" title="Ctrl+Y">↷ redo</button>' +
            '<span class="gx-sep"></span>' +
            '<button type="button" class="a-btn small" data-act="addrow">+ row</button>' +
            '<button type="button" class="a-btn small" data-act="addcol">+ column</button>' +
            '<button type="button" class="a-btn small danger" data-act="delrow">delete row</button>' +
            '<button type="button" class="a-btn small danger" data-act="delcol">delete column</button>' +
            '<span class="gx-sep"></span>' +
            '<input type="search" class="gx-find" placeholder="find (Ctrl+F)…" aria-label="Find in this table">' +
            '<button type="button" class="a-btn small" data-act="findprev" title="Shift+Enter">↑</button>' +
            '<button type="button" class="a-btn small" data-act="findnext" title="Enter">↓</button>' +
            '<label class="gx-chk"><input type="checkbox" class="gx-hdr" checked> first row is the header</label>' +
            '<span class="a-spacer"></span><span class="dim gx-info"></span></div>' +
            '<div class="gx-bar"><b class="gx-addr"></b><span class="gx-full"></span></div>' +
            '<div class="gx-scroll" tabindex="0"><div class="gx-head"></div><div class="gx-sizer"><div class="gx-body"></div></div></div>' +
            '<textarea class="gx-edit" spellcheck="false" hidden></textarea>' +
            '<div class="gx-msg"></div>' +
            '<div class="fb-menu gx-menu" hidden></div>';
        document.body.appendChild(dlg);
        ui = {
            name: dlg.querySelector('.gx-name'), state: dlg.querySelector('.gx-state'), info: dlg.querySelector('.gx-info'),
            addr: dlg.querySelector('.gx-addr'), full: dlg.querySelector('.gx-full'), msg: dlg.querySelector('.gx-msg'),
            scroll: dlg.querySelector('.gx-scroll'), head: dlg.querySelector('.gx-head'), sizer: dlg.querySelector('.gx-sizer'),
            body: dlg.querySelector('.gx-body'), edit: dlg.querySelector('.gx-edit'), menu: dlg.querySelector('.gx-menu'),
            find: dlg.querySelector('.gx-find'), hdr: dlg.querySelector('.gx-hdr')
        };
        wire();
    }

    /* ---------- state: rows are never changed in place, every change makes a new array (cheap undo) ---------- */

    function open(entry, h) { hooks = h; build(); load(entry); }
    function load(entry) {          // also used to reload the file inside the already open dialog
        hooks.api('csv?path=' + encodeURIComponent(entry.path)).then(function (d) {
            if (!d.ok) { hooks.toast(d.error, 'error'); return; }
            var rows = d.rows.length ? d.rows : [['']];
            if (!rows[0].length) rows = [['']];
            S = { entry: entry, rows: rows, saved: rows, fmt: d.format, mtime: d.mtime, hdr: true, textOk: d.text_ok,
                  active: { r: 0, c: 0 }, anchor: { r: 0, c: 0 }, undo: [], redo: [], widths: [], editing: null, dragging: false,
                  findAt: null };
            autosize();
            ui.name.textContent = 'content/' + entry.path;
            ui.state.textContent = '';
            ui.msg.textContent = d.ragged ? 'Some rows had fewer cells than the header. Missing cells are shown empty and will be saved as empty.' : '';
            ui.msg.className = 'gx-msg' + (d.ragged ? ' note' : '');
            ui.hdr.checked = true; ui.find.value = '';
            dlg.querySelector('[data-act="astext"]').hidden = !d.text_ok;
            if (!dlg.open) dlg.showModal();
            ui.scroll.scrollTop = 0; ui.scroll.scrollLeft = 0;
            renderAll(); ui.scroll.focus();
        });
    }

    function autosize() {
        var n = ncols(), w = [], i, r, v, m;
        for (i = 0; i < n; i++) {
            m = 0;
            for (r = 0; r < Math.min(S.rows.length, 300); r++) {
                v = S.rows[r][i] || ''; var nl = v.indexOf('\n'); if (nl >= 0) v = v.slice(0, nl);
                if (v.length > m) m = v.length;
            }
            w.push(clamp(Math.round(m * 7.4 + 26), 90, 320));
        }
        S.widths = w;
    }

    function dirty() { return !!S && S.rows !== S.saved; }
    function snapshot() { return { rows: S.rows, active: { r: S.active.r, c: S.active.c }, anchor: { r: S.anchor.r, c: S.anchor.c }, widths: S.widths.slice() }; }
    function commit(rows, widths) {          // every change goes through here
        S.undo.push(snapshot()); if (S.undo.length > 200) S.undo.shift();
        S.redo = [];
        S.rows = rows;
        if (widths) S.widths = widths;
        changed();
    }
    function restore(snap) { S.rows = snap.rows; S.active = snap.active; S.anchor = snap.anchor; S.widths = snap.widths; changed(); ensureVisible(S.active.r, S.active.c); }
    function undo() { if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); }
    function redo() { if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); }
    function changed() {
        S.active.r = clamp(S.active.r, 0, S.rows.length - 1); S.active.c = clamp(S.active.c, 0, ncols() - 1);
        S.anchor.r = clamp(S.anchor.r, 0, S.rows.length - 1); S.anchor.c = clamp(S.anchor.c, 0, ncols() - 1);
        ui.state.textContent = dirty() ? '● unsaved changes' : '';
        renderAll();
    }

    function cloneRows(rows, touched) {       // touched: row numbers about to change
        var out = rows.slice();
        touched.forEach(function (r) { if (r < out.length) out[r] = out[r].slice(); });
        return out;
    }
    function range() {
        return { r1: Math.min(S.active.r, S.anchor.r), r2: Math.max(S.active.r, S.anchor.r),
                 c1: Math.min(S.active.c, S.anchor.c), c2: Math.max(S.active.c, S.anchor.c) };
    }
    function blankRow(n) { var r = [], i; for (i = 0; i < n; i++) r.push(''); return r; }

    /* ---------- edits ---------- */

    function setCell(r, c, v) {
        if ((S.rows[r][c] || '') === v) return;
        var rows = cloneRows(S.rows, [r]); rows[r][c] = v; commit(rows);
    }
    function clearSelection() {
        var g = range(), rows, r, c, any = false;
        rows = cloneRows(S.rows, rangeList(g.r1, g.r2));
        for (r = g.r1; r <= g.r2; r++) for (c = g.c1; c <= g.c2; c++) if (rows[r][c] !== '') { rows[r][c] = ''; any = true; }
        if (any) commit(rows);
    }
    function rangeList(a, b) { var o = [], i; for (i = a; i <= b; i++) o.push(i); return o; }

    function insertRows(at, count, data) {
        var rows = S.rows.slice(), n = ncols(), add = [], i;
        for (i = 0; i < count; i++) add.push(data ? data[i].slice() : blankRow(n));
        Array.prototype.splice.apply(rows, [at, 0].concat(add));
        commit(rows);
    }
    function deleteRows(a, b) {
        var rows = S.rows.slice();
        rows.splice(a, b - a + 1);
        if (!rows.length) rows = [blankRow(ncols())];
        commit(rows);
    }
    function insertCol(at, count) {
        var rows = S.rows.map(function (r) { var x = r.slice(); var i; for (i = 0; i < count; i++) x.splice(at, 0, ''); return x; });
        var w = S.widths.slice(), j; for (j = 0; j < count; j++) w.splice(at, 0, DEFW);
        commit(rows, w);
    }
    function deleteCols(a, b) {
        var n = ncols();
        if (b - a + 1 >= n) { commit(S.rows.map(function () { return ['']; }), [DEFW]); return; }
        var rows = S.rows.map(function (r) { var x = r.slice(); x.splice(a, b - a + 1); return x; });
        var w = S.widths.slice(); w.splice(a, b - a + 1);
        commit(rows, w);
    }
    function moveRows(a, b, dir) {            // dir -1 up, +1 down
        if ((dir < 0 && a === 0) || (dir > 0 && b === S.rows.length - 1)) return;
        var rows = S.rows.slice(), block = rows.splice(a, b - a + 1);
        Array.prototype.splice.apply(rows, [a + dir, 0].concat(block));
        S.active.r += dir; S.anchor.r += dir;
        commit(rows);
    }
    function moveCol(c, dir) {
        var n = ncols(); if ((dir < 0 && c === 0) || (dir > 0 && c === n - 1)) return;
        var rows = S.rows.map(function (r) { var x = r.slice(), t = x[c]; x[c] = x[c + dir]; x[c + dir] = t; return x; });
        var w = S.widths.slice(), t = w[c]; w[c] = w[c + dir]; w[c + dir] = t;
        S.active.c += dir; S.anchor.c = S.active.c;
        commit(rows, w);
    }
    function sortBy(c, desc) {
        var start = bodyStart(), head = S.rows.slice(0, start), body = S.rows.slice(start);
        var num = function (s) { return s !== '' && !isNaN(Number(s)); };
        body = body.map(function (r, i) { return { r: r, i: i }; }).sort(function (x, y) {
            var a = x.r[c] || '', b = y.r[c] || '', k;
            if (a === '' && b !== '') return 1;
            if (b === '' && a !== '') return -1;
            k = (num(a) && num(b)) ? Number(a) - Number(b) : a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
            if (desc) k = -k;
            return k || x.i - y.i;
        }).map(function (o) { return o.r; });
        commit(head.concat(body));
    }

    /* ---------- clipboard (tab separated, like Excel and Google Sheets) ---------- */

    function toTsv(g) {
        var out = [], r, c, line, v;
        for (r = g.r1; r <= g.r2; r++) {
            line = [];
            for (c = g.c1; c <= g.c2; c++) {
                v = S.rows[r][c] || '';
                line.push(/[\t\n\r"]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v);
            }
            out.push(line.join('\t'));
        }
        return out.join('\n');
    }
    function parseTsv(text) {
        var rows = [], row = [], cell = '', i = 0, q = false, ch, atStart = true;
        text = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        for (; i < text.length; i++) {
            ch = text[i];
            if (q) {
                if (ch === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; }
                else cell += ch;
            } else if (ch === '"' && atStart) { q = true; atStart = false; }
            else if (ch === '\t') { row.push(cell); cell = ''; atStart = true; }
            else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; atStart = true; }
            else { cell += ch; atStart = false; }
        }
        if (cell !== '' || row.length) { row.push(cell); rows.push(row); }
        return rows;
    }
    function paste(text) {
        var block = parseTsv(text), g = range(), r, c, need, rows, n, bw = 0, tr, tc;
        if (!block.length) return;
        block.forEach(function (b) { if (b.length > bw) bw = b.length; });
        if (block.length === 1 && bw === 1 && (g.r2 > g.r1 || g.c2 > g.c1)) {       // one value into a selected area: fill it
            rows = cloneRows(S.rows, rangeList(g.r1, g.r2));
            for (r = g.r1; r <= g.r2; r++) for (c = g.c1; c <= g.c2; c++) rows[r][c] = block[0][0];
            commit(rows); return;
        }
        n = ncols(); need = g.c1 + bw;
        rows = S.rows.map(function (x) { return x.slice(); });
        var widths = S.widths.slice();
        if (need > n) { rows.forEach(function (x) { while (x.length < need) x.push(''); }); while (widths.length < need) widths.push(DEFW); }
        while (rows.length < g.r1 + block.length) rows.push(blankRow(rows[0].length));
        for (r = 0; r < block.length; r++) for (c = 0; c < bw; c++) rows[g.r1 + r][g.c1 + c] = block[r][c] === undefined ? '' : block[r][c];
        S.anchor = { r: g.r1, c: g.c1 }; S.active = { r: g.r1 + block.length - 1, c: g.c1 + bw - 1 };
        commit(rows, widths);
    }

    /* ---------- drawing ---------- */

    function colLeft(c) { var x = GW, i; for (i = 0; i < c; i++) x += S.widths[i]; return x; }
    function totalWidth() { return colLeft(ncols()) + 40; }
    function cellText(v) { v = v || ''; if (v.length > 400) v = v.slice(0, 400); return esc(v.replace(/\r?\n/g, ' ⏎ ')); }

    function renderAll() {
        if (!S) return;
        var tw = totalWidth(), bs = bodyStart(), count = S.rows.length - bs;
        ui.sizer.style.height = (Math.max(0, count) * RH + 40) + 'px';
        ui.sizer.style.width = tw + 'px';
        ui.head.style.width = tw + 'px';
        renderHead(); renderBody(); renderInfo();
    }
    function inRange(g, r, c) { return r >= g.r1 && r <= g.r2 && c >= g.c1 && c <= g.c2; }

    function renderHead() {
        var n = ncols(), g = range(), html = '<div class="gx-corner" style="width:' + GW + 'px">' + (S.hdr ? 'hdr' : '#') + '</div>', c, cls, w;
        for (c = 0; c < n; c++) {
            w = S.widths[c];
            cls = 'gx-h' + (S.hdr ? ' cell' : '') + (S.hdr && inRange(g, 0, c) ? ' sel' : '') + (S.hdr && S.active.r === 0 && S.active.c === c ? ' act' : '') + (S.active.c === c ? ' col' : '');
            html += '<div class="' + cls + '" data-c="' + c + '"' + (S.hdr ? ' data-r="0"' : '') + ' style="width:' + w + 'px">' +
                '<span class="gx-ht">' + (S.hdr ? cellText(S.rows[0][c]) : letters(c)) + '</span>' +
                '<button type="button" class="gx-hm" data-c="' + c + '" aria-label="Column options" title="Column options">▾</button>' +
                '<i class="gx-rz" data-c="' + c + '"></i></div>';
        }
        ui.head.innerHTML = html;
    }

    function renderBody() {
        var bs = bodyStart(), sc = ui.scroll, vh = sc.clientHeight, first = Math.max(0, Math.floor(sc.scrollTop / RH) - 3),
            last = Math.min(S.rows.length - bs - 1, Math.ceil((sc.scrollTop + vh) / RH) + 3), g = range(), n = ncols(), tw = totalWidth(), html = '', i, r, c, row, cls;
        for (i = first; i <= last; i++) {
            r = i + bs; row = S.rows[r];
            html += '<div class="gx-row' + (r >= g.r1 && r <= g.r2 ? ' rowsel' : '') + '" style="top:' + (i * RH) + 'px;width:' + tw + 'px">' +
                '<div class="gx-rn" data-r="' + r + '" style="width:' + GW + 'px">' + (r + 1) + '</div>';
            for (c = 0; c < n; c++) {
                cls = 'gx-c' + (inRange(g, r, c) ? ' sel' : '') + (S.active.r === r && S.active.c === c ? ' act' : '');
                html += '<div class="' + cls + '" data-r="' + r + '" data-c="' + c + '" style="width:' + S.widths[c] + 'px">' + cellText(row[c]) + '</div>';
            }
            html += '</div>';
        }
        ui.body.innerHTML = html;
    }
    function renderInfo() {
        var f = S.fmt, enc = f.encoding === 'utf-8' ? 'UTF-8' + (f.bom ? ' with BOM' : '') : (f.encoding === 'cp1252' ? 'Windows-1252' : 'Latin-1');
        var dl = f.delimiter === '\t' ? 'tab' : (f.delimiter === ';' ? 'semicolon' : 'comma');
        var rows = S.rows.length - bodyStart();
        ui.info.textContent = rows + ' row' + (rows === 1 ? '' : 's') + ' × ' + ncols() + ' col' + (ncols() === 1 ? '' : 's') + ' · ' + enc + ' · ' + dl + ' separated';
        var a = S.active, v = S.rows[a.r][a.c] || '';
        ui.addr.textContent = letters(a.c) + (a.r + 1);
        ui.full.textContent = v.length > 600 ? v.slice(0, 600) + '…' : v;
        dlg.querySelector('[data-act="undo"]').disabled = !S.undo.length;
        dlg.querySelector('[data-act="redo"]').disabled = !S.redo.length;
    }

    function ensureVisible(r, c) {
        var sc = ui.scroll, bs = bodyStart(), top, left = colLeft(c), right = left + S.widths[c], vw = sc.clientWidth, vh = sc.clientHeight;
        if (r >= bs) {
            top = (r - bs) * RH;
            if (top < sc.scrollTop) sc.scrollTop = top;
            else if (top + RH > sc.scrollTop + vh - HH) sc.scrollTop = top + RH - (vh - HH);
        }
        if (left - GW < sc.scrollLeft) sc.scrollLeft = Math.max(0, left - GW);
        else if (right > sc.scrollLeft + vw) sc.scrollLeft = right - vw + 8;
        renderBody();
    }

    /* ---------- moving around ---------- */

    function moveTo(r, c, extend) {
        r = clamp(r, 0, S.rows.length - 1); c = clamp(c, 0, ncols() - 1);
        S.active = { r: r, c: c };
        if (!extend) S.anchor = { r: r, c: c };
        renderHead(); ensureVisible(r, c); renderInfo();
    }
    function selectRows(a, b) { S.anchor = { r: a, c: 0 }; S.active = { r: b, c: ncols() - 1 }; renderHead(); renderBody(); renderInfo(); }
    function selectCol(c, extend) {
        if (extend) { S.active = { r: S.rows.length - 1, c: c }; }
        else { S.anchor = { r: 0, c: c }; S.active = { r: S.rows.length - 1, c: c }; }
        renderHead(); renderBody(); renderInfo();
    }

    /* ---------- the cell editor ---------- */

    function startEdit(r, c, text) {
        if (S.editing) finishEdit(true);
        S.active = { r: r, c: c }; S.anchor = { r: r, c: c };
        ensureVisible(r, c); renderHead(); renderBody(); renderInfo();
        var cellEl = r === 0 && S.hdr ? ui.head.querySelector('.gx-h[data-c="' + c + '"]') : ui.body.querySelector('.gx-c[data-r="' + r + '"][data-c="' + c + '"]');
        if (!cellEl) return;
        S.editing = { r: r, c: c, original: S.rows[r][c] || '' };
        ui.edit.hidden = false;
        ui.edit.value = text != null ? text : (S.rows[r][c] || '');
        placeEdit();
        ui.edit.focus();
        if (text == null) ui.edit.setSelectionRange(ui.edit.value.length, ui.edit.value.length);
    }
    function placeEdit() {
        if (!S || !S.editing) return;
        var e = S.editing, cellEl = e.r === 0 && S.hdr ? ui.head.querySelector('.gx-h[data-c="' + e.c + '"]') : ui.body.querySelector('.gx-c[data-r="' + e.r + '"][data-c="' + e.c + '"]');
        if (!cellEl) { finishEdit(true); return; }
        var rc = cellEl.getBoundingClientRect(), sr = ui.scroll.getBoundingClientRect();
        if (rc.bottom < sr.top + 4 || rc.top > sr.bottom - 4) { finishEdit(true); return; }
        ui.edit.style.left = rc.left + 'px'; ui.edit.style.top = rc.top + 'px'; ui.edit.style.width = Math.max(rc.width, 140) + 'px';
        ui.edit.style.height = clamp(ui.edit.scrollHeight + 4, RH, 240) + 'px';
    }
    function finishEdit(save) {
        if (!S || !S.editing) return;
        var e = S.editing; S.editing = null;
        var v = ui.edit.value;
        ui.edit.hidden = true;
        if (save && v !== e.original) setCell(e.r, e.c, v); else renderBody();
        ui.scroll.focus();
    }

    /* ---------- menus ---------- */

    function showMenu(x, y, items) {
        var m = ui.menu; m.textContent = '';
        items.forEach(function (it) {
            if (it === '-') { m.appendChild(el('div', 'fb-menu-sep')); return; }
            var b = el('button', 'fb-menu-item', it.label); b.type = 'button'; b.disabled = !!it.off;
            b.addEventListener('click', function () { hideMenu(); it.run(); ui.scroll.focus(); });
            m.appendChild(b);
        });
        m.hidden = false;
        var dr = dlg.getBoundingClientRect();
        m.style.left = Math.min(x, window.innerWidth - m.offsetWidth - 8) + 'px';
        m.style.top = Math.min(y, window.innerHeight - m.offsetHeight - 8) + 'px';
        void dr;
    }
    function hideMenu() { ui.menu.hidden = true; }

    function rowItems() {
        var g = range(), many = g.r2 > g.r1, who = many ? g.r2 - g.r1 + 1 + ' rows' : 'row ' + (g.r1 + 1);
        return [
            { label: 'insert ' + (many ? (g.r2 - g.r1 + 1) + ' rows' : 'a row') + ' above', run: function () { insertRows(g.r1, g.r2 - g.r1 + 1); moveTo(g.r1, S.active.c); } },
            { label: 'insert ' + (many ? (g.r2 - g.r1 + 1) + ' rows' : 'a row') + ' below', run: function () { insertRows(g.r2 + 1, g.r2 - g.r1 + 1); moveTo(g.r2 + 1, S.active.c); } },
            { label: 'duplicate ' + who, run: function () { insertRows(g.r2 + 1, g.r2 - g.r1 + 1, S.rows.slice(g.r1, g.r2 + 1)); } },
            { label: 'move ' + who + ' up', run: function () { moveRows(g.r1, g.r2, -1); }, off: g.r1 === 0 },
            { label: 'move ' + who + ' down', run: function () { moveRows(g.r1, g.r2, 1); }, off: g.r2 === S.rows.length - 1 },
            { label: 'delete ' + who, run: function () { deleteRows(g.r1, g.r2); } }
        ];
    }
    function colItems(c) {
        var g = range(), a = c, b = c;
        if (c >= g.c1 && c <= g.c2) { a = g.c1; b = g.c2; }
        var cnt = b - a + 1, who = cnt > 1 ? cnt + ' columns' : 'column ' + letters(a);
        return [
            { label: 'insert ' + (cnt > 1 ? cnt + ' columns' : 'a column') + ' left', run: function () { insertCol(a, cnt); moveTo(S.active.r, a); } },
            { label: 'insert ' + (cnt > 1 ? cnt + ' columns' : 'a column') + ' right', run: function () { insertCol(b + 1, cnt); moveTo(S.active.r, b + 1); } },
            { label: 'delete ' + who, run: function () { deleteCols(a, b); } },
            '-',
            { label: 'move column left', run: function () { moveCol(c, -1); }, off: c === 0 || cnt > 1 },
            { label: 'move column right', run: function () { moveCol(c, 1); }, off: c === ncols() - 1 || cnt > 1 },
            '-',
            { label: 'sort A → Z (rows move together)', run: function () { sortBy(c, false); } },
            { label: 'sort Z → A', run: function () { sortBy(c, true); } }
        ];
    }

    /* ---------- find ---------- */

    function findNext(dir) {
        var q = ui.find.value.toLowerCase(); if (!q) return;
        var n = ncols(), total = S.rows.length * n, start = S.active.r * n + S.active.c, i, k, r, c;
        for (i = 1; i <= total; i++) {
            k = ((start + dir * i) % total + total) % total; r = Math.floor(k / n); c = k % n;
            if ((S.rows[r][c] || '').toLowerCase().indexOf(q) >= 0) { moveTo(r, c); ui.msg.textContent = ''; ui.msg.className = 'gx-msg'; return; }
        }
        ui.msg.textContent = 'No cell contains “' + ui.find.value + '”.'; ui.msg.className = 'gx-msg note';
    }

    /* ---------- saving and closing ---------- */

    function save(force) {
        if (!S) return Promise.resolve(false);
        if (S.editing) finishEdit(true);
        ui.msg.textContent = ''; ui.msg.className = 'gx-msg';
        var sending = S.rows;
        return hooks.api('csv', { json: { path: S.entry.path, rows: sending, format: S.fmt, mtime: S.mtime, force: !!force } }).then(function (d) {
            if (d.ok) {
                S.saved = sending; S.mtime = d.mtime; S.fmt = d.format;
                ui.state.textContent = S.rows === sending ? 'saved ✓' : '● unsaved changes';
                if (d.converted) { ui.msg.textContent = 'Saved. The old encoding of this file could not hold some of the characters you typed, so it is now UTF-8.'; ui.msg.className = 'gx-msg note'; }
                renderInfo();
                return true;
            }
            if (d.changed) {
                return hooks.modal({ title: 'File changed', text: d.error + ' What now?',
                    buttons: [{ label: 'overwrite with mine', value: 'go', cls: 'warn' }, { label: 'reload theirs (lose my changes)', value: 'reload' }, { label: 'cancel', value: null }] }).then(function (r) {
                    if (r.value === 'go') return save(true);
                    if (r.value === 'reload') { var e = S.entry; S.editing = null; load(e); }
                    return false;
                });
            }
            ui.msg.textContent = d.error; ui.msg.className = 'gx-msg';
            return false;
        });
    }
    function closeIt() {
        if (!S) { dlg.close(); return; }
        if (S.editing) finishEdit(true);
        if (!dirty()) { dlg.close(); return; }
        hooks.modal({ title: 'Unsaved changes', text: 'Close without saving?',
            buttons: [{ label: 'discard', value: 'x', cls: 'danger' }, { label: 'keep editing', value: null, cls: 'primary' }] }).then(function (r) { if (r.value) dlg.close(); });
    }
    function asText() {
        var go = function () { var e = S.entry; S.rows = S.saved; dlg.close(); hooks.textEditor(e); };
        if (!dirty()) { go(); return; }
        hooks.modal({ title: 'Unsaved changes', text: 'Open the plain text box and lose the changes you made in the table?',
            buttons: [{ label: 'discard and open', value: 'x', cls: 'danger' }, { label: 'stay in the table', value: null, cls: 'primary' }] }).then(function (r) { if (r.value) go(); });
    }

    /* ---------- events ---------- */

    function cellAt(t) { return t && t.closest ? t.closest('.gx-c, .gx-h.cell') : null; }

    function wire() {
        dlg.addEventListener('click', function (e) {
            var b = e.target.closest('[data-act]'); if (!b || !S) return;
            var a = b.dataset.act, g = range();
            if (a === 'save') save();
            else if (a === 'close') closeIt();
            else if (a === 'astext') asText();
            else if (a === 'undo') undo();
            else if (a === 'redo') redo();
            else if (a === 'addrow') { insertRows(S.rows.length, 1); moveTo(S.rows.length - 1, 0); startEdit(S.rows.length - 1, 0); }
            else if (a === 'addcol') { insertCol(ncols(), 1); moveTo(S.active.r, ncols() - 1); }
            else if (a === 'delrow') deleteRows(g.r1, g.r2);
            else if (a === 'delcol') deleteCols(g.c1, g.c2);
            else if (a === 'findnext') findNext(1);
            else if (a === 'findprev') findNext(-1);
            if (a !== 'save' && a !== 'close' && a !== 'astext' && a !== 'addrow') ui.scroll.focus();
        });
        ui.hdr.addEventListener('change', function () { if (!S) return; S.hdr = ui.hdr.checked; S.active.r = Math.max(S.active.r, 0); ui.scroll.scrollTop = 0; renderAll(); });
        ui.find.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); findNext(e.shiftKey ? -1 : 1); }
            else if (e.key === 'Escape') { e.preventDefault(); ui.find.blur(); ui.scroll.focus(); }
        });
        ui.scroll.addEventListener('scroll', function () {
            if (!S) return;
            if (S.raf) return;
            S.raf = requestAnimationFrame(function () { if (!S) return; S.raf = 0; renderBody(); placeEdit(); });
        });

        ui.scroll.addEventListener('mousedown', function (e) {
            if (!S) return;
            hideMenu();
            if (S.editing) finishEdit(true);
            var t = e.target;
            if (e.button === 2) return;
            var rz = t.closest('.gx-rz');
            if (rz) { startResize(e, parseInt(rz.dataset.c, 10)); return; }
            var hm = t.closest('.gx-hm');
            if (hm) { e.preventDefault(); var c = parseInt(hm.dataset.c, 10), rc = hm.getBoundingClientRect(); showMenu(rc.left, rc.bottom + 2, colItems(c)); return; }
            var cell = cellAt(t);
            if (cell) {
                var r = parseInt(cell.dataset.r, 10), cc = parseInt(cell.dataset.c, 10);
                if (e.detail === 2) { e.preventDefault(); startEdit(r, cc); return; }      // double click (the cells are redrawn on the first click, so 'dblclick' itself never arrives)
                if (e.shiftKey) { S.active = { r: r, c: cc }; } else { S.active = { r: r, c: cc }; S.anchor = { r: r, c: cc }; }
                S.dragging = true; renderHead(); renderBody(); renderInfo(); ui.scroll.focus({ preventScroll: true }); e.preventDefault();
                return;
            }
            var rn = t.closest('.gx-rn');
            if (rn) { var rr = parseInt(rn.dataset.r, 10); if (e.shiftKey) selectRows(S.anchor.r, rr); else selectRows(rr, rr); S.dragging = 'rows'; e.preventDefault(); return; }
            var h = t.closest('.gx-h');
            if (h && !S.hdr) { selectCol(parseInt(h.dataset.c, 10), e.shiftKey); e.preventDefault(); return; }
        });
        document.addEventListener('mousemove', function (e) {
            if (!S || !S.dragging) return;
            var t = document.elementFromPoint(e.clientX, e.clientY); if (!t) return;
            if (S.dragging === 'rows') { var rn = t.closest && t.closest('.gx-rn'); if (rn) { var rr = parseInt(rn.dataset.r, 10); if (rr !== S.active.r) selectRows(S.anchor.r, rr); } return; }
            var cell = cellAt(t); if (!cell) return;
            var r = parseInt(cell.dataset.r, 10), c = parseInt(cell.dataset.c, 10);
            if (r !== S.active.r || c !== S.active.c) { S.active = { r: r, c: c }; renderHead(); renderBody(); renderInfo(); }
        });
        document.addEventListener('mouseup', function () { if (S) S.dragging = false; });
        ui.scroll.addEventListener('contextmenu', function (e) {
            if (!S) return;
            var cell = cellAt(e.target), rn = e.target.closest('.gx-rn'), h = e.target.closest('.gx-h');
            if (!cell && !rn && !h) return;
            e.preventDefault();
            if (rn) { var rr = parseInt(rn.dataset.r, 10), g = range(); if (rr < g.r1 || rr > g.r2) selectRows(rr, rr); showMenu(e.clientX, e.clientY, rowItems()); return; }
            if (h && !cell) { showMenu(e.clientX, e.clientY, colItems(parseInt(h.dataset.c, 10))); return; }
            var r = parseInt(cell.dataset.r, 10), c = parseInt(cell.dataset.c, 10), gg = range();
            if (!inRange(gg, r, c)) { S.active = { r: r, c: c }; S.anchor = { r: r, c: c }; renderHead(); renderBody(); renderInfo(); }
            showMenu(e.clientX, e.clientY, rowItems().concat(['-'], colItems(c)));
        });
        document.addEventListener('mousedown', function (e) { if (dlg && !ui.menu.hidden && !e.target.closest('.gx-menu') && !e.target.closest('.gx-hm')) hideMenu(); });

        ui.edit.addEventListener('keydown', function (e) {
            if (!S || !S.editing) return;
            var r = S.editing.r, c = S.editing.c;
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); S.editing = null; ui.edit.hidden = true; renderBody(); ui.scroll.focus(); }
            else if (e.key === 'Enter' && e.altKey) { e.preventDefault(); var t = ui.edit, s = t.selectionStart; t.value = t.value.slice(0, s) + '\n' + t.value.slice(t.selectionEnd); t.setSelectionRange(s + 1, s + 1); placeEdit(); }
            else if (e.key === 'Enter') { e.preventDefault(); finishEdit(true); moveTo(r + (e.shiftKey ? -1 : 1), c); }
            else if (e.key === 'Tab') { e.preventDefault(); finishEdit(true); stepTab(r, c, e.shiftKey); }
        });
        ui.edit.addEventListener('input', placeEdit);
        ui.edit.addEventListener('blur', function () { if (S && S.editing && document.activeElement !== ui.edit) { setTimeout(function () { if (S && S.editing) finishEdit(true); }, 0); } });

        ui.scroll.addEventListener('keydown', onKey);
        ui.scroll.addEventListener('copy', function (e) { if (!S) return; e.clipboardData.setData('text/plain', toTsv(range())); e.preventDefault(); });
        ui.scroll.addEventListener('cut', function (e) { if (!S) return; e.clipboardData.setData('text/plain', toTsv(range())); e.preventDefault(); clearSelection(); });
        ui.scroll.addEventListener('paste', function (e) { if (!S) return; e.preventDefault(); paste((e.clipboardData || window.clipboardData).getData('text')); });

        dlg.addEventListener('cancel', function (e) {
            e.preventDefault();
            if (S && S.editing) { S.editing = null; ui.edit.hidden = true; renderBody(); return; }
            if (!ui.menu.hidden) { hideMenu(); return; }
            closeIt();
        });
        dlg.addEventListener('close', function () { S = null; ui.edit.hidden = true; hideMenu(); hooks.reload(true); });
        dlg.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
        });
    }

    function stepTab(r, c, back) {
        var n = ncols();
        if (back) { c--; if (c < 0) { c = n - 1; r--; } } else { c++; if (c >= n) { c = 0; r++; } }
        moveTo(r, c);
    }

    function onKey(e) {
        if (!S || S.editing || e.target !== ui.scroll) return;
        var k = e.key, a = S.active, ctrl = e.ctrlKey || e.metaKey, vis = Math.max(1, Math.floor((ui.scroll.clientHeight - HH) / RH) - 1);
        if (ctrl && !e.shiftKey && k.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
        if (ctrl && (k.toLowerCase() === 'y' || (e.shiftKey && k.toLowerCase() === 'z'))) { e.preventDefault(); redo(); return; }
        if (ctrl && k.toLowerCase() === 'a') { e.preventDefault(); S.anchor = { r: 0, c: 0 }; S.active = { r: S.rows.length - 1, c: ncols() - 1 }; renderHead(); renderBody(); renderInfo(); return; }
        if (ctrl && k.toLowerCase() === 'f') { e.preventDefault(); ui.find.focus(); ui.find.select(); return; }
        if (ctrl && (k === 'c' || k === 'x' || k === 'v' || k === 's')) return;           // the copy / cut / paste events and the dialog handler do these
        if (e.altKey && (k === 'ArrowUp' || k === 'ArrowDown')) { e.preventDefault(); var g = range(); moveRows(g.r1, g.r2, k === 'ArrowUp' ? -1 : 1); return; }
        if (k === 'ArrowUp') { e.preventDefault(); moveTo(ctrl ? 0 : a.r - 1, a.c, e.shiftKey); }
        else if (k === 'ArrowDown') { e.preventDefault(); moveTo(ctrl ? S.rows.length - 1 : a.r + 1, a.c, e.shiftKey); }
        else if (k === 'ArrowLeft') { e.preventDefault(); moveTo(a.r, ctrl ? 0 : a.c - 1, e.shiftKey); }
        else if (k === 'ArrowRight') { e.preventDefault(); moveTo(a.r, ctrl ? ncols() - 1 : a.c + 1, e.shiftKey); }
        else if (k === 'Home') { e.preventDefault(); moveTo(ctrl ? 0 : a.r, 0, e.shiftKey); }
        else if (k === 'End') { e.preventDefault(); moveTo(ctrl ? S.rows.length - 1 : a.r, ncols() - 1, e.shiftKey); }
        else if (k === 'PageDown') { e.preventDefault(); moveTo(a.r + vis, a.c, e.shiftKey); }
        else if (k === 'PageUp') { e.preventDefault(); moveTo(a.r - vis, a.c, e.shiftKey); }
        else if (k === 'Tab') { e.preventDefault(); stepTab(a.r, a.c, e.shiftKey); }
        else if (k === 'Enter' || k === 'F2') { e.preventDefault(); startEdit(a.r, a.c); }
        else if (k === 'Delete' || k === 'Backspace') { e.preventDefault(); clearSelection(); }
        else if (k.length === 1 && !ctrl && !e.altKey) { e.preventDefault(); startEdit(a.r, a.c, k); }
    }

    function startResize(e, c) {
        e.preventDefault(); e.stopPropagation();
        var x0 = e.clientX, w0 = S.widths[c];
        function move(ev) { S.widths[c] = clamp(w0 + ev.clientX - x0, MINW, MAXW); renderAll(); }
        function up() { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); }
        document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
    }

    window.FBGrid = { open: open, _parseTsv: parseTsv };
})();
