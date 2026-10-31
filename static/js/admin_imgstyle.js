/* Live preview + slider/number sync for the "picture style" panels (macro img_style_panel).
   The CSS built here mirrors img_css() in app.py; the server is the one that validates and stores. */
(function () {
    'use strict';
    var SAMPLE = 'data:image/svg+xml;utf8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#f59e0b"/></linearGradient></defs>' +
        '<rect width="320" height="180" fill="url(#g)"/><circle cx="90" cy="70" r="34" fill="#fff" opacity=".85"/>' +
        '<rect x="170" y="40" width="110" height="100" rx="12" fill="#0b1220" opacity=".55"/>' +
        '<text x="160" y="165" text-anchor="middle" font-family="sans-serif" font-size="13" fill="#fff">sample picture (none uploaded yet)</text></svg>');
    var BAD = /url\(|image-set\(|expression\(|javascript:|@|\{|\}|<|>|\\/i;

    var diffs = {};
    window.imgStyleDiff = function (prefix) { return diffs[prefix] ? diffs[prefix]() : null; };

    function clamp(v, lo, hi, d) { v = parseFloat(v); return isNaN(v) ? d : Math.max(lo, Math.min(hi, v)); }

    function init(box) {
        var prefix = box.getAttribute('data-prefix'), kind = box.getAttribute('data-kind');
        var form = box.closest('form');
        var frame = box.querySelector('.imgs-frame'), img = frame.querySelector('img');
        var flag = box.querySelector('.imgs-flag'), count = box.querySelector('.imgs-count');
        var defaults = {};
        try { defaults = JSON.parse(box.getAttribute('data-defaults')); } catch (e) { defaults = {}; }
        var objectUrl = null;

        function el(key) { return form.elements[prefix + '_' + key]; }
        function val(key) {
            var e = el(key);
            if (!e) return defaults[key];
            if (e.length !== undefined && !e.tagName) {            // radio group
                for (var i = 0; i < e.length; i++) if (e[i].checked) return e[i].value;
                return defaults[key];
            }
            return e.value;
        }

        function build() {
            var s = {
                fit: val('fit') === 'contain' ? 'contain' : 'cover',
                x: clamp(val('x'), 0, 100, defaults.x), y: clamp(val('y'), 0, 100, defaults.y),
                zoom: clamp(val('zoom'), 100, 300, 100), bstyle: val('bstyle') || 'none',
                bw: clamp(val('bw'), 0, 20, 2), bcolor: val('bcolor') || defaults.bcolor,
                rmode: val('rmode') || 'default', radius: clamp(val('radius'), 0, 80, 16),
                shadow: val('shadow') || 'none', css: (val('css') || '').replace(/\r/g, '').trim()
            };
            var f = [], im = [];
            if (s.bstyle !== 'none' && s.bw > 0) f.push('border: ' + s.bw + 'px ' + s.bstyle + ' ' + s.bcolor);
            if (s.rmode === 'custom') f.push('border-radius: ' + s.radius + 'px');
            else if (s.rmode === 'circle') f.push('border-radius: 50%');
            var sh = { soft: '0 6px 18px rgba(0,0,0,.28)', strong: '0 16px 44px rgba(0,0,0,.5)', glow: '0 0 26px ' + s.bcolor }[s.shadow];
            if (sh) f.push('box-shadow: ' + sh);
            if (s.css && !BAD.test(s.css.replace(/\s+/g, ''))) {
                s.css.split(/[;\n]+/).forEach(function (d) { d = d.trim(); if (d) f.push(d); });
            }
            var moved = s.fit !== defaults.fit || s.x !== defaults.x || s.y !== defaults.y || s.zoom !== defaults.zoom;
            if (moved) {
                im.push('object-fit: ' + s.fit, 'object-position: ' + s.x + '% ' + s.y + '%');
                if (s.zoom !== 100) im.push('transform: scale(' + (s.zoom / 100).toFixed(2) + ')', 'transform-origin: ' + s.x + '% ' + s.y + '%');
            }
            var changed = false;
            Object.keys(defaults).forEach(function (k) {
                if (!el(k)) return;
                var d = defaults[k], v = s[k];
                if (typeof d === 'number') v = Number(v);
                if (v !== d) changed = true;
            });
            return { frame: f.join('; '), img: im.join('; '), changed: changed, css: s.css };
        }

        function diff() {                              // what would be saved: only the values that differ from the default
            var out = {}, any = false;
            Object.keys(defaults).forEach(function (k) {
                if (!el(k)) return;
                var v = val(k), d = defaults[k];
                if (typeof d === 'number') v = Number(v);
                else if (k === 'css') v = (v || '').replace(/\r/g, '').trim();
                else if (k === 'bcolor') v = String(v).toLowerCase();
                if (v !== d) { out[k] = v; any = true; }
            });
            return any ? out : null;
        }
        diffs[prefix] = diff;

        function source() {
            var sel = box.getAttribute('data-file'), input = sel ? form.querySelector(sel) : null;
            if (input && input.files && input.files[0] && /^image\//.test(input.files[0].type)) {
                if (objectUrl) URL.revokeObjectURL(objectUrl);
                objectUrl = URL.createObjectURL(input.files[0]);
                return objectUrl;
            }
            return box.getAttribute('data-src') || SAMPLE;
        }

        function update() {
            var r = build();
            frame.setAttribute('style', r.frame);
            img.setAttribute('style', r.img);
            if (flag) flag.hidden = !r.changed;
            if (count) count.textContent = r.css.length + ' / ' + (el('css') ? el('css').maxLength : 600);
        }

        function setSource() { img.src = source(); update(); }

        // slider <-> number pairs
        box.querySelectorAll('input[type=range][data-pair]').forEach(function (range) {
            var num = document.getElementById(range.getAttribute('data-pair'));
            if (!num) return;
            range.addEventListener('input', function () { num.value = range.value; update(); });
            num.addEventListener('input', function () { if (num.value !== '') range.value = num.value; update(); });
        });
        box.addEventListener('input', update);
        box.addEventListener('change', update);

        var sel = box.getAttribute('data-file'), input = sel ? form.querySelector(sel) : null;
        if (input) input.addEventListener('change', setSource);

        var reset = box.querySelector('[data-imgs-reset]');
        if (reset) reset.addEventListener('click', function () {
            Object.keys(defaults).forEach(function (k) {
                var e = el(k);
                if (!e) return;
                if (e.length !== undefined && !e.tagName) {
                    for (var i = 0; i < e.length; i++) e[i].checked = (e[i].value === String(defaults[k]));
                } else {
                    e.value = defaults[k];
                    var pair = box.querySelector('input[type=range][data-pair="' + e.id + '"]');
                    if (pair) pair.value = defaults[k];
                }
            });
            update();
        });

        // the preview image is loaded lazily, when the panel is first opened or already open
        var loaded = false;
        function load() { if (!loaded) { loaded = true; setSource(); } }
        box.addEventListener('toggle', function () { if (box.open) load(); });
        if (box.open) load(); else update();
    }

    document.querySelectorAll('[data-imgs]').forEach(init);
})();
